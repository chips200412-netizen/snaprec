"""Isolated SQLite release rehearsal for the approved Windows RR1-DB1 lane.

This module never accepts a database path. Every database it touches is created
under a fresh, process-owned temporary directory and removed before the public
function returns.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .domain.models import CollectionItemCreateRequest
from .repositories.sqlite import SQLiteRepository
from .services.collections import CollectionService


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEMP_PARENT = _PROJECT_ROOT / "var" / "wanan" / "temp"
_IDEMPOTENCY_KEY = "rr1-db1-canary-key"
_SEARCH_CANARY = "RR1Canary"
_COLLECTION_IMPORT_V16_TABLES = frozenset(
    {
        "collection_import_batches",
        "collection_import_batch_items",
        "collection_import_batch_idempotency",
        "collection_import_save_attempts",
    }
)
_COLLECTION_IMPORT_V16_DROP_ORDER = (
    "collection_import_save_attempts",
    "collection_import_batch_idempotency",
    "collection_import_batch_items",
    "collection_import_batches",
)
_USER_COVER_V18_TABLES = frozenset({"user_cover_assets", "collection_user_covers"})


class ReleaseRehearsalError(RuntimeError):
    """A stable RR1-DB1 contract failure with no private diagnostic payload."""


@contextmanager
def _owned_temporary_directory():
    """Create an inherited-ACL Windows directory and remove that exact root."""

    _TEMP_PARENT.mkdir(parents=True, exist_ok=True)
    root = _TEMP_PARENT / f"rr1-db1-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        if root.exists():
            shutil.rmtree(root)


def _owned_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise ReleaseRehearsalError("RR1_DB1_PATH_OUTSIDE_OWNED_ROOT")
    return resolved_path


def _online_backup(source: Path, target: Path, root: Path) -> None:
    source = _owned_path(source, root)
    target = _owned_path(target, root)
    if not source.is_file():
        raise ReleaseRehearsalError("RR1_DB1_BACKUP_SOURCE_MISSING")
    if target.exists():
        raise ReleaseRehearsalError("RR1_DB1_BACKUP_TARGET_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_db:
        with closing(sqlite3.connect(target)) as target_db:
            source_db.backup(target_db)


def _canonical_dump_hash(database_path: Path) -> str:
    with closing(sqlite3.connect(database_path)) as db:
        dump = "\n".join(line.strip() for line in db.iterdump())
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _collection_import_tables(db: sqlite3.Connection) -> frozenset[str]:
    placeholders = ", ".join("?" for _ in _COLLECTION_IMPORT_V16_TABLES)
    rows = db.execute(
        "SELECT name FROM sqlite_master "
        f"WHERE type='table' AND name IN ({placeholders})",
        tuple(sorted(_COLLECTION_IMPORT_V16_TABLES)),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


def _drop_v16_collection_import_schema(db: sqlite3.Connection) -> None:
    # Fixed identifiers in child-to-parent order; no caller input reaches SQL.
    for table in _COLLECTION_IMPORT_V16_DROP_ORDER:
        db.execute(f'DROP TABLE IF EXISTS "{table}"')


def _database_snapshot(database_path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(database_path)) as db:
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        integrity = db.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(library_items)")
        }
        collection_import_tables = _collection_import_tables(db)
        tables = {
            str(row[0])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        user_cover_tables = tables & _USER_COVER_V18_TABLES
        old_user_author_null = (
            int(db.execute("SELECT COUNT(*) FROM library_items WHERE user_author IS NOT NULL").fetchone()[0]) == 0
            if "user_author" in columns else True
        )
        counts = {
            "items": int(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0]),
            "idempotency": int(
                db.execute(
                    "SELECT COUNT(*) FROM collection_idempotency_keys"
                ).fetchone()[0]
            ),
            "search_documents": int(
                db.execute(
                    "SELECT COUNT(*) FROM collection_search_documents"
                ).fetchone()[0]
            ),
        }
    return {
        "version": version,
        "integrity_ok": integrity == [("ok",)],
        "foreign_keys_ok": not foreign_keys,
        "has_revision": "revision" in columns,
        "has_source_topic_selection": "selected_source_topic_indices_json" in columns,
        "has_user_author": "user_author" in columns,
        "user_cover_tables": tuple(sorted(user_cover_tables)),
        "old_user_author_null": old_user_author_null,
        "collection_import_tables": tuple(sorted(collection_import_tables)),
        "counts": counts,
        "dump_hash": _canonical_dump_hash(database_path),
    }


def _synthetic_preview() -> dict[str, Any]:
    return {
        "preview_id": "rr1-db1-preview",
        "original_input": "RR1 synthetic share",
        "source_url": "https://example.invalid/rr1-db1-canary",
        "canonical_url": "https://example.invalid/rr1-db1-canary",
        "identity_url": "https://example.invalid/rr1-db1-canary",
        "source_kind": "webpage",
        "platform": "web",
        "metadata_status": "generic",
        "metadata": {
            "title": {
                "value": _SEARCH_CANARY,
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


def _synthetic_request() -> CollectionItemCreateRequest:
    return CollectionItemCreateRequest.model_validate(
        {
            "preview_id": "rr1-db1-preview",
            "user_title": _SEARCH_CANARY,
            "organization_confirmation": {
                "primary_category": "readiness",
                "secondary_category": "sqlite",
                "organization_tags": ["rr1-synthetic"],
            },
            "personal_tags": ["rr1-canary"],
            "inspiration": {
                "content": "RR1 synthetic inspiration",
                "input_mode": "text",
                "transcription_status": "not_applicable",
            },
        }
    )


def _create_v14_synthetic_database(database_path: Path) -> str:
    repository = SQLiteRepository(database_path)
    repository.create_collection_preview(_synthetic_preview())
    item = CollectionService(repository).create(
        _synthetic_request(), _IDEMPOTENCY_KEY
    )
    with closing(sqlite3.connect(database_path)) as db:
        if _collection_import_tables(db) != _COLLECTION_IMPORT_V16_TABLES:
            raise ReleaseRehearsalError("RR1_DB1_CURRENT_SCHEMA_INCOMPLETE")
        db.execute("DROP TRIGGER IF EXISTS collection_search_dirty_library_update")
        db.execute("DROP TABLE collection_user_covers")
        db.execute("DROP TABLE user_cover_assets")
        db.execute("ALTER TABLE library_items DROP COLUMN user_author")
        db.execute("ALTER TABLE library_items DROP COLUMN revision")
        db.execute("ALTER TABLE library_items DROP COLUMN selected_source_topic_indices_json")
        rows = db.execute(
            "SELECT idempotency_key, response_json FROM collection_idempotency_keys"
        ).fetchall()
        for idempotency_key, response_json in rows:
            response = json.loads(response_json)
            response.pop("revision", None)
            response.pop("selected_source_topic_indices", None)
            response.pop("user_author", None)
            response.pop("user_cover_asset_id", None)
            db.execute(
                "UPDATE collection_idempotency_keys SET response_json=? "
                "WHERE idempotency_key=?",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    idempotency_key,
                ),
            )
        _drop_v16_collection_import_schema(db)
        if _collection_import_tables(db):
            raise ReleaseRehearsalError("RR1_DB1_V14_SCHEMA_INVALID")
        db.execute("PRAGMA user_version=14")
        db.commit()
    return item.id


def _verify_migrated_canaries(database_path: Path, item_id: str) -> dict[str, bool]:
    repository = SQLiteRepository(database_path)
    service = CollectionService(repository)
    restored = service.get(item_id)
    replayed = service.create(_synthetic_request(), _IDEMPOTENCY_KEY)
    search = service.search(query=_SEARCH_CANARY)
    search_ids = {item.id for item in search.items}
    return {
        "canary_item_restored": (
            restored.id == item_id and restored.revision == 1
            and restored.selected_source_topic_indices is None
        ),
        "canary_idempotency_replayed": replayed.id == item_id,
        "canary_search_hit": item_id in search_ids,
    }


def _require(condition: bool, error_code: str) -> None:
    if not condition:
        raise ReleaseRehearsalError(error_code)


def run_sqlite_release_rehearsal() -> dict[str, Any]:
    """Run the approved RR1-DB1 rehearsal without accepting external paths."""

    if platform.system() != "Windows":
        raise ReleaseRehearsalError("RR1_DB1_WINDOWS_RUNTIME_REQUIRED")

    report: dict[str, Any]
    temporary_path: Path | None = None
    with _owned_temporary_directory() as root:
        temporary_path = root
        source = root / "source-v14.db"
        backup = root / "backup-v14.db"
        working = root / "working-copy.db"
        restored = root / "restored-v14.db"

        item_id = _create_v14_synthetic_database(source)
        source_before = _database_snapshot(source)
        source_excludes_v16_import = not source_before["collection_import_tables"]
        source_excludes_v18_user_cover = not source_before["user_cover_tables"] and not source_before["has_user_author"]
        _require(source_before["version"] == 14, "RR1_DB1_SOURCE_VERSION_INVALID")
        _require(
            not source_before["has_revision"]
            and not source_before["has_source_topic_selection"]
            and source_excludes_v18_user_cover,
            "RR1_DB1_SOURCE_SCHEMA_INVALID",
        )
        _require(
            source_excludes_v16_import,
            "RR1_DB1_SOURCE_CONTAINS_V16_SCHEMA",
        )
        _require(source_before["integrity_ok"], "RR1_DB1_SOURCE_INTEGRITY_FAILED")
        _require(source_before["foreign_keys_ok"], "RR1_DB1_SOURCE_FOREIGN_KEYS_FAILED")

        _online_backup(source, backup, root)
        source_after_backup = _database_snapshot(source)
        backup_snapshot = _database_snapshot(backup)
        backup_excludes_v16_import = not backup_snapshot["collection_import_tables"]
        backup_excludes_v18_user_cover = not backup_snapshot["user_cover_tables"] and not backup_snapshot["has_user_author"]
        source_unchanged = source_after_backup == source_before
        backup_equivalent = backup_snapshot == source_before
        _require(source_unchanged, "RR1_DB1_SOURCE_CHANGED_DURING_BACKUP")
        _require(backup_equivalent, "RR1_DB1_BACKUP_NOT_EQUIVALENT")
        _require(
            backup_excludes_v16_import,
            "RR1_DB1_BACKUP_CONTAINS_V16_SCHEMA",
        )

        _online_backup(backup, working, root)
        before_fault = _database_snapshot(working)
        observed_stages: list[str] = []

        def fail_after_v15_ddl(stage: str, db: sqlite3.Connection) -> None:
            observed_stages.append(stage)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(library_items)")
            }
            if stage != "after_v15_ddl" or "revision" not in columns:
                raise ReleaseRehearsalError("RR1_DB1_FAULT_STAGE_INVALID")
            raise ReleaseRehearsalError("RR1_DB1_INJECTED_V15_FAILURE")

        try:
            SQLiteRepository(working, migration_fault=fail_after_v15_ddl)
        except ReleaseRehearsalError as exc:
            _require(
                str(exc) == "RR1_DB1_INJECTED_V15_FAILURE",
                "RR1_DB1_UNEXPECTED_MIGRATION_FAILURE",
            )
        else:
            raise ReleaseRehearsalError("RR1_DB1_INJECTED_FAILURE_NOT_RAISED")

        rollback_snapshot = _database_snapshot(working)
        rollback_excludes_v16_import = not rollback_snapshot[
            "collection_import_tables"
        ]
        rollback_preserved = (
            observed_stages == ["after_v15_ddl"]
            and rollback_snapshot == before_fault
            and rollback_snapshot["version"] == 14
            and not rollback_snapshot["has_revision"]
            and rollback_excludes_v16_import
            and not rollback_snapshot["has_user_author"]
            and not rollback_snapshot["user_cover_tables"]
        )
        _require(rollback_preserved, "RR1_DB1_ROLLBACK_NOT_PRESERVED")

        SQLiteRepository(working)
        migrated_snapshot = _database_snapshot(working)
        migrated_canaries = _verify_migrated_canaries(working, item_id)
        migrated_has_v16_import = (
            frozenset(migrated_snapshot["collection_import_tables"])
            == _COLLECTION_IMPORT_V16_TABLES
        )
        migrated_has_v18_user_cover = (
            migrated_snapshot["has_user_author"]
            and frozenset(migrated_snapshot["user_cover_tables"]) == _USER_COVER_V18_TABLES
            and migrated_snapshot["old_user_author_null"]
        )
        # The rehearsal continues to prove the locked v14 -> v15 revision
        # stage and its fault boundary.  A successful repository open now also
        # applies the additive v16 import and v17 source-topic expands.
        retry_passed_v15_canaries = (
            migrated_snapshot["has_revision"]
            and migrated_snapshot["integrity_ok"]
            and migrated_snapshot["foreign_keys_ok"]
            and all(migrated_canaries.values())
        )
        retry_reached_current_schema = (
            migrated_snapshot["version"] == 18
            and migrated_has_v16_import
            and migrated_snapshot["has_source_topic_selection"]
            and migrated_has_v18_user_cover
            and retry_passed_v15_canaries
        )
        _require(
            retry_reached_current_schema,
            "RR1_DB1_RETRY_MIGRATION_FAILED",
        )

        _online_backup(backup, restored, root)
        restore_snapshot = _database_snapshot(restored)
        restore_excludes_v16_import = not restore_snapshot[
            "collection_import_tables"
        ]
        restore_excludes_v18_user_cover = not restore_snapshot["user_cover_tables"] and not restore_snapshot["has_user_author"]
        restore_equivalent = restore_snapshot == backup_snapshot
        _require(restore_equivalent, "RR1_DB1_RESTORE_NOT_EQUIVALENT")
        _require(
            restore_excludes_v16_import,
            "RR1_DB1_RESTORE_CONTAINS_V16_SCHEMA",
        )

        source_final = _database_snapshot(source)
        source_unchanged = source_final == source_before
        _require(source_unchanged, "RR1_DB1_SOURCE_CHANGED")

        checks = {
            "workspace_cleaned": False,
            "source_unchanged": source_unchanged,
            "online_backup_used": True,
            "backup_equivalent": backup_equivalent,
            "source_v14_excludes_v16_collection_import": source_excludes_v16_import,
            "backup_v14_excludes_v16_collection_import": backup_excludes_v16_import,
            "source_v14_excludes_v18_user_cover": source_excludes_v18_user_cover,
            "backup_v14_excludes_v18_user_cover": backup_excludes_v18_user_cover,
            "rollback_preserved_v14": rollback_preserved,
            "rollback_v14_excludes_v16_collection_import": (
                rollback_excludes_v16_import
            ),
            # Preserve the historical public check name: it means the retry
            # crossed and retained the locked v15 revision/canary boundary.
            "retry_migrated_v15": retry_passed_v15_canaries,
            "retry_has_v16_collection_import_schema": migrated_has_v16_import,
            "retry_has_v17_source_topic_selection": migrated_snapshot["has_source_topic_selection"],
            "retry_has_v18_user_author": migrated_snapshot["has_user_author"],
            "retry_has_v18_user_cover_tables": frozenset(migrated_snapshot["user_cover_tables"]) == _USER_COVER_V18_TABLES,
            "retry_v18_old_user_author_null": migrated_snapshot["old_user_author_null"],
            "retry_reached_current_schema": retry_reached_current_schema,
            "restore_equivalent_v14": restore_equivalent,
            "restored_v14_excludes_v16_collection_import": (
                restore_excludes_v16_import
            ),
            "restored_v14_excludes_v18_user_cover": restore_excludes_v18_user_cover,
            **migrated_canaries,
        }
        report = {
            "slice": "RR1-DB1",
            "status": "passed",
            "platform": "Windows",
            "runtime": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
            },
            "versions": {
                "source": source_before["version"],
                "backup": backup_snapshot["version"],
                "rollback": rollback_snapshot["version"],
                "migrated": migrated_snapshot["version"],
                "restored": restore_snapshot["version"],
            },
            "counts": {
                "source_items": source_before["counts"]["items"],
                "backup_items": backup_snapshot["counts"]["items"],
                "migrated_items": migrated_snapshot["counts"]["items"],
                "restored_items": restore_snapshot["counts"]["items"],
                "idempotency_records": migrated_snapshot["counts"]["idempotency"],
                "search_documents": migrated_snapshot["counts"]["search_documents"],
            },
            "checks": checks,
            "hashes": {
                "source_dump": source_before["dump_hash"],
                "backup_dump": backup_snapshot["dump_hash"],
                "rollback_dump": rollback_snapshot["dump_hash"],
                "migrated_dump": migrated_snapshot["dump_hash"],
                "restore_dump": restore_snapshot["dump_hash"],
            },
            "error_code": None,
        }

    assert temporary_path is not None
    report["checks"]["workspace_cleaned"] = not temporary_path.exists()
    _require(report["checks"]["workspace_cleaned"], "RR1_DB1_CLEANUP_FAILED")
    return report
