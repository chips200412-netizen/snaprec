from __future__ import annotations

import uuid

from ..domain.models import JobBatch
from ..repositories.sqlite import (
    BatchJobsNotAllowedError,
    BatchJobsNotFoundError,
    SQLiteRepository,
)
from .pipeline import PipelineError


class JobBatchService:
    MIN_ITEMS = 2
    MAX_ITEMS = 10

    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def create(self, items: list[tuple[str, str, str]]) -> JobBatch:
        if len(items) < self.MIN_ITEMS:
            raise PipelineError(
                "INVALID_INPUT",
                "每个批次至少需要选择 2 个任务。",
            )
        if len(items) > self.MAX_ITEMS:
            raise PipelineError(
                "BATCH_LIMIT_EXCEEDED",
                "每个批次最多只能选择 10 个任务。",
            )
        job_ids = [item[0] for item in items]
        if len(set(job_ids)) != len(job_ids):
            raise PipelineError(
                "DUPLICATE_BATCH_ITEM",
                "同一个任务不能在批次中重复出现。",
            )
        try:
            return self.repository.create_job_batch(str(uuid.uuid4()), items)
        except BatchJobsNotFoundError as exc:
            raise PipelineError(
                "BATCH_JOB_NOT_FOUND",
                "批次包含不存在的任务，请刷新后重新选择。",
            ) from exc
        except BatchJobsNotAllowedError as exc:
            raise PipelineError(
                "BATCH_ITEM_NOT_ALLOWED",
                "批次包含已完成、已取消或不可安全重试的任务。",
            ) from exc

    def list(self, *, limit: int = 10) -> list[JobBatch]:
        return self.repository.list_job_batches(limit)

    def get(self, batch_id: str) -> JobBatch:
        batch = self.repository.get_job_batch(batch_id)
        if batch is None:
            raise PipelineError("NOT_FOUND", "批次不存在。")
        return batch

    def cancel_job(self, batch_id: str, job_id: str) -> JobBatch:
        batch = self.repository.get_job_batch(batch_id)
        if batch is None:
            raise PipelineError("NOT_FOUND", "批次不存在。")
        if not self.repository.job_belongs_to_batch(batch_id, job_id):
            raise PipelineError(
                "BATCH_JOB_NOT_MEMBER",
                "该任务不属于当前批次。",
            )
        cancelled = self.repository.request_job_cancel(job_id)
        if cancelled is None:
            raise PipelineError(
                "BATCH_JOB_NOT_FOUND",
                "批次任务不存在。",
            )
        return self.get(batch_id)
