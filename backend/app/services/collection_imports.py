from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from pydantic import ValidationError

from ..domain.collection_imports import (
    CollectionImportBatchItemDetail,
    CollectionImportBatchSnapshot,
    CollectionImportCancelCommand,
    CollectionImportConfirmCommand,
    CollectionImportRepreviewCommand,
    CollectionImportResumeCommand,
    CollectionImportReviewCommand,
)
from ..domain.models import CollectionItemCreateRequest, CollectionPreviewRequest
from ..repositories.collection_imports import (
    CollectionImportActiveError,
    CollectionImportAttemptLimitError,
    CollectionImportBatchRevisionConflictError,
    CollectionImportIdempotencyReusedError,
    CollectionImportItemRevisionConflictError,
    CollectionImportNotFoundError,
    CollectionImportPreviewExpiringError,
    CollectionImportRepository,
    CollectionImportRequestInvalidError,
    CollectionImportReviewIncompleteError,
    CollectionImportRetiredError,
    CollectionImportStateConflictError,
    collection_import_save_idempotency_key,
    collection_import_save_idempotency_key_hash,
)
from .capture import CaptureService
from .collections import CollectionService, prepare_collection_create
from .pipeline import PipelineError


RAW_BODY_LIMIT = 128 * 1024
ITEM_COUNT_MIN = 2
ITEM_COUNT_MAX = 10
ITEM_CHARACTER_LIMIT = 10_000
INPUT_UTF8_LIMIT = 64 * 1024
_CLIENT_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_GLOBAL_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
_GLOBAL_SAVE_SLOT = threading.BoundedSemaphore(1)
_SAVE_CLAIM_RELEASED = threading.Event()
_KNOWN_NOT_WRITTEN_SAVE_ERRORS = frozenset(
    {
        "PREVIEW_NOT_FOUND",
        "PREVIEW_INVALID",
        "PREVIEW_EXPIRED",
        "COLLECTION_VALIDATION_FAILED",
    }
)


class CollectionImportError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class SimulatedCollectionImportCrash(RuntimeError):
    """Test-only fault signal that intentionally leaves a durable claim."""


def _reject_request() -> CollectionImportError:
    return CollectionImportError(
        "BATCH_REQUEST_INVALID",
        "批量导入请求无效。",
        422,
    )


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _require_unicode_scalar_strings(value: Any) -> None:
    """Reject JSON escapes that decode to lone UTF-16 surrogates."""

    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            current.encode("utf-8", errors="strict")
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())


def _load_collection_import_object(body: bytes) -> dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_object_without_duplicates)
        if not isinstance(payload, dict):
            raise ValueError("JSON root must be an object")
        _require_unicode_scalar_strings(payload)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ):
        raise _reject_request() from None
    return payload


def parse_collection_import_create(
    body: bytes, idempotency_key: str | None
) -> tuple[list[dict[str, str]], str, str]:
    """Validate the complete bounded request before any database or network work."""

    if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
        idempotency_key
    ):
        raise _reject_request()

    payload = _load_collection_import_object(body)
    if set(payload) != {"items"}:
        raise _reject_request()
    raw_items = payload["items"]
    if (
        not isinstance(raw_items, list)
        or not ITEM_COUNT_MIN <= len(raw_items) <= ITEM_COUNT_MAX
    ):
        raise _reject_request()

    items: list[dict[str, str]] = []
    seen_client_ids: set[str] = set()
    total_bytes = 0
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "client_item_id",
            "input_text",
        }:
            raise _reject_request()
        client_item_id = raw_item["client_item_id"]
        input_text = raw_item["input_text"]
        if (
            not isinstance(client_item_id, str)
            or not _CLIENT_ITEM_ID.fullmatch(client_item_id)
            or client_item_id in seen_client_ids
            or not isinstance(input_text, str)
            or not input_text.strip()
            or len(input_text) > ITEM_CHARACTER_LIMIT
        ):
            raise _reject_request()
        try:
            input_bytes = len(input_text.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            raise _reject_request() from None
        total_bytes += input_bytes
        if total_bytes > INPUT_UTF8_LIMIT:
            raise _reject_request()
        seen_client_ids.add(client_item_id)
        items.append(
            {"client_item_id": client_item_id, "input_text": input_text}
        )

    canonical = json.dumps(
        items, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    request_fingerprint = hashlib.sha256(canonical).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    return items, key_hash, request_fingerprint


def parse_collection_import_review(body: bytes) -> CollectionImportReviewCommand:
    try:
        return CollectionImportReviewCommand.model_validate(
            _load_collection_import_object(body)
        )
    except (ValidationError, TypeError, ValueError):
        raise _reject_request() from None


def parse_collection_import_repreview(body: bytes) -> CollectionImportRepreviewCommand:
    try:
        return CollectionImportRepreviewCommand.model_validate(
            _load_collection_import_object(body)
        )
    except (ValidationError, TypeError, ValueError):
        raise _reject_request() from None


def parse_collection_import_confirm(body: bytes) -> CollectionImportConfirmCommand:
    try:
        return CollectionImportConfirmCommand.model_validate(
            _load_collection_import_object(body)
        )
    except (ValidationError, TypeError, ValueError):
        raise _reject_request() from None


def parse_collection_import_resume(body: bytes) -> CollectionImportResumeCommand:
    try:
        return CollectionImportResumeCommand.model_validate(
            _load_collection_import_object(body)
        )
    except (ValidationError, TypeError, ValueError):
        raise _reject_request() from None


def parse_collection_import_cancel(body: bytes) -> CollectionImportCancelCommand:
    try:
        return CollectionImportCancelCommand.model_validate(
            _load_collection_import_object(body)
        )
    except (ValidationError, TypeError, ValueError):
        raise _reject_request() from None


class CollectionImportPreviewCoordinator:
    def __init__(
        self,
        repository: CollectionImportRepository,
        capture_service: CaptureService,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        fault: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.repository = repository
        self.capture_service = capture_service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.fault = fault
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="collection-import-preview"
        )
        self._lock = threading.RLock()
        self._futures: set[Future[None]] = set()
        self._closed = False

    def _invoke_fault(self, stage: str, context: dict[str, Any]) -> None:
        if self.fault is not None:
            self.fault(stage, context)

    def accept_batch(self, batch_id: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            accepted = False
            try:
                for _ in range(2):
                    future = self._executor.submit(self._drain_batch, batch_id)
                    self._futures.add(future)
                    future.add_done_callback(self._finished)
                    accepted = True
            except RuntimeError:
                return accepted
            return accepted

    def _finished(self, future: Future[None]) -> None:
        # Retrieving the exception avoids unobserved Future diagnostics; details
        # are deliberately not logged because they may carry sensitive input.
        try:
            future.exception()
        except Exception:
            pass
        with self._lock:
            self._futures.discard(future)

    def _drain_batch(self, batch_id: str) -> None:
        while True:
            try:
                claim = self.repository.claim_preview(
                    batch_id=batch_id,
                    preview_id=self.id_factory(),
                    claim_token=self.id_factory(),
                    now=self.clock(),
                )
            except Exception:
                # A durable wake is not considered successfully drained when
                # the claim transaction itself fails.  Persist interruption
                # for every still-unclaimed item so create/resume can return an
                # honest, explicitly recoverable snapshot.
                try:
                    self.repository.mark_dispatch_failed(batch_id, self.clock())
                except Exception:
                    pass
                return
            if claim is None:
                return
            context = {
                "batch_id": batch_id,
                "batch_item_id": claim["batch_item_id"],
                "preview_id": claim["preview_id"],
                "preview_generation": claim["preview_generation"],
            }
            try:
                self._invoke_fault("after_preview_claim", context)
                with _GLOBAL_PREVIEW_SLOTS:
                    preview = self.capture_service.preview(
                        CollectionPreviewRequest(input_text=claim["input_text"]),
                        preview_id=claim["preview_id"],
                        allow_rendered_cover=False,
                    )
                self._invoke_fault("after_preview_persisted", context)
                settled = self.repository.publish_preview_success(
                    batch_id=batch_id,
                    batch_item_id=claim["batch_item_id"],
                    claim_token=claim["claim_token"],
                    preview_id=preview.preview_id,
                    now=self.clock(),
                )
                if not settled:
                    self.repository.publish_preview_interrupted(
                        batch_id=batch_id,
                        batch_item_id=claim["batch_item_id"],
                        claim_token=claim["claim_token"],
                        now=self.clock(),
                    )
            except SimulatedCollectionImportCrash:
                # Preserve the claim exactly as a crashed process would.  A new
                # service instance will reconcile the reserved preview locally.
                return
            except PipelineError as exc:
                try:
                    settled = self.repository.publish_preview_failure(
                        batch_id=batch_id,
                        batch_item_id=claim["batch_item_id"],
                        claim_token=claim["claim_token"],
                        error_code=exc.code,
                        now=self.clock(),
                    )
                except Exception:
                    settled = False
                if settled:
                    continue
                try:
                    self.repository.publish_preview_interrupted(
                        batch_id=batch_id,
                        batch_item_id=claim["batch_item_id"],
                        claim_token=claim["claim_token"],
                        now=self.clock(),
                    )
                except Exception:
                    pass
                return
            except Exception:
                # Live-process worker failures must not strand a durable claim.
                # The token CAS also prevents an obsolete worker from changing
                # a newer generation.  SimulatedCollectionImportCrash above is
                # the only test-only path that models a dead process exactly.
                try:
                    self.repository.publish_preview_interrupted(
                        batch_id=batch_id,
                        batch_item_id=claim["batch_item_id"],
                        claim_token=claim["claim_token"],
                        now=self.clock(),
                    )
                except Exception:
                    pass
                return

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                futures = tuple(self._futures)
            if not futures:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _, pending = wait(futures, timeout=remaining)
            if pending and time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)


class CollectionImportSaveCoordinator:
    """Run frozen collection saves one at a time in original position order."""

    def __init__(
        self,
        repository: CollectionImportRepository,
        collection_service: CollectionService,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        fault: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.repository = repository
        self.collection_service = collection_service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.fault = fault
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="collection-import-save"
        )
        self._lock = threading.RLock()
        self._futures: set[Future[None]] = set()
        self._closed = False

    def _invoke_fault(self, stage: str, context: dict[str, Any]) -> None:
        if self.fault is not None:
            self.fault(stage, context)

    def accept_batch(self, batch_id: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                future = self._executor.submit(self._drain_batch, batch_id)
            except RuntimeError:
                return False
            self._futures.add(future)
            future.add_done_callback(self._finished)
            return True

    def _finished(self, future: Future[None]) -> None:
        try:
            future.exception()
        except Exception:
            pass
        with self._lock:
            self._futures.discard(future)

    def _record_and_settle(
        self,
        context: dict[str, Any],
        *,
        result: str,
        collection_item_id: str | None,
        error_code: str | None,
    ) -> bool:
        if not self.repository.record_save_result(
            batch_id=context["batch_id"],
            batch_item_id=context["batch_item_id"],
            save_attempt_id=context["save_attempt_id"],
            claim_token=context["claim_token"],
            result=result,
            collection_item_id=collection_item_id,
            error_code=error_code,
            now=self.clock(),
        ):
            return False
        self._invoke_fault("after_save_result_observed", context)
        return self.repository.settle_observed_save(
            batch_id=context["batch_id"],
            batch_item_id=context["batch_item_id"],
            save_attempt_id=context["save_attempt_id"],
            claim_token=context["claim_token"],
            now=self.clock(),
        )

    def _process_claim(self, claim: dict[str, Any]) -> None:
        batch_id = claim["batch_id"]
        context = {
            "batch_id": batch_id,
            "batch_item_id": claim["batch_item_id"],
            "save_attempt_id": claim["save_attempt_id"],
            "position": claim["position"],
            "claim_token": claim["claim_token"],
        }
        self._invoke_fault("after_save_claim", context)
        key = collection_import_save_idempotency_key(
            batch_id,
            claim["batch_item_id"],
            claim["save_attempt_id"],
        )
        try:
            frozen = json.loads(claim["frozen_request_json"])
            request = CollectionItemCreateRequest.model_validate(frozen)
            prepared = prepare_collection_create(request, claim["preview"])
        except SimulatedCollectionImportCrash:
            raise
        except (
            json.JSONDecodeError,
            TypeError,
            UnicodeError,
            ValidationError,
            PipelineError,
        ):
            if self.repository.mark_save_call_started(
                batch_id=batch_id,
                batch_item_id=claim["batch_item_id"],
                save_attempt_id=claim["save_attempt_id"],
                claim_token=claim["claim_token"],
                now=self.clock(),
            ):
                self._record_and_settle(
                    context,
                    result="known_not_written",
                    collection_item_id=None,
                    error_code="BATCH_SAVE_ATTEMPT_INVALID",
                )
            return
        if (
            request.preview_id != claim["preview_id"]
            or prepared.request_hash != claim["collection_request_hash"]
            or hashlib.sha256(key.encode("ascii")).hexdigest()
            != claim["idempotency_key_hash"]
        ):
            if self.repository.mark_save_call_started(
                batch_id=batch_id,
                batch_item_id=claim["batch_item_id"],
                save_attempt_id=claim["save_attempt_id"],
                claim_token=claim["claim_token"],
                now=self.clock(),
            ):
                self._record_and_settle(
                    context,
                    result="known_not_written",
                    collection_item_id=None,
                    error_code="BATCH_SAVE_ATTEMPT_INVALID",
                )
            return

        if not self.repository.mark_save_call_started(
            batch_id=batch_id,
            batch_item_id=claim["batch_item_id"],
            save_attempt_id=claim["save_attempt_id"],
            claim_token=claim["claim_token"],
            now=self.clock(),
        ):
            return
        try:
            self._invoke_fault("after_save_call_started", context)
            with _GLOBAL_SAVE_SLOT:
                item = self.collection_service.create(request, key)
            self._invoke_fault("after_collection_save_returned", context)
        except SimulatedCollectionImportCrash:
            raise
        except PipelineError as exc:
            if exc.code == "COLLECTION_EXISTS":
                self._record_and_settle(
                    context,
                    result="exists",
                    collection_item_id=getattr(exc, "collection_item_id", None),
                    error_code=None,
                )
                return
            if exc.code in _KNOWN_NOT_WRITTEN_SAVE_ERRORS:
                self._record_and_settle(
                    context,
                    result="known_not_written",
                    collection_item_id=None,
                    error_code=exc.code,
                )
                return
            self._reconcile_or_mark_unknown(context, key)
            return
        except Exception:
            self._reconcile_or_mark_unknown(context, key)
            return

        self._record_and_settle(
            context,
            result="success",
            collection_item_id=item.id,
            error_code=None,
        )

    def _reconcile_or_mark_unknown(
        self, context: dict[str, Any], idempotency_key: str
    ) -> None:
        try:
            reconciled = self.repository.reconcile_save_after_exception(
                batch_id=context["batch_id"],
                batch_item_id=context["batch_item_id"],
                save_attempt_id=context["save_attempt_id"],
                claim_token=context["claim_token"],
                idempotency_key=idempotency_key,
                now=self.clock(),
            )
        except Exception:
            reconciled = None
        if reconciled is None:
            self.repository.mark_save_outcome_unknown(
                batch_id=context["batch_id"],
                batch_item_id=context["batch_item_id"],
                save_attempt_id=context["save_attempt_id"],
                claim_token=context["claim_token"],
                now=self.clock(),
            )

    def _drain_batch(self, batch_id: str) -> None:
        while True:
            try:
                claim = self.repository.claim_save(
                    batch_id=batch_id,
                    claim_token=self.id_factory(),
                    now=self.clock(),
                )
            except Exception:
                self._interrupt_unclaimed_queue(batch_id)
                return
            if claim is None:
                try:
                    pending = self.repository.has_pending_save_work(batch_id)
                except Exception:
                    self._interrupt_unclaimed_queue(batch_id)
                    return
                if pending:
                    _SAVE_CLAIM_RELEASED.wait(0.05)
                    _SAVE_CLAIM_RELEASED.clear()
                    continue
                return
            try:
                self._process_claim(claim)
            except SimulatedCollectionImportCrash:
                return
            except BaseException:
                recovered = False
                for _ in range(2):
                    try:
                        recovered = self.repository.settle_abandoned_save_claim(
                            batch_id=batch_id,
                            batch_item_id=claim["batch_item_id"],
                            save_attempt_id=claim["save_attempt_id"],
                            claim_token=claim["claim_token"],
                            now=self.clock(),
                        )
                        break
                    except Exception:
                        continue
                if not recovered:
                    try:
                        self.repository.mark_save_outcome_unknown(
                            batch_id=batch_id,
                            batch_item_id=claim["batch_item_id"],
                            save_attempt_id=claim["save_attempt_id"],
                            claim_token=claim["claim_token"],
                            now=self.clock(),
                        )
                    except Exception:
                        pass
                return
            finally:
                _SAVE_CLAIM_RELEASED.set()

    def _interrupt_unclaimed_queue(self, batch_id: str) -> None:
        try:
            self.repository.mark_save_dispatch_failed(batch_id, self.clock())
        except Exception:
            # If SQLite itself is unavailable there is no safe durable write to
            # make.  Leave the frozen queue untouched for startup/local
            # reconciliation; never call CollectionService speculatively.
            pass

    def wait_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                futures = tuple(self._futures)
            if not futures:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _, pending = wait(futures, timeout=remaining)
            if pending and time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)


class CollectionImportService:
    def __init__(
        self,
        repository: CollectionImportRepository,
        coordinator: CollectionImportPreviewCoordinator,
        save_coordinator: CollectionImportSaveCoordinator,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.coordinator = coordinator
        self.save_coordinator = save_coordinator
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def recover_startup(self) -> None:
        self.repository.reconcile_startup(self.clock())

    @staticmethod
    def _translate_command_error(exc: Exception) -> CollectionImportError:
        if isinstance(exc, CollectionImportNotFoundError):
            if exc.code == "BATCH_ITEM_NOT_FOUND":
                return CollectionImportError(
                    "BATCH_ITEM_NOT_FOUND", "批量导入项不存在。", 404
                )
            return CollectionImportError(
                "BATCH_NOT_FOUND", "批量导入不存在。", 404
            )
        if isinstance(exc, CollectionImportBatchRevisionConflictError):
            return CollectionImportError(
                "BATCH_REVISION_CONFLICT",
                "批次已被其他操作更新。",
                409,
                current_revision=exc.current_revision,
            )
        if isinstance(exc, CollectionImportItemRevisionConflictError):
            return CollectionImportError(
                "BATCH_ITEM_REVISION_CONFLICT",
                "批次项已被其他操作更新。",
                409,
                current_revision=exc.current_revision,
            )
        if isinstance(exc, CollectionImportAttemptLimitError):
            return CollectionImportError(
                "BATCH_ATTEMPT_LIMIT",
                "该批次项已达到重试次数上限。",
                409,
            )
        if isinstance(exc, CollectionImportReviewIncompleteError):
            return CollectionImportError(
                "BATCH_REVIEW_INCOMPLETE",
                "批次仍有未完成或无效的审核决定。",
                409,
            )
        if isinstance(exc, CollectionImportPreviewExpiringError):
            return CollectionImportError(
                "BATCH_PREVIEW_EXPIRING",
                "所选来源预览已过期或即将过期，请重新检查后再确认。",
                409,
            )
        if isinstance(exc, CollectionImportRequestInvalidError):
            return _reject_request()
        if isinstance(exc, CollectionImportStateConflictError):
            return CollectionImportError(
                "BATCH_STATE_CONFLICT",
                "当前批次或批次项状态不允许该操作。",
                409,
            )
        raise exc

    def create(
        self,
        *,
        items: list[dict[str, str]],
        key_hash: str,
        request_fingerprint: str,
    ) -> tuple[CollectionImportBatchSnapshot, int]:
        now = self.clock()
        try:
            snapshot, created, terminal = self.repository.create_or_replay(
                items=items,
                key_hash=key_hash,
                request_fingerprint=request_fingerprint,
                batch_id=self.id_factory(),
                batch_item_ids=[self.id_factory() for _ in items],
                now=now,
                expires_at=now + timedelta(days=7),
            )
        except CollectionImportIdempotencyReusedError as exc:
            raise CollectionImportError(
                "IDEMPOTENCY_KEY_REUSED",
                "该 Idempotency-Key 已用于不同的批量导入内容。",
                409,
            ) from exc
        except CollectionImportRetiredError as exc:
            raise CollectionImportError(
                "BATCH_RETIRED",
                "该批次已按保留策略清理。",
                410,
            ) from exc
        except CollectionImportActiveError as exc:
            raise CollectionImportError(
                "BATCH_ACTIVE",
                "已有一个未结束的批量导入。",
                409,
                batch_id=exc.batch_id,
            ) from exc

        if created and not self.coordinator.accept_batch(snapshot["batch_id"]):
            self.repository.mark_dispatch_failed(snapshot["batch_id"], self.clock())
            refreshed = self.repository.get_batch(
                snapshot["batch_id"], self.clock()
            )
            if refreshed is not None:
                snapshot = refreshed
        return CollectionImportBatchSnapshot.model_validate(snapshot), (
            200 if terminal else 202
        )

    def get_active(self) -> CollectionImportBatchSnapshot | None:
        snapshot = self.repository.get_active(self.clock())
        return (
            CollectionImportBatchSnapshot.model_validate(snapshot)
            if snapshot is not None
            else None
        )

    def get_batch(self, batch_id: str) -> CollectionImportBatchSnapshot:
        snapshot = self.repository.get_batch(batch_id, self.clock())
        if snapshot is None:
            raise CollectionImportError(
                "BATCH_NOT_FOUND", "批量导入不存在。", 404
            )
        return CollectionImportBatchSnapshot.model_validate(snapshot)

    def get_item(
        self, batch_id: str, batch_item_id: str
    ) -> CollectionImportBatchItemDetail:
        self.repository.expire_item_preview(
            batch_id=batch_id,
            batch_item_id=batch_item_id,
            now=self.clock(),
        )
        try:
            detail = self.repository.get_item(batch_id, batch_item_id)
        except CollectionImportNotFoundError as exc:
            raise CollectionImportError(
                "BATCH_NOT_FOUND", "批量导入不存在。", 404
            ) from exc
        if detail is None:
            raise CollectionImportError(
                "BATCH_ITEM_NOT_FOUND", "批量导入项不存在。", 404
            )
        return CollectionImportBatchItemDetail.model_validate(detail)

    def review_item(
        self,
        batch_id: str,
        batch_item_id: str,
        command: CollectionImportReviewCommand,
    ) -> CollectionImportBatchSnapshot:
        now = self.clock()
        payload = command.model_dump(mode="json")
        expected_batch_revision = payload.pop("expected_batch_revision")
        expected_item_revision = payload.pop("expected_item_revision")
        decision = payload.pop("decision")
        try:
            snapshot = self.repository.review_item(
                batch_id=batch_id,
                batch_item_id=batch_item_id,
                expected_batch_revision=expected_batch_revision,
                expected_item_revision=expected_item_revision,
                decision=decision,
                draft=payload,
                now=now,
            )
        except (
            CollectionImportNotFoundError,
            CollectionImportBatchRevisionConflictError,
            CollectionImportItemRevisionConflictError,
            CollectionImportAttemptLimitError,
            CollectionImportRequestInvalidError,
            CollectionImportStateConflictError,
        ) as exc:
            raise self._translate_command_error(exc) from exc
        return CollectionImportBatchSnapshot.model_validate(snapshot)

    def repreview_item(
        self,
        batch_id: str,
        batch_item_id: str,
        command: CollectionImportRepreviewCommand,
    ) -> CollectionImportBatchSnapshot:
        payload = command.model_dump(mode="json")
        replacement_provided = "input_text" in command.model_fields_set
        try:
            snapshot = self.repository.queue_repreview(
                batch_id=batch_id,
                batch_item_id=batch_item_id,
                expected_batch_revision=payload["expected_batch_revision"],
                expected_item_revision=payload["expected_item_revision"],
                replacement_provided=replacement_provided,
                replacement_input=payload["input_text"],
                item_character_limit=ITEM_CHARACTER_LIMIT,
                input_utf8_limit=INPUT_UTF8_LIMIT,
                now=self.clock(),
            )
        except (
            CollectionImportNotFoundError,
            CollectionImportBatchRevisionConflictError,
            CollectionImportItemRevisionConflictError,
            CollectionImportAttemptLimitError,
            CollectionImportRequestInvalidError,
            CollectionImportStateConflictError,
        ) as exc:
            raise self._translate_command_error(exc) from exc

        if not self.coordinator.accept_batch(batch_id):
            self.repository.mark_dispatch_failed(batch_id, self.clock())
            refreshed = self.repository.get_batch(batch_id, self.clock())
            if refreshed is not None:
                snapshot = refreshed
        return CollectionImportBatchSnapshot.model_validate(snapshot)

    def confirm_batch(
        self,
        batch_id: str,
        command: CollectionImportConfirmCommand,
    ) -> CollectionImportBatchSnapshot:
        def freeze_request(
            preview_id: str,
            draft: dict[str, Any],
            preview: dict[str, Any],
            batch_item_id: str,
            save_attempt_id: str,
        ) -> tuple[dict[str, Any], str, str]:
            request = CollectionItemCreateRequest.model_validate(
                {"preview_id": preview_id, **draft}
            )
            prepared = prepare_collection_create(request, preview)
            return (
                request.model_dump(
                    mode="json",
                    exclude={"user_author", "user_cover_asset_id"},
                ),
                prepared.request_hash,
                collection_import_save_idempotency_key_hash(
                    batch_id, batch_item_id, save_attempt_id
                ),
            )

        try:
            snapshot = self.repository.confirm_batch(
                batch_id=batch_id,
                expected_batch_revision=command.expected_batch_revision,
                freeze_request=freeze_request,
                save_attempt_id_factory=self.id_factory,
                now=self.clock(),
            )
        except (
            CollectionImportNotFoundError,
            CollectionImportBatchRevisionConflictError,
            CollectionImportAttemptLimitError,
            CollectionImportReviewIncompleteError,
            CollectionImportPreviewExpiringError,
            CollectionImportStateConflictError,
        ) as exc:
            raise self._translate_command_error(exc) from exc

        if not self.save_coordinator.accept_batch(batch_id):
            self.repository.mark_save_dispatch_failed(batch_id, self.clock())
            refreshed = self.repository.get_batch(batch_id, self.clock())
            if refreshed is not None:
                snapshot = refreshed
        return CollectionImportBatchSnapshot.model_validate(snapshot)

    def resume_batch(
        self,
        batch_id: str,
        command: CollectionImportResumeCommand,
    ) -> CollectionImportBatchSnapshot:
        try:
            snapshot, preview_wake, save_wake = self.repository.resume_batch(
                batch_id=batch_id,
                expected_batch_revision=command.expected_batch_revision,
                now=self.clock(),
            )
        except (
            CollectionImportNotFoundError,
            CollectionImportBatchRevisionConflictError,
            CollectionImportStateConflictError,
        ) as exc:
            raise self._translate_command_error(exc) from exc

        if preview_wake and not self.coordinator.accept_batch(batch_id):
            self.repository.mark_dispatch_failed(batch_id, self.clock())
        if save_wake and not self.save_coordinator.accept_batch(batch_id):
            self.repository.mark_save_dispatch_failed(batch_id, self.clock())
        if preview_wake or save_wake:
            refreshed = self.repository.get_batch(batch_id, self.clock())
            if refreshed is not None:
                snapshot = refreshed
        return CollectionImportBatchSnapshot.model_validate(snapshot)

    def cancel_batch(
        self,
        batch_id: str,
        command: CollectionImportCancelCommand,
    ) -> tuple[CollectionImportBatchSnapshot, int]:
        try:
            snapshot = self.repository.cancel_batch(
                batch_id=batch_id,
                expected_batch_revision=command.expected_batch_revision,
                now=self.clock(),
            )
        except (
            CollectionImportNotFoundError,
            CollectionImportBatchRevisionConflictError,
            CollectionImportStateConflictError,
        ) as exc:
            raise self._translate_command_error(exc) from exc
        return CollectionImportBatchSnapshot.model_validate(snapshot), (
            200 if snapshot["status"] == "cancelled" else 202
        )
