from __future__ import annotations

import json
import hashlib
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .collection_search import (
    migrate_collection_search,
    normalize_search_text,
    refresh_collection_document,
    search_collection_items as search_indexed_collection_items,
)

from ..domain.models import (
    AnnotationTarget,
    AutomaticTag,
    AutomaticTagging,
    CategoryFacet,
    FocusedAnswer,
    FocusedHistoryItem,
    Job,
    JobBatch,
    JobBatchItem,
    LibraryFacets,
    LibraryVideo,
    PersonalAnnotation,
    PersonalNotes,
    PersonalSpark,
    RetainedMedia,
    SecondaryCategoryFacet,
    TagFacet,
    VideoClassification,
    VideoDetail,
    VideoQuestion,
    VideoResult,
    VideoSearchPage,
    validate_public_url_shape,
)


def normalize_focus_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def focus_query_hash(value: str) -> str:
    return hashlib.sha256(normalize_focus_query(value).encode("utf-8")).hexdigest()


def resource_key(platform: str, video_id: str) -> str:
    return f"{platform}:{video_id}"


def normalize_tag(value: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not display:
        raise ValueError("标签不能为空。")
    if len(display) > 64:
        raise ValueError("单个标签不能超过 64 个字符。")
    return display.casefold(), display


def normalize_identity_url(value: str) -> str:
    raw = value.strip()
    validate_public_url_shape(raw, label="收藏链接")
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("收藏链接必须使用 HTTP 或 HTTPS。")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("收藏链接不能包含用户凭据。")
    if not parsed.hostname:
        raise ValueError("收藏链接缺少有效主机。")
    host = parsed.hostname.encode("idna").decode("ascii").casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("收藏链接端口无效。") from exc
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))


def validate_personal_text(
    value: str, *, label: str, max_length: int
) -> str:
    if not value.strip():
        raise ValueError(f"{label}不能为空。")
    if len(value) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符。")
    return value


class AmbiguousVideoIdError(LookupError):
    pass


class BatchJobsNotFoundError(LookupError):
    def __init__(self, job_ids: list[str]):
        super().__init__(", ".join(job_ids))
        self.job_ids = job_ids


class BatchJobsNotAllowedError(ValueError):
    def __init__(self, job_ids: list[str]):
        super().__init__(", ".join(job_ids))
        self.job_ids = job_ids


class CollectionExistsError(ValueError):
    def __init__(self, collection_item_id: str):
        super().__init__(collection_item_id)
        self.collection_item_id = collection_item_id


class IdempotencyKeyReusedError(ValueError):
    pass


class CollectionRevisionConflictError(ValueError):
    def __init__(
        self,
        collection_item_id: str,
        expected_revision: int,
        current_revision: int,
    ):
        super().__init__(collection_item_id)
        self.collection_item_id = collection_item_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


_JOB_TRANSITIONS = {
    "queued": {
        "resolving", "fetching_metadata", "fetching_subtitles",
        "transcribing", "cleaning", "extracting", "saving",
        "completed", "completed_with_warnings",
    },
    "resolving": {"fetching_metadata", "completed"},
    "fetching_metadata": {"fetching_subtitles", "completed", "completed_with_warnings"},
    "fetching_subtitles": {
        "transcribing", "cleaning", "saving",
        "completed", "completed_with_warnings",
    },
    "transcribing": {"cleaning", "completed", "completed_with_warnings"},
    "cleaning": {"extracting", "completed", "completed_with_warnings"},
    "extracting": {"saving", "completed", "completed_with_warnings"},
    "saving": {"completed", "completed_with_warnings"},
}
_JOB_TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}
_SQLITE_BUSY_TIMEOUT_MS = 5_000


class SQLiteRepository:
    def __init__(
        self,
        database_path: str | Path,
        *,
        migration_fault: Callable[[str, sqlite3.Connection], None] | None = None,
    ):
        self.database_path = Path(database_path)
        self._migration_fault = migration_fault
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def _connect(self):
        # Keep the long-standing CPython default explicit so every process uses
        # the same bounded busy policy. This is connection-local and does not
        # change the database journal mode or any persistent PRAGMA.
        connection = sqlite3.connect(
            self.database_path,
            timeout=_SQLITE_BUSY_TIMEOUT_MS / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self._connect() as db:
            # SQLite ``executescript`` implicitly commits pending work. Every
            # migration statement below therefore uses ``execute`` inside this
            # explicit transaction so a failed upgrade leaves no half schema.
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
            ).fetchone()
            if exists is None:
                self._create_v3_schema(db)
            else:
                columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(videos)").fetchall()
                }
                if "resource_key" not in columns:
                    self._migrate_legacy_to_v3(db)
            self._migrate_v3_to_v4(db)
            self._migrate_v4_to_v5(db)
            self._migrate_v5_to_v6(db)
            self._migrate_v6_to_v7(db)
            self._migrate_v7_to_v8(db)
            self._migrate_v8_to_v9(db)
            self._migrate_v9_to_v10(db)
            self._migrate_v10_to_v11(db)
            self._migrate_v11_to_v12(db)
            self._migrate_v12_to_v13(db)
            migrated_v15 = self._migrate_v14_to_v15(db)
            if migrated_v15 and self._migration_fault is not None:
                self._migration_fault("after_v15_ddl", db)
            migrated_v16 = self._migrate_v15_to_v16(db)
            if migrated_v16 and self._migration_fault is not None:
                self._migration_fault("after_v16_ddl", db)
            migrated_v17 = self._migrate_v16_to_v17(db)
            if migrated_v17 and self._migration_fault is not None:
                self._migration_fault("after_v17_ddl", db)
            migrated_v18 = self._migrate_v17_to_v18(db)
            if migrated_v18 and self._migration_fault is not None:
                self._migration_fault("after_v18_ddl", db)
            migrate_collection_search(db)
            foreign_key_violations = db.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_violations:
                raise sqlite3.IntegrityError(
                    "database migration failed foreign_key_check"
                )
            db.execute("PRAGMA user_version = 18")

    @staticmethod
    def _migrate_v17_to_v18(db: sqlite3.Connection) -> bool:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(library_items)")}
        if not columns:
            return False
        changed = False
        if "user_author" not in columns:
            db.execute("ALTER TABLE library_items ADD COLUMN user_author TEXT")
            changed = True
        asset_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_cover_assets'"
        ).fetchone()
        if asset_table is None:
            db.execute(
                """CREATE TABLE user_cover_assets (
                    asset_id TEXT PRIMARY KEY,
                    storage_name TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL CHECK(media_type='image/webp'),
                    byte_size INTEGER NOT NULL CHECK(byte_size > 0 AND byte_size <= 5242880),
                    content_sha256 TEXT NOT NULL,
                    width INTEGER NOT NULL CHECK(width > 0 AND width <= 8192),
                    height INTEGER NOT NULL CHECK(height > 0 AND height <= 8192),
                    claim_token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )"""
            )
            db.execute(
                """CREATE TABLE collection_user_covers (
                    collection_item_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(collection_item_id) REFERENCES library_items(id) ON DELETE CASCADE,
                    FOREIGN KEY(asset_id) REFERENCES user_cover_assets(asset_id) ON DELETE RESTRICT
                )"""
            )
            db.execute(
                "CREATE INDEX idx_collection_user_covers_asset ON collection_user_covers(asset_id, collection_item_id)"
            )
            db.execute(
                "CREATE INDEX idx_user_cover_assets_expiry ON user_cover_assets(expires_at, asset_id)"
            )
            changed = True
        return changed

    @staticmethod
    def _migrate_v16_to_v17(db: sqlite3.Connection) -> bool:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(library_items)")}
        if not columns or "selected_source_topic_indices_json" in columns:
            return False
        db.execute(
            "ALTER TABLE library_items ADD COLUMN selected_source_topic_indices_json TEXT"
        )
        return True

    @staticmethod
    def _migrate_v15_to_v16(db: sqlite3.Connection) -> bool:
        """Expand the database with the isolated collection-import namespace.

        The caller already owns the repository's single ``BEGIN IMMEDIATE``
        migration transaction.  Deliberately keep these tables independent of
        the legacy deep-analysis ``job_batches`` tables: collection import has
        different identities, recovery rules, and retention semantics.
        """

        existed = db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='collection_import_batches'"
        ).fetchone() is not None
        statements = [
            """CREATE TABLE IF NOT EXISTS collection_import_batches (
                batch_id TEXT PRIMARY KEY,
                active_slot INTEGER NOT NULL DEFAULT 1 CHECK(active_slot = 1),
                status TEXT NOT NULL CHECK(status IN (
                    'previewing', 'awaiting_review', 'saving', 'cancelling',
                    'interrupted', 'completed', 'completed_with_issues',
                    'cancelled'
                )),
                revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                cancel_requested INTEGER NOT NULL DEFAULT 0
                    CHECK(cancel_requested IN (0, 1)),
                total_count INTEGER NOT NULL CHECK(total_count BETWEEN 2 AND 10),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                terminal_at TEXT
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                idx_collection_import_single_active
                ON collection_import_batches(active_slot)
                WHERE terminal_at IS NULL""",
            """CREATE INDEX IF NOT EXISTS idx_collection_import_terminal_order
                ON collection_import_batches(terminal_at DESC, batch_id DESC)""",
            """CREATE TABLE IF NOT EXISTS collection_import_batch_items (
                batch_item_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK(position >= 0),
                client_item_id TEXT NOT NULL,
                pending_input_text TEXT,
                display_label TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'queued', 'previewing', 'ready', 'needs_review',
                    'preview_expired', 'duplicate_in_batch', 'save_queued',
                    'saving', 'saved', 'already_exists', 'skipped', 'failed',
                    'cancelled', 'interrupted', 'outcome_unknown'
                )),
                decision TEXT NOT NULL DEFAULT 'pending'
                    CHECK(decision IN ('pending', 'save', 'skip')),
                preview_generation INTEGER NOT NULL DEFAULT 0
                    CHECK(preview_generation BETWEEN 0 AND 5),
                preview_id TEXT,
                identity_url TEXT,
                review_revision INTEGER NOT NULL DEFAULT 0
                    CHECK(review_revision >= 0),
                item_revision INTEGER NOT NULL DEFAULT 1
                    CHECK(item_revision >= 1),
                preview_claim_token TEXT,
                duplicate_of_batch_item_id TEXT,
                error_code TEXT,
                error_stage TEXT,
                terminal_reason TEXT,
                collection_item_id TEXT,
                draft_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, position),
                UNIQUE(batch_id, client_item_id),
                FOREIGN KEY(batch_id) REFERENCES collection_import_batches(batch_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(duplicate_of_batch_item_id)
                    REFERENCES collection_import_batch_items(batch_item_id)
                    ON DELETE SET NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE SET NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                idx_collection_import_preview_claim
                ON collection_import_batch_items(preview_claim_token)
                WHERE preview_claim_token IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS idx_collection_import_item_queue
                ON collection_import_batch_items(batch_id, state, position)""",
            """CREATE INDEX IF NOT EXISTS idx_collection_import_item_identity
                ON collection_import_batch_items(batch_id, identity_url, position)""",
            """CREATE TABLE IF NOT EXISTS collection_import_batch_idempotency (
                key_hash TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                batch_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                purged_at TEXT,
                FOREIGN KEY(batch_id) REFERENCES collection_import_batches(batch_id)
                    ON DELETE SET NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_collection_import_idem_expiry
                ON collection_import_batch_idempotency(expires_at, key_hash)""",
            """CREATE TABLE IF NOT EXISTS collection_import_save_attempts (
                save_attempt_id TEXT PRIMARY KEY,
                batch_item_id TEXT NOT NULL,
                preview_id TEXT NOT NULL,
                preview_generation INTEGER NOT NULL CHECK(preview_generation >= 1),
                review_revision INTEGER NOT NULL CHECK(review_revision >= 1),
                frozen_request_json TEXT,
                collection_request_hash TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                attempt_phase TEXT NOT NULL CHECK(attempt_phase IN (
                    'frozen', 'claimed', 'call_started', 'result_observed',
                    'settled'
                )),
                result TEXT NOT NULL CHECK(result IN (
                    'none', 'success', 'exists', 'known_not_written', 'unknown'
                )),
                claim_token TEXT,
                error_code TEXT,
                collection_item_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                settled_at TEXT,
                FOREIGN KEY(batch_item_id)
                    REFERENCES collection_import_batch_items(batch_item_id)
                    ON DELETE CASCADE,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE SET NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                idx_collection_import_save_claim
                ON collection_import_save_attempts(claim_token)
                WHERE claim_token IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                idx_collection_import_one_save_claim_per_item
                ON collection_import_save_attempts(batch_item_id)
                WHERE claim_token IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS idx_collection_import_save_item
                ON collection_import_save_attempts(
                    batch_item_id, created_at, save_attempt_id
                )""",
        ]
        for statement in statements:
            db.execute(statement)
        return not existed

    @staticmethod
    def _migrate_v14_to_v15(db: sqlite3.Connection) -> bool:
        table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_items'"
        ).fetchone()
        if table_exists is None:
            return False
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(library_items)")
        }
        if "revision" not in columns:
            db.execute(
                "ALTER TABLE library_items "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
            return True
        return False

    @staticmethod
    def _migrate_v11_to_v12(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS collection_metadata_cache (
                identity_url TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS collection_metadata_cache_aliases (
                alias_url TEXT PRIMARY KEY,
                identity_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(identity_url) REFERENCES collection_metadata_cache(identity_url)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS collection_previews (
                preview_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_collection_cache_alias_identity
                ON collection_metadata_cache_aliases(identity_url, alias_url)""",
            """CREATE INDEX IF NOT EXISTS idx_collection_previews_expiry
                ON collection_previews(expires_at, preview_id)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v12_to_v13(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS collection_deep_analysis_jobs (
                collection_item_id TEXT NOT NULL,
                config_version TEXT NOT NULL,
                task_key TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(collection_item_id, config_version),
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_collection_deep_job
                ON collection_deep_analysis_jobs(job_id, collection_item_id)""",
            """CREATE TABLE IF NOT EXISTS collection_deep_retry_attempts (
                attempt_key TEXT PRIMARY KEY,
                collection_item_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                failed_revision INTEGER NOT NULL,
                failed_stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_collection_deep_retry_job
                ON collection_deep_retry_attempts(job_id, created_at)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _create_v3_schema(db: sqlite3.Connection, suffix: str = "") -> None:
        name = lambda base: f"{base}{suffix}"
        statements = [
            f"""CREATE TABLE {name("videos")} (
                resource_key TEXT PRIMARY KEY, platform TEXT NOT NULL,
                public_video_id TEXT NOT NULL, video_id TEXT NOT NULL,
                source_url TEXT NOT NULL, title TEXT NOT NULL,
                metadata_json TEXT NOT NULL, subtitle_source TEXT NOT NULL,
                raw_transcript TEXT NOT NULL, clean_transcript TEXT NOT NULL,
                warnings_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(platform, public_video_id)
            )""",
            f"""CREATE TABLE {name("transcript_segments")} (
                resource_key TEXT NOT NULL, video_id TEXT NOT NULL,
                position INTEGER NOT NULL, segment_id TEXT NOT NULL,
                start_time REAL, end_time REAL, text TEXT NOT NULL,
                PRIMARY KEY(resource_key, position),
                UNIQUE(resource_key, segment_id),
                FOREIGN KEY(resource_key) REFERENCES {name("videos")}(resource_key)
                    ON DELETE CASCADE
            )""",
            f"""CREATE TABLE {name("extractions")} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_key TEXT NOT NULL, video_id TEXT NOT NULL,
                mode TEXT NOT NULL, focus_query TEXT NOT NULL,
                query_hash TEXT NOT NULL, result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(resource_key, mode, query_hash),
                FOREIGN KEY(resource_key) REFERENCES {name("videos")}(resource_key)
                    ON DELETE CASCADE
            )""",
            f"""CREATE TABLE {name("claims")} (
                id TEXT NOT NULL, extraction_id INTEGER NOT NULL,
                claim TEXT NOT NULL, claim_type TEXT NOT NULL,
                evidence TEXT NOT NULL, start_time REAL, end_time REAL,
                confidence REAL NOT NULL,
                PRIMARY KEY(id, extraction_id),
                FOREIGN KEY(extraction_id) REFERENCES {name("extractions")}(id)
                    ON DELETE CASCADE
            )""",
            f"""CREATE TABLE {name("claim_evidence")} (
                extraction_id INTEGER NOT NULL, claim_id TEXT NOT NULL,
                resource_key TEXT NOT NULL, video_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                PRIMARY KEY(extraction_id, claim_id, segment_id),
                FOREIGN KEY(claim_id, extraction_id)
                    REFERENCES {name("claims")}(id, extraction_id) ON DELETE CASCADE,
                FOREIGN KEY(resource_key, segment_id)
                    REFERENCES {name("transcript_segments")}(resource_key, segment_id)
                    ON DELETE CASCADE
            )""",
            f"""CREATE TABLE {name("video_aliases")} (
                source_url TEXT PRIMARY KEY, platform TEXT NOT NULL,
                resource_key TEXT NOT NULL, video_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES {name("videos")}(resource_key)
                    ON DELETE CASCADE
            )""",
        ]
        for statement in statements:
            db.execute(statement)
        if not suffix:
            cursor = db.execute(
                """CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, progress INTEGER NOT NULL,
                    error_code TEXT, message TEXT NOT NULL, video_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )

    def _migrate_legacy_to_v3(self, db: sqlite3.Connection) -> None:
        self._create_v3_schema(db, "_v3")
        db.execute(
            """INSERT INTO videos_v3
            SELECT platform || ':' || video_id, platform, video_id, video_id,
                   source_url, title, metadata_json, subtitle_source,
                   raw_transcript, clean_transcript, warnings_json, status,
                   created_at, updated_at FROM videos"""
        )
        db.execute(
            """INSERT INTO transcript_segments_v3
            SELECT v.platform || ':' || s.video_id, s.video_id, s.position,
                   s.segment_id, s.start_time, s.end_time, s.text
            FROM transcript_segments s JOIN videos v ON v.video_id=s.video_id"""
        )
        extraction_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(extractions)")
        }
        rows = db.execute("SELECT * FROM extractions").fetchall()
        for row in rows:
            query_hash = (
                row["query_hash"]
                if "query_hash" in extraction_columns and row["query_hash"]
                else focus_query_hash(row["focus_query"])
            )
            platform = db.execute(
                "SELECT platform FROM videos WHERE video_id=?", (row["video_id"],)
            ).fetchone()["platform"]
            db.execute(
                """INSERT INTO extractions_v3
                (id, resource_key, video_id, mode, focus_query, query_hash,
                 result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"], resource_key(platform, row["video_id"]), row["video_id"],
                    row["mode"], row["focus_query"], query_hash,
                    row["result_json"], row["created_at"],
                ),
            )
        db.execute("INSERT INTO claims_v3 SELECT * FROM claims")
        db.execute(
            """INSERT OR IGNORE INTO claim_evidence_v3
            SELECT ce.extraction_id, ce.claim_id,
                   v.platform || ':' || ce.video_id, ce.video_id, ce.segment_id
            FROM claim_evidence ce
            JOIN videos v ON v.video_id=ce.video_id
            JOIN extractions e ON e.id=ce.extraction_id AND e.video_id=ce.video_id"""
        )
        aliases_exist = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_aliases'"
        ).fetchone()
        if aliases_exist:
            db.execute(
                """INSERT OR IGNORE INTO video_aliases_v3
                SELECT a.source_url, a.platform, a.platform || ':' || a.video_id,
                       a.video_id, a.created_at
                FROM video_aliases a JOIN videos v
                  ON v.video_id=a.video_id AND v.platform=a.platform"""
            )
        for table in (
            "claim_evidence",
            "claims",
            "extractions",
            "transcript_segments",
            "video_aliases",
            "videos",
        ):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        for temporary, final in (
            ("videos_v3", "videos"),
            ("transcript_segments_v3", "transcript_segments"),
            ("extractions_v3", "extractions"),
            ("claims_v3", "claims"),
            ("claim_evidence_v3", "claim_evidence"),
            ("video_aliases_v3", "video_aliases"),
        ):
            db.execute(f"ALTER TABLE {temporary} RENAME TO {final}")

    @staticmethod
    def _migrate_v3_to_v4(db: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(videos)").fetchall()
        }
        if "favorite" not in columns:
            db.execute(
                "ALTER TABLE videos ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
            )
        statements = [
            """CREATE TABLE IF NOT EXISTS tags (
                normalized_name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS video_tags (
                resource_key TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(resource_key, normalized_name),
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE,
                FOREIGN KEY(normalized_name) REFERENCES tags(normalized_name)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_videos_library_order
                ON videos(updated_at DESC, resource_key ASC)""",
            """CREATE INDEX IF NOT EXISTS idx_video_tags_name
                ON video_tags(normalized_name, resource_key)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v4_to_v5(db: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(jobs)").fetchall()
        }
        additions = (
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_retries", "INTEGER NOT NULL DEFAULT 2"),
            ("retryable", "INTEGER NOT NULL DEFAULT 0"),
            ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("request_kind", "TEXT NOT NULL DEFAULT 'none'"),
            ("request_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("warnings_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("checkpoint", "TEXT NOT NULL DEFAULT 'none'"),
            ("heartbeat_at", "TEXT NOT NULL DEFAULT ''"),
            ("revision", "INTEGER NOT NULL DEFAULT 0"),
            ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
            ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
        )
        for name, declaration in additions:
            if name not in columns:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
        db.execute(
            """CREATE TABLE IF NOT EXISTS system_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS job_artifacts (
                job_id TEXT PRIMARY KEY, stage TEXT NOT NULL,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
            )"""
        )

    @staticmethod
    def _migrate_v5_to_v6(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS video_sparks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_key TEXT NOT NULL UNIQUE,
                target_key TEXT CHECK(target_key IS NULL),
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS video_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                target_type TEXT NOT NULL
                    CHECK(target_type IN ('claim', 'step')),
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_video_annotations_resource
                ON video_annotations(resource_key, created_at ASC, id ASC)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v6_to_v7(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS automatic_tag_runs (
                resource_key TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN (
                    'generated', 'skipped_no_transcript', 'failed'
                )),
                generator_id TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                transcript_hash TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                warning TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS automatic_tags (
                normalized_name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS video_automatic_tags (
                resource_key TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                generation_method TEXT NOT NULL
                    CHECK(generation_method IN ('llm', 'deterministic')),
                created_at TEXT NOT NULL,
                PRIMARY KEY(resource_key, normalized_name),
                FOREIGN KEY(resource_key) REFERENCES automatic_tag_runs(resource_key)
                    ON DELETE CASCADE,
                FOREIGN KEY(normalized_name)
                    REFERENCES automatic_tags(normalized_name) ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS video_classifications (
                resource_key TEXT PRIMARY KEY,
                primary_normalized TEXT NOT NULL,
                primary_category TEXT NOT NULL,
                secondary_normalized TEXT NOT NULL,
                secondary_category TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_video_automatic_tags_name
                ON video_automatic_tags(normalized_name, resource_key)""",
            """CREATE INDEX IF NOT EXISTS idx_video_classifications_categories
                ON video_classifications(
                    primary_normalized, secondary_normalized, resource_key
                )""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v7_to_v8(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS job_batch_items (
                batch_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                platform TEXT NOT NULL CHECK(platform IN (
                    'local_upload', 'bilibili', 'douyin'
                )),
                created_at TEXT NOT NULL,
                PRIMARY KEY(batch_id, job_id),
                UNIQUE(batch_id, position),
                FOREIGN KEY(batch_id) REFERENCES job_batches(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_job_batches_created
                ON job_batches(created_at DESC, id ASC)""",
            """CREATE INDEX IF NOT EXISTS idx_job_batch_items_job
                ON job_batch_items(job_id, batch_id)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v8_to_v9(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS retained_media (
                resource_key TEXT PRIMARY KEY,
                storage_key TEXT NOT NULL UNIQUE,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )"""
        )

    @staticmethod
    def _migrate_v9_to_v10(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(resource_key) REFERENCES videos(resource_key)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                question_hash TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(conversation_id, question_hash),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS question_evidence (
                question_id INTEGER NOT NULL,
                evidence_id TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                PRIMARY KEY(question_id, evidence_id, segment_id),
                FOREIGN KEY(question_id) REFERENCES questions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY(resource_key, segment_id)
                    REFERENCES transcript_segments(resource_key, segment_id)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_questions_conversation_order
                ON questions(conversation_id, created_at ASC, id ASC)""",
            """CREATE INDEX IF NOT EXISTS idx_question_evidence_segment
                ON question_evidence(resource_key, segment_id, question_id)""",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _migrate_v10_to_v11(db: sqlite3.Connection) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS library_items (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                platform TEXT NOT NULL,
                original_input TEXT NOT NULL,
                source_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                identity_url TEXT NOT NULL UNIQUE,
                metadata_status TEXT NOT NULL CHECK(metadata_status IN (
                    'recognized', 'generic', 'metadata_unavailable'
                )),
                user_title TEXT,
                deep_analysis_resource_key TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(deep_analysis_resource_key)
                    REFERENCES videos(resource_key) ON DELETE SET NULL
            )""",
            """CREATE TABLE IF NOT EXISTS source_metadata (
                collection_item_id TEXT PRIMARY KEY,
                title_value TEXT NOT NULL, title_source TEXT NOT NULL,
                title_fetched_at TEXT NOT NULL,
                author_value TEXT NOT NULL, author_source TEXT NOT NULL,
                author_fetched_at TEXT NOT NULL,
                cover_url_value TEXT NOT NULL, cover_url_source TEXT NOT NULL,
                cover_url_fetched_at TEXT NOT NULL,
                source_copy_value TEXT NOT NULL, source_copy_source TEXT NOT NULL,
                source_copy_fetched_at TEXT NOT NULL,
                platform_tags_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS organization_suggestions (
                collection_item_id TEXT PRIMARY KEY,
                primary_category TEXT NOT NULL,
                secondary_category TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                basis TEXT NOT NULL,
                method TEXT NOT NULL,
                status TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS organization_confirmations (
                collection_item_id TEXT PRIMARY KEY,
                primary_category TEXT NOT NULL,
                secondary_category TEXT NOT NULL,
                organization_tags_json TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS inspirations (
                id TEXT PRIMARY KEY,
                collection_item_id TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                input_mode TEXT NOT NULL CHECK(input_mode IN ('text', 'voice')),
                transcription_status TEXT NOT NULL CHECK(transcription_status IN (
                    'not_applicable', 'draft', 'completed', 'failed'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS collection_idempotency_keys (
                idempotency_key TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                collection_item_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS collection_url_aliases (
                alias_url TEXT PRIMARY KEY,
                collection_item_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS collection_personal_tags (
                collection_item_id TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                position INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(collection_item_id, normalized_name),
                FOREIGN KEY(collection_item_id) REFERENCES library_items(id)
                    ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_library_items_order
                ON library_items(updated_at DESC, id ASC)""",
            """CREATE INDEX IF NOT EXISTS idx_collection_personal_tags_name
                ON collection_personal_tags(normalized_name, collection_item_id)""",
            """CREATE INDEX IF NOT EXISTS idx_collection_alias_item
                ON collection_url_aliases(collection_item_id, alias_url)""",
        ]
        for statement in statements:
            db.execute(statement)

        favorite_rows = db.execute(
            """SELECT resource_key, platform, public_video_id, source_url,
                      metadata_json, created_at, updated_at
               FROM videos WHERE favorite=1 ORDER BY resource_key ASC"""
        ).fetchall()
        existing_aliases = {
            row["alias_url"]
            for row in db.execute(
                "SELECT alias_url FROM collection_url_aliases"
            ).fetchall()
        }
        existing_aliases.update(
            row["identity_url"]
            for row in db.execute(
                "SELECT identity_url FROM library_items"
            ).fetchall()
        )
        candidates: list[tuple[sqlite3.Row, dict, str, set[str]]] = []
        invalid_count = 0
        for row in favorite_rows:
            if db.execute(
                """SELECT 1 FROM library_items
                   WHERE deep_analysis_resource_key=?""",
                (row["resource_key"],),
            ).fetchone() is not None:
                continue
            payload = json.loads(row["metadata_json"])
            try:
                if row["platform"] == "local_upload":
                    identity = f"legacy:{row['resource_key']}"
                    aliases = {identity}
                else:
                    identity = normalize_identity_url(
                        payload.get("canonical_url") or row["source_url"]
                    )
                    aliases = {identity}
                    for raw_alias in (
                        row["source_url"], payload.get("canonical_url")
                    ):
                        if raw_alias:
                            aliases.add(normalize_identity_url(raw_alias))
            except (TypeError, ValueError):
                invalid_count += 1
                continue
            candidates.append((row, payload, identity, aliases))
        alias_owners: dict[str, list[int]] = {}
        for index, (_, _, _, aliases) in enumerate(candidates):
            for alias in aliases:
                alias_owners.setdefault(alias, []).append(index)
        conflicting_indexes = {
            index
            for owners in alias_owners.values()
            if len(owners) > 1
            for index in owners
        }
        conflicting_indexes.update(
            index
            for index, (_, _, _, aliases) in enumerate(candidates)
            if aliases & existing_aliases
        )
        if invalid_count:
            if db.execute(
                "SELECT 1 FROM system_warnings WHERE code=? LIMIT 1",
                ("COLLECTION_BACKFILL_INVALID_IDENTITY",),
            ).fetchone() is None:
                db.execute(
                    """INSERT INTO system_warnings(code, message, created_at)
                       VALUES (?, ?, ?)""",
                    (
                        "COLLECTION_BACKFILL_INVALID_IDENTITY",
                        f"{invalid_count} 个旧收藏因身份无效未回填；旧数据保持不变。",
                        datetime.now(UTC).isoformat(),
                    ),
                )
        for index, (row, legacy, identity, _) in enumerate(candidates):
            if index in conflicting_indexes:
                if db.execute(
                    "SELECT 1 FROM system_warnings WHERE code=? LIMIT 1",
                    ("COLLECTION_BACKFILL_IDENTITY_CONFLICT",),
                ).fetchone() is None:
                    db.execute(
                        """INSERT INTO system_warnings(code, message, created_at)
                           VALUES (?, ?, ?)""",
                        (
                            "COLLECTION_BACKFILL_IDENTITY_CONFLICT",
                            f"{len(conflicting_indexes)} 个旧收藏身份或别名冲突，已整组跳过；旧数据保持不变。",
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                continue
            key = row["resource_key"]
            classification = db.execute(
                """SELECT primary_category, secondary_category
                   FROM video_classifications WHERE resource_key=?""",
                (key,),
            ).fetchone()
            tag_rows = db.execute(
                """SELECT t.display_name FROM video_tags vt
                   JOIN tags t ON t.normalized_name=vt.normalized_name
                   WHERE vt.resource_key=? ORDER BY vt.created_at ASC,
                   vt.normalized_name ASC""",
                (key,),
            ).fetchall()
            spark = db.execute(
                """SELECT content, created_at, updated_at FROM video_sparks
                   WHERE resource_key=?""",
                (key,),
            ).fetchone()
            fetched_at = row["updated_at"]

            def legacy_field(value: object) -> dict[str, str]:
                text = str(value or "")
                usable = bool(text.strip())
                return {
                    "value": text if usable else "",
                    "source": "legacy_import" if usable else "none",
                    "fetched_at": fetched_at,
                }

            legacy_cover = str(legacy.get("cover_url") or "")
            legacy_cover_warning = None
            if legacy_cover:
                try:
                    validate_public_url_shape(legacy_cover, label="封面链接")
                except ValueError:
                    legacy_cover = ""
                    legacy_cover_warning = "历史封面链接未通过安全校验，已忽略。"
            legacy_platform_tags = [
                value for value in legacy.get("tags", []) if value.strip()
            ]
            legacy_empty_tag_warning = (
                "历史空平台标签已忽略。"
                if len(legacy_platform_tags) != len(legacy.get("tags", []))
                else None
            )
            has_legacy_metadata = any(
                (
                    str(legacy.get("title") or "").strip(),
                    str(legacy.get("author") or "").strip(),
                    legacy_cover,
                    str(legacy.get("description") or "").strip(),
                    *legacy_platform_tags,
                )
            )
            collection_payload = {
                "source_kind": "video",
                "platform": row["platform"],
                "original_input": row["source_url"],
                "source_url": row["source_url"],
                "canonical_url": legacy.get("canonical_url") or "",
                "metadata_status": (
                    "recognized" if has_legacy_metadata else "metadata_unavailable"
                ),
                "user_title": None,
                "metadata": {
                    "title": legacy_field(legacy.get("title")),
                    "author": legacy_field(legacy.get("author")),
                    "cover_url": legacy_field(legacy_cover),
                    "source_copy": legacy_field(legacy.get("description")),
                    "platform_tags": [
                        {"value": value, "source": "legacy_import"}
                        for value in legacy_platform_tags
                    ],
                    "warnings": [
                        *legacy.get("warnings", []),
                        *([legacy_cover_warning] if legacy_cover_warning else []),
                        *(
                            [legacy_empty_tag_warning]
                            if legacy_empty_tag_warning
                            else []
                        ),
                        "历史收藏迁入；逐字段来源标记为 legacy_import。",
                    ],
                },
                "organization_suggestion": {
                    "primary_category": "",
                    "secondary_category": "",
                    "tags": [],
                    "basis": "public_metadata",
                    "method": "deterministic",
                    "status": "insufficient_metadata",
                },
                "organization_confirmation": {
                    "primary_category": classification["primary_category"]
                    if classification else "",
                    "secondary_category": classification["secondary_category"]
                    if classification else "",
                    "organization_tags": [],
                },
                "personal_tags": [tag["display_name"] for tag in tag_rows],
                "inspiration": None
                if spark is None
                else {
                    "content": spark["content"],
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                    "created_at": spark["created_at"],
                    "updated_at": spark["updated_at"],
                },
            }
            item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-collection:{key}"))
            SQLiteRepository._insert_collection_graph(
                db,
                item_id,
                collection_payload,
                identity,
                row["created_at"],
                row["updated_at"],
                deep_analysis_resource_key=key,
            )

    @staticmethod
    def _insert_collection_graph(
        db: sqlite3.Connection,
        item_id: str,
        payload: dict,
        identity_url: str,
        created_at: str,
        updated_at: str,
        *,
        deep_analysis_resource_key: str | None = None,
    ) -> None:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(library_items)")
        }
        base_values = (
            item_id,
            payload["source_kind"],
            payload["platform"],
            payload["original_input"],
            payload["source_url"],
            payload.get("canonical_url") or "",
            identity_url,
            payload["metadata_status"],
            payload.get("user_title"),
            deep_analysis_resource_key,
            created_at,
            updated_at,
        )
        if "user_author" in columns:
            db.execute(
                """INSERT INTO library_items(
                    id, source_kind, platform, original_input, source_url,
                    canonical_url, identity_url, metadata_status, user_title,
                    deep_analysis_resource_key, created_at, updated_at, user_author
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*base_values, payload.get("user_author")),
            )
        else:
            # v10 -> v11 backfill invokes this helper before the v18 column exists.
            db.execute(
                """INSERT INTO library_items(
                    id, source_kind, platform, original_input, source_url,
                    canonical_url, identity_url, metadata_status, user_title,
                    deep_analysis_resource_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                base_values,
            )
        metadata = payload["metadata"]
        db.execute(
            """INSERT INTO source_metadata(
                collection_item_id,
                title_value, title_source, title_fetched_at,
                author_value, author_source, author_fetched_at,
                cover_url_value, cover_url_source, cover_url_fetched_at,
                source_copy_value, source_copy_source, source_copy_fetched_at,
                platform_tags_json, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                metadata["title"]["value"], metadata["title"]["source"],
                metadata["title"].get("fetched_at", ""),
                metadata["author"]["value"], metadata["author"]["source"],
                metadata["author"].get("fetched_at", ""),
                metadata["cover_url"]["value"], metadata["cover_url"]["source"],
                metadata["cover_url"].get("fetched_at", ""),
                metadata["source_copy"]["value"], metadata["source_copy"]["source"],
                metadata["source_copy"].get("fetched_at", ""),
                json.dumps(metadata.get("platform_tags", []), ensure_ascii=False),
                json.dumps(metadata.get("warnings", []), ensure_ascii=False),
            ),
        )
        suggestion = payload["organization_suggestion"]
        db.execute(
            """INSERT INTO organization_suggestions(
                collection_item_id, primary_category, secondary_category,
                tags_json, basis, method, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                suggestion.get("primary_category", ""),
                suggestion.get("secondary_category", ""),
                json.dumps(suggestion.get("tags", []), ensure_ascii=False),
                suggestion["basis"], suggestion["method"], suggestion["status"],
            ),
        )
        confirmation = payload["organization_confirmation"]
        db.execute(
            """INSERT INTO organization_confirmations(
                collection_item_id, primary_category, secondary_category,
                organization_tags_json
            ) VALUES (?, ?, ?, ?)""",
            (
                item_id,
                confirmation.get("primary_category", ""),
                confirmation.get("secondary_category", ""),
                json.dumps(
                    confirmation.get("organization_tags", []), ensure_ascii=False
                ),
            ),
        )
        normalized_tags: dict[str, str] = {}
        for value in payload.get("personal_tags", []):
            normalized_name, display_name = normalize_tag(value)
            normalized_tags.setdefault(normalized_name, display_name)
        for position, (normalized_name, display_name) in enumerate(
            normalized_tags.items()
        ):
            db.execute(
                """INSERT INTO collection_personal_tags(
                    collection_item_id, normalized_name, display_name,
                    position, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (item_id, normalized_name, display_name, position, created_at),
            )
        inspiration = payload.get("inspiration")
        if inspiration is not None:
            db.execute(
                """INSERT INTO inspirations(
                    id, collection_item_id, content, input_mode,
                    transcription_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, f"collection-inspiration:{item_id}"
                        )
                    ),
                    item_id,
                    inspiration["content"],
                    inspiration["input_mode"],
                    inspiration["transcription_status"],
                    inspiration.get("created_at", created_at),
                    inspiration.get("updated_at", updated_at),
                ),
            )
        aliases = {identity_url}
        if identity_url.startswith(("http://", "https://")):
            for candidate in (payload.get("source_url"), payload.get("canonical_url")):
                if candidate:
                    aliases.add(normalize_identity_url(candidate))
        for alias in sorted(aliases):
            db.execute(
                """INSERT INTO collection_url_aliases(
                    alias_url, collection_item_id, created_at
                ) VALUES (?, ?, ?)""",
                (alias, item_id, created_at),
            )

    @staticmethod
    def _collection_item_on_connection(
        db: sqlite3.Connection, collection_item_id: str
    ) -> dict | None:
        item = db.execute(
            "SELECT * FROM library_items WHERE id=?", (collection_item_id,)
        ).fetchone()
        if item is None:
            return None
        metadata = db.execute(
            "SELECT * FROM source_metadata WHERE collection_item_id=?",
            (collection_item_id,),
        ).fetchone()
        suggestion = db.execute(
            "SELECT * FROM organization_suggestions WHERE collection_item_id=?",
            (collection_item_id,),
        ).fetchone()
        confirmation = db.execute(
            "SELECT * FROM organization_confirmations WHERE collection_item_id=?",
            (collection_item_id,),
        ).fetchone()
        inspiration = db.execute(
            "SELECT * FROM inspirations WHERE collection_item_id=?",
            (collection_item_id,),
        ).fetchone()
        tag_rows = db.execute(
            """SELECT display_name FROM collection_personal_tags
               WHERE collection_item_id=? ORDER BY position ASC""",
            (collection_item_id,),
        ).fetchall()
        title_value = (
            metadata["title_value"].strip()
            if metadata["title_source"] != "none"
            else ""
        )
        display_title = (
            (item["user_title"] or "").strip()
            or title_value
            or "未命名收藏"
        )
        return {
            "id": item["id"],
            "source_kind": item["source_kind"],
            "platform": item["platform"],
            "original_input": item["original_input"],
            "source_url": item["source_url"],
            "canonical_url": item["canonical_url"],
            "identity_url": item["identity_url"],
            "metadata_status": item["metadata_status"],
            "user_title": item["user_title"],
            "user_author": item["user_author"],
            "user_cover_asset_id": (
                db.execute(
                    "SELECT asset_id FROM collection_user_covers WHERE collection_item_id=?",
                    (collection_item_id,),
                ).fetchone() or {"asset_id": None}
            )["asset_id"],
            "display_title": display_title,
            "metadata": {
                "title": {
                    "value": metadata["title_value"],
                    "source": metadata["title_source"],
                    "fetched_at": metadata["title_fetched_at"],
                },
                "author": {
                    "value": metadata["author_value"],
                    "source": metadata["author_source"],
                    "fetched_at": metadata["author_fetched_at"],
                },
                "cover_url": {
                    "value": metadata["cover_url_value"],
                    "source": metadata["cover_url_source"],
                    "fetched_at": metadata["cover_url_fetched_at"],
                },
                "source_copy": {
                    "value": metadata["source_copy_value"],
                    "source": metadata["source_copy_source"],
                    "fetched_at": metadata["source_copy_fetched_at"],
                },
                "platform_tags": json.loads(metadata["platform_tags_json"]),
                "warnings": json.loads(metadata["warnings_json"]),
            },
            "organization_suggestion": {
                "primary_category": suggestion["primary_category"],
                "secondary_category": suggestion["secondary_category"],
                "tags": json.loads(suggestion["tags_json"]),
                "basis": suggestion["basis"],
                "method": suggestion["method"],
                "status": suggestion["status"],
            },
            "organization_confirmation": {
                "primary_category": confirmation["primary_category"],
                "secondary_category": confirmation["secondary_category"],
                "organization_tags": json.loads(
                    confirmation["organization_tags_json"]
                ),
            },
            "personal_tags": [row["display_name"] for row in tag_rows],
            "selected_source_topic_indices": (
                json.loads(item["selected_source_topic_indices_json"])
                if item["selected_source_topic_indices_json"] is not None
                else None
            ),
            "inspiration": None
            if inspiration is None
            else {
                "id": inspiration["id"],
                "collection_item_id": inspiration["collection_item_id"],
                "content": inspiration["content"],
                "input_mode": inspiration["input_mode"],
                "transcription_status": inspiration["transcription_status"],
                "created_at": inspiration["created_at"],
                "updated_at": inspiration["updated_at"],
            },
            "deep_analysis_resource_key": item["deep_analysis_resource_key"],
            "created_at": item["created_at"],
            "revision": item["revision"],
            "updated_at": item["updated_at"],
        }

    def get_collection_metadata_cache(self, alias_url: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT c.payload_json FROM collection_metadata_cache_aliases a
                   JOIN collection_metadata_cache c ON c.identity_url=a.identity_url
                   WHERE a.alias_url=?""",
                (alias_url,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def upsert_collection_metadata_cache(
        self, payload: dict, aliases: list[str]
    ) -> None:
        identity_url = payload["identity_url"]
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO collection_metadata_cache(
                       identity_url, payload_json, fetched_at, expires_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(identity_url) DO UPDATE SET
                       payload_json=excluded.payload_json,
                       fetched_at=excluded.fetched_at,
                       expires_at=excluded.expires_at,
                       updated_at=excluded.updated_at""",
                (
                    identity_url,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    payload["fetched_at"],
                    payload["expires_at"],
                    now,
                ),
            )
            for alias in dict.fromkeys([identity_url, *aliases]):
                db.execute(
                    """INSERT INTO collection_metadata_cache_aliases(
                           alias_url, identity_url, created_at
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(alias_url) DO UPDATE SET
                           identity_url=excluded.identity_url""",
                    (alias, identity_url, now),
                )

    def create_collection_preview(self, payload: dict) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO collection_previews(
                       preview_id, payload_json, created_at, expires_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    payload["preview_id"],
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    payload["created_at"],
                    payload["expires_at"],
                ),
            )

    def get_collection_preview(self, preview_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload_json FROM collection_previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def replay_collection_item(
        self, idempotency_key: str, request_hash: str
    ) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT request_hash, response_json
                   FROM collection_idempotency_keys WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyKeyReusedError(idempotency_key)
        return json.loads(row["response_json"])

    def create_collection_item(
        self,
        payload: dict,
        idempotency_key: str,
        request_hash: str,
        user_cover_claim_hash: str | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                """SELECT request_hash, response_json
                   FROM collection_idempotency_keys WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["request_hash"] != request_hash:
                    raise IdempotencyKeyReusedError(idempotency_key)
                return json.loads(prior["response_json"])
            identity_url = normalize_identity_url(
                payload.get("canonical_url") or payload["source_url"]
            )
            aliases = {identity_url}
            for candidate in (payload.get("source_url"), payload.get("canonical_url")):
                if candidate:
                    aliases.add(normalize_identity_url(candidate))
            placeholders = ",".join("?" for _ in aliases)
            existing = db.execute(
                f"""SELECT id FROM library_items WHERE identity_url=?
                    UNION SELECT collection_item_id FROM collection_url_aliases
                    WHERE alias_url IN ({placeholders}) LIMIT 1""",
                (identity_url, *sorted(aliases)),
            ).fetchone()
            if existing is not None:
                raise CollectionExistsError(existing["id"])
            cover_asset_id = payload.get("user_cover_asset_id")
            if cover_asset_id is not None:
                self._validate_user_cover_claim(
                    db, cover_asset_id, user_cover_claim_hash, now
                )
            item_id = str(uuid.uuid4())
            self._insert_collection_graph(
                db, item_id, payload, identity_url, now, now
            )
            if "selected_source_topic_indices" in payload:
                db.execute(
                    "UPDATE library_items SET selected_source_topic_indices_json=? WHERE id=?",
                    (json.dumps(payload["selected_source_topic_indices"]), item_id),
                )
            if cover_asset_id is not None:
                db.execute(
                    """INSERT INTO collection_user_covers(
                           collection_item_id, asset_id, created_at
                       ) VALUES (?, ?, ?)""",
                    (item_id, cover_asset_id, now),
                )
                db.execute(
                    "UPDATE user_cover_assets SET expires_at=NULL WHERE asset_id=?",
                    (cover_asset_id,),
                )
            refresh_collection_document(db, item_id)
            response = self._collection_item_on_connection(db, item_id)
            assert response is not None
            db.execute(
                """INSERT INTO collection_idempotency_keys(
                    idempotency_key, request_hash, collection_item_id,
                    response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    request_hash,
                    item_id,
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            return response

    def get_collection_item(self, collection_item_id: str) -> dict | None:
        with self._connect() as db:
            return self._collection_item_on_connection(db, collection_item_id)

    @staticmethod
    def _update_collection_root(
        db: sqlite3.Connection,
        collection_item_id: str,
        expected_revision: int,
        user_title: str | None,
        user_author: str | None,
        updated_at: str,
    ) -> None:
        cursor = db.execute(
            """UPDATE library_items
               SET user_title=?, user_author=?, revision=revision+1, updated_at=?
               WHERE id=? AND revision=?""",
            (user_title, user_author, updated_at, collection_item_id, expected_revision),
        )
        if cursor.rowcount != 1:
            row = db.execute(
                "SELECT revision FROM library_items WHERE id=?",
                (collection_item_id,),
            ).fetchone()
            current_revision = expected_revision if row is None else row["revision"]
            raise CollectionRevisionConflictError(
                collection_item_id,
                expected_revision,
                current_revision,
            )

    @staticmethod
    def _replace_collection_confirmation(
        db: sqlite3.Connection,
        collection_item_id: str,
        current: dict,
        desired: dict,
    ) -> None:
        if current == desired:
            return
        cursor = db.execute(
            """UPDATE organization_confirmations
               SET primary_category=?, secondary_category=?,
                   organization_tags_json=?
               WHERE collection_item_id=?""",
            (
                desired["primary_category"],
                desired["secondary_category"],
                json.dumps(desired["organization_tags"], ensure_ascii=False),
                collection_item_id,
            ),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("collection confirmation is missing")

    @staticmethod
    def _replace_collection_personal_tags(
        db: sqlite3.Connection,
        collection_item_id: str,
        current: list[str],
        desired: list[str],
        updated_at: str,
    ) -> None:
        if current == desired:
            return
        db.execute(
            "DELETE FROM collection_personal_tags WHERE collection_item_id=?",
            (collection_item_id,),
        )
        for position, value in enumerate(desired):
            normalized_name, display_name = normalize_tag(value)
            db.execute(
                """INSERT INTO collection_personal_tags(
                       collection_item_id, normalized_name, display_name,
                       position, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    collection_item_id,
                    normalized_name,
                    display_name,
                    position,
                    updated_at,
                ),
            )

    @staticmethod
    def _replace_collection_inspiration(
        db: sqlite3.Connection,
        collection_item_id: str,
        current: dict | None,
        desired: dict | None,
        updated_at: str,
    ) -> None:
        def editable(value: dict | None) -> dict | None:
            if value is None:
                return None
            return {
                "content": value["content"],
                "input_mode": value["input_mode"],
                "transcription_status": value["transcription_status"],
            }

        if editable(current) == desired:
            return
        if desired is None:
            db.execute(
                "DELETE FROM inspirations WHERE collection_item_id=?",
                (collection_item_id,),
            )
            return
        if current is None:
            db.execute(
                """INSERT INTO inspirations(
                       id, collection_item_id, content, input_mode,
                       transcription_status, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    collection_item_id,
                    desired["content"],
                    desired["input_mode"],
                    desired["transcription_status"],
                    updated_at,
                    updated_at,
                ),
            )
            return
        db.execute(
            """UPDATE inspirations
               SET content=?, input_mode=?, transcription_status=?, updated_at=?
               WHERE collection_item_id=?""",
            (
                desired["content"],
                desired["input_mode"],
                desired["transcription_status"],
                updated_at,
                collection_item_id,
            ),
        )

    def update_collection_item_user_fields(
        self,
        collection_item_id: str,
        expected_revision: int,
        payload: dict,
        user_cover_claim_hash: str | None = None,
    ) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._collection_item_on_connection(db, collection_item_id)
            if current is None:
                return None
            if current["revision"] != expected_revision:
                raise CollectionRevisionConflictError(
                    collection_item_id,
                    expected_revision,
                    current["revision"],
                )
            current_inspiration = current["inspiration"]
            current_inspiration_fields = (
                None
                if current_inspiration is None
                else {
                    "content": current_inspiration["content"],
                    "input_mode": current_inspiration["input_mode"],
                    "transcription_status": current_inspiration[
                        "transcription_status"
                    ],
                }
            )
            desired_user_author = payload.get("user_author", current["user_author"])
            desired_user_cover = payload.get(
                "user_cover_asset_id", current["user_cover_asset_id"]
            )
            if (
                current["user_title"] == payload["user_title"]
                and current["user_author"] == desired_user_author
                and current["user_cover_asset_id"] == desired_user_cover
                and current["organization_confirmation"]
                == payload["organization_confirmation"]
                and current["personal_tags"] == payload["personal_tags"]
                and current_inspiration_fields == payload["inspiration"]
            ):
                return current

            updated_at = datetime.now(UTC).isoformat()
            if (
                desired_user_cover is not None
                and desired_user_cover != current["user_cover_asset_id"]
            ):
                self._validate_user_cover_claim(
                    db, desired_user_cover, user_cover_claim_hash, updated_at
                )
            self._update_collection_root(
                db,
                collection_item_id,
                expected_revision,
                payload["user_title"],
                desired_user_author,
                updated_at,
            )
            self._replace_collection_confirmation(
                db,
                collection_item_id,
                current["organization_confirmation"],
                payload["organization_confirmation"],
            )
            self._replace_collection_personal_tags(
                db,
                collection_item_id,
                current["personal_tags"],
                payload["personal_tags"],
                updated_at,
            )
            self._replace_collection_inspiration(
                db,
                collection_item_id,
                current_inspiration,
                payload["inspiration"],
                updated_at,
            )
            if desired_user_cover != current["user_cover_asset_id"]:
                old_asset_id = current["user_cover_asset_id"]
                db.execute(
                    "DELETE FROM collection_user_covers WHERE collection_item_id=?",
                    (collection_item_id,),
                )
                if desired_user_cover is not None:
                    db.execute(
                        """INSERT INTO collection_user_covers(
                               collection_item_id, asset_id, created_at
                           ) VALUES (?, ?, ?)""",
                        (collection_item_id, desired_user_cover, updated_at),
                    )
                    db.execute(
                        "UPDATE user_cover_assets SET expires_at=NULL WHERE asset_id=?",
                        (desired_user_cover,),
                    )
                if old_asset_id is not None:
                    expires_at = (
                        datetime.fromisoformat(updated_at) + timedelta(hours=24)
                    ).isoformat()
                    db.execute(
                        """UPDATE user_cover_assets SET expires_at=?
                           WHERE asset_id=? AND NOT EXISTS(
                               SELECT 1 FROM collection_user_covers WHERE asset_id=?
                           )""",
                        (expires_at, old_asset_id, old_asset_id),
                    )
            refresh_collection_document(db, collection_item_id)
            stored = self._collection_item_on_connection(db, collection_item_id)
            if stored is None:
                raise sqlite3.IntegrityError("collection disappeared during update")
            return stored

    @staticmethod
    def _validate_user_cover_claim(
        db: sqlite3.Connection,
        asset_id: str,
        claim_hash: str | None,
        now: str,
    ) -> None:
        if not claim_hash:
            raise ValueError("user cover claim is required")
        row = db.execute(
            """SELECT claim_token_hash, expires_at FROM user_cover_assets
               WHERE asset_id=?""",
            (asset_id,),
        ).fetchone()
        if (
            row is None
            or row["claim_token_hash"] != claim_hash
            or (row["expires_at"] is not None and row["expires_at"] <= now)
        ):
            raise ValueError("user cover claim is invalid")

    def create_user_cover_asset(self, record: dict) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO user_cover_assets(
                       asset_id, storage_name, media_type, byte_size,
                       content_sha256, width, height, claim_token_hash,
                       created_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["asset_id"], record["storage_name"], record["media_type"],
                    record["byte_size"], record["content_sha256"], record["width"],
                    record["height"], record["claim_token_hash"],
                    record["created_at"], record["expires_at"],
                ),
            )

    def get_user_cover_asset(self, asset_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM user_cover_assets WHERE asset_id=?", (asset_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_user_cover_asset_for_item(self, item_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT a.* FROM collection_user_covers c
                   JOIN user_cover_assets a ON a.asset_id=c.asset_id
                   WHERE c.collection_item_id=?""",
                (item_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_unbound_user_cover_asset(
        self, asset_id: str, claim_token_hash: str
    ) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM user_cover_assets a WHERE asset_id=?
                   AND claim_token_hash=? AND NOT EXISTS(
                       SELECT 1 FROM collection_user_covers c WHERE c.asset_id=a.asset_id
                   )""",
                (asset_id, claim_token_hash),
            ).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM user_cover_assets WHERE asset_id=?", (asset_id,))
            return dict(row)

    def expired_unbound_user_cover_assets(self, now: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.* FROM user_cover_assets a
                   WHERE a.expires_at IS NOT NULL AND a.expires_at<=?
                   AND NOT EXISTS(
                       SELECT 1 FROM collection_user_covers c WHERE c.asset_id=a.asset_id
                   ) ORDER BY a.expires_at, a.asset_id LIMIT 256""",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_expired_user_cover_asset(
        self, asset_id: str, now: str
    ) -> dict | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM user_cover_assets a WHERE asset_id=?
                   AND expires_at IS NOT NULL AND expires_at<=?
                   AND NOT EXISTS(
                       SELECT 1 FROM collection_user_covers c
                       WHERE c.asset_id=a.asset_id
                   )""",
                (asset_id, now),
            ).fetchone()
            if row is None:
                return None
            cursor = db.execute(
                """DELETE FROM user_cover_assets WHERE asset_id=?
                   AND expires_at IS NOT NULL AND expires_at<=?
                   AND NOT EXISTS(
                       SELECT 1 FROM collection_user_covers c
                       WHERE c.asset_id=user_cover_assets.asset_id
                   )""",
                (asset_id, now),
            )
            if cursor.rowcount != 1:
                return None
            return dict(row)

    def search_collection_items(
        self,
        *,
        query: str,
        platform: str | None,
        primary_category: str,
        secondary_category: str,
        tag: str,
        tag_source: str | None,
        limit: int,
        after: tuple[str, str] | None,
    ) -> dict:
        with self._connect() as db:
            # Page, total and facets must observe the same committed snapshot.
            db.execute("BEGIN")
            return search_indexed_collection_items(
                db, query=query, platform=platform,
                primary_category=primary_category, secondary_category=secondary_category,
                tag=tag, tag_source=tag_source, limit=limit, after=after,
            )

    def save_job(self, job: Job) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            existing = db.execute(
                "SELECT status, progress FROM jobs WHERE id=?", (job.id,)
            ).fetchone()
            if (
                existing is not None
                and job.status not in {"failed", "cancelled"}
                and job.progress < existing["progress"]
            ):
                return False
            if existing is not None and job.status != existing["status"]:
                allowed = (
                    job.status in {"failed", "cancelled"}
                    or job.status
                    in _JOB_TRANSITIONS.get(existing["status"], set())
                )
                if not allowed:
                    return False
            cursor = db.execute(
                """INSERT INTO jobs
                (id, status, progress, error_code, message, video_id,
                 created_at, updated_at, retry_count, max_retries, retryable,
                 cancel_requested, warnings_json, checkpoint, heartbeat_at,
                 revision, lease_owner, lease_expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                status=CASE
                    WHEN excluded.status IN ('failed','cancelled')
                      OR excluded.progress >= jobs.progress
                    THEN excluded.status ELSE jobs.status END,
                progress=CASE WHEN excluded.progress > jobs.progress
                    THEN excluded.progress ELSE jobs.progress END,
                error_code=CASE
                    WHEN excluded.status IN ('failed','cancelled')
                      OR excluded.progress >= jobs.progress
                    THEN excluded.error_code ELSE jobs.error_code END,
                message=CASE
                    WHEN excluded.status IN ('failed','cancelled')
                      OR excluded.progress >= jobs.progress
                    THEN excluded.message ELSE jobs.message END,
                video_id=COALESCE(excluded.video_id, jobs.video_id),
                updated_at=excluded.updated_at,
                retry_count=excluded.retry_count,
                max_retries=excluded.max_retries,
                retryable=excluded.retryable,
                cancel_requested=excluded.cancel_requested,
                warnings_json=excluded.warnings_json,
                checkpoint=excluded.checkpoint,
                heartbeat_at=excluded.heartbeat_at,
                revision=jobs.revision+1,
                lease_owner=excluded.lease_owner,
                lease_expires_at=excluded.lease_expires_at
                WHERE jobs.revision=excluded.revision
                  AND (jobs.lease_owner='' OR
                       jobs.lease_owner=excluded.lease_owner)
                  AND (
                    jobs.status NOT IN
                      ('completed','completed_with_warnings','failed','cancelled')
                    OR jobs.status=excluded.status
                  )""",
                (
                    job.id, job.status, job.progress, job.error_code, job.message,
                    job.video_id, now, now, job.retry_count, job.max_retries,
                    int(job.retryable), int(job.cancel_requested),
                    json.dumps(job.warnings, ensure_ascii=False),
                    job.checkpoint, now, job.revision, job.lease_owner,
                    job.lease_expires_at,
                ),
            )
            row = (
                db.execute(
                    "SELECT revision FROM jobs WHERE id=?", (job.id,)
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            if row:
                job.revision = row["revision"]
            return cursor.rowcount == 1

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], status=row["status"], progress=row["progress"],
            error_code=row["error_code"], message=row["message"],
            video_id=row["video_id"], retry_count=row["retry_count"],
            max_retries=row["max_retries"], retryable=bool(row["retryable"]),
            cancel_requested=bool(row["cancel_requested"]),
            warnings=json.loads(row["warnings_json"]),
            checkpoint=row["checkpoint"], heartbeat_at=row["heartbeat_at"],
            revision=row["revision"], lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
        )

    def get_job(self, job_id: str) -> Job | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else self._job_from_row(row)

    def get_collection_deep_job(
        self, collection_item_id: str, config_version: str
    ) -> Job | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT j.* FROM collection_deep_analysis_jobs d
                JOIN jobs j ON j.id=d.job_id
                WHERE d.collection_item_id=? AND d.config_version=?""",
                (collection_item_id, config_version),
            ).fetchone()
        return None if row is None else self._job_from_row(row)

    def get_or_create_collection_deep_job(
        self,
        collection_item_id: str,
        config_version: str,
        task_key: str,
        source_url: str,
    ) -> tuple[Job, bool]:
        """Atomically claim the one logical deep-analysis job for a material."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute(
                "SELECT 1 FROM library_items WHERE id=?", (collection_item_id,)
            ).fetchone()
            if item is None:
                raise LookupError(collection_item_id)
            existing = db.execute(
                """SELECT j.* FROM collection_deep_analysis_jobs d
                JOIN jobs j ON j.id=d.job_id
                WHERE d.collection_item_id=? AND d.config_version=?""",
                (collection_item_id, config_version),
            ).fetchone()
            if existing is not None:
                return self._job_from_row(existing), False
            job = Job(id=str(uuid.uuid4()), status="queued", progress=0)
            db.execute(
                """INSERT INTO jobs
                (id, status, progress, error_code, message, video_id,
                 created_at, updated_at, retry_count, max_retries, retryable,
                 cancel_requested, request_kind, request_json, warnings_json,
                 checkpoint, heartbeat_at, revision, lease_owner,
                 lease_expires_at)
                VALUES (?, 'queued', 0, NULL, '', NULL, ?, ?, 0, 2, 0, 0,
                        'resolution', ?, '[]', 'none', ?, 0, '', '')""",
                (
                    job.id,
                    now,
                    now,
                    json.dumps(
                        {
                            "source_url": source_url,
                            "focused": False,
                            "focus_query_hash": "",
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
            db.execute(
                """INSERT INTO collection_deep_analysis_jobs
                (collection_item_id, config_version, task_key, job_id,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (collection_item_id, config_version, task_key, job.id, now, now),
            )
            return job, True

    def claim_collection_deep_retry(
        self,
        collection_item_id: str,
        config_version: str,
        attempt_key: str,
        lease_owner: str,
    ) -> tuple[Job, bool]:
        """Claim one revision-bound retry and replay the same attempt key."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute(
                """SELECT j.* FROM collection_deep_retry_attempts a
                JOIN jobs j ON j.id=a.job_id
                WHERE a.attempt_key=? AND a.collection_item_id=?""",
                (attempt_key, collection_item_id),
            ).fetchone()
            if replay is not None:
                return self._job_from_row(replay), False
            row = db.execute(
                """SELECT j.* FROM collection_deep_analysis_jobs d
                JOIN jobs j ON j.id=d.job_id
                WHERE d.collection_item_id=? AND d.config_version=?""",
                (collection_item_id, config_version),
            ).fetchone()
            if row is None:
                raise LookupError(collection_item_id)
            if (
                row["status"] != "failed"
                or not row["retryable"]
                or row["retry_count"] >= row["max_retries"]
            ):
                raise ValueError("retry not allowed")
            cursor = db.execute(
                """UPDATE jobs SET retry_count=retry_count+1, status='queued',
                progress=0, error_code=NULL, message='', retryable=0,
                cancel_requested=0, updated_at=?, heartbeat_at=?,
                revision=revision+1, lease_owner=?
                WHERE id=? AND status='failed' AND retryable=1
                  AND retry_count < max_retries AND revision=?""",
                (now, now, lease_owner, row["id"], row["revision"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("retry already claimed")
            db.execute(
                """INSERT INTO collection_deep_retry_attempts
                (attempt_key, collection_item_id, job_id, failed_revision,
                 failed_stage, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    attempt_key,
                    collection_item_id,
                    row["id"],
                    row["revision"],
                    row["checkpoint"],
                    now,
                ),
            )
            claimed = db.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()
            return self._job_from_row(claimed), True

    def attach_collection_deep_result(
        self, collection_item_id: str, job_id: str, result_resource_key: str
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            linked = db.execute(
                """SELECT 1 FROM collection_deep_analysis_jobs
                WHERE collection_item_id=? AND job_id=?""",
                (collection_item_id, job_id),
            ).fetchone()
            result = db.execute(
                "SELECT 1 FROM videos WHERE resource_key=?", (result_resource_key,)
            ).fetchone()
            if linked is None or result is None:
                return False
            cursor = db.execute(
                """UPDATE library_items
                SET deep_analysis_resource_key=?, updated_at=? WHERE id=?""",
                (result_resource_key, now, collection_item_id),
            )
            db.execute(
                """UPDATE collection_deep_analysis_jobs SET updated_at=?
                WHERE collection_item_id=? AND job_id=?""",
                (now, collection_item_id, job_id),
            )
            return cursor.rowcount == 1

    def get_deep_result_summary(self, result_resource_key: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT resource_key, subtitle_source, clean_transcript,
                          warnings_json, updated_at
                FROM videos WHERE resource_key=?""",
                (result_resource_key,),
            ).fetchone()
        if row is None:
            return None
        warnings = json.loads(row["warnings_json"])
        limited = row["subtitle_source"] == "none" or not row["clean_transcript"].strip()
        return {
            "resource_key": row["resource_key"],
            "limited": limited,
            "warnings": warnings,
            "updated_at": row["updated_at"],
        }

    def create_job_batch(
        self,
        batch_id: str,
        items: list[tuple[str, str, str]],
    ) -> JobBatch:
        """Atomically persist a batch over jobs that already exist."""
        now = datetime.now(UTC).isoformat()
        job_ids = [item[0] for item in items]
        placeholders = ",".join("?" for _ in job_ids)
        with self._connect() as db:
            # Lock the write boundary before validating eligibility. This keeps
            # a worker from moving a selected job to a terminal state between
            # validation and membership insertion.
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                f"""SELECT id, status, retryable, retry_count, max_retries,
                cancel_requested FROM jobs WHERE id IN ({placeholders})""",
                job_ids,
            ).fetchall()
            existing = {row["id"] for row in rows}
            missing = [job_id for job_id in job_ids if job_id not in existing]
            if missing:
                raise BatchJobsNotFoundError(missing)
            disallowed = [
                row["id"]
                for row in rows
                if bool(row["cancel_requested"])
                or (
                    row["status"] in _JOB_TERMINAL
                    and not (
                        row["status"] == "failed"
                        and bool(row["retryable"])
                        and row["retry_count"] < row["max_retries"]
                    )
                )
            ]
            if disallowed:
                raise BatchJobsNotAllowedError(disallowed)
            db.execute(
                "INSERT INTO job_batches(id, created_at) VALUES (?, ?)",
                (batch_id, now),
            )
            db.executemany(
                """INSERT INTO job_batch_items
                (batch_id, job_id, position, title, platform, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (batch_id, job_id, position, title, platform, now)
                    for position, (job_id, title, platform) in enumerate(items)
                ],
            )
        batch = self.get_job_batch(batch_id)
        if batch is None:
            raise RuntimeError("created job batch could not be loaded")
        return batch

    def get_job_batch(self, batch_id: str) -> JobBatch | None:
        with self._connect() as db:
            rows = db.execute(
                f"""{self._job_batch_select()}
                WHERE b.id=?
                ORDER BY i.position ASC""",
                (batch_id,),
            ).fetchall()
        batches = self._job_batches_from_rows(rows)
        return batches[0] if batches else None

    def list_job_batches(self, limit: int = 10) -> list[JobBatch]:
        with self._connect() as db:
            rows = db.execute(
                f"""WITH selected_batches AS (
                    SELECT id, created_at FROM job_batches
                    ORDER BY created_at DESC, id ASC
                    LIMIT ?
                )
                {self._job_batch_select(batch_source="selected_batches")}
                ORDER BY b.created_at DESC, b.id ASC, i.position ASC""",
                (limit,),
            ).fetchall()
        return self._job_batches_from_rows(rows)

    def job_belongs_to_batch(self, batch_id: str, job_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM job_batch_items
                WHERE batch_id=? AND job_id=?""",
                (batch_id, job_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _job_batch_select(batch_source: str = "job_batches") -> str:
        return f"""SELECT
            b.id AS batch_id,
            b.created_at AS batch_created_at,
            i.position AS item_position,
            i.title AS item_title,
            i.platform AS item_platform,
            j.id AS job_id,
            j.status AS job_status,
            j.progress AS job_progress,
            j.error_code AS job_error_code,
            j.message AS job_message,
            j.video_id AS job_video_id,
            j.retry_count AS job_retry_count,
            j.max_retries AS job_max_retries,
            j.retryable AS job_retryable,
            j.cancel_requested AS job_cancel_requested,
            j.warnings_json AS job_warnings_json,
            j.checkpoint AS job_checkpoint,
            j.heartbeat_at AS job_heartbeat_at,
            j.revision AS job_revision,
            j.lease_owner AS job_lease_owner,
            j.lease_expires_at AS job_lease_expires_at,
            j.updated_at AS job_updated_at
        FROM {batch_source} b
        JOIN job_batch_items i ON i.batch_id=b.id
        JOIN jobs j ON j.id=i.job_id"""

    @staticmethod
    def _job_batches_from_rows(rows: list[sqlite3.Row]) -> list[JobBatch]:
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["batch_id"], []).append(row)
        batches: list[JobBatch] = []
        for batch_rows in grouped.values():
            items = [
                JobBatchItem(
                    position=row["item_position"],
                    title=row["item_title"],
                    platform=row["item_platform"],
                    job=Job(
                        id=row["job_id"],
                        status=row["job_status"],
                        progress=row["job_progress"],
                        error_code=row["job_error_code"],
                        message=row["job_message"],
                        video_id=row["job_video_id"],
                        retry_count=row["job_retry_count"],
                        max_retries=row["job_max_retries"],
                        retryable=bool(row["job_retryable"]),
                        cancel_requested=bool(row["job_cancel_requested"]),
                        warnings=json.loads(row["job_warnings_json"]),
                        checkpoint=row["job_checkpoint"],
                        heartbeat_at=row["job_heartbeat_at"],
                        revision=row["job_revision"],
                        lease_owner=row["job_lease_owner"],
                        lease_expires_at=row["job_lease_expires_at"],
                    ),
                )
                for row in batch_rows
            ]
            statuses = [item.job.status for item in items]
            completed = sum(
                status in {"completed", "completed_with_warnings"}
                for status in statuses
            )
            failed = statuses.count("failed")
            cancelled = statuses.count("cancelled")
            active = len(statuses) - completed - failed - cancelled
            if all(status == "queued" for status in statuses):
                batch_status = "queued"
            elif active:
                batch_status = "running"
            elif any(
                status in {"completed_with_warnings", "failed", "cancelled"}
                for status in statuses
            ):
                batch_status = "completed_with_issues"
            else:
                batch_status = "completed"
            batches.append(
                JobBatch(
                    id=batch_rows[0]["batch_id"],
                    status=batch_status,
                    total=len(items),
                    completed=completed,
                    active=active,
                    failed=failed,
                    cancelled=cancelled,
                    created_at=batch_rows[0]["batch_created_at"],
                    updated_at=max(
                        batch_rows[0]["batch_created_at"],
                        *(row["job_updated_at"] for row in batch_rows),
                    ),
                    items=items,
                )
            )
        return batches

    def configure_job_request(
        self, job_id: str, request_kind: str, payload: dict
    ) -> None:
        if request_kind not in {"none", "resolution", "local_upload"}:
            raise ValueError("invalid request kind")
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET request_kind=?, request_json=? WHERE id=?",
                (
                    request_kind,
                    json.dumps(payload, ensure_ascii=False),
                    job_id,
                ),
            )

    def get_job_request(self, job_id: str) -> tuple[str, dict] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT request_kind, request_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return row["request_kind"], json.loads(row["request_json"])

    def request_job_cancel(self, job_id: str) -> Job | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] not in _JOB_TERMINAL:
                db.execute(
                    """UPDATE jobs SET status='cancelled', error_code='CANCELLED',
                    message='任务已取消。', retryable=0, cancel_requested=1,
                    updated_at=?, heartbeat_at=?, revision=revision+1
                    WHERE id=? AND status NOT IN
                    ('completed','completed_with_warnings','failed','cancelled')""",
                    (now, now, job_id),
                )
        return self.get_job(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bool(row["cancel_requested"]) if row else False

    def recover_incomplete_jobs(self, stale_before: str) -> list[Job]:
        terminal = ("completed", "completed_with_warnings", "failed", "cancelled")
        with self._connect() as db:
            rows = db.execute(
                """SELECT id FROM jobs
                WHERE status NOT IN (?, ?, ?, ?)
                  AND (heartbeat_at='' OR heartbeat_at<=?)""",
                (*terminal, stale_before),
            ).fetchall()
        recovered = []
        for row in rows:
            job = self.get_job(row["id"])
            if job is None:
                continue
            request = self.get_job_request(job.id)
            job.status = "failed"
            job.error_code = "PROCESS_INTERRUPTED"
            job.message = "服务曾在任务处理中退出；可按提示安全重试。"
            job.retryable = (
                request is not None
                and request[0] != "none"
                and job.retry_count < job.max_retries
            )
            if self.save_job(job):
                recovered.append(job)
        return recovered

    def prepare_job_retry(self, job_id: str, lease_owner: str) -> Job | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE jobs SET retry_count=retry_count+1, status='queued',
                progress=0, error_code=NULL, message='', retryable=0,
                cancel_requested=0, updated_at=?, heartbeat_at=?,
                revision=revision+1, lease_owner=?
                WHERE id=? AND status='failed' AND retryable=1
                  AND retry_count < max_retries""",
                (now, now, lease_owner, job_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_job(job_id)

    def add_system_warning(self, code: str, message: str) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO system_warnings(code, message, created_at)
                VALUES (?, ?, ?)""",
                (code, message, datetime.now(UTC).isoformat()),
            )

    def save_job_artifact(self, job_id: str, stage: str, payload: dict) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO job_artifacts(job_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage,
                payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                (
                    job_id,
                    stage,
                    json.dumps(payload, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get_job_artifact(self, job_id: str) -> tuple[str, dict] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT stage, payload_json FROM job_artifacts WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return None if row is None else (
            row["stage"],
            json.loads(row["payload_json"]),
        )

    def delete_job_artifact(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM job_artifacts WHERE job_id=?", (job_id,))

    def has_video(self, video_id: str, platform: str | None = None) -> bool:
        return self.get_result(video_id, platform=platform) is not None

    def save_video_alias(self, source_url: str, platform: str, video_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        key = resource_key(platform, video_id)
        with self._connect() as db:
            db.execute(
                """INSERT INTO video_aliases
                (source_url, platform, resource_key, video_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_url) DO UPDATE SET
                platform=excluded.platform, resource_key=excluded.resource_key,
                video_id=excluded.video_id""",
                (source_url, platform, key, video_id, now),
            )

    def load_video_alias(self, source_url: str) -> tuple[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT platform, video_id FROM video_aliases WHERE source_url=?",
                (source_url,),
            ).fetchone()
        return None if row is None else (row["platform"], row["video_id"])

    def save_result(self, result: VideoResult) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            self._save_result(db, result, now)

    @staticmethod
    def _save_result(
        db: sqlite3.Connection,
        result: VideoResult,
        now: str,
    ) -> None:
        payload = result.as_dict()
        key = resource_key(result.platform, result.video_id)
        if db is not None:
            db.execute(
                """INSERT INTO videos
                (resource_key, platform, public_video_id, video_id, source_url, title,
                 metadata_json, subtitle_source, raw_transcript, clean_transcript,
                 warnings_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                source_url=excluded.source_url,
                title=excluded.title,
                metadata_json=excluded.metadata_json,
                subtitle_source=excluded.subtitle_source,
                raw_transcript=excluded.raw_transcript,
                clean_transcript=excluded.clean_transcript,
                warnings_json=excluded.warnings_json,
                status=excluded.status, updated_at=excluded.updated_at""",
                (
                    key, result.platform, result.video_id, result.video_id,
                    result.source_url, result.title,
                    json.dumps(payload, ensure_ascii=False), result.subtitle_source,
                    result.raw_transcript, result.clean_transcript,
                    json.dumps(result.warnings, ensure_ascii=False),
                    "completed_with_warnings" if result.warnings else "completed",
                    now,
                    now,
                ),
            )
            db.execute("DELETE FROM transcript_segments WHERE resource_key=?", (key,))
            db.executemany(
                """INSERT INTO transcript_segments
                (resource_key, video_id, position, segment_id, start_time, end_time, text)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (key, result.video_id, index, s.id, s.start, s.end, s.text)
                    for index, s in enumerate(result.segments)
                ],
            )
            SQLiteRepository._save_automatic_tagging_on_connection(
                db, key, result.automatic_tagging, now
            )
            db.execute(
                """INSERT INTO extractions
                (resource_key, video_id, mode, focus_query, query_hash,
                 result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_key, mode, query_hash)
                DO UPDATE SET result_json=excluded.result_json,
                query_hash=excluded.query_hash""",
                (
                    key, result.video_id, result.extraction_mode, result.focus_query,
                    focus_query_hash(result.focus_query),
                    json.dumps(payload, ensure_ascii=False), now,
                ),
            )
            extraction_row = db.execute(
                """SELECT id FROM extractions
                WHERE resource_key=? AND mode=? AND query_hash=?""",
                (key, result.extraction_mode, focus_query_hash(result.focus_query)),
            ).fetchone()
            extraction_id = int(extraction_row["id"])
            db.execute("DELETE FROM claims WHERE extraction_id=?", (extraction_id,))
            for evidence in result.evidence:
                db.execute(
                    """INSERT INTO claims
                    (id, extraction_id, claim, claim_type, evidence,
                     start_time, end_time, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence.id, extraction_id, evidence.claim, evidence.claim_type,
                        evidence.evidence, evidence.start_time, evidence.end_time,
                        evidence.confidence,
                    ),
                )
                db.executemany(
                    """INSERT INTO claim_evidence
                    (extraction_id, claim_id, resource_key, video_id, segment_id)
                    VALUES (?, ?, ?, ?, ?)""",
                    [
                        (extraction_id, evidence.id, key, result.video_id, segment_id)
                        for segment_id in evidence.segment_ids
                    ],
                )

    def commit_job_result(
        self,
        job: Job,
        result: VideoResult,
        terminal_status: str = "completed",
        focused_result: VideoResult | None = None,
        retained_media: RetainedMedia | None = None,
    ) -> bool:
        """Persist the result and terminal job status in one transaction."""
        if terminal_status not in {"completed", "completed_with_warnings"}:
            raise ValueError("invalid terminal status")
        now = datetime.now(UTC).isoformat()
        try:
            with self._connect() as db:
                current = db.execute(
                    """SELECT status, revision, lease_owner, cancel_requested
                    FROM jobs WHERE id=?""",
                    (job.id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "saving"
                    or bool(current["cancel_requested"])
                    or current["revision"] != job.revision
                    or (
                        current["lease_owner"]
                        and current["lease_owner"] != job.lease_owner
                    )
                ):
                    return False
                self._save_result(db, result, now)
                if retained_media is not None:
                    self._save_retained_media_on_connection(
                        db, retained_media, now
                    )
                if focused_result is not None:
                    if focused_result.focused_answer is None:
                        raise ValueError("focused result is missing focused_answer")
                    self._save_focused_on_connection(
                        db,
                        result.platform,
                        result.video_id,
                        focused_result.focus_query,
                        focused_result.focused_answer,
                        now,
                    )
                cursor = db.execute(
                    """UPDATE jobs SET status=?, progress=100, error_code=NULL,
                    message='', video_id=?, retryable=0,
                    checkpoint='result_saved', updated_at=?, heartbeat_at=?,
                    revision=revision+1
                    WHERE id=? AND status='saving' AND cancel_requested=0
                      AND revision=?
                      AND (lease_owner='' OR lease_owner=?)""",
                    (
                        terminal_status,
                        result.video_id,
                        now,
                        now,
                        job.id,
                        job.revision,
                        job.lease_owner,
                    ),
                )
                if cursor.rowcount != 1:
                    raise sqlite3.IntegrityError("job state changed during commit")
            job.status = terminal_status
            job.progress = 100
            job.error_code = None
            job.message = ""
            job.video_id = result.video_id
            job.retryable = False
            job.checkpoint = "result_saved"
            job.revision += 1
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def _save_retained_media_on_connection(
        db: sqlite3.Connection,
        media: RetainedMedia,
        now: str,
    ) -> None:
        if media.platform != "local_upload":
            raise ValueError("only local uploads may retain media")
        key = resource_key(media.platform, media.video_id)
        exists = db.execute(
            "SELECT 1 FROM videos WHERE resource_key=? AND platform='local_upload'",
            (key,),
        ).fetchone()
        if exists is None:
            raise LookupError(media.video_id)
        db.execute(
            """INSERT INTO retained_media
            (resource_key, storage_key, mime_type, size_bytes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_key) DO UPDATE SET
            storage_key=excluded.storage_key,
            mime_type=excluded.mime_type,
            size_bytes=excluded.size_bytes,
            updated_at=excluded.updated_at""",
            (
                key,
                media.storage_key,
                media.mime_type,
                media.size_bytes,
                media.created_at or now,
                now,
            ),
        )

    def save_retained_media(self, media: RetainedMedia) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            self._save_retained_media_on_connection(db, media, now)

    def get_retained_media(
        self, video_id: str, platform: str | None = None
    ) -> RetainedMedia | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT v.platform, v.public_video_id, r.storage_key,
                r.mime_type, r.size_bytes, r.created_at, r.updated_at
                FROM retained_media r
                JOIN videos v ON v.resource_key=r.resource_key
                WHERE r.resource_key=?""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return RetainedMedia(
            platform=row["platform"],
            video_id=row["public_video_id"],
            storage_key=row["storage_key"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def delete_retained_media(
        self, video_id: str, platform: str | None = None
    ) -> bool:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return False
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM retained_media WHERE resource_key=?", (key,)
            )
        return cursor.rowcount == 1

    @staticmethod
    def _save_focused_on_connection(
        db: sqlite3.Connection,
        platform: str,
        video_id: str,
        focus_query: str,
        answer: FocusedAnswer,
        now: str,
    ) -> None:
        key = resource_key(platform, video_id)
        query_hash = focus_query_hash(focus_query)
        payload = json.dumps(
            {"focused_answer": answer.model_dump(mode="json")},
            ensure_ascii=False,
        )
        cursor = db.execute(
            """INSERT INTO extractions
            (resource_key, video_id, mode, focus_query, query_hash,
             result_json, created_at)
            VALUES (?, ?, 'focused', ?, ?, ?, ?)
            ON CONFLICT(resource_key, mode, query_hash) DO NOTHING""",
            (key, video_id, focus_query, query_hash, payload, now),
        )
        if cursor.rowcount == 0:
            return
        row = db.execute(
            """SELECT id FROM extractions
            WHERE resource_key=? AND mode='focused' AND query_hash=?""",
            (key, query_hash),
        ).fetchone()
        extraction_id = int(row["id"])
        for evidence in answer.supporting_segments:
            db.execute(
                """INSERT INTO claims
                (id, extraction_id, claim, claim_type, evidence,
                 start_time, end_time, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.id, extraction_id, evidence.claim,
                    evidence.claim_type, evidence.evidence,
                    evidence.start_time, evidence.end_time,
                    evidence.confidence,
                ),
            )
            db.executemany(
                """INSERT INTO claim_evidence
                (extraction_id, claim_id, resource_key, video_id, segment_id)
                VALUES (?, ?, ?, ?, ?)""",
                [
                    (extraction_id, evidence.id, key, video_id, segment_id)
                    for segment_id in evidence.segment_ids
                ],
                )

    @staticmethod
    def _save_automatic_tagging_on_connection(
        db: sqlite3.Connection,
        key: str,
        tagging: AutomaticTagging,
        now: str,
    ) -> None:
        if tagging.status == "not_generated":
            return
        db.execute(
            """INSERT INTO automatic_tag_runs
            (resource_key, status, generator_id, generator_version,
             transcript_hash, generated_at, warning)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_key) DO UPDATE SET
            status=excluded.status,
            generator_id=excluded.generator_id,
            generator_version=excluded.generator_version,
            transcript_hash=excluded.transcript_hash,
            generated_at=excluded.generated_at,
            warning=excluded.warning""",
            (
                key,
                tagging.status,
                tagging.generator_id,
                tagging.generator_version,
                tagging.transcript_hash,
                tagging.generated_at,
                tagging.warning,
            ),
        )
        db.execute(
            "DELETE FROM video_automatic_tags WHERE resource_key=?", (key,)
        )
        for tag in tagging.tags:
            normalized_name, display_name = normalize_tag(tag.name)
            db.execute(
                """INSERT INTO automatic_tags
                (normalized_name, display_name, created_at) VALUES (?, ?, ?)
                ON CONFLICT(normalized_name) DO NOTHING""",
                (normalized_name, display_name, now),
            )
            db.execute(
                """INSERT INTO video_automatic_tags
                (resource_key, normalized_name, confidence, generation_method,
                 created_at) VALUES (?, ?, ?, ?, ?)""",
                (
                    key,
                    normalized_name,
                    tag.confidence,
                    tag.generation_method,
                    now,
                ),
            )
        db.execute(
            """DELETE FROM automatic_tags WHERE NOT EXISTS (
            SELECT 1 FROM video_automatic_tags
            WHERE video_automatic_tags.normalized_name=
                  automatic_tags.normalized_name
            )"""
        )

    @staticmethod
    def _automatic_tagging_for_key(
        db: sqlite3.Connection, key: str
    ) -> AutomaticTagging:
        run = db.execute(
            """SELECT status, generator_id, generator_version, transcript_hash,
            generated_at, warning FROM automatic_tag_runs WHERE resource_key=?""",
            (key,),
        ).fetchone()
        if run is None:
            return AutomaticTagging()
        rows = db.execute(
            """SELECT t.display_name, vat.confidence, vat.generation_method
            FROM video_automatic_tags vat
            JOIN automatic_tags t
              ON t.normalized_name=vat.normalized_name
            WHERE vat.resource_key=? ORDER BY vat.normalized_name ASC""",
            (key,),
        ).fetchall()
        return AutomaticTagging(
            status=run["status"],
            tags=[
                AutomaticTag(
                    name=row["display_name"],
                    confidence=row["confidence"],
                    generation_method=row["generation_method"],
                )
                for row in rows
            ],
            generator_id=run["generator_id"],
            generator_version=run["generator_version"],
            transcript_hash=run["transcript_hash"],
            generated_at=run["generated_at"],
            warning=run["warning"],
        )

    @staticmethod
    def _classification_for_key(
        db: sqlite3.Connection, key: str
    ) -> VideoClassification:
        row = db.execute(
            """SELECT primary_category, secondary_category, updated_at
            FROM video_classifications WHERE resource_key=?""",
            (key,),
        ).fetchone()
        if row is None:
            return VideoClassification()
        return VideoClassification(
            primary_category=row["primary_category"],
            secondary_category=row["secondary_category"],
            updated_at=row["updated_at"],
        )

    def get_result(self, video_id: str, platform: str | None = None) -> dict | None:
        with self._connect() as db:
            if platform is not None:
                rows = db.execute(
                    """SELECT resource_key, metadata_json FROM videos
                    WHERE resource_key=?""",
                    (resource_key(platform, video_id),),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT resource_key, metadata_json FROM videos
                    WHERE public_video_id=?""",
                    (video_id,),
                ).fetchall()
            if len(rows) > 1:
                raise AmbiguousVideoIdError(video_id)
            row = rows[0] if rows else None
            if row is None:
                return None
            payload = json.loads(row["metadata_json"])
            payload["automatic_tagging"] = self._automatic_tagging_for_key(
                db, row["resource_key"]
            ).model_dump(mode="json")
            return payload

    def load_result(
        self, video_id: str, platform: str | None = None
    ) -> VideoResult | None:
        payload = self.get_result(video_id, platform=platform)
        return None if payload is None else VideoResult.model_validate(payload)

    def get_video_detail(
        self, video_id: str, platform: str | None = None
    ) -> VideoDetail | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT metadata_json, favorite FROM videos WHERE resource_key=?",
                (key,),
            ).fetchone()
            tag_rows = db.execute(
                """SELECT t.display_name FROM video_tags vt
                JOIN tags t ON t.normalized_name=vt.normalized_name
                WHERE vt.resource_key=? ORDER BY t.normalized_name ASC""",
                (key,),
            ).fetchall()
            history_rows = db.execute(
                """SELECT focus_query, query_hash, result_json, created_at
                FROM extractions
                WHERE resource_key=? AND mode='focused'
                ORDER BY created_at DESC, id DESC""",
                (key,),
            ).fetchall()
            question_rows = db.execute(
                """SELECT q.id, q.question, q.question_hash,
                q.answer_json, q.created_at
                FROM questions q
                JOIN conversations c ON c.id=q.conversation_id
                WHERE c.resource_key=?
                ORDER BY q.created_at ASC, q.id ASC""",
                (key,),
            ).fetchall()
            personal_notes = self._personal_notes_for_key(db, key)
            annotation_targets = self._annotation_targets_for_key(db, key)
            automatic_tagging = self._automatic_tagging_for_key(db, key)
            classification = self._classification_for_key(db, key)
        if row is None:
            return None
        history = []
        for history_row in history_rows:
            payload = json.loads(history_row["result_json"])
            history.append(
                FocusedHistoryItem(
                    focus_query=history_row["focus_query"],
                    query_hash=history_row["query_hash"],
                    focused_answer=FocusedAnswer.model_validate(
                        payload["focused_answer"]
                    ),
                    created_at=history_row["created_at"],
                )
            )
        result_payload = json.loads(row["metadata_json"])
        result_payload["automatic_tagging"] = automatic_tagging.model_dump(
            mode="json"
        )
        return VideoDetail(
            result=VideoResult.model_validate(result_payload),
            favorite=bool(row["favorite"]),
            personal_tags=[item["display_name"] for item in tag_rows],
            automatic_tagging=automatic_tagging,
            classification=classification,
            focused_history=history,
            question_history=[
                self._video_question_from_row(item) for item in question_rows
            ],
            personal_notes=personal_notes,
            annotation_targets=annotation_targets,
        )

    @staticmethod
    def _personal_notes_for_key(
        db: sqlite3.Connection, key: str
    ) -> PersonalNotes:
        spark_row = db.execute(
            """SELECT id, content, author, created_at, updated_at
            FROM video_sparks WHERE resource_key=?""",
            (key,),
        ).fetchone()
        annotation_rows = db.execute(
            """SELECT id, target_key, target_type, content, author,
            created_at, updated_at FROM video_annotations
            WHERE resource_key=? ORDER BY created_at ASC, id ASC""",
            (key,),
        ).fetchall()
        spark = (
            None
            if spark_row is None
            else PersonalSpark(
                id=spark_row["id"],
                content=spark_row["content"],
                author=spark_row["author"],
                created_at=spark_row["created_at"],
                updated_at=spark_row["updated_at"],
            )
        )
        annotations = [
            PersonalAnnotation(
                id=row["id"],
                target_key=row["target_key"],
                target_type=row["target_type"],
                content=row["content"],
                author=row["author"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in annotation_rows
        ]
        return PersonalNotes(spark=spark, annotations=annotations)

    def get_personal_notes(
        self, video_id: str, platform: str | None = None
    ) -> PersonalNotes | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            return self._personal_notes_for_key(db, key)

    def upsert_spark(
        self,
        video_id: str,
        content: str,
        author: str,
        platform: str | None = None,
    ) -> PersonalNotes | None:
        content = validate_personal_text(
            content, label="闪念内容", max_length=4000
        )
        author = validate_personal_text(author, label="作者", max_length=100)
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO video_sparks
                (resource_key, target_key, content, author, created_at, updated_at)
                VALUES (?, NULL, ?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                content=excluded.content, author=excluded.author,
                updated_at=excluded.updated_at""",
                (key, content, author, now, now),
            )
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
            return self._personal_notes_for_key(db, key)

    def delete_spark(
        self, video_id: str, platform: str | None = None
    ) -> PersonalNotes | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM video_sparks WHERE resource_key=?", (key,))
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
            return self._personal_notes_for_key(db, key)

    @staticmethod
    def _annotation_targets_for_key(
        db: sqlite3.Connection, key: str
    ) -> list[AnnotationTarget]:
        rows = db.execute(
            """SELECT id, mode, focus_query, query_hash, result_json, created_at
            FROM extractions WHERE resource_key=?
            ORDER BY CASE mode WHEN 'full' THEN 0 ELSE 1 END,
            created_at DESC, id DESC""",
            (key,),
        ).fetchall()
        targets: list[AnnotationTarget] = []
        fingerprint_occurrences: dict[tuple[int, str], int] = {}

        def append_items(
            row: sqlite3.Row,
            items: list[dict],
            *,
            prefix: str,
        ) -> None:
            for index, item in enumerate(items):
                display_key = f"{prefix}-{index}"
                fingerprint = hashlib.sha256(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:32]
                occurrence_key = (row["id"], fingerprint)
                occurrence = fingerprint_occurrences.get(occurrence_key, 0) + 1
                fingerprint_occurrences[occurrence_key] = occurrence
                target_key = f"extraction:{row['id']}:item:{fingerprint}"
                if occurrence > 1:
                    target_key = f"{target_key}:duplicate:{occurrence}"
                targets.append(
                    AnnotationTarget(
                        target_key=target_key,
                        display_key=display_key,
                        target_type=(
                            "step"
                            if item.get("item_type") == "step"
                            else "claim"
                        ),
                        mode=row["mode"],
                        focus_query=row["focus_query"],
                        query_hash=row["query_hash"],
                    )
                )

        for row in rows:
            payload = json.loads(row["result_json"])
            if row["mode"] == "full":
                full = payload.get("full_extraction") or {}
                for group in (
                    "key_points",
                    "important_data",
                    "cases_and_arguments",
                    "steps",
                    "risks",
                    "quotes",
                ):
                    append_items(row, full.get(group) or [], prefix=group)
            elif row["mode"] == "focused":
                answer = payload.get("focused_answer") or {}
                append_items(
                    row,
                    answer.get("key_points") or [],
                    prefix="focused-key-points",
                )
                append_items(
                    row,
                    answer.get("supplementary_context") or [],
                    prefix="focused-context",
                )
        return targets

    def create_annotation(
        self,
        video_id: str,
        target_key: str,
        content: str,
        author: str,
        platform: str | None = None,
    ) -> PersonalNotes | None:
        target_key = validate_personal_text(
            target_key, label="批注目标", max_length=200
        )
        content = validate_personal_text(
            content, label="批注内容", max_length=4000
        )
        author = validate_personal_text(author, label="作者", max_length=100)
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            targets = {
                item.target_key: item.target_type
                for item in self._annotation_targets_for_key(db, key)
            }
            if target_key not in targets:
                raise ValueError("批注目标不属于该视频的已存要点或步骤。")
            db.execute(
                """INSERT INTO video_annotations
                (resource_key, target_key, target_type, content, author,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    target_key,
                    targets[target_key],
                    content,
                    author,
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
            return self._personal_notes_for_key(db, key)

    def update_annotation(
        self,
        video_id: str,
        annotation_id: int,
        content: str,
        author: str | None = None,
        platform: str | None = None,
    ) -> PersonalNotes | None:
        content = validate_personal_text(
            content, label="批注内容", max_length=4000
        )
        if author is not None:
            author = validate_personal_text(author, label="作者", max_length=100)
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            if author is None:
                cursor = db.execute(
                    """UPDATE video_annotations
                    SET content=?, updated_at=?
                    WHERE id=? AND resource_key=?""",
                    (content, now, annotation_id, key),
                )
            else:
                cursor = db.execute(
                    """UPDATE video_annotations
                    SET content=?, author=?, updated_at=?
                    WHERE id=? AND resource_key=?""",
                    (content, author, now, annotation_id, key),
                )
            if cursor.rowcount != 1:
                raise LookupError("annotation")
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
            return self._personal_notes_for_key(db, key)

    def delete_annotation(
        self,
        video_id: str,
        annotation_id: int,
        platform: str | None = None,
    ) -> PersonalNotes | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                """DELETE FROM video_annotations
                WHERE id=? AND resource_key=?""",
                (annotation_id, key),
            )
            if cursor.rowcount != 1:
                raise LookupError("annotation")
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
            return self._personal_notes_for_key(db, key)

    def set_favorite(
        self, video_id: str, favorite: bool, platform: str | None = None
    ) -> bool:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return False
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE videos SET favorite=?, updated_at=? WHERE resource_key=?",
                (int(favorite), now, key),
            )
        return cursor.rowcount == 1

    def update_video_tags(
        self,
        video_id: str,
        tags: list[str],
        *,
        operation: str,
        platform: str | None = None,
    ) -> bool:
        if operation not in {"add", "remove"}:
            raise ValueError("标签操作必须是 add 或 remove。")
        if len(tags) > 50:
            raise ValueError("单次最多处理 50 个标签。")
        normalized: dict[str, str] = {}
        for value in tags:
            key_name, display = normalize_tag(value)
            normalized.setdefault(key_name, display)
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return False
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            if operation == "add":
                for normalized_name, display_name in normalized.items():
                    db.execute(
                        """INSERT INTO tags
                        (normalized_name, display_name, created_at) VALUES (?, ?, ?)
                        ON CONFLICT(normalized_name) DO NOTHING""",
                        (normalized_name, display_name, now),
                    )
                    db.execute(
                        """INSERT INTO video_tags
                        (resource_key, normalized_name, created_at) VALUES (?, ?, ?)
                        ON CONFLICT(resource_key, normalized_name) DO NOTHING""",
                        (key, normalized_name, now),
                    )
            else:
                db.executemany(
                    """DELETE FROM video_tags
                    WHERE resource_key=? AND normalized_name=?""",
                    [(key, normalized_name) for normalized_name in normalized],
                )
                db.execute(
                    """DELETE FROM tags WHERE NOT EXISTS (
                    SELECT 1 FROM video_tags
                    WHERE video_tags.normalized_name=tags.normalized_name
                    )"""
                )
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
        return True

    def load_automatic_tagging(
        self, video_id: str, platform: str | None = None
    ) -> AutomaticTagging | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            return self._automatic_tagging_for_key(db, key)

    def save_automatic_tagging(
        self,
        video_id: str,
        tagging: AutomaticTagging,
        warnings: list[str],
        platform: str | None = None,
    ) -> bool:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return False
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            row = db.execute(
                "SELECT metadata_json FROM videos WHERE resource_key=?", (key,)
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row["metadata_json"])
            payload["automatic_tagging"] = tagging.model_dump(mode="json")
            payload["warnings"] = warnings
            db.execute(
                """UPDATE videos SET metadata_json=?, warnings_json=?,
                status=?, updated_at=? WHERE resource_key=?""",
                (
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(warnings, ensure_ascii=False),
                    "completed_with_warnings" if warnings else "completed",
                    now,
                    key,
                ),
            )
            self._save_automatic_tagging_on_connection(db, key, tagging, now)
        return True

    def set_classification(
        self,
        video_id: str,
        primary_category: str,
        secondary_category: str = "",
        platform: str | None = None,
    ) -> bool:
        primary_normalized, primary_display = normalize_tag(primary_category)
        if secondary_category.strip():
            secondary_normalized, secondary_display = normalize_tag(
                secondary_category
            )
        else:
            secondary_normalized, secondary_display = "", ""
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return False
        now = datetime.now(UTC).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO video_classifications
                (resource_key, primary_normalized, primary_category,
                 secondary_normalized, secondary_category, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                primary_normalized=excluded.primary_normalized,
                primary_category=excluded.primary_category,
                secondary_normalized=excluded.secondary_normalized,
                secondary_category=excluded.secondary_category,
                updated_at=excluded.updated_at""",
                (
                    key,
                    primary_normalized,
                    primary_display,
                    secondary_normalized,
                    secondary_display,
                    now,
                ),
            )
            db.execute(
                "UPDATE videos SET updated_at=? WHERE resource_key=?", (now, key)
            )
        return True

    def search_videos(
        self,
        *,
        query: str = "",
        platform: str | None = None,
        tag: str = "",
        tag_source: str | None = None,
        primary_category: str = "",
        secondary_category: str = "",
        favorite: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> VideoSearchPage:
        if limit < 1 or limit > 100 or offset < 0:
            raise ValueError("分页参数无效。")
        if tag_source not in {None, "platform", "automatic", "personal"}:
            raise ValueError("标签来源无效。")
        normalized_query = normalize_search_text(query)
        normalized_tag = normalize_tag(tag)[0] if tag.strip() else ""
        effective_tag_source = tag_source or "personal"
        primary_filter = (
            normalize_tag(primary_category)[0] if primary_category.strip() else ""
        )
        secondary_filter = (
            normalize_tag(secondary_category)[0]
            if secondary_category.strip()
            else ""
        )
        with self._connect() as db:
            rows = db.execute(
                """SELECT resource_key, platform, public_video_id, source_url,
                metadata_json, subtitle_source, raw_transcript, clean_transcript, favorite,
                created_at, updated_at
                FROM videos"""
            ).fetchall()
            tag_rows = db.execute(
                """SELECT vt.resource_key, vt.normalized_name, t.display_name
                FROM video_tags vt JOIN tags t
                ON t.normalized_name=vt.normalized_name"""
            ).fetchall()
            automatic_tag_rows = db.execute(
                """SELECT vat.resource_key, vat.normalized_name, t.display_name,
                vat.confidence, vat.generation_method
                FROM video_automatic_tags vat JOIN automatic_tags t
                ON t.normalized_name=vat.normalized_name"""
            ).fetchall()
            automatic_run_rows = db.execute(
                """SELECT resource_key, status, generator_id, generator_version,
                transcript_hash, generated_at, warning
                FROM automatic_tag_runs"""
            ).fetchall()
            classification_rows = db.execute(
                """SELECT resource_key, primary_normalized, primary_category,
                secondary_normalized, secondary_category, updated_at
                FROM video_classifications"""
            ).fetchall()
            spark_rows = db.execute(
                """SELECT resource_key, id, content, author, created_at, updated_at
                FROM video_sparks"""
            ).fetchall()
        tags_by_key: dict[str, list[tuple[str, str]]] = {}
        for row in tag_rows:
            tags_by_key.setdefault(row["resource_key"], []).append(
                (row["normalized_name"], row["display_name"])
            )
        automatic_tags_by_key: dict[
            str, list[tuple[str, str, float, str]]
        ] = {}
        for row in automatic_tag_rows:
            automatic_tags_by_key.setdefault(row["resource_key"], []).append(
                (
                    row["normalized_name"],
                    row["display_name"],
                    row["confidence"],
                    row["generation_method"],
                )
            )
        automatic_runs_by_key = {
            row["resource_key"]: row for row in automatic_run_rows
        }
        classifications_by_key = {
            row["resource_key"]: row for row in classification_rows
        }
        sparks_by_key = {
            row["resource_key"]: PersonalSpark(
                id=row["id"],
                content=row["content"],
                author=row["author"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in spark_rows
        }
        matches: list[tuple[str, str, LibraryVideo]] = []
        for row in rows:
            if platform is not None and row["platform"] != platform:
                continue
            if favorite is not None and bool(row["favorite"]) is not favorite:
                continue
            personal = sorted(
                tags_by_key.get(row["resource_key"], []), key=lambda item: item[0]
            )
            payload = json.loads(row["metadata_json"])
            source = []
            for value in payload.get("tags", []):
                try:
                    source.append(normalize_tag(str(value)))
                except ValueError:
                    continue
            automatic = sorted(
                automatic_tags_by_key.get(row["resource_key"], []),
                key=lambda item: item[0],
            )
            tag_identities = {
                "platform": {item[0] for item in source},
                "automatic": {item[0] for item in automatic},
                "personal": {item[0] for item in personal},
            }
            if (
                normalized_tag
                and normalized_tag not in tag_identities[effective_tag_source]
            ):
                continue
            classification_row = classifications_by_key.get(row["resource_key"])
            classification = (
                VideoClassification()
                if classification_row is None
                else VideoClassification(
                    primary_category=classification_row["primary_category"],
                    secondary_category=classification_row["secondary_category"],
                    updated_at=classification_row["updated_at"],
                )
            )
            classification_primary_normalized = (
                "暂未分类".casefold()
                if classification_row is None
                else classification_row["primary_normalized"]
            )
            classification_secondary_normalized = (
                "" if classification_row is None
                else classification_row["secondary_normalized"]
            )
            if (
                primary_filter
                and primary_filter != classification_primary_normalized
            ):
                continue
            if (
                secondary_filter
                and secondary_filter != classification_secondary_normalized
            ):
                continue
            searchable = normalize_search_text(
                " ".join(
                    [
                        str(payload.get("title", "")),
                        str(payload.get("author", "")),
                        str(payload.get("description", "")),
                        " ".join(str(item) for item in payload.get("tags", [])),
                        " ".join(item[1] for item in automatic),
                        " ".join(item[1] for item in personal),
                        classification.primary_category,
                        classification.secondary_category,
                        str(payload.get("summary", "")),
                        row["raw_transcript"],
                        row["clean_transcript"],
                    ]
                )
            )
            if normalized_query and normalized_query not in searchable:
                continue
            run = automatic_runs_by_key.get(row["resource_key"])
            automatic_tagging = (
                AutomaticTagging()
                if run is None
                else AutomaticTagging(
                    status=run["status"],
                    tags=[
                        AutomaticTag(
                            name=item[1],
                            confidence=item[2],
                            generation_method=item[3],
                        )
                        for item in automatic
                    ],
                    generator_id=run["generator_id"],
                    generator_version=run["generator_version"],
                    transcript_hash=run["transcript_hash"],
                    generated_at=run["generated_at"],
                    warning=run["warning"],
                )
            )
            item = LibraryVideo(
                platform=row["platform"],
                video_id=row["public_video_id"],
                source_url=row["source_url"],
                title=str(payload.get("title", "")),
                author=str(payload.get("author", "")),
                description=str(payload.get("description", "")),
                summary=str(payload.get("summary", "")),
                cover_url=str(payload.get("cover_url", "")),
                subtitle_source=row["subtitle_source"],
                source_tags=[str(value) for value in payload.get("tags", [])],
                warnings=[
                    str(value) for value in payload.get("warnings", [])
                ],
                personal_tags=[item[1] for item in personal],
                automatic_tagging=automatic_tagging,
                classification=classification,
                spark=sparks_by_key.get(row["resource_key"]),
                favorite=bool(row["favorite"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            matches.append((row["updated_at"], row["resource_key"], item))
        matches.sort(key=lambda item: item[1])
        matches.sort(key=lambda item: item[0], reverse=True)
        total = len(matches)
        tag_counts: dict[tuple[str, str], tuple[str, int]] = {}
        category_counts: dict[str, tuple[str, int]] = {}
        child_counts: dict[tuple[str, str], tuple[str, int]] = {}
        for _, _, item in matches:
            groups = (
                ("platform", item.source_tags),
                (
                    "automatic",
                    [tag.name for tag in item.automatic_tagging.tags],
                ),
                ("personal", item.personal_tags),
            )
            for source_name, values in groups:
                seen: set[str] = set()
                for value in values:
                    normalized_name, display_name = normalize_tag(value)
                    if normalized_name in seen:
                        continue
                    seen.add(normalized_name)
                    key = (source_name, normalized_name)
                    tag_counts[key] = (
                        tag_counts.get(key, (display_name, 0))[0],
                        tag_counts.get(key, (display_name, 0))[1] + 1,
                    )
            primary_normalized, primary_display = normalize_tag(
                item.classification.primary_category
            )
            category_counts[primary_normalized] = (
                category_counts.get(
                    primary_normalized, (primary_display, 0)
                )[0],
                category_counts.get(
                    primary_normalized, (primary_display, 0)
                )[1] + 1,
            )
            if item.classification.secondary_category:
                secondary_normalized, secondary_display = normalize_tag(
                    item.classification.secondary_category
                )
                child_key = (primary_normalized, secondary_normalized)
                child_counts[child_key] = (
                    child_counts.get(
                        child_key, (secondary_display, 0)
                    )[0],
                    child_counts.get(
                        child_key, (secondary_display, 0)
                    )[1] + 1,
                )
        tag_facets = [
            TagFacet(name=value[0], source=key[0], count=value[1])
            for key, value in tag_counts.items()
        ]
        tag_facets.sort(
            key=lambda facet: (
                -facet.count,
                normalize_search_text(facet.name),
                facet.source,
            )
        )
        category_facets = []
        for primary_normalized, (primary_display, count) in category_counts.items():
            children = [
                SecondaryCategoryFacet(
                    secondary_category=value[0], count=value[1]
                )
                for key, value in child_counts.items()
                if key[0] == primary_normalized
            ]
            children.sort(
                key=lambda facet: (
                    -facet.count,
                    normalize_search_text(facet.secondary_category),
                )
            )
            category_facets.append(
                CategoryFacet(
                    primary_category=primary_display,
                    count=count,
                    children=children,
                )
            )
        category_facets.sort(
            key=lambda facet: (
                -facet.count,
                normalize_search_text(facet.primary_category),
            )
        )
        items = [item[2] for item in matches[offset : offset + limit]]
        return VideoSearchPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            facets=LibraryFacets(tags=tag_facets, categories=category_facets),
        )

    @staticmethod
    def _video_question_from_row(row: sqlite3.Row) -> VideoQuestion:
        return VideoQuestion(
            id=row["id"],
            question=row["question"],
            question_hash=row["question_hash"],
            answer=FocusedAnswer.model_validate(json.loads(row["answer_json"])),
            created_at=row["created_at"],
        )

    def load_video_question(
        self, video_id: str, question_hash: str, platform: str | None = None
    ) -> VideoQuestion | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT q.id, q.question, q.question_hash,
                q.answer_json, q.created_at
                FROM questions q
                JOIN conversations c ON c.id=q.conversation_id
                WHERE c.resource_key=? AND q.question_hash=?""",
                (key, question_hash),
            ).fetchone()
        return None if row is None else self._video_question_from_row(row)

    def save_video_question(
        self,
        video_id: str,
        question: str,
        question_hash: str,
        answer: FocusedAnswer,
        platform: str | None = None,
    ) -> VideoQuestion:
        """Atomically persist one idempotent question and its subtitle links."""
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            raise LookupError(video_id)
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(answer.model_dump(mode="json"), ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                """INSERT INTO conversations
                (resource_key, created_at, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET updated_at=excluded.updated_at""",
                (key, now, now),
            )
            conversation_id = db.execute(
                "SELECT id FROM conversations WHERE resource_key=?", (key,)
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO questions
                (conversation_id, question, question_hash, answer_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id, question_hash) DO NOTHING""",
                (conversation_id, question, question_hash, payload, now),
            )
            row = db.execute(
                """SELECT id, question, question_hash, answer_json, created_at
                FROM questions WHERE conversation_id=? AND question_hash=?""",
                (conversation_id, question_hash),
            ).fetchone()
            if cursor.rowcount == 1:
                links = [
                    (row["id"], evidence.id, key, segment_id)
                    for evidence in answer.supporting_segments
                    for segment_id in evidence.segment_ids
                ]
                if links:
                    db.executemany(
                        """INSERT INTO question_evidence
                        (question_id, evidence_id, resource_key, segment_id)
                        VALUES (?, ?, ?, ?)""",
                        links,
                    )
        return self._video_question_from_row(row)

    def load_focused_answer(
        self, video_id: str, query_hash: str, platform: str | None = None
    ) -> FocusedAnswer | None:
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT result_json FROM extractions
                WHERE resource_key=? AND mode='focused' AND query_hash=?""",
                (key, query_hash),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        return FocusedAnswer.model_validate(payload["focused_answer"])

    def save_focused_answer(
        self,
        video_id: str,
        focus_query: str,
        query_hash: str,
        answer: FocusedAnswer,
        platform: str | None = None,
    ) -> FocusedAnswer:
        """Persist only focused output; transcript remains owned by ``videos``."""
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {"focused_answer": answer.model_dump(mode="json")},
            ensure_ascii=False,
        )
        key = self._resolve_resource_key(video_id, platform)
        if key is None:
            raise LookupError(video_id)
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO extractions
                (resource_key, video_id, mode, focus_query, query_hash,
                 result_json, created_at)
                VALUES (?, ?, 'focused', ?, ?, ?, ?)
                ON CONFLICT(resource_key, mode, query_hash) DO NOTHING""",
                (key, video_id, focus_query, query_hash, payload, now),
            )
            row = db.execute(
                """SELECT id, result_json FROM extractions
                WHERE resource_key=? AND mode='focused' AND query_hash=?""",
                (key, query_hash),
            ).fetchone()
            if cursor.rowcount == 0:
                cached = json.loads(row["result_json"])
                return FocusedAnswer.model_validate(cached["focused_answer"])
            extraction_id = int(row["id"])
            for evidence in answer.supporting_segments:
                db.execute(
                    """INSERT INTO claims
                    (id, extraction_id, claim, claim_type, evidence,
                     start_time, end_time, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evidence.id, extraction_id, evidence.claim, evidence.claim_type,
                        evidence.evidence, evidence.start_time, evidence.end_time,
                        evidence.confidence,
                    ),
                )
                db.executemany(
                    """INSERT INTO claim_evidence
                    (extraction_id, claim_id, resource_key, video_id, segment_id)
                    VALUES (?, ?, ?, ?, ?)""",
                    [
                        (extraction_id, evidence.id, key, video_id, segment_id)
                        for segment_id in evidence.segment_ids
                    ],
                )
        return answer

    def _resolve_resource_key(
        self, video_id: str, platform: str | None
    ) -> str | None:
        if platform is not None:
            key = resource_key(platform, video_id)
            with self._connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM videos WHERE resource_key=?", (key,)
                ).fetchone()
            return key if exists else None
        with self._connect() as db:
            rows = db.execute(
                "SELECT resource_key FROM videos WHERE public_video_id=?",
                (video_id,),
            ).fetchall()
        if len(rows) > 1:
            raise AmbiguousVideoIdError(video_id)
        return rows[0]["resource_key"] if rows else None
