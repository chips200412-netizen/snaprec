from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

from .sqlite import SQLiteRepository, normalize_identity_url


_TERMINAL_BATCH = {"completed", "completed_with_issues", "cancelled"}
_TERMINAL_ITEM = {"saved", "already_exists", "skipped", "cancelled"}
_FAILURE_REASONS = {
    "preview_failed",
    "save_failed",
    "validation_failed",
}
_DEFAULT_DRAFT = {
    "user_title": None,
    "untitled_confirmed": False,
    "organization_confirmation": {
        "primary_category": "",
        "secondary_category": "",
        "organization_tags": [],
    },
    "personal_tags": [],
    "inspiration": None,
}
_SAFE_URL_CANDIDATE = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
_SAFE_WHOLE_URL = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
_SAFE_URL_SCHEME = re.compile(r'https?://', re.IGNORECASE)
_SAFE_PUBLIC_TITLE_SOURCES = {
    "open_graph",
    "page_metadata",
    "platform_public",
}
_SAFE_URL_WRAPPERS = {
    "(": ")",
    "[": "]",
    "{": "}",
    "'": "'",
    "（": "）",
    "【": "】",
    "《": "》",
    "「": "」",
    "『": "』",
}


def collection_import_save_idempotency_key(
    batch_id: str, batch_item_id: str, save_attempt_id: str
) -> str:
    """Derive the opaque single-save key from server-generated identities only."""

    return f"collection-import-save:{batch_id}:{batch_item_id}:{save_attempt_id}"


def collection_import_save_idempotency_key_hash(
    batch_id: str, batch_item_id: str, save_attempt_id: str
) -> str:
    key = collection_import_save_idempotency_key(
        batch_id, batch_item_id, save_attempt_id
    )
    return hashlib.sha256(key.encode("ascii")).hexdigest()


class CollectionImportNotFoundError(LookupError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CollectionImportIdempotencyReusedError(ValueError):
    pass


class CollectionImportRetiredError(ValueError):
    pass


class CollectionImportActiveError(ValueError):
    def __init__(self, batch_id: str):
        super().__init__(batch_id)
        self.batch_id = batch_id


class CollectionImportBatchRevisionConflictError(ValueError):
    def __init__(self, current_revision: int):
        super().__init__(current_revision)
        self.current_revision = current_revision


class CollectionImportItemRevisionConflictError(ValueError):
    def __init__(self, current_revision: int):
        super().__init__(current_revision)
        self.current_revision = current_revision


class CollectionImportStateConflictError(ValueError):
    pass


class CollectionImportAttemptLimitError(ValueError):
    pass


class CollectionImportRequestInvalidError(ValueError):
    pass


class CollectionImportReviewIncompleteError(ValueError):
    pass


class CollectionImportPreviewExpiringError(ValueError):
    pass


class CollectionImportRepository:
    """Transactional persistence for the collection-import namespace only."""

    def __init__(self, repository: SQLiteRepository):
        self._repository = repository

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _decode_json(value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _item_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_item_id": row["batch_item_id"],
            "client_item_id": row["client_item_id"],
            "position": row["position"],
            "display_label": row["display_label"],
            "state": row["state"],
            "decision": row["decision"],
            "item_revision": row["item_revision"],
            "preview_generation": row["preview_generation"],
            "duplicate_of_batch_item_id": row["duplicate_of_batch_item_id"],
            "collection_item_id": row["collection_item_id"],
            "error_code": row["error_code"],
            "terminal_reason": row["terminal_reason"],
        }

    @classmethod
    def _batch_snapshot_on_connection(
        cls, db: sqlite3.Connection, batch_id: str
    ) -> dict[str, Any] | None:
        batch = db.execute(
            "SELECT * FROM collection_import_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return None
        rows = db.execute(
            """SELECT * FROM collection_import_batch_items
               WHERE batch_id=? ORDER BY position ASC""",
            (batch_id,),
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        return {
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "revision": batch["revision"],
            "total": batch["total_count"],
            "queued": counts.get("queued", 0),
            "previewing": counts.get("previewing", 0),
            "ready": counts.get("ready", 0),
            "needs_review": counts.get("needs_review", 0),
            "duplicates": counts.get("duplicate_in_batch", 0),
            "already_exists": counts.get("already_exists", 0),
            "failed": counts.get("failed", 0),
            "selected": sum(row["decision"] == "save" for row in rows),
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "terminal_at": batch["terminal_at"],
            "items": [cls._item_summary(row) for row in rows],
        }

    @classmethod
    def _derive_batch_state_on_connection(
        cls, db: sqlite3.Connection, batch_id: str, now: str
    ) -> None:
        batch = db.execute(
            """SELECT status, cancel_requested, terminal_at
               FROM collection_import_batches WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return
        rows = db.execute(
            """SELECT state, terminal_reason, preview_claim_token
               FROM collection_import_batch_items WHERE batch_id=?""",
            (batch_id,),
        ).fetchall()
        states = {row["state"] for row in rows}
        has_save_claim = db.execute(
            """SELECT 1 FROM collection_import_save_attempts a
               JOIN collection_import_batch_items i
                 ON i.batch_item_id=a.batch_item_id
               WHERE i.batch_id=? AND a.claim_token IS NOT NULL LIMIT 1""",
            (batch_id,),
        ).fetchone()
        has_claim = (
            any(row["preview_claim_token"] for row in rows)
            or has_save_claim is not None
        )
        all_terminal = bool(rows) and all(row["state"] in _TERMINAL_ITEM for row in rows)
        if batch["cancel_requested"]:
            state = "cancelled" if all_terminal else "cancelling"
        elif ({"outcome_unknown", "interrupted"} & states) and not has_claim:
            state = "interrupted"
        elif {"save_queued", "saving"} & states:
            state = "saving"
        elif {"queued", "previewing"} & states:
            state = "previewing"
        elif all_terminal:
            state = (
                "completed_with_issues"
                if any(row["terminal_reason"] in _FAILURE_REASONS for row in rows)
                else "completed"
            )
        else:
            state = "awaiting_review"
        # ``terminal_at`` is the retention identity of the first terminal
        # transition.  Re-derivation (including a late worker write-back after
        # cancel) must never refresh it and extend retention indefinitely.
        terminal_at = (
            (batch["terminal_at"] or now)
            if state in _TERMINAL_BATCH
            else None
        )
        changed = state != batch["status"] or terminal_at != batch["terminal_at"]
        if changed:
            db.execute(
                """UPDATE collection_import_batches
                   SET status=?, terminal_at=?, updated_at=? WHERE batch_id=?""",
                (state, terminal_at, now, batch_id),
            )
        if changed and state in _TERMINAL_BATCH:
            cls._maintain_on_connection(db, now=now)

    @classmethod
    def _purge_terminal_batches_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        now: str,
        limit: int = 100,
    ) -> int:
        try:
            current = datetime.fromisoformat(now)
        except (TypeError, ValueError):
            return 0
        cutoff = cls._iso(current - timedelta(days=7))
        tombstone_expires = cls._iso(current + timedelta(days=7))
        candidates = db.execute(
            """SELECT b.batch_id, b.terminal_at, b.status
               FROM collection_import_batches b
               WHERE b.terminal_at IS NOT NULL
                 AND b.status IN ('completed','completed_with_issues','cancelled')
                 AND (
                     b.terminal_at < ?
                     OR 20 <= (
                         SELECT COUNT(*) FROM collection_import_batches newer
                         WHERE newer.terminal_at IS NOT NULL
                           AND newer.status IN (
                               'completed','completed_with_issues','cancelled'
                           )
                           AND (
                               newer.terminal_at > b.terminal_at
                               OR (newer.terminal_at=b.terminal_at
                                   AND newer.batch_id>b.batch_id)
                           )
                     )
                 )
               ORDER BY b.terminal_at ASC, b.batch_id ASC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        purged = 0
        for candidate in candidates:
            # Re-read both terminal identity and rank inside this same write
            # transaction immediately before deletion.
            row = db.execute(
                """SELECT status, terminal_at FROM collection_import_batches
                   WHERE batch_id=?""",
                (candidate["batch_id"],),
            ).fetchone()
            if (
                row is None
                or row["terminal_at"] != candidate["terminal_at"]
                or row["status"] not in _TERMINAL_BATCH
            ):
                continue
            newer_count = db.execute(
                """SELECT COUNT(*) FROM collection_import_batches
                   WHERE terminal_at IS NOT NULL
                     AND status IN ('completed','completed_with_issues','cancelled')
                     AND (terminal_at>? OR (terminal_at=? AND batch_id>?))""",
                (
                    row["terminal_at"],
                    row["terminal_at"],
                    candidate["batch_id"],
                ),
            ).fetchone()[0]
            if row["terminal_at"] >= cutoff and newer_count < 20:
                continue
            binding_count = db.execute(
                """SELECT COUNT(*) FROM collection_import_batch_idempotency
                   WHERE batch_id=?""",
                (candidate["batch_id"],),
            ).fetchone()[0]
            if binding_count != 1:
                continue
            tombstoned = db.execute(
                """UPDATE collection_import_batch_idempotency
                   SET batch_id=NULL, purged_at=?, expires_at=?
                   WHERE batch_id=?""",
                (now, tombstone_expires, candidate["batch_id"]),
            ).rowcount
            if tombstoned != 1:
                continue
            deleted = db.execute(
                """DELETE FROM collection_import_batches
                   WHERE batch_id=? AND terminal_at=?
                     AND status IN ('completed','completed_with_issues','cancelled')""",
                (candidate["batch_id"], candidate["terminal_at"]),
            ).rowcount
            if deleted != 1:
                raise sqlite3.IntegrityError(
                    "terminal batch tombstone/delete lost atomicity"
                )
            purged += 1
        return purged

    @classmethod
    def _gc_preview_cache_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        now: str,
        limit: int = 100,
    ) -> int:
        candidates = db.execute(
            """SELECT kind, reference_id, expires_at FROM (
                   SELECT 'preview' AS kind, preview_id AS reference_id,
                          expires_at
                   FROM collection_previews p
                   WHERE p.expires_at<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM collection_import_batch_items i
                         WHERE i.preview_id=p.preview_id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM collection_import_save_attempts a
                         WHERE a.preview_id=p.preview_id AND (
                             a.attempt_phase!='settled'
                             OR a.result IN ('none','unknown')
                         )
                     )
                   UNION ALL
                   SELECT 'cache' AS kind, identity_url AS reference_id,
                          expires_at
                   FROM collection_metadata_cache c
                   WHERE c.expires_at<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM collection_import_batch_items i
                         WHERE i.identity_url=c.identity_url
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM collection_previews p
                         WHERE json_valid(p.payload_json)=1
                           AND json_extract(
                               p.payload_json, '$.identity_url'
                           )=c.identity_url
                           AND (
                               p.expires_at>?
                               OR EXISTS (
                                   SELECT 1
                                   FROM collection_import_batch_items i
                                   WHERE i.preview_id=p.preview_id
                               )
                               OR EXISTS (
                                   SELECT 1
                                   FROM collection_import_save_attempts a
                                   WHERE a.preview_id=p.preview_id AND (
                                       a.attempt_phase!='settled'
                                       OR a.result IN ('none','unknown')
                                   )
                               )
                           )
                     )
                     AND NOT EXISTS (
                         SELECT 1
                         FROM collection_import_save_attempts a
                         JOIN collection_import_batch_items i
                           ON i.batch_item_id=a.batch_item_id
                         WHERE i.identity_url=c.identity_url AND (
                             a.attempt_phase!='settled'
                             OR a.result IN ('none','unknown')
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM collection_previews p
                         WHERE (
                             p.expires_at>?
                             OR EXISTS (
                                 SELECT 1
                                 FROM collection_import_batch_items i
                                 WHERE i.preview_id=p.preview_id
                             )
                             OR EXISTS (
                                 SELECT 1
                                 FROM collection_import_save_attempts a
                                 WHERE a.preview_id=p.preview_id AND (
                                     a.attempt_phase!='settled'
                                     OR a.result IN ('none','unknown')
                                 )
                             )
                         ) AND json_valid(p.payload_json)=0
                     )
               )
               ORDER BY expires_at ASC, reference_id ASC, kind ASC LIMIT ?""",
            (now, now, now, now, limit),
        ).fetchall()
        removed = 0
        for candidate in candidates:
            if candidate["kind"] == "preview":
                current = db.execute(
                    """SELECT expires_at FROM collection_previews
                       WHERE preview_id=?""",
                    (candidate["reference_id"],),
                ).fetchone()
                if (
                    current is None
                    or current["expires_at"] != candidate["expires_at"]
                    or current["expires_at"] > now
                ):
                    continue
                batch_reference = db.execute(
                    """SELECT 1 FROM collection_import_batch_items
                       WHERE preview_id=? LIMIT 1""",
                    (candidate["reference_id"],),
                ).fetchone()
                unresolved_reference = db.execute(
                    """SELECT 1 FROM collection_import_save_attempts
                       WHERE preview_id=? AND (
                           attempt_phase!='settled' OR result IN ('none','unknown')
                       ) LIMIT 1""",
                    (candidate["reference_id"],),
                ).fetchone()
                if batch_reference is not None or unresolved_reference is not None:
                    continue
                removed += db.execute(
                    """DELETE FROM collection_previews
                       WHERE preview_id=? AND expires_at=? AND expires_at<=?""",
                    (
                        candidate["reference_id"],
                        candidate["expires_at"],
                        now,
                    ),
                ).rowcount
                continue

            current = db.execute(
                """SELECT expires_at FROM collection_metadata_cache
                   WHERE identity_url=?""",
                (candidate["reference_id"],),
            ).fetchone()
            if (
                current is None
                or current["expires_at"] != candidate["expires_at"]
                or current["expires_at"] > now
            ):
                continue
            # Invalid preview payload is unexpected durable corruption.  Fail
            # closed for cache deletion because its identity cannot be proven
            # unreferenced.
            invalid_reference = db.execute(
                """SELECT 1 FROM collection_previews p
                   WHERE (p.expires_at>? OR EXISTS (
                       SELECT 1 FROM collection_import_batch_items i
                       WHERE i.preview_id=p.preview_id
                   ) OR EXISTS (
                       SELECT 1 FROM collection_import_save_attempts a
                       WHERE a.preview_id=p.preview_id AND (
                           a.attempt_phase!='settled'
                           OR a.result IN ('none','unknown')
                       )
                   )) AND json_valid(p.payload_json)=0 LIMIT 1""",
                (now,),
            ).fetchone()
            if invalid_reference is not None:
                continue
            identity = candidate["reference_id"]
            direct_reference = db.execute(
                """SELECT 1 FROM collection_import_batch_items
                   WHERE identity_url=? LIMIT 1""",
                (identity,),
            ).fetchone()
            preview_reference = db.execute(
                """SELECT 1 FROM collection_previews p
                   WHERE json_valid(p.payload_json)=1
                     AND json_extract(p.payload_json, '$.identity_url')=?
                     AND (
                         p.expires_at>?
                         OR EXISTS (
                             SELECT 1 FROM collection_import_batch_items i
                             WHERE i.preview_id=p.preview_id
                         )
                         OR EXISTS (
                             SELECT 1 FROM collection_import_save_attempts a
                             WHERE a.preview_id=p.preview_id AND (
                                 a.attempt_phase!='settled'
                                 OR a.result IN ('none','unknown')
                             )
                         )
                     ) LIMIT 1""",
                (identity, now),
            ).fetchone()
            unresolved_identity = db.execute(
                """SELECT 1 FROM collection_import_save_attempts a
                   JOIN collection_import_batch_items i
                     ON i.batch_item_id=a.batch_item_id
                   WHERE i.identity_url=? AND (
                       a.attempt_phase!='settled' OR a.result IN ('none','unknown')
                   ) LIMIT 1""",
                (identity,),
            ).fetchone()
            if (
                direct_reference is not None
                or preview_reference is not None
                or unresolved_identity is not None
            ):
                continue
            removed += db.execute(
                """DELETE FROM collection_metadata_cache
                   WHERE identity_url=? AND expires_at=? AND expires_at<=?""",
                (identity, candidate["expires_at"], now),
            ).rowcount
        return removed

    @classmethod
    def _maintain_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        now: str,
    ) -> dict[str, int]:
        purged = cls._purge_terminal_batches_on_connection(db, now=now, limit=100)
        tombstones = db.execute(
            """SELECT key_hash, expires_at
               FROM collection_import_batch_idempotency
               WHERE batch_id IS NULL AND purged_at IS NOT NULL
                 AND expires_at<=?
               ORDER BY expires_at ASC, key_hash ASC LIMIT 100""",
            (now,),
        ).fetchall()
        tombstones_removed = 0
        for tombstone in tombstones:
            tombstones_removed += db.execute(
                """DELETE FROM collection_import_batch_idempotency
                   WHERE key_hash=? AND batch_id IS NULL
                     AND purged_at IS NOT NULL AND expires_at=? AND expires_at<=?""",
                (tombstone["key_hash"], tombstone["expires_at"], now),
            ).rowcount
        gc_removed = cls._gc_preview_cache_on_connection(db, now=now, limit=100)
        return {
            "batches": purged,
            "tombstones": tombstones_removed,
            "preview_cache": gc_removed,
        }

    def maintain(self, now: datetime) -> dict[str, int]:
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._maintain_on_connection(db, now=self._iso(now))

    def create_or_replay(
        self,
        *,
        items: list[dict[str, str]],
        key_hash: str,
        request_fingerprint: str,
        batch_id: str,
        batch_item_ids: list[str],
        now: datetime,
        expires_at: datetime,
    ) -> tuple[dict[str, Any], bool, bool]:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._maintain_on_connection(db, now=timestamp)
            prior = db.execute(
                """SELECT request_fingerprint, batch_id, expires_at
                   FROM collection_import_batch_idempotency WHERE key_hash=?""",
                (key_hash,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["batch_id"] is None
                    and prior["expires_at"] <= timestamp
                ):
                    db.execute(
                        "DELETE FROM collection_import_batch_idempotency WHERE key_hash=?",
                        (key_hash,),
                    )
                    prior = None
                elif prior["request_fingerprint"] != request_fingerprint:
                    raise CollectionImportIdempotencyReusedError()
                elif prior["batch_id"] is None:
                    raise CollectionImportRetiredError()
                else:
                    snapshot = self._batch_snapshot_on_connection(
                        db, prior["batch_id"]
                    )
                    if snapshot is None:
                        raise CollectionImportRetiredError()
                    return snapshot, False, snapshot["status"] in _TERMINAL_BATCH

            active = db.execute(
                """SELECT batch_id FROM collection_import_batches
                   WHERE terminal_at IS NULL LIMIT 1"""
            ).fetchone()
            if active is not None:
                raise CollectionImportActiveError(active["batch_id"])

            db.execute(
                """INSERT INTO collection_import_batches(
                       batch_id, status, revision, cancel_requested, total_count,
                       created_at, updated_at, terminal_at
                   ) VALUES (?, 'previewing', 1, 0, ?, ?, ?, NULL)""",
                (batch_id, len(items), timestamp, timestamp),
            )
            draft_json = json.dumps(
                _DEFAULT_DRAFT, ensure_ascii=False, separators=(",", ":")
            )
            for position, (item, item_id) in enumerate(zip(items, batch_item_ids)):
                db.execute(
                    """INSERT INTO collection_import_batch_items(
                           batch_item_id, batch_id, position, client_item_id,
                           pending_input_text, display_label, state, decision,
                           preview_generation, review_revision, item_revision,
                           draft_json, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'pending', 0, 0, 1,
                                 ?, ?, ?)""",
                    (
                        item_id,
                        batch_id,
                        position,
                        item["client_item_id"],
                        item["input_text"],
                        f"第 {position + 1} 项（等待检查）",
                        draft_json,
                        timestamp,
                        timestamp,
                    ),
                )
            db.execute(
                """INSERT INTO collection_import_batch_idempotency(
                       key_hash, request_fingerprint, batch_id, created_at,
                       expires_at, purged_at
                   ) VALUES (?, ?, ?, ?, ?, NULL)""",
                (
                    key_hash,
                    request_fingerprint,
                    batch_id,
                    timestamp,
                    self._iso(expires_at),
                ),
            )
            snapshot = self._batch_snapshot_on_connection(db, batch_id)
            assert snapshot is not None
            return snapshot, True, False

    def get_active(self, now: datetime | None = None) -> dict[str, Any] | None:
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._maintain_on_connection(
                db, now=self._iso(now or datetime.now(UTC))
            )
            row = db.execute(
                """SELECT batch_id FROM collection_import_batches
                   WHERE terminal_at IS NULL LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            return self._batch_snapshot_on_connection(db, row["batch_id"])

    def get_batch(
        self, batch_id: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._maintain_on_connection(
                db, now=self._iso(now or datetime.now(UTC))
            )
            return self._batch_snapshot_on_connection(db, batch_id)

    def get_item(self, batch_id: str, batch_item_id: str) -> dict[str, Any] | None:
        with self._repository._connect() as db:
            batch = db.execute(
                "SELECT revision FROM collection_import_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            row = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? AND batch_item_id=?""",
                (batch_id, batch_item_id),
            ).fetchone()
            if row is None:
                return None
            preview = None
            if row["preview_id"]:
                preview_row = db.execute(
                    """SELECT payload_json FROM collection_previews
                       WHERE preview_id=?""",
                    (row["preview_id"],),
                ).fetchone()
                if preview_row is not None:
                    preview = self._decode_json(preview_row["payload_json"], None)
            input_text = row["pending_input_text"]
            if input_text is None and preview is not None:
                candidate = preview.get("original_input")
                input_text = candidate if isinstance(candidate, str) else None
            return {
                **self._item_summary(row),
                "batch_id": batch_id,
                "batch_revision": batch["revision"],
                "input_available": input_text is not None,
                "input_text": input_text,
                "preview": preview,
                "draft": self._decode_json(row["draft_json"], _DEFAULT_DRAFT),
                "error_stage": row["error_stage"],
            }

    def expire_item_preview(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        now: datetime,
    ) -> bool:
        """Persist the local-only ready -> expired transition when observed."""

        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT i.state, i.preview_id, p.expires_at
                   FROM collection_import_batch_items i
                   LEFT JOIN collection_previews p ON p.preview_id=i.preview_id
                   WHERE i.batch_id=? AND i.batch_item_id=?""",
                (batch_id, batch_item_id),
            ).fetchone()
            if row is None or row["state"] not in {"ready", "needs_review"}:
                return False
            if row["preview_id"] and row["expires_at"] and row["expires_at"] > timestamp:
                return False
            changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET state='preview_expired', decision='pending',
                       review_revision=0, error_code='PREVIEW_EXPIRED',
                       error_stage='preview', terminal_reason=NULL,
                       item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=?
                     AND state IN ('ready', 'needs_review')""",
                (timestamp, batch_id, batch_item_id),
            ).rowcount
            if changed != 1:
                return False
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def review_item(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        expected_batch_revision: int,
        expected_item_revision: int,
        decision: str,
        draft: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        timestamp = self._iso(now)
        draft_json = json.dumps(
            draft, ensure_ascii=False, separators=(",", ":")
        )
        expired_after_cas = False
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                """SELECT revision, status, cancel_requested, terminal_at
                   FROM collection_import_batches WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            if batch["revision"] != expected_batch_revision:
                raise CollectionImportBatchRevisionConflictError(batch["revision"])

            item = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? AND batch_item_id=?""",
                (batch_id, batch_item_id),
            ).fetchone()
            if item is None:
                raise CollectionImportNotFoundError("BATCH_ITEM_NOT_FOUND")
            if item["item_revision"] != expected_item_revision:
                raise CollectionImportItemRevisionConflictError(
                    item["item_revision"]
                )
            if (
                batch["terminal_at"] is not None
                or batch["cancel_requested"]
                or batch["status"] != "awaiting_review"
            ):
                raise CollectionImportStateConflictError()
            unstable_identity = db.execute(
                """SELECT 1 FROM collection_import_batch_items
                   WHERE batch_id=? AND state IN ('queued', 'previewing')
                   LIMIT 1""",
                (batch_id,),
            ).fetchone()
            if unstable_identity is not None:
                raise CollectionImportStateConflictError()
            if item["state"] == "failed":
                unresolved_attempt = db.execute(
                    """SELECT 1 FROM collection_import_save_attempts
                       WHERE batch_item_id=? AND (
                           attempt_phase != 'settled'
                           OR result IN ('none', 'unknown')
                       ) LIMIT 1""",
                    (batch_item_id,),
                ).fetchone()
                if unresolved_attempt is not None:
                    raise CollectionImportStateConflictError()

            latest_attempt = (
                db.execute(
                    """SELECT attempt_phase, result
                       FROM collection_import_save_attempts
                       WHERE batch_item_id=?
                       ORDER BY rowid DESC LIMIT 1""",
                    (batch_item_id,),
                ).fetchone()
                if item["state"] == "failed" and item["error_stage"] == "save"
                else None
            )
            retrying_known_save_failure = bool(
                latest_attempt is not None
                and latest_attempt["attempt_phase"] == "settled"
                and latest_attempt["result"] == "known_not_written"
            )
            preview_row = None
            preview_expired = False
            if item["state"] in {"ready", "needs_review"} or retrying_known_save_failure:
                preview_row = db.execute(
                    """SELECT payload_json, expires_at FROM collection_previews
                       WHERE preview_id=?""",
                    (item["preview_id"],),
                ).fetchone()
                preview_expired = (
                    preview_row is None
                    or preview_row["expires_at"] <= timestamp
                )

            if decision == "save":
                if (
                    item["state"] not in {"ready", "needs_review"}
                    and not retrying_known_save_failure
                ):
                    raise CollectionImportStateConflictError()
                if preview_expired:
                    expired_after_cas = True
                else:
                    assert preview_row is not None
                    preview = self._decode_json(
                        preview_row["payload_json"], None
                    )
                    if not isinstance(preview, dict):
                        raise CollectionImportStateConflictError()
                    title = ((preview.get("metadata") or {}).get("title") or {})
                    source_title = (
                        title.get("value", "").strip()
                        if title.get("source") != "none"
                        and isinstance(title.get("value"), str)
                        else ""
                    )
                    if not (
                        (draft.get("user_title") or "").strip()
                        or source_title
                        or draft.get("untitled_confirmed") is True
                    ):
                        raise CollectionImportRequestInvalidError()
            elif decision == "skip":
                if item["state"] not in {
                    "ready",
                    "needs_review",
                    "preview_expired",
                    "failed",
                }:
                    raise CollectionImportStateConflictError()
            else:
                raise CollectionImportRequestInvalidError()

            if expired_after_cas:
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET state=?, decision='pending',
                           review_revision=0, error_code='PREVIEW_EXPIRED',
                           error_stage='preview', terminal_reason=?,
                           item_revision=item_revision+1, updated_at=?
                       WHERE batch_id=? AND batch_item_id=?""",
                    (
                        "failed" if retrying_known_save_failure else "preview_expired",
                        "preview_failed" if retrying_known_save_failure else None,
                        timestamp,
                        batch_id,
                        batch_item_id,
                    ),
                )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)

            current_draft = self._decode_json(item["draft_json"], None)
            if (
                not expired_after_cas
                and not preview_expired
                and item["decision"] == decision
                and current_draft == draft
            ):
                snapshot = self._batch_snapshot_on_connection(db, batch_id)
                assert snapshot is not None
                return snapshot

            if not expired_after_cas:
                if preview_expired:
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='preview_expired', decision=?, draft_json=?,
                               review_revision=review_revision+1,
                               error_code='PREVIEW_EXPIRED',
                               error_stage='preview', terminal_reason=NULL,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_id=? AND batch_item_id=?""",
                        (
                            decision,
                            draft_json,
                            timestamp,
                            batch_id,
                            batch_item_id,
                        ),
                    )
                else:
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET decision=?, draft_json=?,
                               review_revision=review_revision+1,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_id=? AND batch_item_id=?""",
                        (
                            decision,
                            draft_json,
                            timestamp,
                            batch_id,
                            batch_item_id,
                        ),
                    )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)
                snapshot = self._batch_snapshot_on_connection(db, batch_id)
                assert snapshot is not None
                return snapshot

        # The matching command durably observes expiry, but does not apply its
        # stale review intent.  Raise only after the transaction has committed.
        if expired_after_cas:
            raise CollectionImportStateConflictError()
        raise AssertionError("review command completed without a result")

    def queue_repreview(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        expected_batch_revision: int,
        expected_item_revision: int,
        replacement_provided: bool,
        replacement_input: str | None,
        item_character_limit: int,
        input_utf8_limit: int,
        now: datetime,
    ) -> dict[str, Any]:
        """Queue repreview without allocating a preview identity or generation."""

        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                """SELECT revision, status, cancel_requested, terminal_at
                   FROM collection_import_batches WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            if batch["revision"] != expected_batch_revision:
                raise CollectionImportBatchRevisionConflictError(batch["revision"])

            item = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? AND batch_item_id=?""",
                (batch_id, batch_item_id),
            ).fetchone()
            if item is None:
                raise CollectionImportNotFoundError("BATCH_ITEM_NOT_FOUND")
            if item["item_revision"] != expected_item_revision:
                raise CollectionImportItemRevisionConflictError(
                    item["item_revision"]
                )
            if (
                batch["terminal_at"] is not None
                or batch["cancel_requested"]
                or batch["status"] in _TERMINAL_BATCH
            ):
                raise CollectionImportStateConflictError()
            allowed_state = item["state"] in {
                "ready",
                "needs_review",
                "preview_expired",
            } or (
                item["state"] == "failed" and item["error_stage"] == "preview"
            )
            if not allowed_state:
                raise CollectionImportStateConflictError()
            unresolved = db.execute(
                """SELECT 1 FROM collection_import_save_attempts
                   WHERE batch_item_id=? AND (
                       attempt_phase != 'settled' OR result IN ('none', 'unknown')
                   ) LIMIT 1""",
                (batch_item_id,),
            ).fetchone()
            if unresolved is not None:
                raise CollectionImportStateConflictError()
            if item["preview_generation"] >= 5:
                raise CollectionImportAttemptLimitError()

            if replacement_provided:
                selected_input = replacement_input
            elif item["pending_input_text"] is not None:
                selected_input = item["pending_input_text"]
            else:
                preview = self._preview_payload_on_connection(db, item["preview_id"])
                selected_input = (
                    preview.get("original_input") if preview is not None else None
                )
            if (
                not isinstance(selected_input, str)
                or not selected_input.strip()
                or len(selected_input) > item_character_limit
            ):
                if replacement_provided:
                    raise CollectionImportRequestInvalidError()
                raise CollectionImportStateConflictError()

            all_items = db.execute(
                """SELECT batch_item_id, pending_input_text, preview_id
                   FROM collection_import_batch_items
                   WHERE batch_id=? ORDER BY position ASC""",
                (batch_id,),
            ).fetchall()
            total_bytes = 0
            for member in all_items:
                if member["batch_item_id"] == batch_item_id:
                    member_input = selected_input
                elif member["pending_input_text"] is not None:
                    member_input = member["pending_input_text"]
                else:
                    preview = self._preview_payload_on_connection(
                        db, member["preview_id"]
                    )
                    member_input = (
                        preview.get("original_input")
                        if preview is not None
                        else None
                    )
                if (
                    not isinstance(member_input, str)
                    or not member_input.strip()
                    or len(member_input) > item_character_limit
                ):
                    raise CollectionImportStateConflictError()
                try:
                    total_bytes += len(member_input.encode("utf-8", errors="strict"))
                except UnicodeEncodeError:
                    raise CollectionImportRequestInvalidError() from None
                if total_bytes > input_utf8_limit:
                    raise CollectionImportRequestInvalidError()

            draft = self._decode_json(item["draft_json"], None)
            if not isinstance(draft, dict):
                raise CollectionImportStateConflictError()
            draft = dict(draft)
            draft["untitled_confirmed"] = False
            old_identity_url = item["identity_url"]
            db.execute(
                """UPDATE collection_import_batch_items
                   SET pending_input_text=?, preview_id=NULL, identity_url=NULL,
                       state='queued', decision='pending', review_revision=0,
                       preview_claim_token=NULL,
                       duplicate_of_batch_item_id=NULL, collection_item_id=NULL,
                       error_code=NULL, error_stage=NULL, terminal_reason=NULL,
                       draft_json=?, item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=?""",
                (
                    selected_input,
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    batch_id,
                    batch_item_id,
                ),
            )
            if old_identity_url:
                self._recalculate_identity_group_on_connection(
                    db,
                    batch_id=batch_id,
                    identity_url=old_identity_url,
                    now=timestamp,
                )
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            snapshot = self._batch_snapshot_on_connection(db, batch_id)
            assert snapshot is not None
            return snapshot

    def confirm_batch(
        self,
        *,
        batch_id: str,
        expected_batch_revision: int,
        freeze_request: Callable[
            [str, dict[str, Any], dict[str, Any], str, str],
            tuple[dict[str, Any], str, str],
        ],
        save_attempt_id_factory: Callable[[], str],
        now: datetime,
        preview_guard_seconds: int = 300,
        save_attempt_limit: int = 5,
    ) -> dict[str, Any]:
        """Atomically freeze every reviewed decision before any collection write."""

        timestamp = self._iso(now)
        guard_deadline = now + timedelta(seconds=preview_guard_seconds)
        expired_item_ids: list[str] = []
        expiring = False
        result: dict[str, Any] | None = None
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                """SELECT revision, status, cancel_requested, terminal_at
                   FROM collection_import_batches WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            if batch["revision"] != expected_batch_revision:
                raise CollectionImportBatchRevisionConflictError(batch["revision"])
            if (
                batch["terminal_at"] is not None
                or batch["cancel_requested"]
                or batch["status"] != "awaiting_review"
            ):
                raise CollectionImportStateConflictError()

            rows = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? ORDER BY position ASC""",
                (batch_id,),
            ).fetchall()
            if not rows or any(
                row["state"]
                in {
                    "queued",
                    "previewing",
                    "save_queued",
                    "saving",
                    "interrupted",
                    "outcome_unknown",
                    "cancelled",
                }
                for row in rows
            ):
                raise CollectionImportStateConflictError()

            frozen: list[
                tuple[sqlite3.Row, str, dict[str, Any], str, str]
            ] = []
            skip_items: list[tuple[str, str | None]] = []
            duplicate_item_ids: list[str] = []
            for row in rows:
                state = row["state"]
                decision = row["decision"]
                if state in {"saved", "already_exists", "skipped"}:
                    continue
                if state == "duplicate_in_batch":
                    duplicate_item_ids.append(row["batch_item_id"])
                    continue
                if state == "cancelled":
                    raise CollectionImportStateConflictError()
                if state not in {
                    "ready",
                    "needs_review",
                    "preview_expired",
                    "failed",
                }:
                    raise CollectionImportStateConflictError()
                if state == "failed":
                    unresolved_attempt = db.execute(
                        """SELECT 1 FROM collection_import_save_attempts
                           WHERE batch_item_id=? AND (
                               attempt_phase != 'settled'
                               OR result IN ('none', 'unknown')
                           ) LIMIT 1""",
                        (row["batch_item_id"],),
                    ).fetchone()
                    if unresolved_attempt is not None:
                        raise CollectionImportStateConflictError()
                if decision == "pending":
                    raise CollectionImportReviewIncompleteError()
                if decision == "skip":
                    skip_items.append(
                        (
                            row["batch_item_id"],
                            row["terminal_reason"] if state == "failed" else None,
                        )
                    )
                    continue
                if decision != "save":
                    raise CollectionImportReviewIncompleteError()
                latest_attempt = (
                    db.execute(
                        """SELECT attempt_phase, result
                           FROM collection_import_save_attempts
                           WHERE batch_item_id=?
                           ORDER BY rowid DESC LIMIT 1""",
                        (row["batch_item_id"],),
                    ).fetchone()
                    if state == "failed" and row["error_stage"] == "save"
                    else None
                )
                retrying_known_failure = bool(
                    latest_attempt is not None
                    and latest_attempt["attempt_phase"] == "settled"
                    and latest_attempt["result"] == "known_not_written"
                )
                if state not in {"ready", "needs_review"} and not retrying_known_failure:
                    raise CollectionImportReviewIncompleteError()
                if row["review_revision"] < 1 or not row["preview_id"]:
                    raise CollectionImportReviewIncompleteError()

                preview_row = db.execute(
                    """SELECT payload_json, expires_at FROM collection_previews
                       WHERE preview_id=?""",
                    (row["preview_id"],),
                ).fetchone()
                preview_payload = (
                    self._decode_json(preview_row["payload_json"], None)
                    if preview_row is not None
                    else None
                )
                try:
                    expires_at = datetime.fromisoformat(preview_row["expires_at"])
                    is_expired = expires_at <= now
                    is_expiring = expires_at < guard_deadline
                except (AttributeError, TypeError, ValueError):
                    is_expired = True
                    is_expiring = True
                if is_expired:
                    expired_item_ids.append(row["batch_item_id"])
                    continue
                if is_expiring:
                    expiring = True
                    continue
                if not isinstance(preview_payload, dict):
                    raise CollectionImportReviewIncompleteError()
                attempt_count = db.execute(
                    """SELECT COUNT(*) FROM collection_import_save_attempts
                       WHERE batch_item_id=?""",
                    (row["batch_item_id"],),
                ).fetchone()[0]
                if attempt_count >= save_attempt_limit:
                    raise CollectionImportAttemptLimitError()
                draft = self._decode_json(row["draft_json"], None)
                if not isinstance(draft, dict):
                    raise CollectionImportReviewIncompleteError()
                save_attempt_id = save_attempt_id_factory()
                try:
                    frozen_request, request_hash, key_hash = freeze_request(
                        row["preview_id"],
                        draft,
                        preview_payload,
                        row["batch_item_id"],
                        save_attempt_id,
                    )
                except Exception as exc:
                    raise CollectionImportReviewIncompleteError() from exc
                if (
                    not isinstance(frozen_request, dict)
                    or not frozen_request
                    or not isinstance(request_hash, str)
                    or not request_hash
                    or not isinstance(key_hash, str)
                    or not key_hash
                ):
                    raise CollectionImportReviewIncompleteError()
                frozen.append(
                    (row, save_attempt_id, frozen_request, request_hash, key_hash)
                )

            if expired_item_ids:
                for item_id in expired_item_ids:
                    item = next(row for row in rows if row["batch_item_id"] == item_id)
                    failed_after_save = (
                        item["state"] == "failed" and item["error_stage"] == "save"
                    )
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state=?, decision='pending', review_revision=0,
                               error_code='PREVIEW_EXPIRED', error_stage='preview',
                               terminal_reason=?, item_revision=item_revision+1,
                               updated_at=? WHERE batch_item_id=?""",
                        (
                            "failed" if failed_after_save else "preview_expired",
                            "preview_failed" if failed_after_save else None,
                            timestamp,
                            item_id,
                        ),
                    )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)
            elif expiring:
                pass
            else:
                attempt_ids = [attempt_id for _, attempt_id, *_ in frozen]
                if len(attempt_ids) != len(set(attempt_ids)) or any(
                    db.execute(
                        """SELECT 1 FROM collection_import_save_attempts
                           WHERE save_attempt_id=?""",
                        (attempt_id,),
                    ).fetchone()
                    is not None
                    for attempt_id in attempt_ids
                ):
                    raise CollectionImportStateConflictError()
                for row, attempt_id, request, request_hash, key_hash in frozen:
                    db.execute(
                        """UPDATE collection_import_save_attempts
                           SET frozen_request_json=NULL
                           WHERE batch_item_id=? AND attempt_phase='settled'""",
                        (row["batch_item_id"],),
                    )
                    db.execute(
                        """INSERT INTO collection_import_save_attempts(
                               save_attempt_id, batch_item_id, preview_id,
                               preview_generation, review_revision,
                               frozen_request_json, collection_request_hash,
                               idempotency_key_hash, attempt_phase, result,
                               claim_token, error_code, collection_item_id,
                               created_at, updated_at, settled_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'frozen', 'none',
                                     NULL, NULL, NULL, ?, ?, NULL)""",
                        (
                            attempt_id,
                            row["batch_item_id"],
                            row["preview_id"],
                            row["preview_generation"],
                            row["review_revision"],
                            json.dumps(
                                request,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            request_hash,
                            key_hash,
                            timestamp,
                            timestamp,
                        ),
                    )
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='save_queued', error_code=NULL,
                               error_stage=NULL, terminal_reason=NULL,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_item_id=?""",
                        (timestamp, row["batch_item_id"]),
                    )
                for item_id, terminal_reason in skip_items:
                    skipped_row = next(
                        row for row in rows if row["batch_item_id"] == item_id
                    )
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='skipped', terminal_reason=?,
                               error_code=NULL, error_stage=NULL,
                               pending_input_text=NULL,
                               display_label=?,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_item_id=?""",
                        (
                            terminal_reason,
                            self._safe_label_before_input_clear_on_connection(
                                db, skipped_row
                            ),
                            timestamp,
                            item_id,
                        ),
                    )
                for item_id in duplicate_item_ids:
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='skipped', decision='skip',
                               terminal_reason='duplicate_in_batch',
                               error_code=NULL, error_stage=NULL,
                               pending_input_text=NULL,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_item_id=?""",
                        (timestamp, item_id),
                    )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)
                result = self._batch_snapshot_on_connection(db, batch_id)

        if expired_item_ids or expiring:
            raise CollectionImportPreviewExpiringError()
        if result is None:
            raise AssertionError("confirm command completed without a snapshot")
        return result

    def claim_preview(
        self,
        *,
        batch_id: str,
        preview_id: str,
        claim_token: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT i.* FROM collection_import_batch_items i
                   JOIN collection_import_batches b ON b.batch_id=i.batch_id
                   WHERE i.batch_id=? AND i.state='queued'
                     AND i.preview_claim_token IS NULL
                     AND i.preview_generation < 5
                     AND b.cancel_requested=0 AND b.terminal_at IS NULL
                   ORDER BY i.position ASC LIMIT 1""",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            if row["pending_input_text"] is None:
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='interrupted', error_code='BATCH_INPUT_UNAVAILABLE',
                           error_stage='preview', item_revision=item_revision+1,
                           updated_at=? WHERE batch_item_id=?""",
                    (timestamp, row["batch_item_id"]),
                )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)
                return None
            next_generation = row["preview_generation"] + 1
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='previewing', preview_generation=?, preview_id=?,
                       preview_claim_token=?, item_revision=item_revision+1,
                       updated_at=? WHERE batch_item_id=? AND state='queued'
                       AND preview_claim_token IS NULL""",
                (
                    next_generation,
                    preview_id,
                    claim_token,
                    timestamp,
                    row["batch_item_id"],
                ),
            )
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            return {
                "batch_item_id": row["batch_item_id"],
                "input_text": row["pending_input_text"],
                "preview_id": preview_id,
                "preview_generation": next_generation,
                "claim_token": claim_token,
            }

    @staticmethod
    def _existing_collection_id(
        db: sqlite3.Connection, identity_url: str
    ) -> str | None:
        row = db.execute(
            """SELECT id FROM library_items WHERE identity_url=?
               UNION
               SELECT collection_item_id AS id FROM collection_url_aliases
               WHERE alias_url=? LIMIT 1""",
            (identity_url, identity_url),
        ).fetchone()
        return row["id"] if row is not None else None

    @staticmethod
    def _display_label(preview: dict[str, Any], position: int) -> str:
        metadata = preview.get("metadata")
        title = metadata.get("title") if isinstance(metadata, dict) else None
        if (
            isinstance(title, dict)
            and title.get("source") in _SAFE_PUBLIC_TITLE_SOURCES
            and isinstance(title.get("value"), str)
        ):
            cleaned: list[str] = []
            unsafe_control = False
            for character in title["value"]:
                if character.isspace():
                    cleaned.append(" ")
                elif unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
                    unsafe_control = True
                    break
                else:
                    cleaned.append(character)
            value = " ".join("".join(cleaned).split())
            # A public page may reflect an attacker-controlled URL as its
            # title.  Never retain the URL token: even a presently benign
            # value could carry userinfo, query or fragment data after a
            # redirect/cache refresh.  The normalized hostname below is the
            # only URL-derived display authority.
            if value and not unsafe_control and not _SAFE_URL_SCHEME.search(value):
                return value[:120]

        for field in ("canonical_url", "source_url", "identity_url"):
            raw_url = preview.get(field)
            if not isinstance(raw_url, str):
                continue
            try:
                normalized = normalize_identity_url(raw_url)
                host = (urlsplit(normalized).hostname or "").casefold().rstrip(".")
            except (UnicodeError, ValueError):
                continue
            if host:
                return host[:120]
        return f"第 {position + 1} 项（输入无效）"

    @staticmethod
    def _safe_input_display_label(input_text: Any, position: int) -> str:
        """Reduce exact input to a hostname or a fixed invalid-input label."""

        host = ""
        if isinstance(input_text, str):
            try:
                matches = list(_SAFE_URL_CANDIDATE.finditer(input_text))
                if len(matches) != 1:
                    raise ValueError("input must contain exactly one public URL")
                match = matches[0]
                candidate = match.group(0)
                whole_input = input_text.strip()
                if _SAFE_WHOLE_URL.fullmatch(whole_input):
                    candidate = whole_input
                else:
                    prefix = input_text[: match.start()]
                    wrappers: list[str] = []
                    while prefix and prefix[-1] in _SAFE_URL_WRAPPERS:
                        wrappers.append(prefix[-1])
                        prefix = prefix[:-1]
                    if (
                        not wrappers
                        and match.end() == len(input_text.rstrip())
                        and candidate.endswith("。")
                    ):
                        candidate = candidate[:-1]
                    for opening in reversed(wrappers):
                        closing = _SAFE_URL_WRAPPERS[opening]
                        if not candidate.endswith(closing):
                            break
                        if (
                            opening != closing
                            and candidate.count(closing)
                            <= candidate.count(opening)
                        ):
                            break
                        candidate = candidate[:-1]
                data_start = min(
                    (
                        candidate.index(separator)
                        for separator in ("?", "#")
                        if separator in candidate
                    ),
                    default=len(candidate),
                )
                if any(
                    scheme.start() < data_start
                    for scheme in list(_SAFE_URL_SCHEME.finditer(candidate))[1:]
                ):
                    raise ValueError("input contains ambiguous public URLs")
                normalized = normalize_identity_url(candidate)
                parsed = urlsplit(normalized)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("userinfo is not a safe display identity")
                # Accessing ``port`` is itself the required malformed-port
                # check.  The label deliberately excludes it, query and
                # fragment even when the URL is otherwise valid.
                _ = parsed.port
                host = (parsed.hostname or "").casefold().rstrip(".")
            except (UnicodeError, ValueError):
                host = ""
        return host[:120] if host else f"第 {position + 1} 项（输入无效）"

    @classmethod
    def _safe_label_before_input_clear_on_connection(
        cls, db: sqlite3.Connection, item: sqlite3.Row
    ) -> str:
        preview = cls._preview_payload_on_connection(db, item["preview_id"])
        if isinstance(preview, dict) and isinstance(
            preview.get("identity_url"), str
        ):
            return cls._display_label(preview, item["position"])
        return cls._safe_input_display_label(
            item["pending_input_text"], item["position"]
        )

    @classmethod
    def _cancel_items_on_connection(
        cls,
        db: sqlite3.Connection,
        rows: list[sqlite3.Row],
        *,
        now: str,
    ) -> int:
        """Cancel exactly the selected rows, sanitizing input in the same CAS."""

        changed = 0
        for row in rows:
            safe_label = cls._safe_label_before_input_clear_on_connection(db, row)
            changed += db.execute(
                """UPDATE collection_import_batch_items
                   SET state='cancelled', decision='pending',
                       pending_input_text=NULL, preview_claim_token=NULL,
                       display_label=?, error_code=NULL, error_stage=NULL,
                       terminal_reason='cancelled_by_user',
                       item_revision=item_revision+1, updated_at=?
                   WHERE batch_item_id=? AND item_revision=? AND state=?""",
                (
                    safe_label,
                    now,
                    row["batch_item_id"],
                    row["item_revision"],
                    row["state"],
                ),
            ).rowcount
        if changed != len(rows):
            raise sqlite3.IntegrityError("cancel item state lost")
        return changed

    @classmethod
    def _preview_payload_on_connection(
        cls, db: sqlite3.Connection, preview_id: str | None
    ) -> dict[str, Any] | None:
        if not preview_id:
            return None
        row = db.execute(
            "SELECT payload_json FROM collection_previews WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            return None
        payload = cls._decode_json(row["payload_json"], None)
        return payload if isinstance(payload, dict) else None

    @classmethod
    def _recalculate_identity_group_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        batch_id: str,
        identity_url: str,
        now: str,
    ) -> None:
        group = db.execute(
            """SELECT * FROM collection_import_batch_items
               WHERE batch_id=? AND identity_url=? AND preview_id IS NOT NULL
               ORDER BY position ASC""",
            (batch_id, identity_url),
        ).fetchall()
        if not group:
            return
        representative = group[0]
        existing_id = cls._existing_collection_id(db, identity_url)
        representative_preview = cls._preview_payload_on_connection(
            db, representative["preview_id"]
        ) or {}
        title = ((representative_preview.get("metadata") or {}).get("title") or {})
        has_title = bool(
            title.get("source") != "none"
            and isinstance(title.get("value"), str)
            and title["value"].strip()
        )
        for index, member in enumerate(group):
            if index == 0:
                state = "already_exists" if existing_id else (
                    "ready" if has_title else "needs_review"
                )
                duplicate_of = None
                collection_item_id = existing_id
            else:
                state = "duplicate_in_batch"
                duplicate_of = representative["batch_item_id"]
                collection_item_id = None
            if (
                member["state"] != state
                or member["duplicate_of_batch_item_id"] != duplicate_of
                or member["collection_item_id"] != collection_item_id
                or member["decision"] != "pending"
            ):
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET state=?, decision='pending', review_revision=0,
                           duplicate_of_batch_item_id=?, collection_item_id=?,
                           error_code=NULL, error_stage=NULL, terminal_reason=NULL,
                           item_revision=item_revision+1, updated_at=?
                       WHERE batch_item_id=?""",
                    (
                        state,
                        duplicate_of,
                        collection_item_id,
                        now,
                        member["batch_item_id"],
                    ),
                )

    @classmethod
    def _publish_success_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        batch_id: str,
        batch_item_id: str,
        claim_token: str,
        preview_id: str,
        now: str,
    ) -> bool:
        item = db.execute(
            """SELECT i.*, b.cancel_requested
               FROM collection_import_batch_items i
               JOIN collection_import_batches b ON b.batch_id=i.batch_id
               WHERE i.batch_id=? AND i.batch_item_id=? AND i.state='previewing'
                 AND i.preview_claim_token=? AND i.preview_id=?""",
            (batch_id, batch_item_id, claim_token, preview_id),
        ).fetchone()
        if item is None:
            return False
        preview_row = db.execute(
            "SELECT payload_json FROM collection_previews WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if preview_row is None:
            return False
        preview = cls._decode_json(preview_row["payload_json"], None)
        if not isinstance(preview, dict) or not isinstance(
            preview.get("identity_url"), str
        ):
            return False
        identity_url = preview["identity_url"]
        if item["cancel_requested"]:
            changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET pending_input_text=NULL, identity_url=?,
                       preview_claim_token=NULL, display_label=?,
                       state='cancelled', decision='pending',
                       error_code=NULL, error_stage=NULL,
                       terminal_reason='cancelled_by_user',
                       item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='previewing'
                     AND preview_claim_token=? AND preview_id=?""",
                (
                    identity_url,
                    cls._display_label(preview, item["position"]),
                    now,
                    batch_id,
                    batch_item_id,
                    claim_token,
                    preview_id,
                ),
            ).rowcount
            if changed != 1:
                return False
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (now, batch_id),
            )
            cls._derive_batch_state_on_connection(db, batch_id, now)
            return True
        db.execute(
            """UPDATE collection_import_batch_items
               SET pending_input_text=NULL, identity_url=?, preview_claim_token=NULL,
                   display_label=?, error_code=NULL, error_stage=NULL,
                   terminal_reason=NULL, updated_at=?
               WHERE batch_item_id=?""",
            (
                identity_url,
                cls._display_label(preview, item["position"]),
                now,
                batch_item_id,
            ),
        )
        cls._recalculate_identity_group_on_connection(
            db,
            batch_id=batch_id,
            identity_url=identity_url,
            now=now,
        )
        db.execute(
            """UPDATE collection_import_batches
               SET revision=revision+1, updated_at=? WHERE batch_id=?""",
            (now, batch_id),
        )
        cls._derive_batch_state_on_connection(db, batch_id, now)
        return True

    def publish_preview_success(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        claim_token: str,
        preview_id: str,
        now: datetime,
    ) -> bool:
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._publish_success_on_connection(
                db,
                batch_id=batch_id,
                batch_item_id=batch_item_id,
                claim_token=claim_token,
                preview_id=preview_id,
                now=self._iso(now),
            )

    def publish_preview_failure(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        claim_token: str,
        error_code: str,
        now: datetime,
    ) -> bool:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                """SELECT i.*, b.cancel_requested
                   FROM collection_import_batch_items i
                   JOIN collection_import_batches b ON b.batch_id=i.batch_id
                   WHERE i.batch_id=? AND i.batch_item_id=?
                     AND i.state='previewing' AND i.preview_claim_token=?""",
                (batch_id, batch_item_id, claim_token),
            ).fetchone()
            if target is None:
                return False
            cancelled = bool(target["cancel_requested"])
            safe_label = self._safe_label_before_input_clear_on_connection(
                db, target
            )
            cursor = db.execute(
                """UPDATE collection_import_batch_items
                   SET state=?, preview_claim_token=NULL,
                       pending_input_text=CASE WHEN ? THEN NULL
                                               ELSE pending_input_text END,
                       display_label=?, decision='pending',
                       error_code=?, error_stage=?, terminal_reason=?,
                       item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='previewing'
                     AND preview_claim_token=?""",
                (
                    "cancelled" if cancelled else "failed",
                    cancelled,
                    safe_label,
                    None if cancelled else error_code,
                    None if cancelled else "preview",
                    "cancelled_by_user" if cancelled else "preview_failed",
                    timestamp,
                    batch_id,
                    batch_item_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def publish_preview_interrupted(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                """SELECT i.*, b.cancel_requested
                   FROM collection_import_batch_items i
                   JOIN collection_import_batches b ON b.batch_id=i.batch_id
                   WHERE i.batch_id=? AND i.batch_item_id=?
                     AND i.state='previewing' AND i.preview_claim_token=?""",
                (batch_id, batch_item_id, claim_token),
            ).fetchone()
            if target is None:
                return False
            cancelled = bool(target["cancel_requested"])
            safe_label = (
                self._safe_label_before_input_clear_on_connection(db, target)
                if cancelled
                else target["display_label"]
            )
            cursor = db.execute(
                """UPDATE collection_import_batch_items
                   SET state=?, preview_claim_token=NULL,
                       pending_input_text=CASE WHEN ? THEN NULL
                                               ELSE pending_input_text END,
                       display_label=?,
                       decision='pending', error_code=?, error_stage=?,
                       terminal_reason=?,
                       item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='previewing'
                     AND preview_claim_token=?""",
                (
                    "cancelled" if cancelled else "interrupted",
                    cancelled,
                    safe_label,
                    None if cancelled else "PROCESS_INTERRUPTED",
                    None if cancelled else "preview",
                    "cancelled_by_user" if cancelled else None,
                    timestamp,
                    batch_id,
                    batch_item_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                return False
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def mark_dispatch_failed(self, batch_id: str, now: datetime) -> None:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                "SELECT cancel_requested FROM collection_import_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                return
            cancelled = bool(batch["cancel_requested"])
            if cancelled:
                targets = db.execute(
                    """SELECT * FROM collection_import_batch_items
                       WHERE batch_id=? AND state='queued'
                         AND preview_claim_token IS NULL""",
                    (batch_id,),
                ).fetchall()
                changed = self._cancel_items_on_connection(
                    db, list(targets), now=timestamp
                )
            else:
                changed = db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='interrupted', decision='pending',
                           error_code='BATCH_DISPATCH_INTERRUPTED',
                           error_stage='preview', terminal_reason=NULL,
                           item_revision=item_revision+1, updated_at=?
                       WHERE batch_id=? AND state='queued'
                         AND preview_claim_token IS NULL""",
                    (timestamp, batch_id),
                ).rowcount
            if changed:
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)

    def claim_save(
        self,
        *,
        batch_id: str,
        claim_token: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Claim exactly one frozen attempt, globally and in position order."""

        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active_claim = db.execute(
                """SELECT 1 FROM collection_import_save_attempts
                   WHERE claim_token IS NOT NULL LIMIT 1"""
            ).fetchone()
            if active_claim is not None:
                return None
            row = db.execute(
                """SELECT i.batch_id, i.batch_item_id, i.position,
                          i.identity_url, a.*
                   FROM collection_import_batch_items i
                   JOIN collection_import_batches b ON b.batch_id=i.batch_id
                   JOIN collection_import_save_attempts a
                     ON a.batch_item_id=i.batch_item_id
                   WHERE i.batch_id=? AND i.state='save_queued'
                     AND b.status='saving' AND b.cancel_requested=0
                     AND b.terminal_at IS NULL
                     AND a.attempt_phase='frozen' AND a.result='none'
                     AND a.claim_token IS NULL
                   ORDER BY i.position ASC, a.created_at ASC,
                            a.save_attempt_id ASC LIMIT 1""",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            claimed = db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='claimed', claim_token=?, updated_at=?
                   WHERE save_attempt_id=? AND attempt_phase='frozen'
                     AND result='none' AND claim_token IS NULL""",
                (claim_token, timestamp, row["save_attempt_id"]),
            ).rowcount
            if claimed != 1:
                return None
            changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET state='saving', error_code=NULL, error_stage=NULL,
                       terminal_reason=NULL, item_revision=item_revision+1,
                       updated_at=?
                   WHERE batch_item_id=? AND state='save_queued'""",
                (timestamp, row["batch_item_id"]),
            ).rowcount
            if changed != 1:
                raise sqlite3.IntegrityError("save item claim lost")
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            preview = self._preview_payload_on_connection(db, row["preview_id"])
            return {
                "batch_id": row["batch_id"],
                "batch_item_id": row["batch_item_id"],
                "position": row["position"],
                "identity_url": row["identity_url"],
                "save_attempt_id": row["save_attempt_id"],
                "preview_id": row["preview_id"],
                "preview": preview,
                "frozen_request_json": row["frozen_request_json"],
                "collection_request_hash": row["collection_request_hash"],
                "idempotency_key_hash": row["idempotency_key_hash"],
                "claim_token": claim_token,
            }

    def has_pending_save_work(self, batch_id: str) -> bool:
        """Return whether a saving batch still has an unclaimed frozen item.

        A coordinator may lose the race for the global database claim to a
        different coordinator instance.  The durable queue is still accepted
        work in that case, so the losing coordinator must distinguish
        contention from a genuinely drained batch and wait for another claim
        opportunity.
        """

        with self._repository._connect() as db:
            return (
                db.execute(
                    """SELECT 1
                       FROM collection_import_batch_items i
                       JOIN collection_import_batches b ON b.batch_id=i.batch_id
                       JOIN collection_import_save_attempts a
                         ON a.batch_item_id=i.batch_item_id
                       WHERE i.batch_id=? AND i.state='save_queued'
                         AND b.status='saving' AND b.cancel_requested=0
                         AND b.terminal_at IS NULL
                         AND a.attempt_phase='frozen' AND a.result='none'
                         AND a.claim_token IS NULL
                       LIMIT 1""",
                    (batch_id,),
                ).fetchone()
                is not None
            )

    def mark_save_call_started(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='call_started', updated_at=?
                   WHERE save_attempt_id=? AND batch_item_id=?
                     AND attempt_phase='claimed' AND result='none'
                     AND claim_token=?""",
                (timestamp, save_attempt_id, batch_item_id, claim_token),
            ).rowcount
            if changed != 1:
                return False
            item_changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
                (timestamp, batch_id, batch_item_id),
            ).rowcount
            if item_changed != 1:
                raise sqlite3.IntegrityError("save call-start item state lost")
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def record_save_result(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        result: str,
        collection_item_id: str | None,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        """Durably record an observed known result before changing item state."""

        if result not in {"success", "exists", "known_not_written"}:
            raise ValueError("save result cannot be recorded as known")
        if result in {"success", "exists"} and not collection_item_id:
            raise ValueError("known save result is missing collection identity")
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='result_observed', result=?, error_code=?,
                       collection_item_id=?, updated_at=?
                   WHERE save_attempt_id=? AND batch_item_id=?
                     AND attempt_phase='call_started' AND result='none'
                     AND claim_token=?""",
                (
                    result,
                    error_code,
                    collection_item_id,
                    timestamp,
                    save_attempt_id,
                    batch_item_id,
                    claim_token,
                ),
            ).rowcount
            if changed != 1:
                return False
            item_changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET item_revision=item_revision+1, updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
                (timestamp, batch_id, batch_item_id),
            ).rowcount
            if item_changed != 1:
                raise sqlite3.IntegrityError("save observed item state lost")
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    @classmethod
    def _settle_observed_save_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        now: str,
    ) -> bool:
        attempt = db.execute(
            """SELECT result, collection_item_id, error_code
               FROM collection_import_save_attempts
               WHERE save_attempt_id=? AND batch_item_id=?
                 AND attempt_phase='result_observed'
                 AND result IN ('success', 'exists', 'known_not_written')
                 AND claim_token=?""",
            (save_attempt_id, batch_item_id, claim_token),
        ).fetchone()
        if attempt is None:
            return False
        batch = db.execute(
            """SELECT cancel_requested FROM collection_import_batches
               WHERE batch_id=?""",
            (batch_id,),
        ).fetchone()
        if batch is None:
            return False
        cancel_requested = bool(batch["cancel_requested"])
        item = db.execute(
            """SELECT * FROM collection_import_batch_items
               WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
            (batch_id, batch_item_id),
        ).fetchone()
        if item is None:
            return False
        safe_label = cls._safe_label_before_input_clear_on_connection(db, item)
        result = attempt["result"]
        collection_item_id = attempt["collection_item_id"]
        error_code = attempt["error_code"]
        if result in {"success", "exists"} and not collection_item_id:
            raise sqlite3.IntegrityError("save result is missing collection identity")
        if result == "success":
            item_state = "saved"
            decision = "save"
            terminal_reason = None
            error_stage = None
        elif result == "exists":
            item_state = "already_exists"
            decision = "save"
            terminal_reason = None
            error_stage = None
        elif cancel_requested:
            item_state = "cancelled"
            decision = "pending"
            terminal_reason = "cancelled_by_user"
            error_stage = None
            collection_item_id = None
        else:
            item_state = "failed"
            decision = "pending"
            terminal_reason = (
                "validation_failed"
                if error_code == "COLLECTION_VALIDATION_FAILED"
                else "preview_failed"
                if error_code == "PREVIEW_EXPIRED"
                else "save_failed"
            )
            error_stage = "preview" if error_code == "PREVIEW_EXPIRED" else "save"
            collection_item_id = None
        db.execute(
            """UPDATE collection_import_save_attempts
               SET attempt_phase='settled', result=?, claim_token=NULL,
                   error_code=?, collection_item_id=?, updated_at=?, settled_at=?
               WHERE save_attempt_id=?""",
            (
                result,
                error_code,
                collection_item_id,
                now,
                now,
                save_attempt_id,
            ),
        )
        changed = db.execute(
            """UPDATE collection_import_batch_items
               SET state=?, decision=?, collection_item_id=?, error_code=?,
                   error_stage=?, terminal_reason=?,
                   pending_input_text=NULL, display_label=?,
                   item_revision=item_revision+1, updated_at=?
               WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
            (
                item_state,
                decision,
                collection_item_id,
                None if item_state == "cancelled" else error_code,
                error_stage,
                terminal_reason,
                safe_label,
                now,
                batch_id,
                batch_item_id,
            ),
        ).rowcount
        if changed != 1:
            raise sqlite3.IntegrityError("save item settlement lost")
        db.execute(
            """UPDATE collection_import_batches
               SET revision=revision+1, updated_at=? WHERE batch_id=?""",
            (now, batch_id),
        )
        cls._derive_batch_state_on_connection(db, batch_id, now)
        return True

    def settle_observed_save(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._settle_observed_save_on_connection(
                db,
                batch_id=batch_id,
                batch_item_id=batch_item_id,
                save_attempt_id=save_attempt_id,
                claim_token=claim_token,
                now=self._iso(now),
            )

    def reconcile_save_after_exception(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        idempotency_key: str,
        now: datetime,
    ) -> str | None:
        """Use only local facts, checking idempotency before identity."""

        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                """SELECT a.collection_request_hash, i.identity_url
                   FROM collection_import_save_attempts a
                   JOIN collection_import_batch_items i
                     ON i.batch_item_id=a.batch_item_id
                   WHERE a.save_attempt_id=? AND a.batch_item_id=?
                     AND a.attempt_phase='call_started' AND a.result='none'
                     AND a.claim_token=? AND i.batch_id=? AND i.state='saving'""",
                (
                    save_attempt_id,
                    batch_item_id,
                    claim_token,
                    batch_id,
                ),
            ).fetchone()
            if attempt is None:
                return None
            idempotency = db.execute(
                """SELECT request_hash, collection_item_id
                   FROM collection_idempotency_keys WHERE idempotency_key=?""",
                (idempotency_key,),
            ).fetchone()
            if idempotency is not None:
                if idempotency["request_hash"] != attempt["collection_request_hash"]:
                    return None
                db.execute(
                    """UPDATE collection_import_save_attempts
                       SET attempt_phase='result_observed', result='success',
                           collection_item_id=?, error_code=NULL, updated_at=?
                       WHERE save_attempt_id=?""",
                    (idempotency["collection_item_id"], timestamp, save_attempt_id),
                )
                self._settle_observed_save_on_connection(
                    db,
                    batch_id=batch_id,
                    batch_item_id=batch_item_id,
                    save_attempt_id=save_attempt_id,
                    claim_token=claim_token,
                    now=timestamp,
                )
                return "success"
            existing_id = (
                self._existing_collection_id(db, attempt["identity_url"])
                if attempt["identity_url"]
                else None
            )
            if existing_id:
                db.execute(
                    """UPDATE collection_import_save_attempts
                       SET attempt_phase='result_observed', result='exists',
                           collection_item_id=?, error_code=NULL, updated_at=?
                       WHERE save_attempt_id=?""",
                    (existing_id, timestamp, save_attempt_id),
                )
                self._settle_observed_save_on_connection(
                    db,
                    batch_id=batch_id,
                    batch_item_id=batch_item_id,
                    save_attempt_id=save_attempt_id,
                    claim_token=claim_token,
                    now=timestamp,
                )
                return "exists"
            return None

    def mark_save_outcome_unknown(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='result_observed', result='unknown',
                       claim_token=NULL, error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                       updated_at=?
                   WHERE save_attempt_id=? AND batch_item_id=?
                     AND attempt_phase='call_started' AND result='none'
                     AND claim_token=?""",
                (timestamp, save_attempt_id, batch_item_id, claim_token),
            ).rowcount
            if changed != 1:
                return False
            item_changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET state='outcome_unknown',
                       error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                       error_stage='save', item_revision=item_revision+1,
                       updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
                (timestamp, batch_id, batch_item_id),
            ).rowcount
            if item_changed != 1:
                raise sqlite3.IntegrityError("save unknown settlement lost")
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def settle_abandoned_save_claim(
        self,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        claim_token: str,
        now: datetime,
    ) -> bool:
        """Fail closed after an in-process worker exception.

        This is deliberately one local SQLite transaction guarded by the
        worker's claim token.  A pre-call claim is known not to have invoked
        ``CollectionService`` and becomes interrupted for explicit resume.  A
        call-started attempt is reconciled from the exact server idempotency
        record first and identity second; absent proof becomes
        ``outcome_unknown``.  A durable known result is never downgraded.
        """

        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute(
                """SELECT a.attempt_phase, a.result, a.collection_request_hash,
                          a.collection_item_id, a.error_code, i.identity_url,
                          i.state, i.batch_item_id, i.item_revision, i.position,
                          i.preview_id, i.pending_input_text, i.display_label,
                          b.cancel_requested
                   FROM collection_import_save_attempts a
                   JOIN collection_import_batch_items i
                     ON i.batch_item_id=a.batch_item_id
                   JOIN collection_import_batches b ON b.batch_id=i.batch_id
                   WHERE a.save_attempt_id=? AND a.batch_item_id=?
                     AND a.claim_token=? AND i.batch_id=?""",
                (
                    save_attempt_id,
                    batch_item_id,
                    claim_token,
                    batch_id,
                ),
            ).fetchone()
            if attempt is None:
                return False

            phase = attempt["attempt_phase"]
            result = attempt["result"]
            if phase == "result_observed" and result in {
                "success",
                "exists",
                "known_not_written",
            }:
                return self._settle_observed_save_on_connection(
                    db,
                    batch_id=batch_id,
                    batch_item_id=batch_item_id,
                    save_attempt_id=save_attempt_id,
                    claim_token=claim_token,
                    now=timestamp,
                )

            if phase == "claimed" and result == "none":
                if attempt["cancel_requested"]:
                    changed = db.execute(
                        """UPDATE collection_import_save_attempts
                           SET attempt_phase='settled', result='known_not_written',
                               claim_token=NULL, frozen_request_json=NULL,
                               error_code='BATCH_CANCELLED_BEFORE_SAVE',
                               updated_at=?, settled_at=?
                           WHERE save_attempt_id=? AND batch_item_id=?
                             AND attempt_phase='claimed' AND result='none'
                             AND claim_token=?""",
                        (
                            timestamp,
                            timestamp,
                            save_attempt_id,
                            batch_item_id,
                            claim_token,
                        ),
                    ).rowcount
                    if changed != 1:
                        return False
                    self._cancel_items_on_connection(
                        db, [attempt], now=timestamp
                    )
                    db.execute(
                        """UPDATE collection_import_batches
                           SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                        (timestamp, batch_id),
                    )
                    self._derive_batch_state_on_connection(
                        db, batch_id, timestamp
                    )
                    return True
                changed = db.execute(
                    """UPDATE collection_import_save_attempts
                       SET attempt_phase='frozen', claim_token=NULL,
                           error_code=NULL, updated_at=?
                       WHERE save_attempt_id=? AND batch_item_id=?
                         AND attempt_phase='claimed' AND result='none'
                         AND claim_token=?""",
                    (
                        timestamp,
                        save_attempt_id,
                        batch_item_id,
                        claim_token,
                    ),
                ).rowcount
                if changed != 1:
                    return False
                item_changed = db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='interrupted', error_code='PROCESS_INTERRUPTED',
                           error_stage='save', item_revision=item_revision+1,
                           updated_at=?
                       WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
                    (timestamp, batch_id, batch_item_id),
                ).rowcount
                if item_changed != 1:
                    raise sqlite3.IntegrityError(
                        "abandoned save claim item state lost"
                    )
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)
                return True

            observed_result: str | None = None
            observed_collection_id: str | None = None
            if phase == "call_started" and result == "none":
                key = collection_import_save_idempotency_key(
                    batch_id, batch_item_id, save_attempt_id
                )
                idempotency = db.execute(
                    """SELECT request_hash, collection_item_id
                       FROM collection_idempotency_keys WHERE idempotency_key=?""",
                    (key,),
                ).fetchone()
                if idempotency is not None:
                    if (
                        idempotency["request_hash"]
                        == attempt["collection_request_hash"]
                    ):
                        observed_result = "success"
                        observed_collection_id = idempotency["collection_item_id"]
                elif attempt["identity_url"]:
                    observed_collection_id = self._existing_collection_id(
                        db, attempt["identity_url"]
                    )
                    if observed_collection_id:
                        observed_result = "exists"

            if observed_result is not None:
                db.execute(
                    """UPDATE collection_import_save_attempts
                       SET attempt_phase='result_observed', result=?,
                           collection_item_id=?, error_code=NULL, updated_at=?
                       WHERE save_attempt_id=? AND batch_item_id=?
                         AND claim_token=?""",
                    (
                        observed_result,
                        observed_collection_id,
                        timestamp,
                        save_attempt_id,
                        batch_item_id,
                        claim_token,
                    ),
                )
                return self._settle_observed_save_on_connection(
                    db,
                    batch_id=batch_id,
                    batch_item_id=batch_item_id,
                    save_attempt_id=save_attempt_id,
                    claim_token=claim_token,
                    now=timestamp,
                )

            changed = db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='result_observed', result='unknown',
                       claim_token=NULL,
                       error_code='BATCH_SAVE_OUTCOME_UNKNOWN', updated_at=?
                   WHERE save_attempt_id=? AND batch_item_id=?
                     AND claim_token=?""",
                (timestamp, save_attempt_id, batch_item_id, claim_token),
            ).rowcount
            if changed != 1:
                return False
            item_changed = db.execute(
                """UPDATE collection_import_batch_items
                   SET state='outcome_unknown',
                       error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                       error_stage='save', item_revision=item_revision+1,
                       updated_at=?
                   WHERE batch_id=? AND batch_item_id=? AND state='saving'""",
                (timestamp, batch_id, batch_item_id),
            ).rowcount
            if item_changed != 1:
                raise sqlite3.IntegrityError(
                    "abandoned save outcome state lost"
                )
            db.execute(
                """UPDATE collection_import_batches
                   SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            return True

    def mark_save_dispatch_failed(self, batch_id: str, now: datetime) -> None:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                "SELECT cancel_requested FROM collection_import_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                return
            cancelled = bool(batch["cancel_requested"])
            if cancelled:
                db.execute(
                    """UPDATE collection_import_save_attempts
                       SET attempt_phase='settled', result='known_not_written',
                           claim_token=NULL, frozen_request_json=NULL,
                           error_code='BATCH_CANCELLED_BEFORE_SAVE',
                           updated_at=?, settled_at=?
                       WHERE save_attempt_id IN (
                           SELECT a.save_attempt_id
                           FROM collection_import_save_attempts a
                           JOIN collection_import_batch_items i
                             ON i.batch_item_id=a.batch_item_id
                           WHERE i.batch_id=? AND i.state='save_queued'
                             AND a.attempt_phase='frozen' AND a.result='none'
                             AND a.claim_token IS NULL
                       )""",
                    (timestamp, timestamp, batch_id),
                )
                targets = db.execute(
                    """SELECT * FROM collection_import_batch_items
                       WHERE batch_id=? AND state='save_queued'""",
                    (batch_id,),
                ).fetchall()
                changed = self._cancel_items_on_connection(
                    db, list(targets), now=timestamp
                )
            else:
                changed = db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='interrupted', pending_input_text=NULL,
                           error_code='BATCH_DISPATCH_INTERRUPTED',
                           error_stage='save', terminal_reason=NULL,
                           item_revision=item_revision+1, updated_at=?
                       WHERE batch_id=? AND state='save_queued'""",
                    (timestamp, batch_id),
                ).rowcount
            if changed:
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
                self._derive_batch_state_on_connection(db, batch_id, timestamp)

    @classmethod
    def _settle_locally_reconciled_attempt_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        batch_id: str,
        batch_item_id: str,
        save_attempt_id: str,
        result: str,
        collection_item_id: str | None,
        now: str,
        cancelled: bool = False,
        error_code: str | None = None,
    ) -> None:
        if result not in {"success", "exists", "known_not_written"}:
            raise ValueError("local save reconciliation must be known")
        if result in {"success", "exists"} and not collection_item_id:
            raise sqlite3.IntegrityError(
                "reconciled save result is missing collection identity"
            )
        item = db.execute(
            """SELECT * FROM collection_import_batch_items
               WHERE batch_id=? AND batch_item_id=?""",
            (batch_id, batch_item_id),
        ).fetchone()
        if item is None:
            raise sqlite3.IntegrityError("reconciled save item is missing")
        safe_label = cls._safe_label_before_input_clear_on_connection(db, item)
        if result == "success":
            item_state = "saved"
            decision = "save"
            terminal_reason = None
            item_error = None
            error_stage = None
        elif result == "exists":
            item_state = "already_exists"
            decision = "save"
            terminal_reason = None
            item_error = None
            error_stage = None
        elif cancelled:
            item_state = "cancelled"
            decision = "pending"
            terminal_reason = "cancelled_by_user"
            item_error = None
            error_stage = None
            collection_item_id = None
        else:
            item_state = "failed"
            decision = "pending"
            terminal_reason = (
                "validation_failed"
                if error_code == "COLLECTION_VALIDATION_FAILED"
                else "preview_failed"
                if error_code == "PREVIEW_EXPIRED"
                else "save_failed"
            )
            item_error = error_code
            error_stage = "preview" if error_code == "PREVIEW_EXPIRED" else "save"
            collection_item_id = None
        db.execute(
            """UPDATE collection_import_save_attempts
               SET attempt_phase='settled', result=?, claim_token=NULL,
                   frozen_request_json=NULL,
                   collection_item_id=?, error_code=?, updated_at=?, settled_at=?
               WHERE save_attempt_id=? AND batch_item_id=?""",
            (
                result,
                collection_item_id,
                error_code,
                now,
                now,
                save_attempt_id,
                batch_item_id,
            ),
        )
        changed = db.execute(
            """UPDATE collection_import_batch_items
               SET state=?, decision=?, collection_item_id=?,
                   pending_input_text=NULL, error_code=?, error_stage=?,
                   terminal_reason=?, preview_claim_token=NULL,
                   display_label=?,
                   item_revision=item_revision+1, updated_at=?
               WHERE batch_id=? AND batch_item_id=?""",
            (
                item_state,
                decision,
                collection_item_id,
                item_error,
                error_stage,
                terminal_reason,
                safe_label,
                now,
                batch_id,
                batch_item_id,
            ),
        ).rowcount
        if changed != 1:
            raise sqlite3.IntegrityError("reconciled save item is missing")

    @classmethod
    def _reconcile_unknown_attempt_on_connection(
        cls,
        db: sqlite3.Connection,
        *,
        batch_id: str,
        item: sqlite3.Row,
        attempt: sqlite3.Row,
        now: str,
    ) -> bool:
        """Settle an unknown attempt only from positive local proof.

        A healthy double miss does not re-claim an attempt that has already
        been durably classified ``unknown``.  It remains blocked until a
        future positive idempotency/identity fact appears.
        """

        key = collection_import_save_idempotency_key(
            batch_id, item["batch_item_id"], attempt["save_attempt_id"]
        )
        idempotency = db.execute(
            """SELECT request_hash, collection_item_id
               FROM collection_idempotency_keys WHERE idempotency_key=?""",
            (key,),
        ).fetchone()
        if idempotency is not None:
            if idempotency["request_hash"] != attempt["collection_request_hash"]:
                return False
            cls._settle_locally_reconciled_attempt_on_connection(
                db,
                batch_id=batch_id,
                batch_item_id=item["batch_item_id"],
                save_attempt_id=attempt["save_attempt_id"],
                result="success",
                collection_item_id=idempotency["collection_item_id"],
                now=now,
            )
            return True
        existing_id = (
            cls._existing_collection_id(db, item["identity_url"])
            if item["identity_url"]
            else None
        )
        if not existing_id:
            return False
        cls._settle_locally_reconciled_attempt_on_connection(
            db,
            batch_id=batch_id,
            batch_item_id=item["batch_item_id"],
            save_attempt_id=attempt["save_attempt_id"],
            result="exists",
            collection_item_id=existing_id,
            now=now,
        )
        return True

    def cancel_batch(
        self,
        *,
        batch_id: str,
        expected_batch_revision: int,
        now: datetime,
    ) -> dict[str, Any]:
        timestamp = self._iso(now)
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                """SELECT revision, status, cancel_requested, terminal_at
                   FROM collection_import_batches WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            if batch["revision"] != expected_batch_revision:
                raise CollectionImportBatchRevisionConflictError(batch["revision"])
            if batch["terminal_at"] is not None:
                if batch["status"] == "cancelled" and batch["cancel_requested"]:
                    snapshot = self._batch_snapshot_on_connection(db, batch_id)
                    assert snapshot is not None
                    return snapshot
                raise CollectionImportStateConflictError()
            if batch["cancel_requested"]:
                snapshot = self._batch_snapshot_on_connection(db, batch_id)
                assert snapshot is not None
                return snapshot

            # The intent commits before any state convergence.  Claim
            # transactions that lose this race can no longer acquire work.
            db.execute(
                """UPDATE collection_import_batches
                   SET cancel_requested=1, revision=revision+1, updated_at=?
                   WHERE batch_id=? AND cancel_requested=0""",
                (timestamp, batch_id),
            )
            # Frozen work is positive proof that CollectionService was never
            # called.  Settle that exact attempt rather than creating a new one.
            frozen = db.execute(
                """SELECT i.batch_item_id, a.save_attempt_id
                   FROM collection_import_batch_items i
                   JOIN collection_import_save_attempts a
                     ON a.batch_item_id=i.batch_item_id
                   WHERE i.batch_id=? AND i.state IN ('save_queued','interrupted','saving')
                     AND a.attempt_phase='frozen' AND a.result='none'
                     AND a.claim_token IS NULL
                   ORDER BY i.position ASC, a.created_at DESC""",
                (batch_id,),
            ).fetchall()
            for target in frozen:
                self._settle_locally_reconciled_attempt_on_connection(
                    db,
                    batch_id=batch_id,
                    batch_item_id=target["batch_item_id"],
                    save_attempt_id=target["save_attempt_id"],
                    result="known_not_written",
                    collection_item_id=None,
                    error_code="BATCH_CANCELLED_BEFORE_SAVE",
                    cancelled=True,
                    now=timestamp,
                )

            # Any already-unknown result stays explicitly blocking.  It is
            # never turned back into a claimable queue by cancellation.
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='outcome_unknown',
                       error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                       error_stage='save', item_revision=item_revision+1,
                       updated_at=?
                   WHERE batch_id=? AND state='interrupted' AND EXISTS (
                       SELECT 1 FROM collection_import_save_attempts a
                       WHERE a.batch_item_id=
                             collection_import_batch_items.batch_item_id
                         AND a.result='unknown'
                   )""",
                (timestamp, batch_id),
            )

            cancel_targets = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? AND (
                       state IN ('queued','ready','needs_review','preview_expired',
                                 'failed','duplicate_in_batch','save_queued')
                       OR (state='previewing' AND preview_claim_token IS NULL)
                       OR (state='interrupted' AND preview_claim_token IS NULL
                           AND NOT EXISTS (
                               SELECT 1 FROM collection_import_save_attempts a
                               WHERE a.batch_item_id=
                                     collection_import_batch_items.batch_item_id
                                 AND (a.attempt_phase!='settled'
                                      OR a.result IN ('none','unknown'))
                           ))
                   )""",
                (batch_id,),
            ).fetchall()
            changed = self._cancel_items_on_connection(
                db, list(cancel_targets), now=timestamp
            )
            # Item changes share the command revision above; workers that won a
            # claim race will independently advance it when they settle.
            _ = changed
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            snapshot = self._batch_snapshot_on_connection(db, batch_id)
            assert snapshot is not None
            return snapshot

    def resume_batch(
        self,
        *,
        batch_id: str,
        expected_batch_revision: int,
        now: datetime,
    ) -> tuple[dict[str, Any], bool, bool]:
        timestamp = self._iso(now)
        changed = False
        with self._repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute(
                """SELECT revision, status, cancel_requested, terminal_at
                   FROM collection_import_batches WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise CollectionImportNotFoundError("BATCH_NOT_FOUND")
            # CAS is deliberately checked before any local reconciliation.
            if batch["revision"] != expected_batch_revision:
                raise CollectionImportBatchRevisionConflictError(batch["revision"])
            if (
                batch["terminal_at"] is not None
                or batch["cancel_requested"]
                or batch["status"] != "interrupted"
            ):
                raise CollectionImportStateConflictError()

            items = db.execute(
                """SELECT * FROM collection_import_batch_items
                   WHERE batch_id=? AND state IN ('interrupted','outcome_unknown')
                   ORDER BY position ASC""",
                (batch_id,),
            ).fetchall()
            for item in items:
                attempt = db.execute(
                    """SELECT * FROM collection_import_save_attempts
                       WHERE batch_item_id=? AND (
                           attempt_phase!='settled' OR result='unknown'
                       ) ORDER BY created_at DESC, save_attempt_id DESC LIMIT 1""",
                    (item["batch_item_id"],),
                ).fetchone()
                if attempt is not None:
                    if attempt["result"] == "unknown":
                        try:
                            reconciled = self._reconcile_unknown_attempt_on_connection(
                                db,
                                batch_id=batch_id,
                                item=item,
                                attempt=attempt,
                                now=timestamp,
                            )
                        except (sqlite3.Error, OSError, TypeError, ValueError):
                            reconciled = False
                        if reconciled:
                            changed = True
                        continue
                    if (
                        item["state"] == "interrupted"
                        and attempt["attempt_phase"] == "frozen"
                        and attempt["result"] == "none"
                        and attempt["claim_token"] is None
                    ):
                        db.execute(
                            """UPDATE collection_import_batch_items
                               SET state='save_queued', error_code=NULL,
                                   error_stage=NULL, terminal_reason=NULL,
                                   item_revision=item_revision+1, updated_at=?
                               WHERE batch_item_id=? AND state='interrupted'""",
                            (timestamp, item["batch_item_id"]),
                        )
                        changed = True
                    continue

                # Interrupted preview work first consumes an already-reserved
                # immutable preview locally.  Only a missing preview is queued.
                preview = self._preview_payload_on_connection(db, item["preview_id"])
                if preview is not None and isinstance(
                    preview.get("identity_url"), str
                ):
                    identity_url = preview["identity_url"]
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET pending_input_text=NULL, identity_url=?,
                               preview_claim_token=NULL, display_label=?,
                               error_code=NULL, error_stage=NULL,
                               terminal_reason=NULL, updated_at=?
                           WHERE batch_item_id=?""",
                        (
                            identity_url,
                            self._display_label(preview, item["position"]),
                            timestamp,
                            item["batch_item_id"],
                        ),
                    )
                    self._recalculate_identity_group_on_connection(
                        db,
                        batch_id=batch_id,
                        identity_url=identity_url,
                        now=timestamp,
                    )
                    changed = True
                elif (
                    item["state"] == "interrupted"
                    and isinstance(item["pending_input_text"], str)
                    and item["pending_input_text"].strip()
                    and item["preview_generation"] >= 5
                ):
                    safe_label = self._safe_label_before_input_clear_on_connection(
                        db, item
                    )
                    limit_changed = db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='failed', decision='pending',
                               preview_claim_token=NULL,
                               pending_input_text=NULL, display_label=?,
                               error_code='BATCH_ATTEMPT_LIMIT',
                               error_stage='preview',
                               terminal_reason='preview_failed',
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_item_id=? AND state='interrupted'
                             AND preview_generation>=5""",
                        (safe_label, timestamp, item["batch_item_id"]),
                    ).rowcount
                    changed = changed or limit_changed == 1
                elif (
                    item["state"] == "interrupted"
                    and isinstance(item["pending_input_text"], str)
                    and item["pending_input_text"].strip()
                    and item["preview_generation"] < 5
                ):
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='queued', preview_claim_token=NULL,
                               error_code=NULL, error_stage=NULL,
                               terminal_reason=NULL,
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_item_id=? AND state='interrupted'""",
                        (timestamp, item["batch_item_id"]),
                    )
                    changed = True

            if changed:
                db.execute(
                    """UPDATE collection_import_batches
                       SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                    (timestamp, batch_id),
                )
            self._derive_batch_state_on_connection(db, batch_id, timestamp)
            preview_wake = db.execute(
                """SELECT 1 FROM collection_import_batch_items
                   WHERE batch_id=? AND state='queued' LIMIT 1""",
                (batch_id,),
            ).fetchone() is not None
            save_wake = db.execute(
                """SELECT 1 FROM collection_import_batch_items
                   WHERE batch_id=? AND state='save_queued' LIMIT 1""",
                (batch_id,),
            ).fetchone() is not None
            snapshot = self._batch_snapshot_on_connection(db, batch_id)
            assert snapshot is not None
            return snapshot, preview_wake, save_wake

    def reconcile_startup(self, now: datetime) -> None:
        """Reconcile only local SQLite facts; never schedule or fetch."""

        timestamp = self._iso(now)
        try:
            with self._repository._connect() as db:
                targets = db.execute(
                    """SELECT i.batch_id, i.batch_item_id,
                              i.preview_claim_token, i.preview_id
                       FROM collection_import_batch_items i
                       JOIN collection_import_batches b ON b.batch_id=i.batch_id
                       WHERE b.terminal_at IS NULL AND i.state='previewing'
                         AND i.preview_claim_token IS NOT NULL
                       ORDER BY i.batch_id, i.position"""
                ).fetchall()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            targets = []
        for target in targets:
            if not target["preview_id"]:
                continue
            try:
                with self._repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._publish_success_on_connection(
                        db,
                        batch_id=target["batch_id"],
                        batch_item_id=target["batch_item_id"],
                        claim_token=target["preview_claim_token"],
                        preview_id=target["preview_id"],
                        now=timestamp,
                    )
            except (sqlite3.Error, OSError, TypeError, ValueError):
                continue

        # A known result that was durably observed before a crash is final local
        # evidence.  Settle it before classifying any remaining in-flight work.
        try:
            with self._repository._connect() as db:
                known_results = db.execute(
                    """SELECT i.batch_id, i.batch_item_id, a.save_attempt_id,
                              a.claim_token
                       FROM collection_import_save_attempts a
                       JOIN collection_import_batch_items i
                         ON i.batch_item_id=a.batch_item_id
                       JOIN collection_import_batches b ON b.batch_id=i.batch_id
                       WHERE b.terminal_at IS NULL AND i.state='saving'
                         AND a.attempt_phase='result_observed'
                         AND a.result IN (
                             'success', 'exists', 'known_not_written'
                         )
                         AND a.claim_token IS NOT NULL
                       ORDER BY i.batch_id, i.position"""
                ).fetchall()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            known_results = []
        for target in known_results:
            try:
                with self._repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self._settle_observed_save_on_connection(
                        db,
                        batch_id=target["batch_id"],
                        batch_item_id=target["batch_item_id"],
                        save_attempt_id=target["save_attempt_id"],
                        claim_token=target["claim_token"],
                        now=timestamp,
                    )
            except (sqlite3.Error, OSError, TypeError, ValueError):
                continue

        # A crash after call_started but before result_observed is reconciled in
        # the required order: exact idempotency record, then identity.  On a
        # fresh process, a healthy double miss proves the exact attempt was not
        # committed; preserve that same frozen attempt for explicit resume.
        try:
            with self._repository._connect() as db:
                started = db.execute(
                    """SELECT i.batch_id, i.batch_item_id, i.identity_url,
                              i.position, a.save_attempt_id, a.claim_token,
                              a.collection_request_hash, b.cancel_requested
                       FROM collection_import_save_attempts a
                       JOIN collection_import_batch_items i
                         ON i.batch_item_id=a.batch_item_id
                       JOIN collection_import_batches b ON b.batch_id=i.batch_id
                       WHERE b.terminal_at IS NULL AND i.state='saving'
                         AND a.attempt_phase='call_started' AND a.result='none'
                         AND a.claim_token IS NOT NULL
                       ORDER BY i.batch_id, i.position"""
                ).fetchall()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            started = []
        for target in started:
            try:
                with self._repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    key = collection_import_save_idempotency_key(
                        target["batch_id"],
                        target["batch_item_id"],
                        target["save_attempt_id"],
                    )
                    idempotency = db.execute(
                        """SELECT request_hash, collection_item_id
                           FROM collection_idempotency_keys
                           WHERE idempotency_key=?""",
                        (key,),
                    ).fetchone()
                    if (
                        idempotency is not None
                        and idempotency["request_hash"]
                        == target["collection_request_hash"]
                    ):
                        result = "success"
                        collection_item_id = idempotency["collection_item_id"]
                    elif idempotency is None:
                        collection_item_id = (
                            self._existing_collection_id(db, target["identity_url"])
                            if target["identity_url"]
                            else None
                        )
                        result = (
                            "exists" if collection_item_id else "known_not_written"
                        )
                    else:
                        result = "unknown"
                        collection_item_id = None
                    if result in {"success", "exists"}:
                        observed = db.execute(
                            """UPDATE collection_import_save_attempts
                               SET attempt_phase='result_observed', result=?,
                                   collection_item_id=?, error_code=NULL,
                                   updated_at=?
                               WHERE save_attempt_id=? AND batch_item_id=?
                                 AND attempt_phase='call_started'
                                 AND result='none' AND claim_token=?""",
                            (
                                result,
                                collection_item_id,
                                timestamp,
                                target["save_attempt_id"],
                                target["batch_item_id"],
                                target["claim_token"],
                            ),
                        ).rowcount
                        if observed != 1:
                            continue
                        self._settle_observed_save_on_connection(
                            db,
                            batch_id=target["batch_id"],
                            batch_item_id=target["batch_item_id"],
                            save_attempt_id=target["save_attempt_id"],
                            claim_token=target["claim_token"],
                            now=timestamp,
                        )
                    elif result == "known_not_written":
                        if target["cancel_requested"]:
                            self._settle_locally_reconciled_attempt_on_connection(
                                db,
                                batch_id=target["batch_id"],
                                batch_item_id=target["batch_item_id"],
                                save_attempt_id=target["save_attempt_id"],
                                result="known_not_written",
                                collection_item_id=None,
                                error_code="BATCH_CANCELLED_BEFORE_SAVE",
                                cancelled=True,
                                now=timestamp,
                            )
                        else:
                            reset = db.execute(
                                """UPDATE collection_import_save_attempts
                                   SET attempt_phase='frozen', result='none',
                                       claim_token=NULL, error_code=NULL,
                                       updated_at=?
                                   WHERE save_attempt_id=? AND batch_item_id=?
                                     AND attempt_phase='call_started'
                                     AND result='none' AND claim_token=?""",
                                (
                                    timestamp,
                                    target["save_attempt_id"],
                                    target["batch_item_id"],
                                    target["claim_token"],
                                ),
                            ).rowcount
                            if reset != 1:
                                continue
                            item_changed = db.execute(
                                """UPDATE collection_import_batch_items
                                   SET state='interrupted',
                                       error_code='PROCESS_INTERRUPTED',
                                       error_stage='save',
                                       item_revision=item_revision+1,
                                       updated_at=?
                                   WHERE batch_item_id=? AND state='saving'""",
                                (timestamp, target["batch_item_id"]),
                            ).rowcount
                            if item_changed != 1:
                                raise sqlite3.IntegrityError(
                                    "startup known-not-written item state lost"
                                )
                        db.execute(
                            """UPDATE collection_import_batches
                               SET revision=revision+1, updated_at=?
                               WHERE batch_id=?""",
                            (timestamp, target["batch_id"]),
                        )
                        self._derive_batch_state_on_connection(
                            db, target["batch_id"], timestamp
                        )
                    else:
                        observed = db.execute(
                            """UPDATE collection_import_save_attempts
                               SET attempt_phase='result_observed',
                                   result='unknown', claim_token=NULL,
                                   error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                                   updated_at=?
                               WHERE save_attempt_id=? AND batch_item_id=?
                                 AND attempt_phase='call_started'
                                 AND result='none' AND claim_token=?""",
                            (
                                timestamp,
                                target["save_attempt_id"],
                                target["batch_item_id"],
                                target["claim_token"],
                            ),
                        ).rowcount
                        if observed != 1:
                            continue
                        item_changed = db.execute(
                            """UPDATE collection_import_batch_items
                               SET state='outcome_unknown',
                                   error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                                   error_stage='save',
                                   item_revision=item_revision+1, updated_at=?
                               WHERE batch_item_id=? AND state='saving'""",
                            (timestamp, target["batch_item_id"]),
                        ).rowcount
                        if item_changed != 1:
                            raise sqlite3.IntegrityError(
                                "startup save unknown item state lost"
                            )
                        db.execute(
                            """UPDATE collection_import_batches
                               SET revision=revision+1, updated_at=?
                               WHERE batch_id=?""",
                            (timestamp, target["batch_id"]),
                        )
                        self._derive_batch_state_on_connection(
                            db, target["batch_id"], timestamp
                        )
            except (sqlite3.Error, OSError, TypeError, ValueError):
                try:
                    self.mark_save_outcome_unknown(
                        batch_id=target["batch_id"],
                        batch_item_id=target["batch_item_id"],
                        save_attempt_id=target["save_attempt_id"],
                        claim_token=target["claim_token"],
                        now=now,
                    )
                except (sqlite3.Error, OSError, TypeError, ValueError):
                    pass

        # A previously fail-closed unknown may later gain positive local proof
        # (for example, the collection transaction committed just before a
        # busy reconciliation read).  Startup may settle that proof but never
        # turns a healthy miss back into claimable work.
        try:
            with self._repository._connect() as db:
                unknowns = db.execute(
                    """SELECT i.batch_id, i.batch_item_id
                       FROM collection_import_batch_items i
                       JOIN collection_import_batches b ON b.batch_id=i.batch_id
                       JOIN collection_import_save_attempts a
                         ON a.batch_item_id=i.batch_item_id
                       WHERE b.terminal_at IS NULL AND i.state='outcome_unknown'
                         AND a.result='unknown'
                       ORDER BY i.batch_id, i.position,
                                a.created_at DESC, a.save_attempt_id DESC"""
                ).fetchall()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            unknowns = []
        seen_unknown_items: set[str] = set()
        for target in unknowns:
            if target["batch_item_id"] in seen_unknown_items:
                continue
            seen_unknown_items.add(target["batch_item_id"])
            try:
                with self._repository._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    item = db.execute(
                        """SELECT * FROM collection_import_batch_items
                           WHERE batch_id=? AND batch_item_id=?
                             AND state='outcome_unknown'""",
                        (target["batch_id"], target["batch_item_id"]),
                    ).fetchone()
                    attempt = db.execute(
                        """SELECT * FROM collection_import_save_attempts
                           WHERE batch_item_id=? AND result='unknown'
                           ORDER BY created_at DESC, save_attempt_id DESC LIMIT 1""",
                        (target["batch_item_id"],),
                    ).fetchone()
                    if item is None or attempt is None:
                        continue
                    if not self._reconcile_unknown_attempt_on_connection(
                        db,
                        batch_id=target["batch_id"],
                        item=item,
                        attempt=attempt,
                        now=timestamp,
                    ):
                        continue
                    db.execute(
                        """UPDATE collection_import_batches
                           SET revision=revision+1, updated_at=? WHERE batch_id=?""",
                        (timestamp, target["batch_id"]),
                    )
                    self._derive_batch_state_on_connection(
                        db, target["batch_id"], timestamp
                    )
            except (sqlite3.Error, OSError, TypeError, ValueError):
                continue

        try:
            with self._repository._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                batches = db.execute(
                    """SELECT batch_id, cancel_requested
                       FROM collection_import_batches
                       WHERE terminal_at IS NULL ORDER BY batch_id"""
                ).fetchall()
                for batch in batches:
                    batch_id = batch["batch_id"]
                    # Retry exact durable known results inside the final
                    # fail-closed transaction.  A historical known failure
                    # must never mask a newer observed success or in-flight
                    # attempt on the same item.
                    known_targets = db.execute(
                        """SELECT i.batch_item_id, a.save_attempt_id,
                                  a.claim_token
                           FROM collection_import_save_attempts a
                           JOIN collection_import_batch_items i
                             ON i.batch_item_id=a.batch_item_id
                           WHERE i.batch_id=? AND i.state='saving'
                             AND a.attempt_phase='result_observed'
                             AND a.result IN (
                                 'success','exists','known_not_written'
                             )
                             AND a.claim_token IS NOT NULL
                           ORDER BY i.position, a.created_at DESC,
                                    a.save_attempt_id DESC""",
                        (batch_id,),
                    ).fetchall()
                    for target in known_targets:
                        self._settle_observed_save_on_connection(
                            db,
                            batch_id=batch_id,
                            batch_item_id=target["batch_item_id"],
                            save_attempt_id=target["save_attempt_id"],
                            claim_token=target["claim_token"],
                            now=timestamp,
                        )

                    current_batch = db.execute(
                        """SELECT cancel_requested, terminal_at
                           FROM collection_import_batches WHERE batch_id=?""",
                        (batch_id,),
                    ).fetchone()
                    if current_batch is None or current_batch["terminal_at"] is not None:
                        continue
                    cancelled = bool(current_batch["cancel_requested"])

                    # Any call_started/none attempt that survived both the
                    # primary reconciliation and its fallback is uncertain,
                    # never claimable.  Fail closed before the bulk state
                    # transition so neither cancel nor recovery can mistake a
                    # historical known failure for the current attempt.
                    residual_attempt_changed = db.execute(
                        """UPDATE collection_import_save_attempts
                           SET attempt_phase='result_observed', result='unknown',
                               claim_token=NULL,
                               error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                               updated_at=?
                           WHERE save_attempt_id IN (
                               SELECT a.save_attempt_id
                               FROM collection_import_save_attempts a
                               JOIN collection_import_batch_items i
                                 ON i.batch_item_id=a.batch_item_id
                               WHERE i.batch_id=?
                                 AND i.state IN (
                                     'saving','save_queued','interrupted'
                                 )
                                 AND a.attempt_phase='call_started'
                                 AND a.result='none'
                           )""",
                        (timestamp, batch_id),
                    ).rowcount
                    residual_item_changed = db.execute(
                        """UPDATE collection_import_batch_items
                           SET state='outcome_unknown',
                               error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                               error_stage='save',
                               item_revision=item_revision+1, updated_at=?
                           WHERE batch_id=?
                             AND state IN (
                                 'saving','save_queued','interrupted'
                             )
                             AND EXISTS (
                                 SELECT 1
                                 FROM collection_import_save_attempts a
                                 WHERE a.batch_item_id=
                                       collection_import_batch_items.batch_item_id
                                   AND a.attempt_phase='result_observed'
                                   AND a.result='unknown'
                             )""",
                        (timestamp, batch_id),
                    ).rowcount
                    if cancelled:
                        attempt_changed = db.execute(
                            """UPDATE collection_import_save_attempts
                               SET attempt_phase='settled',
                                   result='known_not_written', claim_token=NULL,
                                   frozen_request_json=NULL,
                                   error_code='BATCH_CANCELLED_BEFORE_SAVE',
                                   updated_at=?, settled_at=?
                               WHERE save_attempt_id IN (
                                   SELECT a.save_attempt_id
                                   FROM collection_import_save_attempts a
                                   JOIN collection_import_batch_items i
                                     ON i.batch_item_id=a.batch_item_id
                                   WHERE i.batch_id=?
                                     AND a.attempt_phase IN ('frozen','claimed')
                                     AND a.result='none'
                               )""",
                            (timestamp, timestamp, batch_id),
                        ).rowcount
                        cancel_targets = db.execute(
                            """SELECT * FROM collection_import_batch_items
                               WHERE batch_id=? AND (
                                   state IN ('queued','previewing')
                                   OR (state='save_queued' AND NOT EXISTS (
                                       SELECT 1
                                       FROM collection_import_save_attempts a
                                       WHERE a.batch_item_id=
                                             collection_import_batch_items.batch_item_id
                                         AND (a.attempt_phase!='settled'
                                              OR a.result IN ('none','unknown'))
                                   ))
                                   OR (state='saving' AND NOT EXISTS (
                                       SELECT 1
                                       FROM collection_import_save_attempts a
                                       WHERE a.batch_item_id=
                                             collection_import_batch_items.batch_item_id
                                         AND (a.attempt_phase!='settled'
                                              OR a.result IN ('none','unknown'))
                                   ) AND EXISTS (
                                       SELECT 1
                                       FROM collection_import_save_attempts a
                                       WHERE a.batch_item_id=
                                             collection_import_batch_items.batch_item_id
                                         AND a.attempt_phase='settled'
                                         AND a.result='known_not_written'
                                   ))
                                   OR (state='interrupted'
                                       AND preview_claim_token IS NULL
                                       AND NOT EXISTS (
                                           SELECT 1
                                           FROM collection_import_save_attempts a
                                           WHERE a.batch_item_id=
                                                 collection_import_batch_items.batch_item_id
                                             AND (a.attempt_phase!='settled'
                                                  OR a.result IN ('none','unknown'))
                                       ))
                               )""",
                            (batch_id,),
                        ).fetchall()
                        changed = self._cancel_items_on_connection(
                            db, list(cancel_targets), now=timestamp
                        )
                    else:
                        attempt_changed = db.execute(
                            """UPDATE collection_import_save_attempts
                               SET attempt_phase='frozen', claim_token=NULL,
                                   updated_at=?
                               WHERE save_attempt_id IN (
                                   SELECT a.save_attempt_id
                                   FROM collection_import_save_attempts a
                                   JOIN collection_import_batch_items i
                                     ON i.batch_item_id=a.batch_item_id
                                   WHERE i.batch_id=?
                                     AND a.attempt_phase='claimed'
                                     AND a.result='none'
                               )""",
                            (timestamp, batch_id),
                        ).rowcount
                        changed = db.execute(
                            """UPDATE collection_import_batch_items
                               SET state='interrupted', preview_claim_token=NULL,
                                   error_code='PROCESS_INTERRUPTED',
                                   error_stage=CASE
                                     WHEN state IN ('save_queued', 'saving')
                                       THEN 'save'
                                     ELSE 'preview'
                                   END,
                                   item_revision=item_revision+1, updated_at=?
                               WHERE batch_id=? AND (
                                   state IN ('queued', 'previewing', 'save_queued')
                                   OR (
                                       state='saving' AND EXISTS (
                                           SELECT 1
                                           FROM collection_import_save_attempts a
                                           WHERE a.batch_item_id=
                                                 collection_import_batch_items.batch_item_id
                                             AND a.attempt_phase='frozen'
                                             AND a.result='none'
                                             AND a.claim_token IS NULL
                                       )
                                   )
                               )""",
                            (timestamp, batch_id),
                        ).rowcount
                    if (
                        changed
                        or attempt_changed
                        or residual_attempt_changed
                        or residual_item_changed
                    ):
                        db.execute(
                            """UPDATE collection_import_batches
                               SET revision=revision+1, updated_at=?
                               WHERE batch_id=?""",
                            (timestamp, batch_id),
                        )
                    self._derive_batch_state_on_connection(
                        db, batch_id, timestamp
                    )
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return
