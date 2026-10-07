from __future__ import annotations

import hashlib
import uuid

from ..domain.models import CollectionDeepAnalysisSnapshot, Job
from ..repositories.sqlite import SQLiteRepository, resource_key
from .pipeline import PipelineError
from .recovery import JobRecoveryService
from .resolution import ResolutionService


DEEP_ANALYSIS_CONFIG_VERSION = "collection-default-v1"
_SUPPORTED_PLATFORMS = {"bilibili", "douyin"}
_TIMEOUT_CODES = {"FETCH_TIMEOUT", "PROVIDER_TIMEOUT", "ASR_TIMEOUT"}


class CollectionDeepAnalysisService:
    """Bridge collection identities to the existing evidence-first pipeline."""

    def __init__(
        self,
        repository: SQLiteRepository,
        resolution_service: ResolutionService | None,
        recovery_service: JobRecoveryService,
    ) -> None:
        self.repository = repository
        self.resolution_service = resolution_service
        self.recovery_service = recovery_service

    def get(self, material_id: str) -> CollectionDeepAnalysisSnapshot:
        item = self.repository.get_collection_item(material_id)
        if item is None:
            raise PipelineError("NOT_FOUND", "收藏素材不存在。")

        job = self.repository.get_collection_deep_job(
            material_id, DEEP_ANALYSIS_CONFIG_VERSION
        )
        result_key = item.get("deep_analysis_resource_key") or None
        if job is not None and not result_key and job.video_id:
            candidate = resource_key(item["platform"], job.video_id)
            if self.repository.get_deep_result_summary(candidate) is not None:
                # A GET may recover a result identity after a process stopped
                # between the terminal job write and the collection link, but
                # it must remain read-only. The next explicit worker action may
                # persist the link; the snapshot can safely use this identity.
                result_key = candidate

        result = (
            self.repository.get_deep_result_summary(result_key)
            if result_key
            else None
        )
        if job is None and result is not None:
            synthetic = hashlib.sha256(
                f"{material_id}\0{result_key}".encode("utf-8")
            ).hexdigest()[:24]
            return self._result_snapshot(
                material_id,
                f"legacy-{synthetic}",
                result,
                job_revision=0,
            )

        if job is not None:
            if job.status in {"completed", "completed_with_warnings"}:
                if result is None:
                    return CollectionDeepAnalysisSnapshot(
                        material_id=material_id,
                        analysis_job_id=job.id,
                        state="failed",
                        config_version=DEEP_ANALYSIS_CONFIG_VERSION,
                        job_revision=job.revision,
                        failed_stage=job.checkpoint,
                        updated_at=job.heartbeat_at,
                        error_code="RESULT_LINK_FAILED",
                        error_message="解析任务已结束，但结果暂时无法读取。",
                    )
                return self._result_snapshot(
                    material_id,
                    job.id,
                    result,
                    job_revision=job.revision,
                )
            if job.status == "failed":
                return CollectionDeepAnalysisSnapshot(
                    material_id=material_id,
                    analysis_job_id=job.id,
                    state=(
                        "timed_out"
                        if job.error_code in _TIMEOUT_CODES
                        else "failed"
                    ),
                    config_version=DEEP_ANALYSIS_CONFIG_VERSION,
                    job_revision=job.revision,
                    failed_stage=job.checkpoint,
                    can_retry=job.retryable,
                    updated_at=job.heartbeat_at,
                    error_code=job.error_code,
                    error_message=job.message or "深度解析未能完成。",
                )
            if job.status == "cancelled":
                return CollectionDeepAnalysisSnapshot(
                    material_id=material_id,
                    analysis_job_id=job.id,
                    state="failed",
                    config_version=DEEP_ANALYSIS_CONFIG_VERSION,
                    job_revision=job.revision,
                    failed_stage=job.checkpoint,
                    updated_at=job.heartbeat_at,
                    error_code="CANCELLED",
                    error_message="该解析任务已取消，详情页不会自动创建替代任务。",
                )
            return CollectionDeepAnalysisSnapshot(
                material_id=material_id,
                analysis_job_id=job.id,
                state="queued" if job.status == "queued" else "running",
                config_version=DEEP_ANALYSIS_CONFIG_VERSION,
                job_revision=job.revision,
                updated_at=job.heartbeat_at,
            )

        limitation = self._eligibility_limitation(item)
        return CollectionDeepAnalysisSnapshot(
            material_id=material_id,
            state="unavailable" if limitation else "ready",
            config_version=DEEP_ANALYSIS_CONFIG_VERSION,
            can_start=not limitation,
            limitation=limitation,
        )

    def start(
        self, material_id: str
    ) -> tuple[CollectionDeepAnalysisSnapshot, tuple[str, str, str] | None]:
        snapshot = self.get(material_id)
        if not snapshot.can_start:
            return snapshot, None
        item = self.repository.get_collection_item(material_id)
        if item is None:
            raise PipelineError("NOT_FOUND", "收藏素材不存在。")
        target = item.get("canonical_url") or item.get("source_url") or ""
        task_key = hashlib.sha256(
            (
                f"{item.get('identity_url', '')}\0"
                f"{DEEP_ANALYSIS_CONFIG_VERSION}"
            ).encode("utf-8")
        ).hexdigest()
        try:
            job, created = self.repository.get_or_create_collection_deep_job(
                material_id,
                DEEP_ANALYSIS_CONFIG_VERSION,
                task_key,
                target,
            )
        except LookupError:
            raise PipelineError("NOT_FOUND", "收藏素材不存在。") from None
        return self.get(material_id), (
            (material_id, job.id, target) if created else None
        )

    def run_start(self, material_id: str, job_id: str, source_url: str) -> None:
        if self.resolution_service is None:
            return
        job = self.repository.get_job(job_id)
        if job is None:
            return
        try:
            finished, result, _ = self.resolution_service.process(
                source_url, "", job=job
            )
            self.repository.attach_collection_deep_result(
                material_id,
                finished.id,
                resource_key(result.platform, result.video_id),
            )
        except PipelineError:
            # ResolutionService persists the authoritative terminal job state.
            return

    def claim_retry(
        self, material_id: str, attempt_key: str
    ) -> tuple[CollectionDeepAnalysisSnapshot, str | None]:
        snapshot = self.get(material_id)
        if not snapshot.analysis_job_id:
            raise PipelineError("RETRY_NOT_ALLOWED", "该失败不可安全重试。")
        try:
            claimed, created = self.repository.claim_collection_deep_retry(
                material_id,
                DEEP_ANALYSIS_CONFIG_VERSION,
                attempt_key,
                f"retry-{uuid.uuid4()}",
            )
        except (LookupError, ValueError):
            raise PipelineError(
                "RETRY_NOT_ALLOWED", "任务已被其他重试领取或状态已改变。"
            ) from None
        return self.get(material_id), claimed.id if created else None

    def run_retry(self, material_id: str, job_id: str) -> None:
        job = self.repository.get_job(job_id)
        if job is None:
            return
        try:
            finished = self.recovery_service.resume_claimed(job)
            item = self.repository.get_collection_item(material_id)
            if item is None or not finished.video_id:
                return
            candidate = resource_key(item["platform"], finished.video_id)
            if self.repository.get_deep_result_summary(candidate) is not None:
                self.repository.attach_collection_deep_result(
                    material_id, finished.id, candidate
                )
        except PipelineError:
            return

    def _eligibility_limitation(self, item: dict) -> str:
        if self.resolution_service is None:
            return "深度解析服务尚未配置；普通收藏与详情阅读不受影响。"
        if item.get("platform") not in _SUPPORTED_PLATFORMS:
            return "当前来源尚未接入深度解析；仍可正常查看来源、整理结果和个人灵感。"
        target = item.get("canonical_url") or item.get("source_url") or ""
        if not target or self.resolution_service.registry.matching(target) is None:
            return "当前来源身份不符合已支持的公开解析边界。"
        return ""

    @staticmethod
    def _result_snapshot(
        material_id: str,
        job_id: str,
        result: dict,
        *,
        job_revision: int,
    ) -> CollectionDeepAnalysisSnapshot:
        limited = bool(result["limited"])
        limitation = ""
        if limited:
            limitation = (
                next(
                    (
                        warning
                        for warning in result["warnings"]
                        if "字幕" in warning or "有限" in warning
                    ),
                    "未取得可用公开字幕，结果只保留已验证的来源限制，未用标题或简介推测素材内容。",
                )
            )
        return CollectionDeepAnalysisSnapshot(
            material_id=material_id,
            analysis_job_id=job_id,
            state="limited" if limited else "completed",
            config_version=DEEP_ANALYSIS_CONFIG_VERSION,
            job_revision=job_revision,
            can_view_result=True,
            result_id=result["resource_key"],
            result_revision=result["updated_at"],
            result_kind="limited" if limited else "complete",
            updated_at=result["updated_at"],
            limitation=limitation,
        )
