"""Isolated Windows multiprocessing rehearsal for RR1-CONCURRENCY1.

The public entry point accepts no paths or workload controls. Every database is
synthetic, lives below a fresh process-owned root, and is removed before the
function returns. Worker messages contain only stable categories and aggregate
booleans; exception text and synthetic content never leave a worker.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import platform
import shutil
import sqlite3
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from queue import Empty
from typing import Any, Iterable

from .domain.models import CollectionItemCreateRequest, CollectionItemUpdateRequest
from .repositories.collection_search import (
    normalize_search_text,
    refresh_collection_document,
)
from .repositories.sqlite import SQLiteRepository
from .services.collections import CollectionService
from .services.pipeline import PipelineError


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMP_PARENT = _PROJECT_ROOT / "var" / "wanan" / "temp"
_PROCESS_TIMEOUT_SECONDS = 30.0
_EVENT_TIMEOUT_SECONDS = 15.0
_TERMINATE_TIMEOUT_SECONDS = 5.0
_CRASH_EXIT_CODE = 73


class ConcurrencyRehearsalError(RuntimeError):
    """Stable internal failure whose payload never contains private diagnostics."""


def _require(condition: bool, error_code: str) -> None:
    if not condition:
        raise ConcurrencyRehearsalError(error_code)


@contextmanager
def _owned_temporary_directory():
    _TEMP_PARENT.mkdir(parents=True, exist_ok=True)
    parent = _TEMP_PARENT.resolve()
    root = (parent / f"rr1-concurrency-{uuid.uuid4().hex}").resolve()
    _require(root.parent == parent, "RR1_CONCURRENCY_TEMP_ROOT_INVALID")
    root.mkdir()
    try:
        yield root
    finally:
        if root.exists():
            _require(root.parent == parent, "RR1_CONCURRENCY_CLEANUP_SCOPE_INVALID")
            for attempt in range(3):
                try:
                    shutil.rmtree(root)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            _require(not root.exists(), "RR1_CONCURRENCY_CLEANUP_FAILED")


def _owned_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    _require(
        resolved_path != resolved_root and resolved_path.is_relative_to(resolved_root),
        "RR1_CONCURRENCY_PATH_OUTSIDE_OWNED_ROOT",
    )
    return resolved_path


def _preview_id(slot: int) -> str:
    return f"rr1-concurrency-preview-{slot}"


def _source_marker(slot: int) -> str:
    return f"RR1SourceSignal{slot}"


def _create_marker(slot: int, variant: int) -> str:
    return f"RR1CreateSignal{slot}Variant{variant}"


def _update_marker(slot: int, variant: int) -> str:
    return f"RR1UpdateSignal{slot}Variant{variant}"


def _synthetic_preview(slot: int) -> dict[str, Any]:
    marker = _source_marker(slot)
    source_url = f"https://example.invalid/rr1-concurrency/{slot}"
    return {
        "preview_id": _preview_id(slot),
        "original_input": f"RR1 synthetic share {slot}",
        "source_url": source_url,
        "canonical_url": source_url,
        "identity_url": source_url,
        "source_kind": "webpage",
        "platform": "web",
        "metadata_status": "generic",
        "metadata": {
            "title": {
                "value": marker,
                "source": "open_graph",
                "fetched_at": "2026-08-30T00:00:00Z",
            },
            "author": {
                "value": "RR1 synthetic author",
                "source": "open_graph",
                "fetched_at": "2026-08-30T00:00:00Z",
            },
            "cover_url": {
                "value": "",
                "source": "none",
                "fetched_at": "2026-08-30T00:00:00Z",
            },
            "source_copy": {
                "value": "RR1 synthetic metadata",
                "source": "page_description",
                "fetched_at": "2026-08-30T00:00:00Z",
            },
            "platform_tags": [
                {"value": "rr1-synthetic", "source": "platform_public"}
            ],
            "warnings": [],
        },
        "organization_suggestion": {
            "primary_category": "readiness",
            "secondary_category": "sqlite",
            "tags": ["rr1-synthetic"],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "generated",
        },
        "created_at": "2026-08-30T00:00:00+00:00",
        "expires_at": "2099-08-30T00:00:00+00:00",
    }


def _create_request_data(slot: int, variant: int = 0) -> dict[str, Any]:
    marker = _create_marker(slot, variant)
    return {
        "preview_id": _preview_id(slot),
        "user_title": marker,
        "organization_confirmation": {
            "primary_category": f"primary-{slot}-{variant}",
            "secondary_category": f"secondary-{slot}-{variant}",
            "organization_tags": [f"organized-{slot}-{variant}"],
        },
        "personal_tags": [f"personal-{slot}-{variant}"],
        "inspiration": {
            "content": f"{marker} inspiration",
            "input_mode": "text",
            "transcription_status": "not_applicable",
        },
    }


def _update_request_data(
    slot: int,
    variant: int,
    *,
    expected_revision: int = 1,
) -> dict[str, Any]:
    marker = _update_marker(slot, variant)
    return {
        "expected_revision": expected_revision,
        "user_title": marker,
        "organization_confirmation": {
            "primary_category": f"updated-primary-{slot}-{variant}",
            "secondary_category": f"updated-secondary-{slot}-{variant}",
            "organization_tags": [f"updated-organized-{slot}-{variant}"],
        },
        "personal_tags": [f"updated-personal-{slot}-{variant}"],
        "inspiration": {
            "content": f"{marker} inspiration",
            "input_mode": "text",
            "transcription_status": "not_applicable",
        },
    }


def _editable_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    inspiration = item.get("inspiration")
    return {
        "user_title": item.get("user_title"),
        "organization_confirmation": item.get("organization_confirmation"),
        "personal_tags": list(item.get("personal_tags") or []),
        "inspiration": None
        if inspiration is None
        else {
            "content": inspiration["content"],
            "input_mode": inspiration["input_mode"],
            "transcription_status": inspiration["transcription_status"],
        },
    }


def _expected_create_snapshot(slot: int, variant: int) -> dict[str, Any]:
    request = CollectionItemCreateRequest.model_validate(
        _create_request_data(slot, variant)
    ).model_dump(mode="json")
    return {
        "user_title": request["user_title"],
        "organization_confirmation": request["organization_confirmation"],
        "personal_tags": request["personal_tags"],
        "inspiration": request["inspiration"],
    }


def _expected_update_snapshot(slot: int, variant: int) -> dict[str, Any]:
    request = CollectionItemUpdateRequest.model_validate(
        _update_request_data(slot, variant)
    ).model_dump(mode="json")
    request.pop("expected_revision")
    # CQ3 fields are intentionally outside the frozen RR1 concurrency matrix.
    request.pop("user_author")
    request.pop("user_cover_asset_id")
    return request


def _record_matches_item(
    repository: SQLiteRepository,
    idempotency_key: str,
    item_id: str,
) -> bool:
    with repository._connect() as database:
        row = database.execute(
            """SELECT collection_item_id, response_json
               FROM collection_idempotency_keys WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
    if row is None or row["collection_item_id"] != item_id:
        return False
    try:
        response = json.loads(row["response_json"])
    except (TypeError, ValueError):
        return False
    return response.get("id") == item_id


def _existing_repository(database_path: str | Path) -> SQLiteRepository:
    """Open the already-migrated synthetic DB without startup index repair."""

    repository = SQLiteRepository.__new__(SQLiteRepository)
    repository.database_path = Path(database_path)
    repository._migration_fault = None
    return repository


def _create_worker(
    database_path: str,
    slot: int,
    idempotency_key: str,
    variant: int,
    start_barrier: Any,
    result_queue: Any,
) -> None:
    try:
        start_barrier.wait(timeout=_EVENT_TIMEOUT_SECONDS)
        repository = _existing_repository(database_path)
        item = CollectionService(repository).create(
            CollectionItemCreateRequest.model_validate(
                _create_request_data(slot, variant)
            ),
            idempotency_key,
        )
        result_queue.put(
            {
                "category": "success",
                "variant": variant,
                "record_match": _record_matches_item(
                    repository, idempotency_key, item.id
                ),
            }
        )
    except PipelineError as exc:
        result_queue.put(
            {
                "category": (
                    "key_reused"
                    if exc.code == "IDEMPOTENCY_KEY_REUSED"
                    else "business_failure"
                ),
                "variant": variant,
                "record_match": False,
            }
        )
    except BaseException:
        result_queue.put(
            {"category": "worker_failure", "variant": variant, "record_match": False}
        )


def _update_worker(
    database_path: str,
    item_id: str,
    slot: int,
    variant: int,
    expected_revision: int,
    start_barrier: Any,
    result_queue: Any,
) -> None:
    try:
        start_barrier.wait(timeout=_EVENT_TIMEOUT_SECONDS)
        repository = _existing_repository(database_path)
        item = CollectionService(repository).update(
            item_id,
            CollectionItemUpdateRequest.model_validate(
                _update_request_data(
                    slot,
                    variant,
                    expected_revision=expected_revision,
                )
            ),
        )
        result_queue.put(
            {
                "category": "success",
                "variant": variant,
                "revision_two": item.revision == expected_revision + 1,
            }
        )
    except PipelineError as exc:
        result_queue.put(
            {
                "category": (
                    "revision_conflict"
                    if exc.code == "COLLECTION_REVISION_CONFLICT"
                    else "business_failure"
                ),
                "variant": variant,
                "revision_two": getattr(exc, "current_revision", 0)
                == expected_revision + 1,
            }
        )
    except BaseException:
        result_queue.put(
            {"category": "worker_failure", "variant": variant, "revision_two": False}
        )


def _lock_holder_worker(
    database_path: str,
    acquired_event: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    try:
        with closing(sqlite3.connect(database_path, timeout=5.0)) as database:
            database.execute("BEGIN IMMEDIATE")
            acquired_event.set()
            released = release_event.wait(timeout=_EVENT_TIMEOUT_SECONDS)
            if not released:
                database.rollback()
                result_queue.put({"category": "holder_timeout"})
                return
            database.commit()
        result_queue.put({"category": "released"})
    except BaseException:
        result_queue.put({"category": "worker_failure"})


class _ZeroWaitSQLiteRepository(SQLiteRepository):
    """Rehearsal-only connection policy for a proven pre-write busy failure."""

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=0.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class _ObservedBeginConnection:
    """Proxy the real write connection and observe its exact BEGIN call."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        repository: "_ObservedWaitSQLiteRepository",
    ) -> None:
        self._connection = connection
        self._repository = repository

    def execute(
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> sqlite3.Cursor:
        if " ".join(statement.strip().upper().split()) == "BEGIN IMMEDIATE":
            started = time.monotonic()
            self._repository._begin_attempted_event.set()
            cursor = self._connection.execute(statement, parameters)
            self._repository._begin_wait_seconds = time.monotonic() - started
            self._repository._begin_acquired_event.set()
            return cursor
        return self._connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ObservedWaitSQLiteRepository(SQLiteRepository):
    """Use the production connection policy while observing the real create."""

    @contextmanager
    def _connect(self):
        with super()._connect() as database:
            self._normal_budget = self._normal_budget and (
                int(database.execute("PRAGMA busy_timeout").fetchone()[0])
                == 5_000
            )
            yield _ObservedBeginConnection(database, self)


def _zero_wait_busy_worker(
    database_path: str,
    slot: int,
    idempotency_key: str,
    result_queue: Any,
) -> None:
    try:
        request = CollectionItemCreateRequest.model_validate(
            _create_request_data(slot)
        )
        repository = _ZeroWaitSQLiteRepository.__new__(
            _ZeroWaitSQLiteRepository
        )
        repository.database_path = Path(database_path)
        repository._migration_fault = None
        try:
            CollectionService(repository).create(request, idempotency_key)
        except sqlite3.OperationalError as exc:
            error_code = getattr(exc, "sqlite_errorcode", None)
            if (
                not isinstance(error_code, int)
                or error_code & 0xFF != sqlite3.SQLITE_BUSY
            ):
                raise
            with closing(sqlite3.connect(database_path, timeout=0.0)) as database:
                record_absent = (
                    int(
                        database.execute(
                            """SELECT COUNT(*) FROM collection_idempotency_keys
                               WHERE idempotency_key=?""",
                            (idempotency_key,),
                        ).fetchone()[0]
                    )
                    == 0
                    and int(
                        database.execute(
                            """SELECT COUNT(*) FROM library_items
                               WHERE identity_url=?""",
                            (_synthetic_preview(slot)["identity_url"],),
                        ).fetchone()[0]
                    )
                    == 0
                )
            result_queue.put(
                {
                    "category": "busy_before_write",
                    "record_absent": record_absent,
                    "intent_valid": request.preview_id == _preview_id(slot),
                }
            )
            return
        result_queue.put(
            {
                "category": "lock_not_observed",
                "record_absent": False,
                "intent_valid": True,
            }
        )
    except BaseException:
        result_queue.put(
            {
                "category": "worker_failure",
                "record_absent": False,
                "intent_valid": False,
            }
        )


def _waiting_create_worker(
    database_path: str,
    slot: int,
    idempotency_key: str,
    begin_attempted_event: Any,
    lock_acquired_event: Any,
    result_queue: Any,
) -> None:
    try:
        repository = _ObservedWaitSQLiteRepository.__new__(
            _ObservedWaitSQLiteRepository
        )
        repository.database_path = Path(database_path)
        repository._migration_fault = None
        repository._begin_attempted_event = begin_attempted_event
        repository._begin_acquired_event = lock_acquired_event
        repository._begin_wait_seconds = None
        repository._normal_budget = True
        item = CollectionService(repository).create(
            CollectionItemCreateRequest.model_validate(
                _create_request_data(slot)
            ),
            idempotency_key,
        )
        result_queue.put(
            {
                "category": "success",
                "record_match": _record_matches_item(
                    repository, idempotency_key, item.id
                ),
                "wait_observed": (
                    repository._begin_wait_seconds is not None
                    and repository._begin_wait_seconds >= 0.20
                    and repository._normal_budget
                ),
            }
        )
    except BaseException:
        result_queue.put(
            {
                "category": "worker_failure",
                "record_match": False,
                "wait_observed": False,
            }
        )


def _crash_transaction_worker(
    database_path: str,
    item_id: str,
    slot: int,
    changed_event: Any,
) -> None:
    try:
        repository = _existing_repository(database_path)
        desired = CollectionItemUpdateRequest.model_validate(
            _update_request_data(slot, 99)
        ).model_dump(mode="json")
        desired.pop("expected_revision")
        with repository._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            current = repository._collection_item_on_connection(database, item_id)
            if current is None or current["revision"] != 1:
                os._exit(74)
            repository._update_collection_root(
                database,
                item_id,
                1,
                desired["user_title"],
                current["user_author"],
                "2026-08-30T00:00:01+00:00",
            )
            repository._replace_collection_confirmation(
                database,
                item_id,
                current["organization_confirmation"],
                desired["organization_confirmation"],
            )
            repository._replace_collection_personal_tags(
                database,
                item_id,
                current["personal_tags"],
                desired["personal_tags"],
                "2026-08-30T00:00:01+00:00",
            )
            repository._replace_collection_inspiration(
                database,
                item_id,
                current["inspiration"],
                desired["inspiration"],
                "2026-08-30T00:00:01+00:00",
            )
            refresh_collection_document(database, item_id)
            changed_event.set()
            os._exit(_CRASH_EXIT_CODE)
    except BaseException:
        # Multiprocessing prints uncaught child exceptions to inherited stderr.
        # A fixed hard exit keeps the CLI's single redacted-report contract.
        os._exit(74)


def _recovery_check_worker(
    database_path: str,
    item_id: str,
    slot: int,
    result_queue: Any,
) -> None:
    try:
        repository = _existing_repository(database_path)
        item = repository.get_collection_item(item_id)
        with repository._connect() as database:
            row = database.execute(
                """SELECT indexed_text FROM collection_search_documents
                   WHERE collection_item_id=?""",
                (item_id,),
            ).fetchone()
            integrity = database.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
            dirty_count = int(
                database.execute(
                    """SELECT COUNT(*) FROM collection_search_dirty
                       WHERE collection_item_id=?""",
                    (item_id,),
                ).fetchone()[0]
            )
        text = "" if row is None else str(row["indexed_text"])
        service = CollectionService(repository)
        baseline_marker = _create_marker(slot, 0)
        ghost_marker = _update_marker(slot, 99)
        result_queue.put(
            {
                "category": "recovered",
                "baseline_graph": item is not None
                and item["revision"] == 1
                and _editable_snapshot(item) == _expected_create_snapshot(slot, 0),
                "ghost_absent": _update_marker(slot, 99).casefold()
                not in text.casefold(),
                "baseline_search": _search_and_facets_match(
                    service,
                    item_id=item_id,
                    marker=baseline_marker,
                    primary_category=f"primary-{slot}-0",
                    personal_tag=f"personal-{slot}-0",
                ),
                "baseline_fts": _raw_fts_item_ids(
                    repository, baseline_marker
                )
                == [item_id],
                "ghost_fts_absent": not _raw_fts_item_ids(
                    repository, ghost_marker
                ),
                "dirty_absent": dirty_count == 0,
                "integrity": [tuple(row) for row in integrity] == [("ok",)],
                "foreign_keys": not foreign_keys,
            }
        )
    except BaseException:
        result_queue.put(
            {
                "category": "worker_failure",
                "baseline_graph": False,
                "ghost_absent": False,
                "baseline_search": False,
                "baseline_fts": False,
                "ghost_fts_absent": False,
                "dirty_absent": False,
                "integrity": False,
                "foreign_keys": False,
            }
        )


def _start_process(
    context: Any,
    registry: list[Any],
    target: Any,
    arguments: tuple[Any, ...],
) -> Any:
    process = context.Process(target=target, args=arguments)
    registry.append(process)
    process.start()
    return process


def _join_processes(
    processes: Iterable[Any],
    *,
    expected_exit_codes: set[int] | None = None,
) -> None:
    process_list = list(processes)
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    for process in process_list:
        process.join(timeout=max(0.0, deadline - time.monotonic()))
    for process in process_list:
        if process.is_alive():
            process.terminate()
            process.join(timeout=_TERMINATE_TIMEOUT_SECONDS)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=_TERMINATE_TIMEOUT_SECONDS)
        _require(not process.is_alive(), "RR1_CONCURRENCY_PROCESS_NOT_BOUNDED")
    allowed = expected_exit_codes or {0}
    _require(
        all(process.exitcode in allowed for process in process_list),
        "RR1_CONCURRENCY_WORKER_EXIT_INVALID",
    )


def _read_results(result_queue: Any, expected: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while len(results) < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConcurrencyRehearsalError(
                "RR1_CONCURRENCY_WORKER_RESULT_TIMEOUT"
            )
        try:
            results.append(result_queue.get(timeout=remaining))
        except Empty as exc:
            raise ConcurrencyRehearsalError(
                "RR1_CONCURRENCY_WORKER_RESULT_TIMEOUT"
            ) from exc
    return results


def _close_queue(result_queue: Any) -> None:
    try:
        result_queue.close()
    except (OSError, ValueError):
        pass
    try:
        result_queue.cancel_join_thread()
    except (OSError, ValueError):
        pass


def _close_owned_queues(queues: Iterable[Any]) -> None:
    for result_queue in queues:
        try:
            _close_queue(result_queue)
        except BaseException:
            # One broken queue must not prevent later owned queues from closing.
            pass


def _run_barrier_group(
    context: Any,
    registry: list[Any],
    target: Any,
    argument_rows: list[tuple[Any, ...]],
) -> list[dict[str, Any]]:
    result_queue = context.Queue()
    processes: list[Any] = []
    registry_start = len(registry)
    try:
        barrier = context.Barrier(len(argument_rows))
        for arguments in argument_rows:
            processes.append(
                _start_process(
                    context,
                    registry,
                    target,
                    (*arguments, barrier, result_queue),
                )
            )
        results = _read_results(result_queue, len(processes))
        _join_processes(processes)
        return results
    except BaseException:
        # Stop every process registered by this group before closing the queue it
        # may still be using. The slice is fixed and this cleanup is bounded.
        _stop_owned_processes(registry[registry_start:])
        raise
    finally:
        _close_queue(result_queue)


def _stop_owned_processes(registry: Iterable[Any]) -> None:
    process_list = [
        process
        for process in registry
        if not bool(getattr(process, "_closed", False))
    ]

    def is_alive(process: Any) -> bool:
        try:
            return bool(process.is_alive())
        except BaseException:
            # Unknown lifecycle state is unsafe, so the final check fails closed.
            return True

    def join_until(processes: Iterable[Any], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for process in processes:
            try:
                process.join(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException:
                # Continue cleanup for every other owned process.
                pass

    if not process_list:
        return

    join_until(process_list, _TERMINATE_TIMEOUT_SECONDS)
    for process in process_list:
        if is_alive(process):
            try:
                process.terminate()
            except BaseException:
                pass
    join_until(process_list, _TERMINATE_TIMEOUT_SECONDS)
    for process in process_list:
        if is_alive(process) and hasattr(process, "kill"):
            try:
                process.kill()
            except BaseException:
                pass
    join_until(process_list, _TERMINATE_TIMEOUT_SECONDS)

    survivors = [process for process in process_list if is_alive(process)]
    for process in process_list:
        if process not in survivors and hasattr(process, "close"):
            try:
                process.close()
            except BaseException:
                pass
    if survivors:
        raise ConcurrencyRehearsalError(
            "RR1_CONCURRENCY_PROCESS_CLEANUP_FAILED"
        )


def _create_parent_item(
    repository: SQLiteRepository,
    slot: int,
    idempotency_key: str,
) -> str:
    item = CollectionService(repository).create(
        CollectionItemCreateRequest.model_validate(_create_request_data(slot)),
        idempotency_key,
    )
    return item.id


def _item_id_for_key(repository: SQLiteRepository, idempotency_key: str) -> str:
    with repository._connect() as database:
        row = database.execute(
            """SELECT collection_item_id FROM collection_idempotency_keys
               WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
    _require(row is not None, "RR1_CONCURRENCY_IDEMPOTENCY_RECORD_MISSING")
    return str(row["collection_item_id"])


def _database_snapshot(repository: SQLiteRepository) -> dict[str, Any]:
    with repository._connect() as database:
        integrity = database.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            "collections": int(
                database.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]
            ),
            "idempotency_records": int(
                database.execute(
                    "SELECT COUNT(*) FROM collection_idempotency_keys"
                ).fetchone()[0]
            ),
            "search_documents": int(
                database.execute(
                    "SELECT COUNT(*) FROM collection_search_documents"
                ).fetchone()[0]
            ),
            "search_rows": int(
                database.execute(
                    "SELECT COUNT(*) FROM collection_search_fts"
                ).fetchone()[0]
            ),
            "dirty_documents": int(
                database.execute(
                    "SELECT COUNT(*) FROM collection_search_dirty"
                ).fetchone()[0]
            ),
        }
    return {
        "integrity": [tuple(row) for row in integrity] == [("ok",)],
        "foreign_keys": not foreign_keys,
        "counts": counts,
    }


def _document_text(repository: SQLiteRepository, item_id: str) -> str:
    with repository._connect() as database:
        row = database.execute(
            """SELECT indexed_text FROM collection_search_documents
               WHERE collection_item_id=?""",
            (item_id,),
        ).fetchone()
    return "" if row is None else str(row["indexed_text"])


def _raw_fts_item_ids(
    repository: SQLiteRepository, marker: str
) -> list[str]:
    normalized = normalize_search_text(marker)
    phrase = '"' + normalized.replace('"', '""') + '"'
    with repository._connect() as database:
        rows = database.execute(
            """SELECT collection_search_fts.rowid AS fts_rowid,
                      d.collection_item_id
               FROM collection_search_fts
               LEFT JOIN collection_search_documents d
                 ON d.document_id=collection_search_fts.rowid
               WHERE collection_search_fts MATCH ?
               ORDER BY collection_search_fts.rowid""",
            (phrase,),
        ).fetchall()
    return [
        str(row["collection_item_id"])
        if row["collection_item_id"] is not None
        else f"orphan:{row['fts_rowid']}"
        for row in rows
    ]


def _search_and_facets_match(
    service: CollectionService,
    *,
    item_id: str,
    marker: str,
    primary_category: str,
    personal_tag: str,
) -> bool:
    page = service.search(query=marker, limit=100)
    return (
        page.total == 1
        and [item.id for item in page.items] == [item_id]
        and any(
            facet.primary_category == primary_category and facet.count == 1
            for facet in page.facets.categories
        )
        and any(
            facet.name == personal_tag
            and facet.source == "personal"
            and facet.count == 1
            for facet in page.facets.tags
        )
    )


def _assert_categories(
    results: list[dict[str, Any]],
    expected: dict[str, int],
    error_code: str,
) -> None:
    actual: dict[str, int] = {}
    for result in results:
        category = str(result.get("category", "missing"))
        actual[category] = actual.get(category, 0) + 1
    _require(actual == expected, error_code)


def run_sqlite_concurrency_rehearsal() -> dict[str, Any]:
    """Run the approved path-free RR1-CONCURRENCY1 Windows rehearsal."""

    if platform.system() != "Windows":
        raise ConcurrencyRehearsalError(
            "RR1_CONCURRENCY_WINDOWS_RUNTIME_REQUIRED"
        )

    context = multiprocessing.get_context("spawn")
    registry: list[Any] = []
    owned_queues: list[Any] = []
    temporary_path: Path | None = None
    report: dict[str, Any]

    try:
        with ExitStack() as stack:
            root = stack.enter_context(_owned_temporary_directory())
            # ExitStack is LIFO: stop children, close their queues, then remove
            # the process-owned temporary root.
            stack.callback(_close_owned_queues, owned_queues)
            stack.callback(_stop_owned_processes, registry)
            temporary_path = root
            database_path = _owned_path(root / "synthetic.db", root)
            repository = SQLiteRepository(database_path)

            slots = [100, 101, 102, 103, 200, 300, 400, 500, 501, 600]
            for slot in slots:
                repository.create_collection_preview(_synthetic_preview(slot))

            distinct_keys = [f"rr1-distinct-{slot}" for slot in slots[:4]]
            distinct_results = _run_barrier_group(
                context,
                registry,
                _create_worker,
                [
                    (str(database_path), slot, key, 0)
                    for slot, key in zip(slots[:4], distinct_keys, strict=True)
                ],
            )
            _assert_categories(
                distinct_results,
                {"success": 4},
                "RR1_CONCURRENCY_DISTINCT_WRITES_FAILED",
            )
            _require(
                all(result["record_match"] for result in distinct_results),
                "RR1_CONCURRENCY_DISTINCT_IDEMPOTENCY_FAILED",
            )

            replay_key = "rr1-same-key-replay"
            replay_results = _run_barrier_group(
                context,
                registry,
                _create_worker,
                [(str(database_path), 200, replay_key, 0)] * 3,
            )
            _assert_categories(
                replay_results,
                {"success": 3},
                "RR1_CONCURRENCY_REPLAY_FAILED",
            )
            _require(
                all(result["record_match"] for result in replay_results),
                "RR1_CONCURRENCY_REPLAY_IDENTITY_FAILED",
            )

            reused_key = "rr1-same-key-reused"
            reused_results = _run_barrier_group(
                context,
                registry,
                _create_worker,
                [
                    (str(database_path), 300, reused_key, 1),
                    (str(database_path), 300, reused_key, 2),
                ],
            )
            _assert_categories(
                reused_results,
                {"success": 1, "key_reused": 1},
                "RR1_CONCURRENCY_KEY_REUSE_FAILED",
            )
            reused_winner = next(
                int(result["variant"])
                for result in reused_results
                if result["category"] == "success"
            )
            reused_loser = 1 if reused_winner == 2 else 2

            cas_key = "rr1-cas-base"
            cas_item_id = _create_parent_item(repository, 400, cas_key)
            cas_results = _run_barrier_group(
                context,
                registry,
                _update_worker,
                [
                    (str(database_path), cas_item_id, 400, 1, 1),
                    (str(database_path), cas_item_id, 400, 2, 1),
                ],
            )
            _assert_categories(
                cas_results,
                {"success": 1, "revision_conflict": 1},
                "RR1_CONCURRENCY_CAS_FAILED",
            )
            _require(
                all(result["revision_two"] for result in cas_results),
                "RR1_CONCURRENCY_CAS_REVISION_FAILED",
            )
            cas_winner = next(
                int(result["variant"])
                for result in cas_results
                if result["category"] == "success"
            )
            cas_loser = 1 if cas_winner == 2 else 2

            holder_queue = context.Queue()
            owned_queues.append(holder_queue)
            acquired_event = context.Event()
            release_event = context.Event()
            holder = _start_process(
                context,
                registry,
                _lock_holder_worker,
                (
                    str(database_path),
                    acquired_event,
                    release_event,
                    holder_queue,
                ),
            )
            _require(
                acquired_event.wait(timeout=_EVENT_TIMEOUT_SECONDS),
                "RR1_CONCURRENCY_HOLDER_NOT_ACQUIRED",
            )

            retry_slot = 501
            retry_key = "rr1-busy-explicit-retry"
            zero_queue = context.Queue()
            owned_queues.append(zero_queue)
            zero = _start_process(
                context,
                registry,
                _zero_wait_busy_worker,
                (str(database_path), retry_slot, retry_key, zero_queue),
            )
            zero_results = _read_results(zero_queue, 1)
            _join_processes([zero])
            _close_queue(zero_queue)
            _assert_categories(
                zero_results,
                {"busy_before_write": 1},
                "RR1_CONCURRENCY_PREWRITE_BUSY_NOT_OBSERVED",
            )
            _require(
                zero_results[0]["record_absent"]
                and zero_results[0]["intent_valid"],
                "RR1_CONCURRENCY_PREWRITE_BUSY_NOT_CLEAN",
            )

            waiting_queue = context.Queue()
            owned_queues.append(waiting_queue)
            waiting_begin_attempted = context.Event()
            waiting_acquired = context.Event()
            waiting = _start_process(
                context,
                registry,
                _waiting_create_worker,
                (
                    str(database_path),
                    500,
                    "rr1-busy-wait",
                    waiting_begin_attempted,
                    waiting_acquired,
                    waiting_queue,
                ),
            )
            _require(
                waiting_begin_attempted.wait(timeout=_EVENT_TIMEOUT_SECONDS),
                "RR1_CONCURRENCY_WAITER_BEGIN_NOT_ATTEMPTED",
            )
            time.sleep(0.50)
            _require(
                not waiting_acquired.is_set(),
                "RR1_CONCURRENCY_WAITER_BYPASSED_LOCK",
            )
            release_event.set()
            _require(
                waiting_acquired.wait(timeout=_EVENT_TIMEOUT_SECONDS),
                "RR1_CONCURRENCY_WAITER_DID_NOT_ACQUIRE",
            )
            holder_results = _read_results(holder_queue, 1)
            waiting_results = _read_results(waiting_queue, 1)
            _join_processes([holder, waiting])
            _close_queue(holder_queue)
            _close_queue(waiting_queue)
            _assert_categories(
                holder_results,
                {"released": 1},
                "RR1_CONCURRENCY_HOLDER_RELEASE_FAILED",
            )
            _assert_categories(
                waiting_results,
                {"success": 1},
                "RR1_CONCURRENCY_BOUNDED_WAIT_FAILED",
            )
            _require(
                waiting_results[0]["record_match"],
                "RR1_CONCURRENCY_BOUNDED_WAIT_IDEMPOTENCY_FAILED",
            )
            _require(
                waiting_results[0]["wait_observed"],
                "RR1_CONCURRENCY_BOUNDED_WAIT_NOT_OBSERVED",
            )

            retry_results = _run_barrier_group(
                context,
                registry,
                _create_worker,
                [
                    (
                        str(database_path),
                        retry_slot,
                        retry_key,
                        0,
                    )
                ],
            )
            _assert_categories(
                retry_results,
                {"success": 1},
                "RR1_CONCURRENCY_EXPLICIT_RETRY_FAILED",
            )

            crash_key = "rr1-crash-base"
            crash_item_id = _create_parent_item(repository, 600, crash_key)
            changed_event = context.Event()
            crash_process = _start_process(
                context,
                registry,
                _crash_transaction_worker,
                (str(database_path), crash_item_id, 600, changed_event),
            )
            _require(
                changed_event.wait(timeout=_EVENT_TIMEOUT_SECONDS),
                "RR1_CONCURRENCY_CRASH_MUTATION_NOT_REACHED",
            )
            _join_processes(
                [crash_process], expected_exit_codes={_CRASH_EXIT_CODE}
            )

            recovery_queue = context.Queue()
            owned_queues.append(recovery_queue)
            recovery_process = _start_process(
                context,
                registry,
                _recovery_check_worker,
                (str(database_path), crash_item_id, 600, recovery_queue),
            )
            recovery_results = _read_results(recovery_queue, 1)
            _join_processes([recovery_process])
            _close_queue(recovery_queue)
            _assert_categories(
                recovery_results,
                {"recovered": 1},
                "RR1_CONCURRENCY_CRASH_RECOVERY_FAILED",
            )
            recovery = recovery_results[0]
            _require(
                all(
                    recovery[name]
                    for name in (
                        "baseline_graph",
                        "ghost_absent",
                        "baseline_search",
                        "baseline_fts",
                        "ghost_fts_absent",
                        "dirty_absent",
                        "integrity",
                        "foreign_keys",
                    )
                ),
                "RR1_CONCURRENCY_CRASH_ROLLBACK_FAILED",
            )

            crash_retry_results = _run_barrier_group(
                context,
                registry,
                _update_worker,
                [(str(database_path), crash_item_id, 600, 100, 1)],
            )
            _assert_categories(
                crash_retry_results,
                {"success": 1},
                "RR1_CONCURRENCY_CRASH_RETRY_FAILED",
            )

            final_repository = _existing_repository(database_path)
            final_service = CollectionService(final_repository)
            expected_states: list[tuple[str, int, int, bool, int]] = [
                (key, slot, 0, False, 1)
                for key, slot in zip(distinct_keys, slots[:4], strict=True)
            ]
            expected_states.extend(
                [
                    (replay_key, 200, 0, False, 1),
                    (reused_key, 300, reused_winner, False, 1),
                    (cas_key, 400, cas_winner, True, 2),
                    ("rr1-busy-wait", 500, 0, False, 1),
                    (retry_key, retry_slot, 0, False, 1),
                    (crash_key, 600, 100, True, 2),
                ]
            )

            winner_graph_atomic = True
            winner_search_atomic = True
            idempotency_records_match = True
            expected_item_ids: list[str] = []
            item_ids_by_key: dict[str, str] = {}
            for key, slot, variant, is_update, revision in expected_states:
                item_id = _item_id_for_key(final_repository, key)
                expected_item_ids.append(item_id)
                item_ids_by_key[key] = item_id
                item = final_repository.get_collection_item(item_id)
                expected_snapshot = (
                    _expected_update_snapshot(slot, variant)
                    if is_update
                    else _expected_create_snapshot(slot, variant)
                )
                marker = (
                    _update_marker(slot, variant)
                    if is_update
                    else _create_marker(slot, variant)
                )
                primary_category = (
                    f"updated-primary-{slot}-{variant}"
                    if is_update
                    else f"primary-{slot}-{variant}"
                )
                personal_tag = (
                    f"updated-personal-{slot}-{variant}"
                    if is_update
                    else f"personal-{slot}-{variant}"
                )
                graph_matches = (
                    item is not None
                    and item["revision"] == revision
                    and _editable_snapshot(item) == expected_snapshot
                )
                document_matches = (
                    marker.casefold()
                    in _document_text(final_repository, item_id).casefold()
                )
                fts_matches = _raw_fts_item_ids(final_repository, marker) == [
                    item_id
                ]
                facets_match = _search_and_facets_match(
                    final_service,
                    item_id=item_id,
                    marker=marker,
                    primary_category=primary_category,
                    personal_tag=personal_tag,
                )
                winner_graph_atomic = winner_graph_atomic and graph_matches
                idempotency_records_match = (
                    idempotency_records_match
                    and _record_matches_item(final_repository, key, item_id)
                )
                winner_search_atomic = (
                    winner_search_atomic
                    and document_matches
                    and fts_matches
                    and facets_match
                )

            _require(
                winner_graph_atomic,
                "RR1_CONCURRENCY_WINNER_GRAPH_NOT_ATOMIC",
            )

            losing_markers = [
                (
                    item_ids_by_key[reused_key],
                    _create_marker(300, reused_loser),
                ),
                (item_ids_by_key[cas_key], _update_marker(400, cas_loser)),
                (item_ids_by_key[cas_key], _create_marker(400, 0)),
                (item_ids_by_key[crash_key], _update_marker(600, 99)),
                (item_ids_by_key[crash_key], _create_marker(600, 0)),
            ]
            for item_id, marker in losing_markers:
                loser_absent = (
                    marker.casefold()
                    not in _document_text(final_repository, item_id).casefold()
                    and not _raw_fts_item_ids(final_repository, marker)
                    and final_service.search(query=marker, limit=100).total == 0
                )
                winner_search_atomic = winner_search_atomic and loser_absent

            snapshot = _database_snapshot(final_repository)
            counts = snapshot["counts"]
            expected_collection_count = 10
            idempotency_ok = (
                counts["collections"] == expected_collection_count
                and counts["idempotency_records"] == expected_collection_count
                and len(expected_item_ids) == expected_collection_count
                and len(set(expected_item_ids)) == expected_collection_count
                and idempotency_records_match
            )
            search_ok = (
                winner_search_atomic
                and counts["search_documents"] == counts["collections"]
                and counts["search_rows"] == counts["collections"]
                and counts["dirty_documents"] == 0
            )
            _require(idempotency_ok, "RR1_CONCURRENCY_FINAL_IDEMPOTENCY_FAILED")
            _require(search_ok, "RR1_CONCURRENCY_FINAL_SEARCH_FAILED")
            _require(snapshot["integrity"], "RR1_CONCURRENCY_INTEGRITY_FAILED")
            _require(
                snapshot["foreign_keys"],
                "RR1_CONCURRENCY_FOREIGN_KEYS_FAILED",
            )

            process_ids = [process.pid for process in registry]
            independent_processes = (
                all(process_id is not None for process_id in process_ids)
                and len({id(process) for process in registry}) == len(registry)
                and os.getpid() not in process_ids
            )
            bounded_processes = all(
                not process.is_alive() and process.exitcode is not None
                for process in registry
            )
            _require(
                independent_processes,
                "RR1_CONCURRENCY_PROCESSES_NOT_INDEPENDENT",
            )
            _require(
                bounded_processes,
                "RR1_CONCURRENCY_PROCESSES_NOT_BOUNDED",
            )

            report = {
                "slice": "RR1-CONCURRENCY1",
                "status": "passed",
                "platform": "Windows",
                "runtime": {
                    "python": platform.python_version(),
                    "sqlite": sqlite3.sqlite_version,
                    "start_method": "spawn",
                },
                "counts": {
                    "workers_started": len(registry),
                    "workers_completed": sum(
                        process.exitcode is not None for process in registry
                    ),
                    "collections": counts["collections"],
                    "idempotency_records": counts["idempotency_records"],
                    "search_documents": counts["search_documents"],
                    "dirty_documents": counts["dirty_documents"],
                },
                "outcomes": {
                    "distinct_writes": {"committed": 4},
                    "same_key_replay": {"responses": 3, "records": 1},
                    "key_reused": {"committed": 1, "rejected": 1},
                    "cas": {"committed": 1, "conflicts": 1},
                    "busy": {
                        "prewrite_rejected": 1,
                        "waited_committed": 1,
                        "retried_committed": 1,
                        "same_identity_retried": 1,
                    },
                    "crash_recovery": {
                        "rolled_back": 1,
                        "retried_committed": 1,
                    },
                },
                "checks": {
                    "independent_processes": independent_processes,
                    "bounded_processes": bounded_processes,
                    "integrity": snapshot["integrity"],
                    "foreign_keys": snapshot["foreign_keys"],
                    "winner_graph_atomic": winner_graph_atomic,
                    "idempotency": idempotency_ok,
                    "search": search_ok,
                    "crash_rollback": all(
                        recovery[name]
                        for name in (
                            "baseline_graph",
                            "ghost_absent",
                            "baseline_search",
                            "baseline_fts",
                            "ghost_fts_absent",
                            "dirty_absent",
                            "integrity",
                            "foreign_keys",
                        )
                    ),
                    "workspace_cleanup": False,
                },
                "error_code": None,
            }
    finally:
        _stop_owned_processes(registry)

    assert temporary_path is not None
    report["checks"]["workspace_cleanup"] = not temporary_path.exists()
    _require(
        report["checks"]["workspace_cleanup"],
        "RR1_CONCURRENCY_CLEANUP_FAILED",
    )
    return report
