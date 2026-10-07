from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.repositories.collection_imports import (
    CollectionImportBatchRevisionConflictError,
    CollectionImportRepository,
    collection_import_save_idempotency_key,
)
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)
from backend.app.services.safe_http import SafeFetchResult


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _Validator:
    def resolve(self, _url: str):
        return object()


class _Fetcher:
    def __init__(self):
        self.calls: list[str] = []
        self.url_validator = _Validator()

    def fetch(self, url: str) -> SafeFetchResult:
        self.calls.append(url)
        return SafeFetchResult(
            original_url=url,
            final_url=url,
            status_code=200,
            media_type="text/html",
            body=b"<html><head><title>Public title</title></head></html>",
            redirects=(),
            content_type="text/html; charset=utf-8",
        )


def _payload() -> dict:
    return {
        "items": [
            {
                "client_item_id": "one",
                "input_text": "https://slice4.example/one?private=1#secret",
            },
            {
                "client_item_id": "two",
                "input_text": "https://slice4.example/two?private=2#secret",
            },
        ]
    }


class CollectionImportSlice4ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "slice4-api.sqlite3"
        repository = SQLiteRepository(self.db_path)
        pipeline = LocalFullPipeline(
            repository,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        self.fetcher = _Fetcher()
        self._client_context = TestClient(
            create_app(
                pipeline,
                capture_fetcher=self.fetcher,
                upload_root=self.root / "uploads",
                collection_import_clock=lambda: FIXED_NOW,
            )
        )
        self.client = self._client_context.__enter__()

    def tearDown(self):
        self._client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def _create_ready(self) -> dict:
        response = self.client.post(
            "/api/v1/collection-import-batches",
            json=_payload(),
            headers={"Idempotency-Key": "slice4-api-key"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(
            self.client.app.state.collection_import_coordinator.wait_idle(10)
        )
        restored = self.client.get(
            f"/api/v1/collection-import-batches/{response.json()['batch_id']}"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        return restored.json()

    def _authority(self, batch_id: str) -> tuple[int, int]:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(
                "SELECT revision, cancel_requested "
                "FROM collection_import_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()

    def test_resume_and_cancel_use_strict_bounded_json_and_cas_first(self):
        batch = self._create_ready()
        before_items = {
            item["batch_item_id"]: item["item_revision"]
            for item in batch["items"]
        }
        self.assertEqual(
            {item["display_label"] for item in batch["items"]},
            {"Public title"},
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            before_rows = db.execute(
                """SELECT batch_item_id, display_label, item_revision,
                          pending_input_text
                   FROM collection_import_batch_items
                   WHERE batch_id=? ORDER BY position""",
                (batch["batch_id"],),
            ).fetchall()
        self.assertTrue(before_rows)
        self.assertTrue(all(row[3] is None for row in before_rows))
        paths = (
            f"/api/v1/collection-import-batches/{batch['batch_id']}/resume",
            f"/api/v1/collection-import-batches/{batch['batch_id']}/cancel",
        )
        invalid_bodies = (
            b"{}",
            b"[]",
            b'{"expected_batch_revision":null}',
            b'{"expected_batch_revision":true}',
            b'{"expected_batch_revision":1.0}',
            b'{"expected_batch_revision":1,"extra":1}',
            b'{"expected_batch_revision":1,"expected_batch_revision":1}',
            b"\xff",
        )
        authority = self._authority(batch["batch_id"])
        for path in paths:
            unsupported = self.client.post(
                path,
                content=b"{}",
                headers={"Content-Type": "text/plain"},
            )
            self.assertEqual(unsupported.status_code, 415, unsupported.text)
            self.assertEqual(self._authority(batch["batch_id"]), authority)
            for body in invalid_bodies:
                with self.subTest(path=path, body=body):
                    response = self.client.post(
                        path,
                        content=body,
                        headers={"Content-Type": "application/json"},
                    )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"],
                        "BATCH_REQUEST_INVALID",
                    )
                    self.assertEqual(
                        self._authority(batch["batch_id"]), authority
                    )

        # Revision conflict wins over the otherwise-invalid resume state and
        # has no cancellation or scheduling side effect.
        conflict = self.client.post(
            paths[0],
            json={"expected_batch_revision": batch["revision"] + 1},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(
            conflict.json()["error"]["code"], "BATCH_REVISION_CONFLICT"
        )
        self.assertEqual(self._authority(batch["batch_id"]), authority)

        cancelled = self.client.post(
            paths[1],
            json={"expected_batch_revision": batch["revision"]},
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(cancelled.json()["revision"], batch["revision"] + 1)
        self.assertEqual(
            {item["state"] for item in cancelled.json()["items"]},
            {"cancelled"},
        )
        self.assertEqual(
            {item["display_label"] for item in cancelled.json()["items"]},
            {"Public title"},
        )
        for item in cancelled.json()["items"]:
            self.assertEqual(
                item["item_revision"],
                before_items[item["batch_item_id"]] + 1,
            )
        with closing(sqlite3.connect(self.db_path)) as db:
            after_rows = db.execute(
                """SELECT batch_item_id, display_label, item_revision,
                          pending_input_text
                   FROM collection_import_batch_items
                   WHERE batch_id=? ORDER BY position""",
                (batch["batch_id"],),
            ).fetchall()
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in after_rows],
            [(row[0], row[1], row[2] + 1) for row in before_rows],
        )
        self.assertTrue(all(row[3] is None for row in after_rows))

        terminal_resume = self.client.post(
            paths[0],
            json={"expected_batch_revision": cancelled.json()["revision"]},
        )
        self.assertEqual(terminal_resume.status_code, 409, terminal_resume.text)
        self.assertEqual(
            terminal_resume.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )

    def test_command_not_found_is_stable(self):
        for command in ("resume", "cancel"):
            response = self.client.post(
                f"/api/v1/collection-import-batches/missing/{command}",
                json={"expected_batch_revision": 1},
            )
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "BATCH_NOT_FOUND")


class CollectionImportSlice4RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "slice4-repository.sqlite3"
        self.base = SQLiteRepository(self.db_path)
        self.repository = CollectionImportRepository(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def _create_batch(self, batch_id: str = "batch-slice4") -> dict:
        snapshot, created, _terminal = self.repository.create_or_replay(
            items=_payload()["items"],
            key_hash=f"key-{batch_id}",
            request_fingerprint=f"fingerprint-{batch_id}",
            batch_id=batch_id,
            batch_item_ids=(f"{batch_id}-one", f"{batch_id}-two"),
            now=FIXED_NOW,
            expires_at=FIXED_NOW + timedelta(days=7),
        )
        self.assertTrue(created)
        return snapshot

    def test_safe_input_label_uses_only_one_valid_public_hostname(self):
        safe = CollectionImportRepository._safe_input_display_label
        shared = safe(
            "复制分享文案（https://Sub.Example/path?token=secret#fragment）再打开",
            0,
        )
        self.assertEqual(shared, "sub.example")
        self.assertNotIn("token", shared)
        self.assertNotIn("fragment", shared)
        self.assertEqual(
            safe("https://a.example https://b.example", 1),
            "第 2 项（输入无效）",
        )
        self.assertEqual(
            safe("https://user:password@example.com/private", 2),
            "第 3 项（输入无效）",
        )
        self.assertEqual(
            safe("https://example.com:not-a-port/private", 3),
            "第 4 项（输入无效）",
        )
        long_host = (
            "a" * 60 + "." + "b" * 60 + "." + "c" * 60 + ".example"
        )
        bounded = safe(f"https://{long_host}/private?secret=1", 4)
        self.assertEqual(len(bounded), 120)
        self.assertTrue(long_host.startswith(bounded))

    def test_preview_label_trusts_only_safe_public_titles_and_never_urls(self):
        label = CollectionImportRepository._display_label
        base = {
            "identity_url": "https://identity.example/private?identity=secret#fragment",
            "source_url": "https://source.example/private?source=secret#fragment",
            "canonical_url": "https://Canonical.Example:8443/private?token=secret#fragment",
        }

        for source in ("open_graph", "page_metadata", "platform_public"):
            with self.subTest(source=source):
                preview = {
                    **base,
                    "metadata": {
                        "title": {
                            "value": "  Normal\n\t\N{NO-BREAK SPACE}public title  ",
                            "source": source,
                        }
                    },
                }
                self.assertEqual(label(preview, 0), "Normal public title")

        secret_title = (
            "https://user:password@evil.example/private"
            "?token=secret#fragment"
        )
        for source in (
            "share_text",
            "user",
            "legacy_import",
            "none",
            "unknown",
            None,
        ):
            with self.subTest(untrusted_source=source):
                preview = {
                    **base,
                    "metadata": {
                        "title": {"value": secret_title, "source": source}
                    },
                }
                result = label(preview, 0)
                self.assertEqual(result, "canonical.example")
                for secret in (
                    "user",
                    "password",
                    "token",
                    "secret",
                    "fragment",
                    "?",
                    "#",
                ):
                    self.assertNotIn(secret, result)

        reflected = {
            **base,
            "metadata": {
                "title": {
                    "value": f"Reflected {secret_title}",
                    "source": "open_graph",
                }
            },
        }
        self.assertEqual(label(reflected, 0), "canonical.example")

        for unsafe_value in ("Public\x00Title", "Public\u202eTitle", "Public\u200bTitle"):
            with self.subTest(unsafe_value=repr(unsafe_value)):
                preview = {
                    **base,
                    "metadata": {
                        "title": {
                            "value": unsafe_value,
                            "source": "page_metadata",
                        }
                    },
                }
                self.assertEqual(label(preview, 0), "canonical.example")

        fallback = {
            "canonical_url": "https://user:password@bad.example/private",
            "source_url": "https://source.example:not-a-port/private",
            "identity_url": "https://Identity.Example./private?token=secret#fragment",
            "metadata": {"title": []},
        }
        self.assertEqual(label(fallback, 1), "identity.example")
        self.assertEqual(
            label({"metadata": {"title": "malformed"}}, 2),
            "第 3 项（输入无效）",
        )
        self.assertEqual(
            len(
                label(
                    {
                        **base,
                        "metadata": {
                            "title": {
                                "value": "界" * 121,
                                "source": "platform_public",
                            }
                        },
                    },
                    0,
                )
            ),
            120,
        )

    def _insert_preview(
        self,
        preview_id: str,
        *,
        identity_url: str,
        expires_at: datetime,
        title: str = "Public title",
        title_source: str = "open_graph",
        valid: bool = True,
    ) -> None:
        payload = (
            json.dumps(
                {
                    "identity_url": identity_url,
                    "canonical_url": identity_url,
                    "source_url": identity_url,
                    "original_input": identity_url + "?private=1#secret",
                    "metadata": {
                        "title": {"value": title, "source": title_source}
                    },
                },
                separators=(",", ":"),
            )
            if valid
            else "{invalid"
        )
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO collection_previews(
                       preview_id, payload_json, created_at, expires_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    preview_id,
                    payload,
                    FIXED_NOW.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def _insert_library_item(
        self, item_id: str, identity_url: str = "https://library.example/item"
    ) -> None:
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO library_items(
                       id, source_kind, platform, original_input, source_url,
                       canonical_url, identity_url, metadata_status, user_title,
                       created_at, updated_at
                   ) VALUES (?, 'url', 'web', ?, ?, ?, ?, 'recognized', ?, ?, ?)""",
                (
                    item_id,
                    identity_url,
                    identity_url,
                    identity_url,
                    identity_url,
                    "Retained collection",
                    timestamp,
                    timestamp,
                ),
            )

    def _set_saving_result(
        self,
        *,
        result: str,
        collection_item_id: str | None = None,
        error_code: str | None = None,
    ) -> tuple[dict, str, str, str]:
        batch = self._create_batch()
        first, second = batch["items"]
        attempt_id = "attempt-observed"
        claim_token = "save-claim"
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='saving', revision=7, updated_at=?
                   WHERE batch_id=?""",
                (timestamp, batch["batch_id"]),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='saving', decision='save', preview_generation=1,
                       preview_id='save-preview', identity_url=?,
                       review_revision=1, pending_input_text='private save input'
                   WHERE batch_item_id=?""",
                ("https://save.example/item", first["batch_item_id"]),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='ready', pending_input_text='private queued input'
                   WHERE batch_item_id=?""",
                (second["batch_item_id"],),
            )
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision, frozen_request_json,
                       collection_request_hash, idempotency_key_hash,
                       attempt_phase, result, claim_token, error_code,
                       collection_item_id, created_at, updated_at
                   ) VALUES (?, ?, 'save-preview', 1, 1, '{}', 'request-hash',
                             'key-hash', 'result_observed', ?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    first["batch_item_id"],
                    result,
                    claim_token,
                    error_code,
                    collection_item_id,
                    timestamp,
                    timestamp,
                ),
            )
        return batch, first["batch_item_id"], attempt_id, claim_token

    def test_cancel_preview_failure_and_success_are_cancelled_by_claim_cas(self):
        batch = self._create_batch()
        first = batch["items"][0]
        claim = self.repository.claim_preview(
            batch_id=batch["batch_id"],
            preview_id="preview-failure",
            claim_token="preview-claim-failure",
            now=FIXED_NOW,
        )
        self.assertIsNotNone(claim)
        active = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        with self.base._connect() as db:
            before_cancel = db.execute(
                """SELECT display_label, item_revision, pending_input_text
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (first["batch_item_id"],),
            ).fetchone()
        cancelling = self.repository.cancel_batch(
            batch_id=batch["batch_id"],
            expected_batch_revision=active["revision"],
            now=FIXED_NOW,
        )
        self.assertEqual(cancelling["status"], "cancelling")
        with self.base._connect() as db:
            after_cancel = db.execute(
                """SELECT display_label, item_revision, pending_input_text
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (first["batch_item_id"],),
            ).fetchone()
        self.assertEqual(tuple(after_cancel), tuple(before_cancel))
        self.assertTrue(
            self.repository.publish_preview_failure(
                batch_id=batch["batch_id"],
                batch_item_id=first["batch_item_id"],
                claim_token="preview-claim-failure",
                error_code="UNSAFE_URL",
                now=FIXED_NOW,
            )
        )
        final = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual({item["state"] for item in final["items"]}, {"cancelled"})
        self.assertNotIn("failed", {item["state"] for item in final["items"]})
        self.assertEqual(final["items"][0]["display_label"], "slice4.example")
        with self.base._connect() as db:
            row = db.execute(
                """SELECT pending_input_text, error_code, error_stage
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (first["batch_item_id"],),
            ).fetchone()
            self.assertEqual(tuple(row), (None, None, None))

        # Repeat the other write-back branch in an independent database.
        other_root = self.root / "success"
        other_root.mkdir()
        other_base = SQLiteRepository(other_root / "success.sqlite3")
        other = CollectionImportRepository(other_base)
        other_snapshot, _, _ = other.create_or_replay(
            items=_payload()["items"],
            key_hash="success-key",
            request_fingerprint="success-fingerprint",
            batch_id="success-batch",
            batch_item_ids=("success-one", "success-two"),
            now=FIXED_NOW,
            expires_at=FIXED_NOW + timedelta(days=7),
        )
        success_claim = other.claim_preview(
            batch_id="success-batch",
            preview_id="preview-success",
            claim_token="preview-claim-success",
            now=FIXED_NOW,
        )
        self.assertIsNotNone(success_claim)
        with other_base._connect() as db:
            db.execute(
                """INSERT INTO collection_previews(
                       preview_id, payload_json, created_at, expires_at
                   ) VALUES (?, ?, ?, ?)""",
                (
                    "preview-success",
                    json.dumps(
                        {
                            "identity_url": "https://success.example/item",
                            "canonical_url": (
                                "https://Canonical.Example/private"
                                "?canonical_token=secret#canonical_fragment"
                            ),
                            "source_url": "https://source.example/item",
                            "metadata": {
                                "title": {
                                    "value": (
                                        "https://user:password@evil.example/private"
                                        "?token=secret#fragment"
                                    ),
                                    "source": "share_text",
                                }
                            },
                        }
                    ),
                    FIXED_NOW.isoformat(),
                    (FIXED_NOW + timedelta(days=1)).isoformat(),
                ),
            )
        active = other.get_batch("success-batch", FIXED_NOW)
        with other_base._connect() as db:
            before_publish = db.execute(
                """SELECT display_label, item_revision, pending_input_text
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (other_snapshot["items"][0]["batch_item_id"],),
            ).fetchone()
        cancelling = other.cancel_batch(
            batch_id="success-batch",
            expected_batch_revision=active["revision"],
            now=FIXED_NOW,
        )
        self.assertEqual(cancelling["revision"], active["revision"] + 1)
        self.assertTrue(
            other.publish_preview_success(
                batch_id="success-batch",
                batch_item_id=other_snapshot["items"][0]["batch_item_id"],
                claim_token="preview-claim-success",
                preview_id="preview-success",
                now=FIXED_NOW,
            )
        )
        self.assertFalse(
            other.publish_preview_interrupted(
                batch_id="success-batch",
                batch_item_id=other_snapshot["items"][0]["batch_item_id"],
                claim_token="preview-claim-success",
                now=FIXED_NOW,
            )
        )
        other_final = other.get_batch("success-batch", FIXED_NOW)
        self.assertEqual(other_final["status"], "cancelled")
        self.assertEqual(
            {item["state"] for item in other_final["items"]}, {"cancelled"}
        )
        self.assertEqual(other_final["revision"], cancelling["revision"] + 1)
        self.assertEqual(
            other_final["items"][0]["display_label"], "canonical.example"
        )
        with other_base._connect() as db:
            after_publish = db.execute(
                """SELECT display_label, item_revision, pending_input_text
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (other_snapshot["items"][0]["batch_item_id"],),
            ).fetchone()
        self.assertEqual(after_publish[0], "canonical.example")
        self.assertEqual(after_publish[1], before_publish[1] + 1)
        self.assertIsNone(after_publish[2])
        for secret in (
            "user",
            "password",
            "token",
            "secret",
            "fragment",
            "?",
            "#",
        ):
            self.assertNotIn(secret, after_publish[0])

    def test_cancelled_known_save_failure_never_becomes_failed(self):
        batch, item_id, attempt_id, claim_token = self._set_saving_result(
            result="known_not_written",
            error_code="COLLECTION_VALIDATION_FAILED",
        )
        cancelling = self.repository.cancel_batch(
            batch_id=batch["batch_id"],
            expected_batch_revision=7,
            now=FIXED_NOW,
        )
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertTrue(
            self.repository.settle_observed_save(
                batch_id=batch["batch_id"],
                batch_item_id=item_id,
                save_attempt_id=attempt_id,
                claim_token=claim_token,
                now=FIXED_NOW,
            )
        )
        final = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual({item["state"] for item in final["items"]}, {"cancelled"})
        with self.base._connect() as db:
            item = db.execute(
                """SELECT state, pending_input_text, error_code, error_stage
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (item_id,),
            ).fetchone()
            self.assertEqual(tuple(item), ("cancelled", None, None, None))

    def test_cancelled_save_success_is_retained_without_rollback(self):
        self._insert_library_item("saved-collection")
        batch, item_id, attempt_id, claim_token = self._set_saving_result(
            result="success",
            collection_item_id="saved-collection",
        )
        self.repository.cancel_batch(
            batch_id=batch["batch_id"],
            expected_batch_revision=7,
            now=FIXED_NOW,
        )
        self.assertTrue(
            self.repository.settle_observed_save(
                batch_id=batch["batch_id"],
                batch_item_id=item_id,
                save_attempt_id=attempt_id,
                claim_token=claim_token,
                now=FIXED_NOW,
            )
        )
        final = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["items"][0]["state"], "saved")
        self.assertEqual(final["items"][0]["collection_item_id"], "saved-collection")
        self.assertEqual(final["items"][1]["state"], "cancelled")
        with self.base._connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM library_items WHERE id='saved-collection'"
                ).fetchone()[0],
                1,
            )

    def _set_interrupted_attempt(self, *, unknown: bool) -> tuple[dict, str, str]:
        batch = self._create_batch()
        first, second = batch["items"]
        timestamp = FIXED_NOW.isoformat()
        attempt_id = "attempt-unknown" if unknown else "attempt-frozen"
        with self.base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='interrupted', revision=11, updated_at=?
                   WHERE batch_id=?""",
                (timestamp, batch["batch_id"]),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state=?, decision='save', preview_generation=1,
                       preview_id='attempt-preview', identity_url=?,
                       review_revision=1, error_code='PROCESS_INTERRUPTED',
                       error_stage='save'
                   WHERE batch_item_id=?""",
                (
                    "outcome_unknown" if unknown else "interrupted",
                    "https://attempt.example/item",
                    first["batch_item_id"],
                ),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='cancelled', decision='pending',
                       pending_input_text=NULL, terminal_reason='cancelled_by_user'
                   WHERE batch_item_id=?""",
                (second["batch_item_id"],),
            )
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision, frozen_request_json,
                       collection_request_hash, idempotency_key_hash,
                       attempt_phase, result, claim_token, error_code,
                       created_at, updated_at
                   ) VALUES (?, ?, 'attempt-preview', 1, 1, '{}', 'attempt-hash',
                             'attempt-key-hash', ?, ?, NULL, ?, ?, ?)""",
                (
                    attempt_id,
                    first["batch_item_id"],
                    "result_observed" if unknown else "frozen",
                    "unknown" if unknown else "none",
                    "BATCH_SAVE_OUTCOME_UNKNOWN" if unknown else None,
                    timestamp,
                    timestamp,
                ),
            )
        return batch, first["batch_item_id"], attempt_id

    def _prepare_fifth_generation_preview(
        self,
        *,
        root: Path,
        batch_id: str,
        reserved_preview_exists: bool,
    ) -> tuple[SQLiteRepository, dict]:
        base = SQLiteRepository(root / "fifth-generation.sqlite3")
        repository = CollectionImportRepository(base)
        batch, created, _terminal = repository.create_or_replay(
            items=_payload()["items"],
            key_hash=f"key-{batch_id}",
            request_fingerprint=f"fingerprint-{batch_id}",
            batch_id=batch_id,
            batch_item_ids=(f"{batch_id}-one", f"{batch_id}-two"),
            now=FIXED_NOW,
            expires_at=FIXED_NOW + timedelta(days=7),
        )
        self.assertTrue(created)
        first, second = batch["items"]
        timestamp = FIXED_NOW.isoformat()
        with base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='previewing', revision=6, updated_at=?
                   WHERE batch_id=?""",
                (timestamp, batch_id),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='previewing', decision='pending',
                       preview_generation=5, preview_id='generation-five-preview',
                       preview_claim_token='generation-five-claim',
                       item_revision=6, error_code=NULL, error_stage=NULL,
                       terminal_reason=NULL, updated_at=?
                   WHERE batch_item_id=?""",
                (timestamp, first["batch_item_id"]),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='ready', decision='pending', preview_generation=1,
                       preview_id='stable-other-preview',
                       identity_url='https://stable-other.example/item',
                       pending_input_text=NULL, display_label='Stable other',
                       preview_claim_token=NULL, item_revision=4,
                       error_code=NULL, error_stage=NULL, terminal_reason=NULL,
                       updated_at=? WHERE batch_item_id=?""",
                (timestamp, second["batch_item_id"]),
            )
            if reserved_preview_exists:
                db.execute(
                    """INSERT INTO collection_previews(
                           preview_id, payload_json, created_at, expires_at
                       ) VALUES ('generation-five-preview', ?, ?, ?)""",
                    (
                        json.dumps(
                            {
                                "identity_url": "https://reserved.example/item",
                                "canonical_url": "https://reserved.example/item",
                                "source_url": "https://reserved.example/item",
                                "original_input": "https://reserved.example/item",
                                "metadata": {
                                    "title": {
                                        "value": "Reserved public title",
                                        "source": "open_graph",
                                    }
                                },
                            },
                            separators=(",", ":"),
                        ),
                        timestamp,
                        (FIXED_NOW + timedelta(days=1)).isoformat(),
                    ),
                )
        return base, batch

    @staticmethod
    def _skip_payload(batch: dict, item: dict) -> dict:
        return {
            "expected_batch_revision": batch["revision"],
            "expected_item_revision": item["item_revision"],
            "decision": "skip",
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

    def test_resume_exposes_fifth_generation_missing_preview_as_reviewable_failure(self):
        root = self.root / "generation-five-missing"
        root.mkdir()
        base, batch = self._prepare_fifth_generation_preview(
            root=root,
            batch_id="generation-five-missing",
            reserved_preview_exists=False,
        )
        private_input = (
            "复制 https://user:password@attempt-limit.example/private"
            "?token=secret#fragment 后打开"
        )
        with base._connect() as db:
            db.execute(
                """UPDATE collection_import_batch_items
                   SET pending_input_text=? WHERE batch_item_id=?""",
                (private_input, batch["items"][0]["batch_item_id"]),
            )
        fetcher = _Fetcher()
        pipeline = LocalFullPipeline(
            base,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        app = create_app(
            pipeline,
            capture_fetcher=fetcher,
            upload_root=root / "uploads",
            collection_import_clock=lambda: FIXED_NOW,
        )
        wake_calls: list[str] = []
        save_wake_calls: list[str] = []
        app.state.collection_import_coordinator.accept_batch = (
            lambda batch_id: wake_calls.append(batch_id) or True
        )
        app.state.collection_import_save_coordinator.accept_batch = (
            lambda batch_id: save_wake_calls.append(batch_id) or True
        )

        with TestClient(app) as client:
            startup_response = client.get(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
            )
            self.assertEqual(startup_response.status_code, 200)
            startup = startup_response.json()
            first_before, other_before = startup["items"]
            self.assertEqual(startup["status"], "interrupted")
            self.assertEqual(startup["revision"], 7)
            self.assertEqual(first_before["state"], "interrupted")
            self.assertEqual(first_before["item_revision"], 7)
            self.assertEqual(first_before["preview_generation"], 5)

            resumed_response = client.post(
                f"/api/v1/collection-import-batches/{batch['batch_id']}/resume",
                json={"expected_batch_revision": startup["revision"]},
            )
            self.assertEqual(resumed_response.status_code, 202)
            resumed = resumed_response.json()
            first_after, other_after = resumed["items"]
            self.assertEqual(resumed["status"], "awaiting_review")
            self.assertEqual(resumed["revision"], startup["revision"] + 1)
            self.assertEqual(first_after["state"], "failed")
            self.assertEqual(first_after["decision"], "pending")
            self.assertEqual(first_after["error_code"], "BATCH_ATTEMPT_LIMIT")
            self.assertEqual(first_after["terminal_reason"], "preview_failed")
            self.assertEqual(first_after["preview_generation"], 5)
            self.assertEqual(first_after["display_label"], "第 1 项（输入无效）")
            self.assertEqual(
                first_after["item_revision"], first_before["item_revision"] + 1
            )
            self.assertEqual(other_after, other_before)
            self.assertEqual(wake_calls, [])
            self.assertEqual(save_wake_calls, [])
            self.assertEqual(fetcher.calls, [])
            for secret in ("user", "password", "token", "secret", "fragment"):
                self.assertNotIn(secret, resumed_response.text)

            with base._connect() as db:
                row = db.execute(
                    """SELECT state, decision, preview_generation, preview_id,
                              preview_claim_token, display_label,
                              pending_input_text, error_code, error_stage,
                              item_revision
                       FROM collection_import_batch_items
                       WHERE batch_item_id=?""",
                    (first_after["batch_item_id"],),
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    (
                        "failed",
                        "pending",
                        5,
                        "generation-five-preview",
                        None,
                        "第 1 项（输入无效）",
                        None,
                        "BATCH_ATTEMPT_LIMIT",
                        "preview",
                        first_before["item_revision"] + 1,
                    ),
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM collection_previews").fetchone()[0],
                    0,
                )

            skipped_response = client.patch(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
                f"/items/{first_after['batch_item_id']}",
                json=self._skip_payload(resumed, first_after),
            )
            self.assertEqual(skipped_response.status_code, 200)
            skipped = skipped_response.json()
            self.assertEqual(skipped["items"][0]["state"], "failed")
            self.assertEqual(skipped["items"][0]["decision"], "skip")
            self.assertEqual(
                skipped["items"][0]["error_code"], "BATCH_ATTEMPT_LIMIT"
            )
            self.assertEqual(wake_calls, [])
            self.assertEqual(save_wake_calls, [])
            self.assertEqual(fetcher.calls, [])

    def test_startup_consumes_reserved_fifth_generation_before_attempt_limit(self):
        root = self.root / "generation-five-reserved"
        root.mkdir()
        base, batch = self._prepare_fifth_generation_preview(
            root=root,
            batch_id="generation-five-reserved",
            reserved_preview_exists=True,
        )
        fetcher = _Fetcher()
        pipeline = LocalFullPipeline(
            base,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        app = create_app(
            pipeline,
            capture_fetcher=fetcher,
            upload_root=root / "uploads",
            collection_import_clock=lambda: FIXED_NOW,
        )
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
            )
            self.assertEqual(response.status_code, 200)
            restored = response.json()
            first = restored["items"][0]
            self.assertEqual(restored["status"], "awaiting_review")
            self.assertEqual(first["state"], "ready")
            self.assertEqual(first["preview_generation"], 5)
            self.assertEqual(first["display_label"], "Reserved public title")
            self.assertIsNone(first["error_code"])
            self.assertEqual(fetcher.calls, [])
            with base._connect() as db:
                row = db.execute(
                    """SELECT state, preview_generation, preview_id,
                              preview_claim_token, pending_input_text, error_code
                       FROM collection_import_batch_items
                       WHERE batch_item_id=?""",
                    (first["batch_item_id"],),
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    (
                        "ready",
                        5,
                        "generation-five-preview",
                        None,
                        None,
                        None,
                    ),
                )
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM collection_previews").fetchone()[0],
                    1,
                )

    def test_resume_reuses_frozen_attempt_and_never_reclaims_unknown(self):
        batch, item_id, attempt_id = self._set_interrupted_attempt(unknown=False)
        resumed, preview_wake, save_wake = self.repository.resume_batch(
            batch_id=batch["batch_id"],
            expected_batch_revision=11,
            now=FIXED_NOW,
        )
        self.assertFalse(preview_wake)
        self.assertTrue(save_wake)
        self.assertEqual(resumed["items"][0]["state"], "save_queued")
        with self.base._connect() as db:
            attempts = db.execute(
                """SELECT save_attempt_id, attempt_phase, result, claim_token
                   FROM collection_import_save_attempts
                   WHERE batch_item_id=?""",
                (item_id,),
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in attempts],
                [(attempt_id, "frozen", "none", None)],
            )

        other_root = self.root / "unknown"
        other_root.mkdir()
        other_base = SQLiteRepository(other_root / "unknown.sqlite3")
        other_repository = CollectionImportRepository(other_base)
        original_base, original_repository = self.base, self.repository
        self.base, self.repository = other_base, other_repository
        try:
            unknown_batch, unknown_item, unknown_attempt = (
                self._set_interrupted_attempt(unknown=True)
            )
            unchanged, preview_wake, save_wake = self.repository.resume_batch(
                batch_id=unknown_batch["batch_id"],
                expected_batch_revision=11,
                now=FIXED_NOW,
            )
            self.assertFalse(preview_wake)
            self.assertFalse(save_wake)
            self.assertEqual(unchanged["revision"], 11)
            self.assertEqual(unchanged["items"][0]["state"], "outcome_unknown")
            with self.base._connect() as db:
                attempt = db.execute(
                    """SELECT save_attempt_id, attempt_phase, result, claim_token
                       FROM collection_import_save_attempts
                       WHERE batch_item_id=?""",
                    (unknown_item,),
                ).fetchone()
                self.assertEqual(
                    tuple(attempt),
                    (unknown_attempt, "result_observed", "unknown", None),
                )
        finally:
            self.base, self.repository = original_base, original_repository

    def test_resume_cas_precedes_positive_local_reconciliation(self):
        batch, item_id, attempt_id = self._set_interrupted_attempt(unknown=True)
        self._insert_library_item(
            "idempotent-collection", "https://different.example/identity"
        )
        idempotency_key = collection_import_save_idempotency_key(
            batch["batch_id"], item_id, attempt_id
        )
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO collection_idempotency_keys(
                       idempotency_key, request_hash, collection_item_id,
                       response_json, created_at
                   ) VALUES (?, 'attempt-hash', 'idempotent-collection', '{}', ?)""",
                (idempotency_key, FIXED_NOW.isoformat()),
            )
        with self.assertRaises(CollectionImportBatchRevisionConflictError):
            self.repository.resume_batch(
                batch_id=batch["batch_id"],
                expected_batch_revision=10,
                now=FIXED_NOW,
            )
        with self.base._connect() as db:
            self.assertEqual(
                db.execute(
                    """SELECT result FROM collection_import_save_attempts
                       WHERE save_attempt_id=?""",
                    (attempt_id,),
                ).fetchone()[0],
                "unknown",
            )

        reconciled, preview_wake, save_wake = self.repository.resume_batch(
            batch_id=batch["batch_id"],
            expected_batch_revision=11,
            now=FIXED_NOW,
        )
        self.assertFalse(preview_wake)
        self.assertFalse(save_wake)
        self.assertEqual(reconciled["items"][0]["state"], "saved")
        self.assertEqual(
            reconciled["items"][0]["collection_item_id"],
            "idempotent-collection",
        )

    def test_startup_cancel_is_local_idempotent_and_settles_frozen_attempt(self):
        batch = self._create_batch()
        first, second = batch["items"]
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='cancelling', cancel_requested=1, revision=13,
                       updated_at=? WHERE batch_id=?""",
                (timestamp, batch["batch_id"]),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='save_queued', decision='save', preview_generation=1,
                       preview_id='startup-save-preview', review_revision=1
                   WHERE batch_item_id=?""",
                (first["batch_item_id"],),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='previewing', preview_generation=1,
                       preview_id='startup-preview',
                       preview_claim_token='startup-preview-claim'
                   WHERE batch_item_id=?""",
                (second["batch_item_id"],),
            )
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision, frozen_request_json,
                       collection_request_hash, idempotency_key_hash,
                       attempt_phase, result, created_at, updated_at
                   ) VALUES ('startup-attempt', ?, 'startup-save-preview', 1, 1,
                             '{""preview_id"":""startup-save-preview""}',
                             'startup-hash', 'startup-key-hash', 'frozen', 'none',
                             ?, ?)""",
                (first["batch_item_id"], timestamp, timestamp),
            )
        self.repository.reconcile_startup(FIXED_NOW)
        first_snapshot = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(first_snapshot["status"], "cancelled")
        self.assertEqual(
            {item["state"] for item in first_snapshot["items"]}, {"cancelled"}
        )
        with self.base._connect() as db:
            attempt = db.execute(
                """SELECT save_attempt_id, attempt_phase, result, claim_token,
                          frozen_request_json
                   FROM collection_import_save_attempts
                   WHERE save_attempt_id='startup-attempt'"""
            ).fetchone()
            self.assertEqual(
                tuple(attempt),
                ("startup-attempt", "settled", "known_not_written", None, None),
            )
            inputs = db.execute(
                """SELECT pending_input_text FROM collection_import_batch_items
                   WHERE batch_id=?""",
                (batch["batch_id"],),
            ).fetchall()
            self.assertEqual([row[0] for row in inputs], [None, None])
        revision = first_snapshot["revision"]
        self.repository.reconcile_startup(FIXED_NOW + timedelta(minutes=1))
        second_snapshot = self.repository.get_batch(
            batch["batch_id"], FIXED_NOW + timedelta(minutes=1)
        )
        self.assertEqual(second_snapshot["revision"], revision)

    def _insert_historical_known_failure(self, batch_item_id: str) -> None:
        timestamp = (FIXED_NOW - timedelta(days=1)).isoformat()
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision, frozen_request_json,
                       collection_request_hash, idempotency_key_hash,
                       attempt_phase, result, claim_token, error_code,
                       created_at, updated_at, settled_at
                   ) VALUES ('historical-known-failure', ?, 'old-preview', 1, 1,
                             NULL, 'old-hash', 'old-key-hash', 'settled',
                             'known_not_written', NULL,
                             'COLLECTION_VALIDATION_FAILED', ?, ?, ?)""",
                (batch_item_id, timestamp, timestamp, timestamp),
            )

    def test_startup_final_bulk_fails_closed_after_double_reconcile_fault(self):
        batch, item_id, attempt_id = self._set_interrupted_attempt(unknown=False)
        self._insert_historical_known_failure(item_id)
        with self.base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='cancelling', cancel_requested=1
                   WHERE batch_id=?""",
                (batch["batch_id"],),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='queued'
                   WHERE batch_id=? AND batch_item_id<>?""",
                (batch["batch_id"], item_id),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='saving' WHERE batch_item_id=?""",
                (item_id,),
            )
            db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='call_started', claim_token='current-claim'
                   WHERE save_attempt_id=?""",
                (attempt_id,),
            )

        original_identity_lookup = CollectionImportRepository._existing_collection_id

        def identity_fault(_db: sqlite3.Connection, _identity_url: str):
            raise sqlite3.OperationalError("injected identity lookup fault")

        def mark_unknown_fault(**_kwargs):
            raise sqlite3.OperationalError("injected unknown fallback fault")

        CollectionImportRepository._existing_collection_id = staticmethod(
            identity_fault
        )
        self.repository.mark_save_outcome_unknown = mark_unknown_fault
        try:
            self.repository.reconcile_startup(FIXED_NOW)
        finally:
            CollectionImportRepository._existing_collection_id = staticmethod(
                original_identity_lookup
            )
            del self.repository.__dict__["mark_save_outcome_unknown"]

        snapshot = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(snapshot["status"], "cancelling")
        self.assertEqual(snapshot["items"][0]["state"], "outcome_unknown")
        with self.base._connect() as db:
            current = db.execute(
                """SELECT attempt_phase, result, claim_token,
                          frozen_request_json
                   FROM collection_import_save_attempts
                   WHERE save_attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            historical = db.execute(
                """SELECT attempt_phase, result
                   FROM collection_import_save_attempts
                   WHERE save_attempt_id='historical-known-failure'"""
            ).fetchone()
            before = db.execute(
                """SELECT revision, updated_at FROM collection_import_batches
                   WHERE batch_id=?""",
                (batch["batch_id"],),
            ).fetchone()
        self.assertEqual(
            tuple(current), ("result_observed", "unknown", None, "{}")
        )
        self.assertEqual(tuple(historical), ("settled", "known_not_written"))

        self.repository.reconcile_startup(FIXED_NOW + timedelta(minutes=1))
        with self.base._connect() as db:
            after = db.execute(
                """SELECT revision, updated_at FROM collection_import_batches
                   WHERE batch_id=?""",
                (batch["batch_id"],),
            ).fetchone()
        self.assertEqual(tuple(after), tuple(before))

    def test_startup_final_bulk_retries_current_success_not_historical_failure(self):
        self._insert_library_item(
            "startup-success", "https://startup.example/success"
        )
        batch, item_id, attempt_id, _claim = self._set_saving_result(
            result="success", collection_item_id="startup-success"
        )
        self._insert_historical_known_failure(item_id)
        with self.base._connect() as db:
            db.execute(
                """UPDATE collection_import_batches
                   SET status='cancelling', cancel_requested=1
                   WHERE batch_id=?""",
                (batch["batch_id"],),
            )
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='queued'
                   WHERE batch_id=? AND batch_item_id<>?""",
                (batch["batch_id"], item_id),
            )

        original_settle = self.repository._settle_observed_save_on_connection
        calls = 0

        def settle_fault_once(db: sqlite3.Connection, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("injected first settle fault")
            return original_settle(db, **kwargs)

        self.repository._settle_observed_save_on_connection = settle_fault_once
        try:
            self.repository.reconcile_startup(FIXED_NOW)
        finally:
            del self.repository.__dict__["_settle_observed_save_on_connection"]

        snapshot = self.repository.get_batch(batch["batch_id"], FIXED_NOW)
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(snapshot["items"][0]["state"], "saved")
        self.assertEqual(
            snapshot["items"][0]["collection_item_id"], "startup-success"
        )
        with self.base._connect() as db:
            current = db.execute(
                """SELECT attempt_phase, result, collection_item_id
                   FROM collection_import_save_attempts
                   WHERE save_attempt_id=?""",
                (attempt_id,),
            ).fetchone()
            self.assertEqual(
                tuple(current), ("settled", "success", "startup-success")
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM library_items WHERE id='startup-success'"
                ).fetchone()[0],
                1,
            )

    def _insert_terminal_batch(
        self,
        batch_id: str,
        terminal_at: datetime,
        *,
        with_binding: bool = True,
        collection_item_id: str | None = None,
        preview_id: str | None = None,
        identity_url: str | None = None,
    ) -> None:
        timestamp = terminal_at.isoformat()
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO collection_import_batches(
                       batch_id, status, revision, cancel_requested, total_count,
                       created_at, updated_at, terminal_at
                   ) VALUES (?, 'completed', 1, 0, 2, ?, ?, ?)""",
                (batch_id, timestamp, timestamp, timestamp),
            )
            db.execute(
                """INSERT INTO collection_import_batch_items(
                       batch_item_id, batch_id, position, client_item_id,
                       pending_input_text, display_label, state, decision,
                       preview_generation, preview_id, identity_url,
                       review_revision, item_revision, collection_item_id,
                       draft_json, created_at, updated_at
                   ) VALUES (?, ?, 0, ?, NULL, 'retained', ?, ?, ?, ?, ?,
                             1, 1, ?, '{}', ?, ?)""",
                (
                    f"{batch_id}-item",
                    batch_id,
                    f"client-{batch_id}",
                    "saved" if collection_item_id else "skipped",
                    "save" if collection_item_id else "skip",
                    1 if preview_id else 0,
                    preview_id,
                    identity_url,
                    collection_item_id,
                    timestamp,
                    timestamp,
                ),
            )
            db.execute(
                """INSERT INTO collection_import_batch_items(
                       batch_item_id, batch_id, position, client_item_id,
                       pending_input_text, display_label, state, decision,
                       preview_generation, review_revision, item_revision,
                       draft_json, created_at, updated_at
                   ) VALUES (?, ?, 1, ?, NULL, 'retained second', 'skipped',
                             'skip', 0, 0, 1, '{}', ?, ?)""",
                (
                    f"{batch_id}-item-two",
                    batch_id,
                    f"client-{batch_id}-two",
                    timestamp,
                    timestamp,
                ),
            )
            if with_binding:
                db.execute(
                    """INSERT INTO collection_import_batch_idempotency(
                           key_hash, request_fingerprint, batch_id, created_at,
                           expires_at, purged_at
                       ) VALUES (?, ?, ?, ?, ?, NULL)""",
                    (
                        f"binding-{batch_id}",
                        f"fingerprint-{batch_id}",
                        batch_id,
                        timestamp,
                        (terminal_at + timedelta(days=7)).isoformat(),
                    ),
                )

    def test_retention_uses_age_and_top20_gates_with_exact_boundary(self):
        cutoff = FIXED_NOW - timedelta(days=7)
        self._insert_terminal_batch("age-old", cutoff - timedelta(microseconds=1))
        self._insert_terminal_batch("age-boundary", cutoff)
        result = self.repository.maintain(FIXED_NOW)
        self.assertEqual(result["batches"], 1)
        with self.base._connect() as db:
            remaining = {
                row[0]
                for row in db.execute(
                    "SELECT batch_id FROM collection_import_batches"
                ).fetchall()
            }
            self.assertNotIn("age-old", remaining)
            self.assertIn("age-boundary", remaining)
            tombstone = db.execute(
                """SELECT batch_id, purged_at, expires_at
                   FROM collection_import_batch_idempotency
                   WHERE key_hash='binding-age-old'"""
            ).fetchone()
            self.assertEqual(
                tuple(tombstone),
                (
                    None,
                    FIXED_NOW.isoformat(),
                    (FIXED_NOW + timedelta(days=7)).isoformat(),
                ),
            )

        other_root = self.root / "top20"
        other_root.mkdir()
        other_base = SQLiteRepository(other_root / "top20.sqlite3")
        other_repository = CollectionImportRepository(other_base)
        original_base, original_repository = self.base, self.repository
        self.base, self.repository = other_base, other_repository
        try:
            for index in range(21):
                self._insert_terminal_batch(f"recent-{index:02d}", FIXED_NOW)
            top_result = self.repository.maintain(FIXED_NOW)
            self.assertEqual(top_result["batches"], 1)
            with self.base._connect() as db:
                retained = {
                    row[0]
                    for row in db.execute(
                        "SELECT batch_id FROM collection_import_batches"
                    ).fetchall()
                }
            self.assertNotIn("recent-00", retained)
            self.assertEqual(len(retained), 20)
        finally:
            self.base, self.repository = original_base, original_repository

    def test_retention_is_bounded_fail_closed_and_preserves_collection_data(self):
        old = FIXED_NOW - timedelta(days=8)
        for index in range(101):
            self._insert_terminal_batch(f"old-{index:03d}", old)
        first = self.repository.maintain(FIXED_NOW)
        self.assertEqual(first["batches"], 100)
        with self.base._connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM collection_import_batches"
                ).fetchone()[0],
                1,
            )
        second = self.repository.maintain(FIXED_NOW)
        self.assertEqual(second["batches"], 1)

        # A missing creation binding fails closed: never delete a batch without
        # its required seven-day tombstone.
        self._insert_terminal_batch("missing-binding", old, with_binding=False)
        self.assertEqual(self.repository.maintain(FIXED_NOW)["batches"], 0)
        self.assertIsNotNone(
            self.repository.get_batch("missing-binding", FIXED_NOW)
        )

        self._insert_library_item("retained-library", "https://keep.example/item")
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            db.execute(
                """INSERT INTO source_metadata(
                       collection_item_id, title_value, title_source,
                       title_fetched_at, author_value, author_source,
                       author_fetched_at, cover_url_value, cover_url_source,
                       cover_url_fetched_at, source_copy_value,
                       source_copy_source, source_copy_fetched_at,
                       platform_tags_json, warnings_json
                   ) VALUES ('retained-library', 'title', 'html', ?, '', 'none', ?,
                             'https://images.example/cover.jpg', 'html', ?, '',
                             'none', ?, '[]', '[]')""",
                (timestamp, timestamp, timestamp, timestamp),
            )
            db.execute(
                """INSERT INTO collection_idempotency_keys(
                       idempotency_key, request_hash, collection_item_id,
                       response_json, created_at
                   ) VALUES ('retained-idem', 'retained-hash',
                             'retained-library', '{}', ?)""",
                (timestamp,),
            )
            db.execute(
                """INSERT INTO collection_search_documents(
                       collection_item_id, indexed_text
                   ) VALUES ('retained-library', 'retained searchable title')"""
            )
            db.execute(
                """INSERT INTO collection_metadata_cache(
                       identity_url, payload_json, fetched_at, expires_at,
                       updated_at
                   ) VALUES ('https://keep.example/item', '{}', ?, ?, ?)""",
                (
                    timestamp,
                    (FIXED_NOW + timedelta(days=1)).isoformat(),
                    timestamp,
                ),
            )
            db.execute(
                """INSERT INTO collection_metadata_cache_aliases(
                       alias_url, identity_url, created_at
                   ) VALUES ('https://keep.example/alias',
                             'https://keep.example/item', ?)""",
                (timestamp,),
            )
        self._insert_terminal_batch(
            "purge-with-library",
            old,
            collection_item_id="retained-library",
            identity_url="https://keep.example/item",
        )
        self.assertEqual(self.repository.maintain(FIXED_NOW)["batches"], 1)
        with self.base._connect() as db:
            for table in (
                "library_items",
                "source_metadata",
                "collection_idempotency_keys",
                "collection_search_documents",
                "collection_metadata_cache",
                "collection_metadata_cache_aliases",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    1,
                    table,
                )

    def test_reference_aware_preview_cache_gc_and_cycle_bound(self):
        expired = FIXED_NOW - timedelta(seconds=1)
        self._insert_preview(
            "free-preview",
            identity_url="https://free.example/item",
            expires_at=expired,
        )
        self._insert_preview(
            "retained-preview",
            identity_url="https://retained.example/item",
            expires_at=expired,
        )
        self._insert_preview(
            "live-preview",
            identity_url="https://live.example/item",
            expires_at=FIXED_NOW + timedelta(seconds=1),
        )
        self._insert_terminal_batch(
            "retained-batch",
            FIXED_NOW,
            preview_id="retained-preview",
            identity_url="https://retained.example/item",
        )
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            for identity in (
                "https://free.example/item",
                "https://retained.example/item",
                "https://live.example/item",
            ):
                db.execute(
                    """INSERT INTO collection_metadata_cache(
                           identity_url, payload_json, fetched_at, expires_at,
                           updated_at
                       ) VALUES (?, '{}', ?, ?, ?)""",
                    (identity, timestamp, expired.isoformat(), timestamp),
                )
                db.execute(
                    """INSERT INTO collection_metadata_cache_aliases(
                           alias_url, identity_url, created_at
                       ) VALUES (?, ?, ?)""",
                    (identity + "/alias", identity, timestamp),
                )
        result = self.repository.maintain(FIXED_NOW)
        self.assertEqual(result["preview_cache"], 2)
        with self.base._connect() as db:
            previews = {
                row[0] for row in db.execute("SELECT preview_id FROM collection_previews")
            }
            caches = {
                row[0]
                for row in db.execute(
                    "SELECT identity_url FROM collection_metadata_cache"
                )
            }
            aliases = {
                row[0]
                for row in db.execute(
                    "SELECT identity_url FROM collection_metadata_cache_aliases"
                )
            }
        self.assertNotIn("free-preview", previews)
        self.assertIn("retained-preview", previews)
        self.assertIn("live-preview", previews)
        self.assertNotIn("https://free.example/item", caches)
        self.assertIn("https://retained.example/item", caches)
        self.assertIn("https://live.example/item", caches)
        self.assertNotIn("https://free.example/item", aliases)

        other_root = self.root / "gc-bound"
        other_root.mkdir()
        other_base = SQLiteRepository(other_root / "gc.sqlite3")
        other_repository = CollectionImportRepository(other_base)
        with other_base._connect() as db:
            for index in range(101):
                db.execute(
                    """INSERT INTO collection_previews(
                           preview_id, payload_json, created_at, expires_at
                       ) VALUES (?, '{}', ?, ?)""",
                    (
                        f"expired-{index:03d}",
                        timestamp,
                        expired.isoformat(),
                    ),
                )
        self.assertEqual(
            other_repository.maintain(FIXED_NOW)["preview_cache"], 100
        )
        self.assertEqual(
            other_repository.maintain(FIXED_NOW)["preview_cache"], 1
        )

    def test_gc_filters_protected_rows_before_limit_and_fails_closed(self):
        expired = FIXED_NOW - timedelta(seconds=1)
        live = FIXED_NOW + timedelta(seconds=1)
        timestamp = FIXED_NOW.isoformat()
        with self.base._connect() as db:
            for index in range(100):
                identity = f"https://000-protected-{index:03d}.example/item"
                db.execute(
                    """INSERT INTO collection_previews(
                           preview_id, payload_json, created_at, expires_at
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        f"live-protector-{index:03d}",
                        json.dumps({"identity_url": identity}),
                        timestamp,
                        live.isoformat(),
                    ),
                )
                db.execute(
                    """INSERT INTO collection_metadata_cache(
                           identity_url, payload_json, fetched_at, expires_at,
                           updated_at
                       ) VALUES (?, '{}', ?, ?, ?)""",
                    (identity, timestamp, expired.isoformat(), timestamp),
                )
            db.execute(
                """INSERT INTO collection_metadata_cache(
                       identity_url, payload_json, fetched_at, expires_at,
                       updated_at
                   ) VALUES ('https://zzz-free.example/item', '{}', ?, ?, ?)""",
                (timestamp, expired.isoformat(), timestamp),
            )
        result = self.repository.maintain(FIXED_NOW)
        self.assertEqual(result["preview_cache"], 1)
        self.assertLessEqual(result["preview_cache"], 100)
        with self.base._connect() as db:
            self.assertEqual(
                db.execute(
                    """SELECT COUNT(*) FROM collection_metadata_cache
                       WHERE identity_url LIKE 'https://000-protected-%'"""
                ).fetchone()[0],
                100,
            )
            self.assertIsNone(
                db.execute(
                    """SELECT 1 FROM collection_metadata_cache
                       WHERE identity_url='https://zzz-free.example/item'"""
                ).fetchone()
            )

        other_root = self.root / "gc-fail-closed"
        other_root.mkdir()
        other_base = SQLiteRepository(other_root / "gc-fail-closed.sqlite3")
        other_repository = CollectionImportRepository(other_base)
        original_base, original_repository = self.base, self.repository
        self.base, self.repository = other_base, other_repository
        try:
            batch = self._create_batch("gc-protection-batch")
            first, second = batch["items"]
            with self.base._connect() as db:
                db.execute(
                    """UPDATE collection_import_batches
                       SET status='interrupted', revision=5
                       WHERE batch_id=?""",
                    (batch["batch_id"],),
                )
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='outcome_unknown', pending_input_text=NULL,
                           error_code='BATCH_SAVE_OUTCOME_UNKNOWN',
                           error_stage='save'
                       WHERE batch_item_id=?""",
                    (first["batch_item_id"],),
                )
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET state='cancelled', pending_input_text=NULL,
                           terminal_reason='cancelled_by_user'
                       WHERE batch_item_id=?""",
                    (second["batch_item_id"],),
                )
                db.execute(
                    """INSERT INTO collection_previews(
                           preview_id, payload_json, created_at, expires_at
                       ) VALUES ('unresolved-preview', ?, ?, ?)""",
                    (
                        json.dumps(
                            {"identity_url": "https://unresolved.example/item"}
                        ),
                        timestamp,
                        expired.isoformat(),
                    ),
                )
                db.execute(
                    """INSERT INTO collection_metadata_cache(
                           identity_url, payload_json, fetched_at, expires_at,
                           updated_at
                       ) VALUES ('https://unresolved.example/item', '{}', ?, ?, ?)""",
                    (timestamp, expired.isoformat(), timestamp),
                )
                db.execute(
                    """INSERT INTO collection_import_save_attempts(
                           save_attempt_id, batch_item_id, preview_id,
                           preview_generation, review_revision,
                           frozen_request_json, collection_request_hash,
                           idempotency_key_hash, attempt_phase, result,
                           created_at, updated_at
                       ) VALUES ('unresolved-attempt', ?, 'unresolved-preview',
                                 1, 1, '{}', 'unresolved-hash',
                                 'unresolved-key-hash', 'result_observed',
                                 'unknown', ?, ?)""",
                    (first["batch_item_id"], timestamp, timestamp),
                )
            unresolved_result = self.repository.maintain(FIXED_NOW)
            self.assertEqual(unresolved_result["preview_cache"], 0)
            with self.base._connect() as db:
                self.assertIsNotNone(
                    db.execute(
                        """SELECT 1 FROM collection_previews
                           WHERE preview_id='unresolved-preview'"""
                    ).fetchone()
                )
                self.assertIsNotNone(
                    db.execute(
                        """SELECT 1 FROM collection_metadata_cache
                           WHERE identity_url='https://unresolved.example/item'"""
                    ).fetchone()
                )
                db.execute(
                    """INSERT INTO collection_previews(
                           preview_id, payload_json, created_at, expires_at
                       ) VALUES ('invalid-relevant-preview', '{invalid', ?, ?)""",
                    (timestamp, expired.isoformat()),
                )
                db.execute(
                    """UPDATE collection_import_batch_items
                       SET preview_id='invalid-relevant-preview'
                       WHERE batch_item_id=?""",
                    (second["batch_item_id"],),
                )
                db.execute(
                    """INSERT INTO collection_metadata_cache(
                           identity_url, payload_json, fetched_at, expires_at,
                           updated_at
                       ) VALUES ('https://invalid-guard.example/item', '{}',
                                 ?, ?, ?)""",
                    (timestamp, expired.isoformat(), timestamp),
                )
            invalid_result = self.repository.maintain(FIXED_NOW)
            self.assertEqual(invalid_result["preview_cache"], 0)
            with self.base._connect() as db:
                self.assertIsNotNone(
                    db.execute(
                        """SELECT 1 FROM collection_metadata_cache
                           WHERE identity_url='https://invalid-guard.example/item'"""
                    ).fetchone()
                )
        finally:
            self.base, self.repository = original_base, original_repository

    def test_terminal_at_is_not_refreshed_by_rederivation(self):
        terminal_at = FIXED_NOW - timedelta(days=1)
        self._insert_terminal_batch("stable-terminal", terminal_at)
        later = FIXED_NOW + timedelta(hours=1)
        with self.base._connect() as db:
            before = db.execute(
                """SELECT revision, updated_at, terminal_at
                   FROM collection_import_batches
                   WHERE batch_id='stable-terminal'"""
            ).fetchone()
            db.execute("BEGIN IMMEDIATE")
            CollectionImportRepository._derive_batch_state_on_connection(
                db, "stable-terminal", later.isoformat()
            )
        with self.base._connect() as db:
            observed = db.execute(
                """SELECT revision, updated_at, terminal_at
                   FROM collection_import_batches
                   WHERE batch_id='stable-terminal'"""
            ).fetchone()
        self.assertEqual(tuple(observed), tuple(before))
        self.assertEqual(observed["terminal_at"], terminal_at.isoformat())


if __name__ == "__main__":
    unittest.main()
