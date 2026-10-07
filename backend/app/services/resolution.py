from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse

from ..adapters.base import (
    ResolvedVideo,
    SubtitlePayload,
    VideoAdapter,
    VideoMetadata,
)
from ..adapters.registry import AdapterRegistry
from ..domain.models import FullExtraction, Job, Segment, VideoResult
from ..repositories.sqlite import SQLiteRepository, focus_query_hash
from .cleaning import clean_transcript
from .evidence import EvidenceValidationError, validate_evidence
from .focused import FocusedExtractionService
from .interfaces import AutoTagger, FocusedExtractor, FullExtractor
from .markdown import render_markdown
from .pipeline import PipelineCancelled, PipelineError, is_retryable_error
from .providers import DeterministicAutoTagger
from .tagging import (
    AUTOMATIC_TAGGING_FAILED_WARNING,
    generate_automatic_tagging,
)

_URL_RE = re.compile(
    r"https?://[^\s<>'\"，。！？；：、）】》」』]+", re.IGNORECASE
)
_TRAILING_PUNCTUATION = ".,!?;:，。！？；：、)]}）】》」』"


def extract_urls(input_text: str) -> list[str]:
    return [
        match.group(0).rstrip(_TRAILING_PUNCTUATION)
        for match in _URL_RE.finditer(input_text)
    ]


def _input_metadata(adapter: VideoAdapter, input_text: str) -> VideoMetadata:
    reader = getattr(adapter, "get_share_metadata", None)
    if not callable(reader):
        return VideoMetadata()
    metadata = reader(input_text)
    return metadata if isinstance(metadata, VideoMetadata) else VideoMetadata()


def _merge_metadata(primary: VideoMetadata, fallback: VideoMetadata) -> VideoMetadata:
    uses_fallback = any(
        (
            not primary.author and bool(fallback.author),
            not primary.title and bool(fallback.title),
            not primary.description and bool(fallback.description),
            not primary.tags and bool(fallback.tags),
            not primary.duration and bool(fallback.duration),
            not primary.cover_url and bool(fallback.cover_url),
        )
    )
    return VideoMetadata(
        author=primary.author or fallback.author,
        title=primary.title or fallback.title,
        description=primary.description or fallback.description,
        tags=primary.tags or fallback.tags,
        duration=primary.duration or fallback.duration,
        cover_url=primary.cover_url or fallback.cover_url,
        warnings=list(
            dict.fromkeys(
                [*primary.warnings, *(fallback.warnings if uses_fallback else [])]
            )
        ),
    )


def _metadata_dict(metadata: VideoMetadata) -> dict[str, object]:
    return {
        "author": metadata.author,
        "title": metadata.title,
        "description": metadata.description,
        "tags": metadata.tags,
        "duration": metadata.duration,
        "cover_url": metadata.cover_url,
        "warnings": metadata.warnings,
    }


class ResolutionService:
    def __init__(
        self,
        repository: SQLiteRepository,
        registry: AdapterRegistry,
        full_extractor: FullExtractor,
        focused_extractor: FocusedExtractor,
        auto_tagger: AutoTagger | None = None,
    ):
        self.repository = repository
        self.registry = registry
        self.full_extractor = full_extractor
        self.auto_tagger = auto_tagger or DeterministicAutoTagger()
        self.focused_service = FocusedExtractionService(repository, focused_extractor)

    def _select_supported(self, input_text: str) -> tuple[str, VideoAdapter]:
        if not input_text.strip():
            raise PipelineError("INVALID_INPUT", "链接或分享文本不能为空。")
        all_urls = extract_urls(input_text)
        supported = [
            (url, adapter)
            for url in all_urls
            if (adapter := self.registry.matching(url)) is not None
        ]
        if len(supported) > 1:
            raise PipelineError("AMBIGUOUS_LINKS", "检测到多个受支持的视频链接，请只保留一个。")
        if not supported:
            message = (
                "暂不支持该平台，请上传本地视频或音频。"
                if all_urls
                else "未识别到受支持的视频链接。"
            )
            raise PipelineError(
                "UNSUPPORTED_PLATFORM" if all_urls else "INVALID_INPUT", message
            )
        return supported[0]

    def preview(
        self,
        input_text: str,
    ) -> tuple[ResolvedVideo, VideoMetadata]:
        """Resolve public identity and metadata without creating a parse job."""
        source_url, adapter = self._select_supported(input_text)
        video = adapter.resolve(source_url)
        metadata = _merge_metadata(
            adapter.get_metadata(video),
            _input_metadata(adapter, input_text),
        )
        return video, metadata

    def process(
        self,
        input_text: str,
        focus_query: str = "",
        job: Job | None = None,
    ) -> tuple[Job, VideoResult, str]:
        source_url, adapter = self._select_supported(input_text)
        input_metadata = _input_metadata(adapter, input_text)
        job = job or Job(id=str(uuid.uuid4()), status="queued", progress=0)
        if not self.repository.save_job(job):
            persisted = self.repository.get_job(job.id)
            if persisted is not None and persisted.cancel_requested:
                raise PipelineCancelled()
            raise PipelineError("JOB_STATE_CONFLICT", "任务状态已被其他执行器更新。")
        self.repository.configure_job_request(
            job.id,
            "resolution",
            {
                "source_url": source_url,
                "focused": bool(focus_query.strip()),
                "focus_query_hash": (
                    focus_query_hash(focus_query) if focus_query.strip() else ""
                ),
            },
        )
        try:
            self._transition(job, "resolving", 10)
            alias = self.repository.load_video_alias(source_url)
            if alias is not None:
                _, alias_video_id = alias
                cached = self.repository.load_result(
                    alias_video_id, platform=alias[0]
                )
                if cached is not None and cached.subtitle_source != "none":
                    job.video_id = alias_video_id
                    result = (
                        self.focused_service.extract(
                            alias_video_id, focus_query, platform=alias[0]
                        )
                        if focus_query.strip()
                        else cached
                    )
                    self._transition(job, "completed", 100)
                    return job, result, render_markdown(result)
            identity = adapter.identify(source_url) if hasattr(adapter, "identify") else None
            if identity is not None:
                identified_id, _ = identity
                identified_platform = getattr(adapter, "platform", "")
                cached = self.repository.load_result(
                    identified_id, platform=identified_platform
                )
                if cached is not None and cached.subtitle_source != "none":
                    job.video_id = identified_id
                    result = (
                        self.focused_service.extract(
                            identified_id, focus_query, platform=identified_platform
                        )
                        if focus_query.strip()
                        else cached
                    )
                    self._transition(job, "completed", 100)
                    return job, result, render_markdown(result)
            video = adapter.resolve(source_url)
            job.video_id = video.video_id
            artifact = {
                "video": {
                    "platform": video.platform,
                    "source_url": video.source_url,
                    "canonical_url": video.canonical_url,
                    "video_id": video.video_id,
                    "page_html": video.page_html,
                    "aliases": list(video.aliases),
                },
                "input_metadata": _metadata_dict(input_metadata),
            }
            self.repository.save_job_artifact(job.id, "resolved", artifact)
            cached = self.repository.load_result(
                video.video_id, platform=video.platform
            )
            # A no-subtitle result is intentionally retried on a later request:
            # public subtitles can appear later and must not be locked out forever.
            if cached is not None and cached.subtitle_source != "none":
                if focus_query.strip():
                    result = self.focused_service.extract(
                        video.video_id, focus_query, platform=video.platform
                    )
                else:
                    result = cached
                self._transition(job, "completed", 100)
                return job, result, render_markdown(result)

            self._transition(job, "fetching_metadata", 30)
            metadata = _merge_metadata(adapter.get_metadata(video), input_metadata)
            artifact["metadata"] = {
                "author": metadata.author, "title": metadata.title,
                "description": metadata.description, "tags": metadata.tags,
                "duration": metadata.duration, "cover_url": metadata.cover_url,
                "warnings": metadata.warnings,
            }
            self.repository.save_job_artifact(job.id, "metadata", artifact)
            self._transition(job, "fetching_subtitles", 50)
            subtitles = adapter.get_subtitles(video)
            artifact["subtitles"] = {
                "segments": [item.model_dump(mode="json") for item in subtitles.segments],
                "source": subtitles.source, "warnings": subtitles.warnings,
            }
            self.repository.save_job_artifact(job.id, "subtitles", artifact)
            warnings = [*metadata.warnings, *subtitles.warnings]
            if any(_is_numbered_part(alias) for alias in video.aliases):
                warnings.append(
                    "该链接指定了分P；当前原型以主视频 ID 缓存，结果仅代表页面公开提供的当前字幕。"
                )

            if subtitles.segments:
                raw = "\n".join(segment.text for segment in subtitles.segments)
                self._transition(job, "cleaning", 65)
                cleaned = clean_transcript(raw)
                artifact["raw"] = raw
                artifact["cleaned"] = cleaned
                self.repository.save_job_artifact(job.id, "clean", artifact)
                self._transition(job, "extracting", 80)
                try:
                    summary, extraction, evidence = self.full_extractor.extract(
                        raw, cleaned, subtitles.segments
                    )
                except Exception as exc:
                    raise PipelineError(
                        "EXTRACTION_FAILED", "内容提取服务暂时失败。"
                    ) from exc
                try:
                    validate_evidence(extraction, evidence, subtitles.segments)
                except EvidenceValidationError as exc:
                    raise PipelineError(
                        "INVALID_EXTRACTION", "提取结果未通过证据校验。"
                    ) from exc
            else:
                raw, cleaned, summary = "", "", ""
                extraction, evidence = FullExtraction(), []
                if not any("字幕" in warning for warning in warnings):
                    warnings.append(
                        "字幕残缺或不可用，无法判断视频内容；未使用标题或简介补充结论。"
                    )
                elif not any("字幕残缺" in warning for warning in warnings):
                    warnings.append("字幕残缺，无法判断未提供的完整视频内容。")
                warnings.append("未获取平台媒体，未触发 ASR；可上传本地媒体继续处理。")

            automatic_tagging = generate_automatic_tagging(
                self.auto_tagger,
                raw,
                cleaned,
                subtitles.segments,
                extraction,
                self.repository.load_automatic_tagging(
                    video.video_id, platform=video.platform
                ),
            )
            if automatic_tagging.status == "failed":
                warnings.append(AUTOMATIC_TAGGING_FAILED_WARNING)
            result = self._build_result(
                video.platform,
                video.source_url,
                video.canonical_url,
                video.video_id,
                metadata,
                subtitles.source,
                raw,
                cleaned,
                subtitles.segments,
                summary,
                extraction,
                evidence,
                warnings,
            )
            result = result.model_copy(
                update={"automatic_tagging": automatic_tagging}
            )
            self.repository.save_job_artifact(
                job.id, "extraction", {"result": result.model_dump(mode="json")}
            )
            focused_result = (
                self.focused_service.extract_unsaved(result, focus_query)
                if focus_query.strip()
                else None
            )
            self._transition(job, "saving", 90)
            terminal = "completed_with_warnings" if warnings else "completed"
            if not self.repository.commit_job_result(
                job, result, terminal, focused_result=focused_result
            ):
                persisted = self.repository.get_job(job.id)
                if persisted is not None and persisted.cancel_requested:
                    raise PipelineCancelled()
                raise PipelineError(
                    "JOB_STATE_CONFLICT",
                    "保存结果时任务状态已改变，结果未写入。",
                )
            for alias_url in video.aliases or (source_url,):
                self.repository.save_video_alias(
                    alias_url, video.platform, video.video_id
                )
            self.repository.delete_job_artifact(job.id)
            if focused_result is not None:
                result = focused_result
            return job, result, render_markdown(result)
        except PipelineError as exc:
            job.status = "cancelled" if isinstance(exc, PipelineCancelled) else "failed"
            job.error_code, job.message = exc.code, str(exc)
            job.retryable = (
                not isinstance(exc, PipelineCancelled)
                and is_retryable_error(exc.code)
                and not focus_query.strip()
                and job.retry_count < job.max_retries
            )
            self.repository.save_job(job)
            raise
        except Exception as exc:
            job.status, job.error_code = "failed", "EXTRACTION_FAILED"
            job.message = "链接解析失败，请稍后重试或上传本地媒体。"
            job.retryable = False
            self.repository.save_job(job)
            raise PipelineError(job.error_code, job.message) from exc

    def resume(
        self, job: Job, focus_query: str = ""
    ) -> tuple[Job, VideoResult, str]:
        artifact = self.repository.get_job_artifact(job.id)
        if artifact is None:
            raise PipelineError("RETRY_NOT_AVAILABLE", "链接任务没有可恢复检查点。")
        stage, payload = artifact
        try:
            if stage in {"resolved", "metadata"}:
                video_data = payload["video"]
                video = ResolvedVideo(
                    platform=video_data["platform"],
                    source_url=video_data["source_url"],
                    canonical_url=video_data["canonical_url"],
                    video_id=video_data["video_id"],
                    page_html=video_data.get("page_html", ""),
                    aliases=tuple(video_data.get("aliases", ())),
                )
                adapter = self.registry.matching(video.source_url)
                if adapter is None:
                    raise PipelineError(
                        "RETRY_NOT_AVAILABLE", "恢复时平台适配器不可用。"
                    )
                if stage == "resolved":
                    self._transition(job, "fetching_metadata", 30)
                    metadata = _merge_metadata(
                        adapter.get_metadata(video),
                        VideoMetadata(**payload.get("input_metadata", {})),
                    )
                    payload["metadata"] = {
                        "author": metadata.author, "title": metadata.title,
                        "description": metadata.description, "tags": metadata.tags,
                        "duration": metadata.duration,
                        "cover_url": metadata.cover_url,
                        "warnings": metadata.warnings,
                    }
                    self.repository.save_job_artifact(job.id, "metadata", payload)
                self._transition(job, "fetching_subtitles", 50)
                subtitles = adapter.get_subtitles(video)
                payload["subtitles"] = {
                    "segments": [
                        item.model_dump(mode="json") for item in subtitles.segments
                    ],
                    "source": subtitles.source,
                    "warnings": subtitles.warnings,
                }
                self.repository.save_job_artifact(job.id, "subtitles", payload)
                stage = "subtitles"
            if stage == "extraction":
                result = VideoResult.model_validate(payload["result"])
                video_aliases = (result.source_url,)
            elif stage in {"subtitles", "clean"}:
                video_data = payload["video"]
                metadata = VideoMetadata(**payload["metadata"])
                subtitle_data = payload["subtitles"]
                subtitles = SubtitlePayload(
                    segments=[
                        Segment.model_validate(item)
                        for item in subtitle_data["segments"]
                    ],
                    source=subtitle_data["source"],
                    warnings=subtitle_data["warnings"],
                )
                raw = payload.get("raw") or "\n".join(
                    item.text for item in subtitles.segments
                )
                cleaned = payload.get("cleaned") or clean_transcript(raw)
                warnings = [*metadata.warnings, *subtitles.warnings]
                if subtitles.segments:
                    if stage == "subtitles":
                        self._transition(job, "cleaning", 65)
                        payload["raw"] = raw
                        payload["cleaned"] = cleaned
                        self.repository.save_job_artifact(job.id, "clean", payload)
                    self._transition(job, "extracting", 80)
                    try:
                        summary, extraction, evidence = self.full_extractor.extract(
                            raw, cleaned, subtitles.segments
                        )
                        validate_evidence(extraction, evidence, subtitles.segments)
                    except EvidenceValidationError as exc:
                        raise PipelineError(
                            "INVALID_EXTRACTION", "提取结果未通过证据校验。"
                        ) from exc
                    except Exception as exc:
                        raise PipelineError(
                            "EXTRACTION_FAILED", "内容提取服务暂时失败。"
                        ) from exc
                else:
                    summary, extraction, evidence = "", FullExtraction(), []
                    warnings.append(
                        "字幕残缺或不可用，无法判断视频内容；未使用标题或简介补充结论。"
                    )
                    warnings.append(
                        "未获取平台媒体，未触发 ASR；可上传本地媒体继续处理。"
                    )
                result = self._build_result(
                    video_data["platform"], video_data["source_url"],
                    video_data["canonical_url"], video_data["video_id"],
                    metadata, subtitles.source, raw, cleaned,
                    subtitles.segments, summary, extraction, evidence, warnings,
                )
                video_aliases = tuple(video_data.get("aliases") or (result.source_url,))
                self.repository.save_job_artifact(
                    job.id, "extraction",
                    {"result": result.model_dump(mode="json")},
                )
            else:
                raise PipelineError(
                    "RETRY_NOT_AVAILABLE",
                    "链接任务尚未达到可脱离平台恢复的检查点。",
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
            focused_result = (
                self.focused_service.extract_unsaved(result, focus_query)
                if focus_query.strip() else None
            )
            self._transition(job, "saving", 90)
            terminal = "completed_with_warnings" if result.warnings else "completed"
            if not self.repository.commit_job_result(
                job, result, terminal, focused_result=focused_result
            ):
                raise PipelineError("JOB_STATE_CONFLICT", "恢复提交时任务状态已改变。")
            for alias_url in video_aliases:
                self.repository.save_video_alias(
                    alias_url, result.platform, result.video_id
                )
            self.repository.delete_job_artifact(job.id)
            output = focused_result or result
            return job, output, render_markdown(output)
        except PipelineError as exc:
            job.status = "cancelled" if isinstance(exc, PipelineCancelled) else "failed"
            job.error_code, job.message = exc.code, str(exc)
            job.retryable = (
                not isinstance(exc, PipelineCancelled)
                and is_retryable_error(exc.code)
                and not focus_query.strip()
                and job.retry_count < job.max_retries
            )
            self.repository.save_job(job)
            raise

    @staticmethod
    def _build_result(
        platform: str,
        source_url: str,
        canonical_url: str,
        video_id: str,
        metadata: VideoMetadata,
        subtitle_source: str,
        raw: str,
        cleaned: str,
        segments,
        summary: str,
        extraction: FullExtraction,
        evidence,
        warnings: list[str],
    ) -> VideoResult:
        return VideoResult(
            platform=platform,
            source_url=source_url,
            canonical_url=canonical_url,
            video_id=video_id,
            author=metadata.author,
            title=metadata.title,
            description=metadata.description,
            tags=metadata.tags,
            duration=metadata.duration,
            cover_url=metadata.cover_url,
            subtitle_source=subtitle_source,
            raw_transcript=raw,
            clean_transcript=cleaned,
            segments=segments,
            focus_query="",
            extraction_mode="full",
            summary=summary,
            full_extraction=extraction,
            evidence=evidence,
            warnings=warnings,
        )

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


def _is_numbered_part(url: str) -> bool:
    values = parse_qs(urlparse(url).query).get("p", [])
    return any(value.isdigit() and int(value) > 1 for value in values)
