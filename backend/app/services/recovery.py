from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..domain.models import Job
from ..repositories.sqlite import SQLiteRepository
from .pipeline import LocalFullPipeline, PipelineError
from .resolution import ResolutionService

TEMP_MARKER = ".video-knowledge-temp"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    return bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


class TemporaryMediaService:
    def __init__(
        self,
        repository: SQLiteRepository,
        roots: Iterable[tuple[str | Path, tuple[str, ...]]],
        *,
        max_age_seconds: float = 24 * 60 * 60,
        now: Callable[[], float] = time.time,
        remover: Callable[[Path], None] | None = None,
        max_candidates: int = 1000,
        max_entries_per_directory: int = 10000,
    ):
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must not be negative")
        self.repository = repository
        self.roots = []
        forbidden = {Path.cwd().resolve(), Path.home().resolve()}
        for root, prefixes in roots:
            candidate = Path(root)
            if candidate.exists() and (
                candidate.is_symlink() or _is_reparse_point(candidate)
            ):
                raise ValueError("temporary root must not be a symbolic link")
            resolved = candidate.resolve()
            if (
                resolved in forbidden
                or resolved == Path(resolved.anchor)
                or resolved.parent == resolved
            ):
                raise ValueError("temporary root is too broad")
            self.roots.append((candidate, prefixes))
        self.max_age_seconds = max_age_seconds
        self.now = now
        self.remover = remover or (lambda path: shutil.rmtree(path))
        self.max_candidates = max(1, max_candidates)
        self.max_entries_per_directory = max(1, max_entries_per_directory)

    def cleanup_orphans(self) -> list[str]:
        warnings: list[str] = []
        for root, prefixes in self.roots:
            if not root.exists() or not root.is_dir():
                continue
            root_resolved = root.resolve()
            for candidate_index, candidate in enumerate(root.iterdir()):
                if candidate_index >= self.max_candidates:
                    message = "临时目录扫描达到安全上限，剩余候选已跳过。"
                    warnings.append(message)
                    self.repository.add_system_warning("ORPHAN_SCAN_LIMIT", message)
                    break
                if not candidate.name.startswith(prefixes):
                    continue
                try:
                    if candidate.is_symlink() or _is_reparse_point(candidate):
                        raise ValueError("symbolic link")
                    resolved = candidate.resolve(strict=True)
                    if (
                        resolved.parent != root_resolved
                        or resolved == root_resolved
                        or not candidate.is_dir()
                    ):
                        raise ValueError("unsafe target")
                    marker = candidate / TEMP_MARKER
                    if (
                        not marker.is_file()
                        or marker.is_symlink()
                        or marker.resolve(strict=True).parent != resolved
                    ):
                        # A matching prefix without our ownership marker belongs
                        # to somebody else and must not be touched.
                        continue
                    ownership = marker.read_text(encoding="utf-8").strip()
                    if not ownership.startswith("job:"):
                        raise ValueError("invalid ownership marker")
                    active = self.repository.get_job(ownership[4:])
                    if active is None:
                        raise ValueError("unknown ownership job")
                    if active.status not in {
                        "completed",
                        "completed_with_warnings",
                        "failed",
                        "cancelled",
                    }:
                        continue
                    entry_count = 0
                    for current_root, directories, files in os.walk(
                        candidate, followlinks=False
                    ):
                        for name in [*directories, *files]:
                            entry_count += 1
                            if entry_count > self.max_entries_per_directory:
                                raise ValueError("scan limit")
                            nested = Path(current_root) / name
                            if nested.is_symlink() or _is_reparse_point(nested):
                                raise ValueError("nested symbolic link")
                            nested_resolved = nested.resolve(strict=True)
                            if resolved not in nested_resolved.parents and nested_resolved != resolved:
                                raise ValueError("nested target escaped")
                    age = self.now() - candidate.stat(follow_symlinks=False).st_mtime
                    if age < self.max_age_seconds:
                        continue
                    self.remover(candidate)
                except ValueError:
                    message = (
                        "发现不安全的临时目录候选，已跳过且未跟随链接。"
                    )
                    warnings.append(message)
                    self.repository.add_system_warning(
                        "ORPHAN_SKIPPED_UNSAFE", message
                    )
                except OSError:
                    message = "孤立临时目录清理失败，需要人工检查受限临时根目录。"
                    warnings.append(message)
                    self.repository.add_system_warning(
                        "ORPHAN_CLEANUP_FAILED", message
                    )
        return warnings


class JobRecoveryService:
    def __init__(
        self,
        repository: SQLiteRepository,
        pipeline: LocalFullPipeline,
        resolution_service: ResolutionService | None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        base_delay_seconds: float = 0,
        heartbeat_timeout_seconds: float = 600,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.pipeline = pipeline
        self.resolution_service = resolution_service
        self.sleeper = sleeper
        self.base_delay_seconds = max(0, base_delay_seconds)
        self.heartbeat_timeout_seconds = max(0, heartbeat_timeout_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))

    def recover_startup(self) -> list[Job]:
        stale_before = (
            self.clock() - timedelta(seconds=self.heartbeat_timeout_seconds)
        ).isoformat()
        return self.repository.recover_incomplete_jobs(stale_before)

    def cancel(self, job_id: str) -> Job:
        job = self.repository.request_job_cancel(job_id)
        if job is None:
            raise PipelineError("NOT_FOUND", "任务不存在。")
        return job

    def retry(self, job_id: str) -> Job:
        job = self.claim_retry(job_id)
        return self.resume_claimed(job)

    def claim_retry(self, job_id: str) -> Job:
        """Atomically move one retryable failed job back to queued."""
        job = self.repository.get_job(job_id)
        if job is None:
            raise PipelineError("NOT_FOUND", "任务不存在。")
        if job.status not in {"failed"}:
            raise PipelineError("RETRY_NOT_ALLOWED", "只有失败任务可以重试。")
        if not job.retryable:
            raise PipelineError("RETRY_NOT_ALLOWED", "该失败不可安全重试。")
        if job.retry_count >= job.max_retries:
            raise PipelineError("RETRY_LIMIT_EXCEEDED", "任务已达到最大重试次数。")
        request = self.repository.get_job_request(job.id)
        if request is None or request[0] == "none":
            raise PipelineError("RETRY_NOT_ALLOWED", "任务缺少可恢复请求信息。")
        lease_owner = f"retry-{uuid.uuid4()}"
        job = self.repository.prepare_job_retry(job.id, lease_owner)
        if job is None:
            raise PipelineError("RETRY_NOT_ALLOWED", "任务已被其他执行器领取或状态已改变。")
        return job

    def resume_claimed(self, job: Job) -> Job:
        """Resume a retry already claimed by ``claim_retry``."""
        request = self.repository.get_job_request(job.id)
        if request is None or request[0] == "none":
            self._fail_retry(job, "RETRY_NOT_ALLOWED", "任务缺少可恢复请求信息。")
        delay = self.base_delay_seconds * (2 ** max(0, job.retry_count - 1))
        if delay:
            self.sleeper(delay)
        kind, payload = request
        if kind == "resolution":
            if self.resolution_service is None:
                self._fail_retry(job, "RETRY_NOT_AVAILABLE", "链接解析服务不可用。")
            if bool(payload.get("focused")):
                self._fail_retry(
                    job,
                    "RETRY_NOT_AVAILABLE",
                    "定向问题正文未持久化，需由用户重新提交该问题。",
                )
            try:
                if self.repository.get_job_artifact(job.id) is not None:
                    return self.resolution_service.resume(job)[0]
                return self.resolution_service.process(
                    str(payload.get("source_url", "")),
                    "",
                    job=job,
                )[0]
            except PipelineError:
                raise
        if kind == "local_upload":
            video_id = str(payload.get("video_id", ""))
            artifact = self.repository.get_job_artifact(job.id)
            if artifact is not None:
                try:
                    return self.pipeline.resume(job)[0]
                except PipelineError:
                    raise
            cached = self.repository.load_result(
                video_id, platform="local_upload"
            )
            if cached is None or job.checkpoint != "result_saved":
                self._fail_retry(
                    job,
                    "MEDIA_UNAVAILABLE",
                    "原上传媒体已按保留策略删除，且没有可复用结果。",
                )
            job.video_id = video_id
            job.status = "completed"
            job.progress = 100
            job.error_code = None
            job.message = ""
            job.retryable = False
            self.repository.save_job(job)
            return self.repository.get_job(job.id) or job
        self._fail_retry(job, "RETRY_NOT_ALLOWED", "任务类型不可重试。")

    def _fail_retry(self, job: Job, code: str, message: str):
        job.status = "failed"
        job.error_code = code
        job.message = message
        job.retryable = False
        self.repository.save_job(job)
        raise PipelineError(code, message)
