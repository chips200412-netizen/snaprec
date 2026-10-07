from __future__ import annotations

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from ..domain.models import Job, Segment, VideoResult
from ..repositories.sqlite import SQLiteRepository
from .cleaning import clean_transcript
from .evidence import EvidenceValidationError, validate_evidence
from .interfaces import (
    AsrProvider,
    AutoTagger,
    FocusedExtractor,
    FullExtractor,
    SubtitleProvider,
)
from .markdown import render_markdown
from .media import PreparedMedia, RetainedMediaError, RetainedMediaService
from .providers import DeterministicAutoTagger, find_sidecar_subtitle
from .tagging import (
    AUTOMATIC_TAGGING_FAILED_WARNING,
    generate_automatic_tagging,
)

_ALLOWED_SUFFIXES = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".mp4", ".mov", ".mkv", ".webm"
}
_RETRYABLE_ERRORS = {
    "CONTENT_UNAVAILABLE",
    "SUBTITLE_UNAVAILABLE",
    "ASR_FAILED",
    "EXTRACTION_FAILED",
    "PROCESS_INTERRUPTED",
}
_TEMP_MARKER = ".video-knowledge-temp"


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class PipelineCancelled(PipelineError):
    def __init__(self):
        super().__init__("CANCELLED", "任务已取消。")


def is_retryable_error(code: str) -> bool:
    return code in _RETRYABLE_ERRORS


class LocalFullPipeline:
    def __init__(
        self,
        repository: SQLiteRepository,
        asr: AsrProvider,
        extractor: FullExtractor,
        temp_root: str | Path | None = None,
        subtitle_provider: SubtitleProvider | None = None,
        focused_extractor: FocusedExtractor | None = None,
        auto_tagger: AutoTagger | None = None,
        max_media_bytes: int = 512 * 1024 * 1024,
        retained_media_root: str | Path | None = None,
    ):
        if max_media_bytes <= 0:
            raise ValueError("max_media_bytes must be positive")
        self.repository = repository
        self.asr = asr
        self.extractor = extractor
        self.temp_root = Path(temp_root) if temp_root else None
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        self.subtitle_provider = subtitle_provider
        self.focused_extractor = focused_extractor
        self.auto_tagger = auto_tagger or DeterministicAutoTagger()
        self.max_media_bytes = max_media_bytes
        self.media_service = (
            RetainedMediaService(repository, retained_media_root)
            if retained_media_root is not None
            else None
        )

    def process(
        self,
        media_path: str | Path,
        cancel_requested: Callable[[], bool] | None = None,
        job: Job | None = None,
        retain_media: bool = False,
    ) -> tuple[Job, VideoResult, str]:
        source = Path(media_path)
        job = job or Job(id=str(uuid.uuid4()), status="queued", progress=0)
        if not self.repository.save_job(job):
            persisted = self.repository.get_job(job.id)
            if persisted is not None and persisted.cancel_requested:
                raise PipelineCancelled()
            raise PipelineError("JOB_STATE_CONFLICT", "任务状态已被其他执行器更新。")
        workdir: Path | None = None
        prepared_media: PreparedMedia | None = None
        try:
            self._validate_source(source, self.max_media_bytes)
            video_id = _sha256(source)
            job.video_id = video_id
            self.repository.save_job(job)
            self.repository.configure_job_request(
                job.id,
                "local_upload",
                {
                    "video_id": video_id,
                    "platform": "local_upload",
                    "retain_media": retain_media,
                },
            )
            cancel_requested = self._combined_cancel(job.id, cancel_requested)
            self._transition(job, "resolving", 10)
            # An explicitly supplied subtitle is authoritative and may correct an
            # earlier ASR result, so it must bypass the media-only cache key.
            source_sidecar = find_sidecar_subtitle(source)
            cached = (
                None
                if source_sidecar is not None
                else self.repository.load_result(video_id, platform="local_upload")
            )
            if cached is not None:
                if retain_media:
                    if self.media_service is None:
                        raise PipelineError(
                            "MEDIA_RETENTION_FAILED",
                            "持久媒体目录未配置，无法保留上传媒体。",
                        )
                    try:
                        self.media_service.retain_for_existing(source, video_id)
                    except RetainedMediaError as exc:
                        raise PipelineError(exc.code, str(exc)) from exc
                self._transition(job, "completed", 100)
                return job, cached, render_markdown(cached)
            workdir = Path(tempfile.mkdtemp(prefix="video-job-", dir=self.temp_root))
            (workdir / _TEMP_MARKER).write_text(
                f"job:{job.id}", encoding="utf-8"
            )
            staged = workdir / f"media{source.suffix.lower()}"
            shutil.copyfile(source, staged)
            if source_sidecar is not None:
                shutil.copyfile(
                    source_sidecar,
                    Path(f"{staged}{source_sidecar.suffix.casefold()}"),
                )
            if cancel_requested is not None and cancel_requested():
                raise PipelineCancelled()

            self._transition(job, "fetching_metadata", 20)
            self._transition(job, "fetching_subtitles", 30)
            segments = (
                self.subtitle_provider.get_subtitles(staged)
                if self.subtitle_provider is not None
                else None
            )
            subtitle_source = "user_upload"
            if not segments:
                self._check_cancel(cancel_requested)
                self._transition(job, "transcribing", 45)
                try:
                    segments = self.asr.transcribe(staged)
                except Exception as exc:
                    raise PipelineError("ASR_FAILED", "无法生成字幕，请配置 ASR 或提供字幕。") from exc
                subtitle_source = "asr"
            raw = "\n".join(segment.text for segment in segments)
            if not raw.strip():
                raise PipelineError("ASR_FAILED", "ASR 未返回可用字幕。")
            checkpoint_base = {
                "video_id": video_id,
                "source_name": source.name,
                "subtitle_source": subtitle_source,
                "segments": [item.model_dump(mode="json") for item in segments],
                "raw": raw,
            }
            self.repository.save_job_artifact(
                job.id, "transcript", checkpoint_base
            )

            self._check_cancel(cancel_requested)
            self._transition(job, "cleaning", 60)
            cleaned = clean_transcript(raw)
            checkpoint_base["cleaned"] = cleaned
            self.repository.save_job_artifact(job.id, "clean", checkpoint_base)
            self._check_cancel(cancel_requested)
            self._transition(job, "extracting", 75)
            try:
                summary, extraction, evidence = self.extractor.extract(
                    raw, cleaned, segments
                )
            except Exception as exc:
                raise PipelineError(
                    "EXTRACTION_FAILED", "内容提取服务暂时失败。"
                ) from exc
            try:
                validate_evidence(extraction, evidence, segments)
            except EvidenceValidationError as exc:
                raise PipelineError(
                    "INVALID_EXTRACTION", "提取结果未通过证据校验。"
                ) from exc
            automatic_tagging = generate_automatic_tagging(
                self.auto_tagger,
                raw,
                cleaned,
                segments,
                extraction,
                self.repository.load_automatic_tagging(
                    video_id, platform="local_upload"
                ),
            )
            warnings = []
            if automatic_tagging.status == "failed":
                warnings.append(AUTOMATIC_TAGGING_FAILED_WARNING)
            result = VideoResult(
                platform="local_upload",
                source_url=f"local://{source.name}",
                canonical_url=f"local://sha256/{video_id}",
                video_id=video_id,
                author="",
                title=source.stem,
                description="",
                tags=[],
                duration=max((s.end or 0 for s in segments), default=0),
                cover_url="",
                subtitle_source=subtitle_source,
                raw_transcript=raw,
                clean_transcript=cleaned,
                segments=segments,
                focus_query="",
                extraction_mode="full",
                summary=summary,
                full_extraction=extraction,
                evidence=evidence,
                automatic_tagging=automatic_tagging,
                warnings=warnings,
            )
            self.repository.save_job_artifact(
                job.id, "extraction", {"result": result.model_dump(mode="json")}
            )
            self._check_cancel(cancel_requested)
            self._transition(job, "saving", 90)
            markdown = render_markdown(result)
            try:
                shutil.rmtree(workdir)
                workdir = None
            except OSError as exc:
                raise PipelineError(
                    "CLEANUP_FAILED",
                    "临时媒体清理失败，需要人工检查受限任务目录。",
                ) from exc
            self._check_cancel(cancel_requested)
            terminal = (
                "completed_with_warnings" if result.warnings else "completed"
            )
            if retain_media:
                if self.media_service is None:
                    raise PipelineError(
                        "MEDIA_RETENTION_FAILED",
                        "持久媒体目录未配置，无法保留上传媒体。",
                    )
                try:
                    prepared_media = self.media_service.prepare(source, video_id)
                except RetainedMediaError as exc:
                    raise PipelineError(exc.code, str(exc)) from exc
            if not self.repository.commit_job_result(
                job,
                result,
                terminal,
                retained_media=(
                    prepared_media.record if prepared_media is not None else None
                ),
            ):
                persisted = self.repository.get_job(job.id)
                if persisted is not None and persisted.cancel_requested:
                    raise PipelineCancelled()
                raise PipelineError(
                    "JOB_STATE_CONFLICT",
                    "保存结果时任务状态已改变，结果未写入。",
                )
            if prepared_media is not None and self.media_service is not None:
                self.media_service.activate(prepared_media)
            prepared_media = None
            self.repository.delete_job_artifact(job.id)
            return job, result, markdown
        except PipelineError as exc:
            job.status = "cancelled" if isinstance(exc, PipelineCancelled) else "failed"
            job.error_code, job.message = exc.code, str(exc)
            cached_result = (
                self.repository.load_result(
                    job.video_id, platform="local_upload"
                )
                if job.video_id
                else None
            )
            artifact = self.repository.get_job_artifact(job.id)
            job.retryable = (
                not isinstance(exc, PipelineCancelled)
                and is_retryable_error(exc.code)
                and (cached_result is not None or artifact is not None)
                and job.retry_count < job.max_retries
            )
            self.repository.save_job(job)
            raise
        except Exception as exc:
            job.status, job.error_code = "failed", "EXTRACTION_FAILED"
            job.message = "处理失败，请检查输入和服务配置。"
            job.retryable = False
            self.repository.save_job(job)
            raise PipelineError(job.error_code, job.message) from exc
        finally:
            if prepared_media is not None and self.media_service is not None:
                self.media_service.discard(prepared_media)
            if workdir is not None:
                try:
                    shutil.rmtree(workdir)
                except OSError:
                    updated = job.model_copy(deep=True)
                    updated.warnings.append(
                        "临时媒体清理失败，需要人工检查受限任务目录。"
                    )
                    if self.repository.save_job(updated):
                        job.warnings = updated.warnings
                        job.revision = updated.revision

    def _transition(self, job: Job, status: str, progress: int) -> None:
        if (
            status not in {"cancelled", "failed"}
            and self.repository.is_cancel_requested(job.id)
        ):
            raise PipelineCancelled()
        job.status, job.progress, job.error_code, job.message = status, progress, None, ""
        if job.checkpoint != "result_saved":
            job.checkpoint = status
        job.retryable = False
        if not self.repository.save_job(job):
            persisted = self.repository.get_job(job.id)
            if persisted is not None and persisted.cancel_requested:
                raise PipelineCancelled()
            raise PipelineError("JOB_STATE_CONFLICT", "任务状态已被其他执行器更新。")

    def _combined_cancel(
        self,
        job_id: str,
        callback: Callable[[], bool] | None,
    ) -> Callable[[], bool]:
        return lambda: self.repository.is_cancel_requested(job_id) or (
            callback is not None and callback()
        )

    def resume(self, job: Job) -> tuple[Job, VideoResult, str]:
        artifact = self.repository.get_job_artifact(job.id)
        if artifact is None:
            raise PipelineError(
                "MEDIA_UNAVAILABLE",
                "原上传媒体已删除，且不存在可验证的阶段产物。",
            )
        stage, payload = artifact
        try:
            if stage == "extraction":
                result = VideoResult.model_validate(payload["result"])
            else:
                segments = [
                    Segment.model_validate(item) for item in payload["segments"]
                ]
                raw = str(payload["raw"])
                cleaned = (
                    str(payload["cleaned"])
                    if stage == "clean"
                    else clean_transcript(raw)
                )
                self.repository.save_job_artifact(
                    job.id, "clean", {**payload, "cleaned": cleaned}
                )
                self._transition(job, "extracting", 75)
                try:
                    summary, extraction, evidence = self.extractor.extract(
                        raw, cleaned, segments
                    )
                except Exception as exc:
                    raise PipelineError(
                        "EXTRACTION_FAILED", "内容提取服务暂时失败。"
                    ) from exc
                try:
                    validate_evidence(extraction, evidence, segments)
                except EvidenceValidationError as exc:
                    raise PipelineError(
                        "INVALID_EXTRACTION", "提取结果未通过证据校验。"
                    ) from exc
                automatic_tagging = generate_automatic_tagging(
                    self.auto_tagger,
                    raw,
                    cleaned,
                    segments,
                    extraction,
                    self.repository.load_automatic_tagging(
                        str(payload["video_id"]), platform="local_upload"
                    ),
                )
                warnings = []
                if automatic_tagging.status == "failed":
                    warnings.append(AUTOMATIC_TAGGING_FAILED_WARNING)
                result = VideoResult(
                    platform="local_upload",
                    source_url=f"local://{payload['source_name']}",
                    canonical_url=f"local://sha256/{payload['video_id']}",
                    video_id=str(payload["video_id"]),
                    author="",
                    title=Path(str(payload["source_name"])).stem,
                    description="",
                    tags=[],
                    duration=max((item.end or 0 for item in segments), default=0),
                    cover_url="",
                    subtitle_source=str(payload["subtitle_source"]),
                    raw_transcript=raw,
                    clean_transcript=cleaned,
                    segments=segments,
                    focus_query="",
                    extraction_mode="full",
                    summary=summary,
                    full_extraction=extraction,
                    evidence=evidence,
                    automatic_tagging=automatic_tagging,
                    warnings=warnings,
                )
                self.repository.save_job_artifact(
                    job.id,
                    "extraction",
                    {"result": result.model_dump(mode="json")},
                )
            if result.automatic_tagging.status == "not_generated":
                automatic_tagging = generate_automatic_tagging(
                    self.auto_tagger,
                    result.raw_transcript,
                    result.clean_transcript,
                    result.segments,
                    result.full_extraction,
                    self.repository.load_automatic_tagging(
                        result.video_id, platform=result.platform
                    ),
                )
                warnings = list(result.warnings)
                if (
                    automatic_tagging.status == "failed"
                    and AUTOMATIC_TAGGING_FAILED_WARNING not in warnings
                ):
                    warnings.append(AUTOMATIC_TAGGING_FAILED_WARNING)
                result = result.model_copy(
                    update={
                        "automatic_tagging": automatic_tagging,
                        "warnings": warnings,
                    }
                )
                self.repository.save_job_artifact(
                    job.id,
                    "extraction",
                    {"result": result.model_dump(mode="json")},
                )
            if self.repository.is_cancel_requested(job.id):
                raise PipelineCancelled()
            self._transition(job, "saving", 90)
            if self.repository.is_cancel_requested(job.id):
                raise PipelineCancelled()
            terminal = (
                "completed_with_warnings" if result.warnings else "completed"
            )
            if not self.repository.commit_job_result(job, result, terminal):
                persisted = self.repository.get_job(job.id)
                if persisted is not None and persisted.cancel_requested:
                    raise PipelineCancelled()
                raise PipelineError(
                    "JOB_STATE_CONFLICT",
                    "保存结果时任务状态已改变，结果未写入。",
                )
            self.repository.delete_job_artifact(job.id)
            return job, result, render_markdown(result)
        except PipelineError as exc:
            job.status = "cancelled" if isinstance(exc, PipelineCancelled) else "failed"
            job.error_code, job.message = exc.code, str(exc)
            job.retryable = (
                not isinstance(exc, PipelineCancelled)
                and is_retryable_error(exc.code)
                and job.retry_count < job.max_retries
            )
            self.repository.save_job(job)
            raise
        except Exception as exc:
            job.status = "failed"
            job.error_code = "EXTRACTION_FAILED"
            job.message = "恢复阶段产物失败。"
            job.retryable = False
            self.repository.save_job(job)
            raise PipelineError(job.error_code, job.message) from exc

    @staticmethod
    def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
        if cancel_requested is not None and cancel_requested():
            raise PipelineCancelled()

    @staticmethod
    def _validate_source(source: Path, max_media_bytes: int) -> None:
        if not source.is_file():
            raise PipelineError("INVALID_INPUT", "本地媒体文件不存在。")
        if source.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise PipelineError("UNSUPPORTED_MEDIA_TYPE", "不支持该媒体类型。")
        if source.stat().st_size == 0:
            raise PipelineError("INVALID_INPUT", "本地媒体文件为空。")
        if source.stat().st_size > max_media_bytes:
            raise PipelineError("UPLOAD_TOO_LARGE", "媒体文件超过大小限制。")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
