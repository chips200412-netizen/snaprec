from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import CollectionItemCreateRequest
from backend.app.repositories.collection_imports import (
    CollectionImportRepository,
    CollectionImportStateConflictError,
    collection_import_save_idempotency_key_hash,
)
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.collection_imports import (
    CollectionImportSaveCoordinator,
    SimulatedCollectionImportCrash,
)
from backend.app.services.collections import prepare_collection_create
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.pipeline import PipelineError
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)
from backend.app.services.safe_http import SafeFetchResult


class _Validator:
    def resolve(self, _url: str):
        return object()


class InstrumentedFetcher:
    def __init__(self, *, delay: float = 0.0, title: str = "公开标题"):
        self.delay = delay
        self.title = title
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self.url_validator = _Validator()

    def fetch(self, url: str) -> SafeFetchResult:
        with self._lock:
            self.calls.append(url)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            html = f"<html><head><title>{self.title}</title></head></html>"
            return SafeFetchResult(
                original_url=url,
                final_url=url,
                status_code=200,
                media_type="text/html",
                body=html.encode("utf-8"),
                redirects=(),
                content_type="text/html; charset=utf-8",
            )
        finally:
            with self._lock:
                self.active -= 1


def _payload(*urls: str) -> dict:
    return {
        "items": [
            {"client_item_id": f"item-{index}", "input_text": url}
            for index, url in enumerate(urls, 1)
        ]
    }


class CollectionImportMigrationTests(unittest.TestCase):
    def test_v16_schema_is_atomic_independent_and_retriable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migration.sqlite3"
            SQLiteRepository(path)
            collection_tables = {
                "collection_import_batches",
                "collection_import_batch_items",
                "collection_import_batch_idempotency",
                "collection_import_save_attempts",
            }
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(collection_tables.issubset(tables))
                self.assertIn("job_batches", tables)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                now = "2026-08-31T00:00:00+00:00"
                db.execute(
                    """INSERT INTO collection_import_batches(
                           batch_id, status, revision, cancel_requested,
                           total_count, created_at, updated_at
                       ) VALUES ('active-a', 'previewing', 1, 0, 2, ?, ?)""",
                    (now, now),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        """INSERT INTO collection_import_batches(
                               batch_id, status, revision, cancel_requested,
                               total_count, created_at, updated_at
                           ) VALUES ('active-b', 'previewing', 1, 0, 2, ?, ?)""",
                        (now, now),
                    )
                db.execute(
                    "DELETE FROM collection_import_batches WHERE batch_id='active-a'"
                )
                db.execute("DROP TABLE collection_import_save_attempts")
                db.execute("DROP TABLE collection_import_batch_idempotency")
                db.execute("DROP TABLE collection_import_batch_items")
                db.execute("DROP TABLE collection_import_batches")
                db.execute("PRAGMA user_version=15")
                db.commit()

            observed: list[str] = []

            def fail_after_v16(stage: str, _db: sqlite3.Connection) -> None:
                observed.append(stage)
                if stage == "after_v16_ddl":
                    raise RuntimeError("injected v16 failure")

            with self.assertRaisesRegex(RuntimeError, "injected v16"):
                SQLiteRepository(path, migration_fault=fail_after_v16)
            self.assertEqual(observed, ["after_v16_ddl"])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 15)
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue(collection_tables.isdisjoint(tables))

            SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


class CollectionImportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "imports.sqlite3"
        self.repository = SQLiteRepository(self.db_path)
        self.fetcher = InstrumentedFetcher()
        pipeline = LocalFullPipeline(
            self.repository,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        self._client_context = TestClient(
            create_app(
                pipeline,
                capture_fetcher=self.fetcher,
                upload_root=self.root / "uploads",
            )
        )
        self.client = self._client_context.__enter__()

    def tearDown(self):
        self._client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def _row_count(self, table: str) -> int:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _database_dump(self) -> str:
        with closing(sqlite3.connect(self.db_path)) as db:
            return "\n".join(line.strip() for line in db.iterdump())

    def _database_copy(self, name: str) -> Path:
        path = self.root / name
        with closing(sqlite3.connect(self.db_path)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
        return path

    def _create_ready_batch(self, *urls: str, key: str) -> dict:
        response = self.client.post(
            "/api/v1/collection-import-batches",
            json=_payload(*urls),
            headers={"Idempotency-Key": key},
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

    @staticmethod
    def _review_payload(batch: dict, item: dict, *, decision: str = "save") -> dict:
        return {
            "expected_batch_revision": batch["revision"],
            "expected_item_revision": item["item_revision"],
            "decision": decision,
            "user_title": "  我的批量标题  ",
            "untitled_confirmed": True,
            "organization_confirmation": {
                "primary_category": " 技术与工具 ",
                "secondary_category": " 开发工具 ",
                "organization_tags": [" AI   编程 ", "ai 编程"],
            },
            "personal_tags": [" 稍后读 ", "稍后读"],
            "inspiration": {
                "content": "只属于用户的灵感",
                "input_mode": "text",
                "transcription_status": "not_applicable",
            },
        }

    def _review_all(
        self, batch: dict, *, decisions: list[str] | None = None
    ) -> dict:
        decisions = decisions or ["save"] * len(batch["items"])
        current = batch
        for index, decision in enumerate(decisions):
            item = current["items"][index]
            response = self.client.patch(
                f"/api/v1/collection-import-batches/{current['batch_id']}"
                f"/items/{item['batch_item_id']}",
                json=self._review_payload(current, item, decision=decision),
            )
            self.assertEqual(response.status_code, 200, response.text)
            current = response.json()
        return current

    def _confirm(self, batch: dict):
        return self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}/confirm",
            json={"expected_batch_revision": batch["revision"]},
        )

    def test_review_patch_two_level_cas_persists_only_decision_and_draft(self):
        batch = self._create_ready_batch(
            "https://review.example/one",
            "https://review.example/two",
            key="review-cas-001",
        )
        target = batch["items"][0]
        before_batch_revision = batch["revision"]
        before_item_revision = target["item_revision"]

        response = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(batch, target),
        )
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        self.assertEqual(snapshot["revision"], before_batch_revision + 1)
        self.assertEqual(
            [item["position"] for item in snapshot["items"]], [0, 1]
        )
        reviewed = snapshot["items"][0]
        self.assertEqual(reviewed["decision"], "save")
        self.assertEqual(reviewed["item_revision"], before_item_revision + 1)
        self.assertNotIn("input_text", reviewed)
        self.assertNotIn("draft", reviewed)
        self.assertNotIn("inspiration", response.text)
        self.assertEqual(self._row_count("library_items"), 0)
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)

        detail = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        draft = detail.json()["draft"]
        self.assertEqual(draft["user_title"], "我的批量标题")
        self.assertEqual(
            draft["organization_confirmation"],
            {
                "primary_category": "技术与工具",
                "secondary_category": "开发工具",
                "organization_tags": ["AI 编程"],
            },
        )
        self.assertEqual(draft["personal_tags"], ["稍后读"])
        self.assertEqual(draft["inspiration"]["content"], "只属于用户的灵感")

        both_stale = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(batch, target, decision="skip"),
        )
        self.assertEqual(both_stale.status_code, 409, both_stale.text)
        self.assertEqual(
            both_stale.json()["error"],
            {
                "code": "BATCH_REVISION_CONFLICT",
                "message": "批次已被其他操作更新。",
                "current_revision": snapshot["revision"],
            },
        )

        item_stale_payload = self._review_payload(
            snapshot, target, decision="skip"
        )
        item_stale_payload["expected_batch_revision"] = snapshot["revision"]
        item_stale_payload["expected_item_revision"] = before_item_revision
        item_stale = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=item_stale_payload,
        )
        self.assertEqual(item_stale.status_code, 409, item_stale.text)
        self.assertEqual(
            item_stale.json()["error"],
            {
                "code": "BATCH_ITEM_REVISION_CONFLICT",
                "message": "批次项已被其他操作更新。",
                "current_revision": reviewed["item_revision"],
            },
        )
        self.assertEqual(self._row_count("library_items"), 0)

    def test_review_patch_rejects_forged_preview_and_unstable_item_states(self):
        batch = self._create_ready_batch(
            "https://state.example/item",
            "https://state.example/item#duplicate",
            key="review-states-001",
        )
        representative, duplicate = batch["items"]
        forged = self._review_payload(batch, representative)
        forged["preview_id"] = "client-forged-preview"
        forged["inspiration"]["content"] = "sensitive-review-content"
        response = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{representative['batch_item_id']}",
            json=forged,
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "BATCH_REQUEST_INVALID"
        )
        self.assertNotIn("sensitive-review-content", response.text)

        duplicate_response = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{duplicate['batch_item_id']}",
            json=self._review_payload(batch, duplicate),
        )
        self.assertEqual(duplicate_response.status_code, 409)
        self.assertEqual(
            duplicate_response.json()["error"]["code"],
            "BATCH_STATE_CONFLICT",
        )

        # The item table is the authority for identity stability; a stale
        # denormalized batch status must not make PATCH writable.
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE collection_import_batch_items SET state='queued'
                   WHERE batch_item_id=?""",
                (duplicate["batch_item_id"],),
            )
            db.commit()
        before_unstable = self._database_dump()
        unstable = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{representative['batch_item_id']}",
            json=self._review_payload(batch, representative),
        )
        self.assertEqual(unstable.status_code, 409, unstable.text)
        self.assertEqual(
            unstable.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )
        self.assertEqual(self._database_dump(), before_unstable)
        self.assertEqual(self._row_count("library_items"), 0)

    def test_review_patch_checks_both_cas_levels_before_observing_expiry(self):
        batch = self._create_ready_batch(
            "https://expiry.example/one",
            "https://expiry.example/two",
            key="review-expiry-cas-001",
        )
        target = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id = db.execute(
                "SELECT preview_id FROM collection_import_batch_items "
                "WHERE batch_item_id=?",
                (target["batch_item_id"],),
            ).fetchone()[0]
            db.execute(
                "UPDATE collection_previews SET expires_at=? WHERE preview_id=?",
                ("2000-01-01T00:00:00+00:00", preview_id),
            )
            db.commit()

        before_stale_batch = self._database_dump()
        stale_batch_payload = self._review_payload(batch, target)
        stale_batch_payload["expected_batch_revision"] = batch["revision"] - 1
        stale_batch = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=stale_batch_payload,
        )
        self.assertEqual(stale_batch.status_code, 409, stale_batch.text)
        self.assertEqual(
            stale_batch.json()["error"]["code"], "BATCH_REVISION_CONFLICT"
        )
        self.assertEqual(self._database_dump(), before_stale_batch)

        stale_item_payload = self._review_payload(batch, target)
        stale_item_payload["expected_item_revision"] = target["item_revision"] - 1
        stale_item = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=stale_item_payload,
        )
        self.assertEqual(stale_item.status_code, 409, stale_item.text)
        self.assertEqual(
            stale_item.json()["error"]["code"],
            "BATCH_ITEM_REVISION_CONFLICT",
        )
        self.assertEqual(self._database_dump(), before_stale_batch)

        matching = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(batch, target),
        )
        self.assertEqual(matching.status_code, 409, matching.text)
        self.assertEqual(
            matching.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            observed_batch_revision = db.execute(
                "SELECT revision FROM collection_import_batches WHERE batch_id=?",
                (batch["batch_id"],),
            ).fetchone()[0]
            observed_item = db.execute(
                """SELECT state, decision, item_revision, review_revision,
                          draft_json
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (target["batch_item_id"],),
            ).fetchone()
        self.assertEqual(observed_batch_revision, batch["revision"] + 1)
        self.assertEqual(observed_item[0], "preview_expired")
        self.assertEqual(observed_item[1], "pending")
        self.assertEqual(observed_item[2], target["item_revision"] + 1)
        self.assertEqual(observed_item[3], 0)
        self.assertNotIn("只属于用户的灵感", observed_item[4])

        expired_batch = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        expired_target = expired_batch["items"][0]
        skipped = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(
                expired_batch, expired_target, decision="skip"
            ),
        )
        self.assertEqual(skipped.status_code, 200, skipped.text)
        self.assertEqual(skipped.json()["items"][0]["decision"], "skip")

        # A concurrent expiry does not make an explicit skip unsafe: the same
        # matching-CAS transaction records preview_expired + skip once.
        skip_race_batch = skipped.json()
        skip_race_target = skip_race_batch["items"][1]
        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id = db.execute(
                "SELECT preview_id FROM collection_import_batch_items "
                "WHERE batch_item_id=?",
                (skip_race_target["batch_item_id"],),
            ).fetchone()[0]
            db.execute(
                "UPDATE collection_previews SET expires_at=? WHERE preview_id=?",
                ("2000-01-01T00:00:00+00:00", preview_id),
            )
            db.commit()
        skip_race = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{skip_race_target['batch_item_id']}",
            json=self._review_payload(
                skip_race_batch, skip_race_target, decision="skip"
            ),
        )
        self.assertEqual(skip_race.status_code, 200, skip_race.text)
        self.assertEqual(
            skip_race.json()["revision"], skip_race_batch["revision"] + 1
        )
        self.assertEqual(skip_race.json()["items"][1]["state"], "preview_expired")
        self.assertEqual(skip_race.json()["items"][1]["decision"], "skip")
        self.assertEqual(
            skip_race.json()["items"][1]["item_revision"],
            skip_race_target["item_revision"] + 1,
        )
        self.assertEqual(self._row_count("library_items"), 0)

    def test_review_patch_supports_noop_and_explicit_decision_updates(self):
        batch = self._create_ready_batch(
            "https://updates.example/one",
            "https://updates.example/two",
            key="review-updates-001",
        )
        target = batch["items"][0]
        first = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(batch, target),
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_snapshot = first.json()
        first_item = first_snapshot["items"][0]

        replay = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=self._review_payload(first_snapshot, first_item),
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["revision"], first_snapshot["revision"])
        self.assertEqual(
            replay.json()["items"][0]["item_revision"],
            first_item["item_revision"],
        )

        skip_payload = self._review_payload(
            replay.json(), replay.json()["items"][0], decision="skip"
        )
        skipped = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=skip_payload,
        )
        self.assertEqual(skipped.status_code, 200, skipped.text)
        self.assertEqual(skipped.json()["revision"], first_snapshot["revision"] + 1)
        self.assertEqual(skipped.json()["items"][0]["decision"], "skip")

        save_payload = self._review_payload(
            skipped.json(), skipped.json()["items"][0], decision="save"
        )
        saved_decision = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=save_payload,
        )
        self.assertEqual(saved_decision.status_code, 200, saved_decision.text)
        self.assertEqual(saved_decision.json()["items"][0]["decision"], "save")
        self.assertEqual(self._row_count("library_items"), 0)
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)

    def test_review_patch_needs_review_uses_single_save_title_eligibility(self):
        batch = self._create_ready_batch(
            "https://untitled.example/one",
            "https://untitled.example/two",
            key="review-untitled-001",
        )
        target = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id, payload_json = db.execute(
                """SELECT i.preview_id, p.payload_json
                   FROM collection_import_batch_items i
                   JOIN collection_previews p ON p.preview_id=i.preview_id
                   WHERE i.batch_item_id=?""",
                (target["batch_item_id"],),
            ).fetchone()
            payload = json.loads(payload_json)
            payload["metadata"]["title"]["source"] = "none"
            payload["metadata"]["title"]["value"] = ""
            db.execute(
                "UPDATE collection_previews SET payload_json=? WHERE preview_id=?",
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    preview_id,
                ),
            )
            db.execute(
                """UPDATE collection_import_batch_items SET state='needs_review'
                   WHERE batch_item_id=?""",
                (target["batch_item_id"],),
            )
            db.commit()

        invalid_payload = self._review_payload(batch, target)
        invalid_payload["user_title"] = None
        invalid_payload["untitled_confirmed"] = False
        before_invalid = self._database_dump()
        invalid = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=invalid_payload,
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(
            invalid.json()["error"]["code"], "BATCH_REQUEST_INVALID"
        )
        self.assertEqual(self._database_dump(), before_invalid)

        explicit_untitled = dict(invalid_payload)
        explicit_untitled["untitled_confirmed"] = True
        accepted = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}",
            json=explicit_untitled,
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["items"][0]["decision"], "save")
        self.assertEqual(accepted.json()["items"][0]["state"], "needs_review")
        self.assertEqual(self._row_count("library_items"), 0)

    def test_repreview_queues_without_generation_bump_then_recomputes_both_groups(self):
        batch = self._create_ready_batch(
            "https://old.example/item#one",
            "https://old.example/item#two",
            "https://new.example/item",
            key="repreview-groups-001",
        )
        first, old_duplicate, new_representative = batch["items"]
        first_review = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{first['batch_item_id']}",
            json=self._review_payload(batch, first),
        )
        self.assertEqual(first_review.status_code, 200, first_review.text)
        after_first = first_review.json()
        new_representative = after_first["items"][2]
        second_review = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{new_representative['batch_item_id']}",
            json=self._review_payload(after_first, new_representative),
        )
        self.assertEqual(second_review.status_code, 200, second_review.text)
        before_repreview = second_review.json()
        first = before_repreview["items"][0]
        original_generation = first["preview_generation"]
        with closing(sqlite3.connect(self.db_path)) as db:
            original_preview_id = db.execute(
                "SELECT preview_id FROM collection_import_batch_items "
                "WHERE batch_item_id=?",
                (first["batch_item_id"],),
            ).fetchone()[0]

        release = threading.Event()
        entered = threading.Event()

        def block_after_repreview_claim(stage: str, _context: dict) -> None:
            if stage == "after_preview_claim":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test claim gate timed out")

        coordinator = self.client.app.state.collection_import_coordinator
        coordinator.fault = block_after_repreview_claim
        response = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{first['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": before_repreview["revision"],
                "expected_item_revision": first["item_revision"],
                "input_text": "https://new.example/item#replacement",
            },
        )
        try:
            self.assertEqual(response.status_code, 202, response.text)
            queued = response.json()
            self.assertEqual(
                [item["position"] for item in queued["items"]], [0, 1, 2]
            )
            self.assertEqual(queued["items"][0]["state"], "queued")
            self.assertEqual(
                queued["items"][0]["preview_generation"], original_generation
            )
            self.assertEqual(queued["items"][0]["decision"], "pending")
            self.assertEqual(queued["items"][1]["state"], "ready")
            self.assertIsNone(
                queued["items"][1]["duplicate_of_batch_item_id"]
            )
            self.assertNotIn("input_text", response.text)
            self.assertNotIn("draft", response.text)
            self.assertNotIn("只属于用户的灵感", response.text)
            self.assertTrue(entered.wait(5))
            with closing(sqlite3.connect(self.db_path)) as db:
                claimed = db.execute(
                    """SELECT state, preview_generation, preview_id,
                              pending_input_text, review_revision, decision
                       FROM collection_import_batch_items
                       WHERE batch_item_id=?""",
                    (first["batch_item_id"],),
                ).fetchone()
            self.assertEqual(claimed[0], "previewing")
            self.assertEqual(claimed[1], original_generation + 1)
            self.assertNotEqual(claimed[2], original_preview_id)
            self.assertEqual(
                claimed[3], "https://new.example/item#replacement"
            )
            self.assertEqual(claimed[4], 0)
            self.assertEqual(claimed[5], "pending")

            in_flight = self.client.get(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
            ).json()
            in_flight_target = in_flight["items"][0]
            before_paused_review = self._database_dump()
            paused_review = self.client.patch(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
                f"/items/{first['batch_item_id']}",
                json=self._review_payload(in_flight, in_flight_target),
            )
            self.assertEqual(paused_review.status_code, 409, paused_review.text)
            self.assertEqual(
                paused_review.json()["error"]["code"],
                "BATCH_STATE_CONFLICT",
            )
            self.assertEqual(self._database_dump(), before_paused_review)
        finally:
            release.set()
            coordinator.fault = None
        self.assertTrue(
            self.client.app.state.collection_import_coordinator.wait_idle(10)
        )
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["items"][0]["state"], "ready")
        self.assertEqual(final["items"][1]["state"], "ready")
        self.assertEqual(final["items"][2]["state"], "duplicate_in_batch")
        self.assertEqual(
            final["items"][2]["duplicate_of_batch_item_id"],
            final["items"][0]["batch_item_id"],
        )
        self.assertEqual(final["items"][2]["decision"], "pending")
        detail = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{first['batch_item_id']}"
        ).json()
        self.assertEqual(detail["draft"]["user_title"], "我的批量标题")
        self.assertFalse(detail["draft"]["untitled_confirmed"])
        self.assertEqual(detail["draft"]["personal_tags"], ["稍后读"])
        self.assertEqual(
            detail["draft"]["inspiration"]["content"], "只属于用户的灵感"
        )
        self.assertEqual(self._row_count("library_items"), 0)

    def test_repreview_state_allowlist_and_denylist_are_exact(self):
        batch = self._create_ready_batch(
            "https://matrix.example/one",
            "https://matrix.example/two",
            key="repreview-matrix-001",
        )
        target = batch["items"][0]
        allowed = (
            ("ready", None, "awaiting_review"),
            ("needs_review", None, "awaiting_review"),
            ("preview_expired", "preview", "awaiting_review"),
            ("failed", "preview", "interrupted"),
        )
        for state, error_stage, batch_status in allowed:
            with self.subTest(allowed=state, batch_status=batch_status):
                copy_path = self._database_copy(f"allowed-{state}.sqlite3")
                copied_repository = SQLiteRepository(copy_path)
                with closing(sqlite3.connect(copy_path)) as db:
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state=?, error_stage=?, decision='save'
                           WHERE batch_item_id=?""",
                        (state, error_stage, target["batch_item_id"]),
                    )
                    db.execute(
                        """UPDATE collection_import_batches
                           SET status=?, cancel_requested=0,
                               terminal_at=NULL WHERE batch_id=?""",
                        (batch_status, batch["batch_id"]),
                    )
                    db.commit()
                    current = db.execute(
                        """SELECT b.revision, i.item_revision,
                                  i.preview_generation
                           FROM collection_import_batches b
                           JOIN collection_import_batch_items i
                             ON i.batch_id=b.batch_id
                           WHERE i.batch_item_id=?""",
                        (target["batch_item_id"],),
                    ).fetchone()
                result = CollectionImportRepository(
                    copied_repository
                ).queue_repreview(
                    batch_id=batch["batch_id"],
                    batch_item_id=target["batch_item_id"],
                    expected_batch_revision=current[0],
                    expected_item_revision=current[1],
                    replacement_provided=True,
                    replacement_input=f"https://allowed.example/{state}",
                    item_character_limit=10_000,
                    input_utf8_limit=64 * 1024,
                    now=datetime.now(UTC),
                )
                queued = result["items"][0]
                self.assertEqual(queued["state"], "queued")
                self.assertEqual(queued["decision"], "pending")
                self.assertEqual(queued["preview_generation"], current[2])

        forbidden = (
            ("duplicate_in_batch", None),
            ("save_queued", None),
            ("saving", None),
            ("saved", None),
            ("already_exists", None),
            ("skipped", None),
            ("cancelled", None),
            ("outcome_unknown", "save"),
            ("queued", None),
            ("previewing", None),
            ("interrupted", "preview"),
            ("failed", "save"),
        )
        for index, (state, error_stage) in enumerate(forbidden):
            with self.subTest(forbidden=state, error_stage=error_stage):
                copy_path = self._database_copy(
                    f"forbidden-{index}-{state}.sqlite3"
                )
                copied_repository = SQLiteRepository(copy_path)
                with closing(sqlite3.connect(copy_path)) as db:
                    db.execute(
                        """UPDATE collection_import_batch_items
                           SET state=?, error_stage=? WHERE batch_item_id=?""",
                        (state, error_stage, target["batch_item_id"]),
                    )
                    db.execute(
                        """UPDATE collection_import_batches
                           SET status='awaiting_review', cancel_requested=0,
                               terminal_at=NULL WHERE batch_id=?""",
                        (batch["batch_id"],),
                    )
                    db.commit()
                    current = db.execute(
                        """SELECT b.revision, i.item_revision
                           FROM collection_import_batches b
                           JOIN collection_import_batch_items i
                             ON i.batch_id=b.batch_id
                           WHERE i.batch_item_id=?""",
                        (target["batch_item_id"],),
                    ).fetchone()
                    before = "\n".join(
                        line.strip() for line in db.iterdump()
                    )
                with self.assertRaises(CollectionImportStateConflictError):
                    CollectionImportRepository(
                        copied_repository
                    ).queue_repreview(
                        batch_id=batch["batch_id"],
                        batch_item_id=target["batch_item_id"],
                        expected_batch_revision=current[0],
                        expected_item_revision=current[1],
                        replacement_provided=True,
                        replacement_input="https://forbidden.example/item",
                        item_character_limit=10_000,
                        input_utf8_limit=64 * 1024,
                        now=datetime.now(UTC),
                    )
                with closing(sqlite3.connect(copy_path)) as db:
                    after = "\n".join(
                        line.strip() for line in db.iterdump()
                    )
                self.assertEqual(after, before)

        forbidden_batches = (
            ("completed", "2026-08-31T00:00:00+00:00", 0),
            ("cancelling", None, 1),
        )
        for index, (status_name, terminal_at, cancel_requested) in enumerate(
            forbidden_batches
        ):
            with self.subTest(forbidden_batch=status_name):
                copy_path = self._database_copy(
                    f"forbidden-batch-{index}.sqlite3"
                )
                copied_repository = SQLiteRepository(copy_path)
                with closing(sqlite3.connect(copy_path)) as db:
                    db.execute(
                        """UPDATE collection_import_batches
                           SET status=?, terminal_at=?, cancel_requested=?
                           WHERE batch_id=?""",
                        (
                            status_name,
                            terminal_at,
                            cancel_requested,
                            batch["batch_id"],
                        ),
                    )
                    db.commit()
                    current = db.execute(
                        """SELECT b.revision, i.item_revision
                           FROM collection_import_batches b
                           JOIN collection_import_batch_items i
                             ON i.batch_id=b.batch_id
                           WHERE i.batch_item_id=?""",
                        (target["batch_item_id"],),
                    ).fetchone()
                    before = "\n".join(
                        line.strip() for line in db.iterdump()
                    )
                with self.assertRaises(CollectionImportStateConflictError):
                    CollectionImportRepository(
                        copied_repository
                    ).queue_repreview(
                        batch_id=batch["batch_id"],
                        batch_item_id=target["batch_item_id"],
                        expected_batch_revision=current[0],
                        expected_item_revision=current[1],
                        replacement_provided=True,
                        replacement_input="https://terminal.example/item",
                        item_character_limit=10_000,
                        input_utf8_limit=64 * 1024,
                        now=datetime.now(UTC),
                    )
                with closing(sqlite3.connect(copy_path)) as db:
                    after = "\n".join(
                        line.strip() for line in db.iterdump()
                    )
                self.assertEqual(after, before)

    def test_repreview_validation_and_full_batch_utf8_limit_are_atomic(self):
        inputs = tuple(
            f"https://bytes-{index}.example/item " + ("a" * 7_950)
            for index in range(6)
        )
        batch = self._create_ready_batch(
            *inputs,
            key="repreview-bytes-001",
        )
        target = batch["items"][0]
        oversized_batch_replacement = (
            "https://bytes-replacement.example/item " + ("界" * 9_900)
        )
        self.assertLessEqual(len(oversized_batch_replacement), 10_000)
        before = self._database_dump()
        calls_before = list(self.fetcher.calls)
        response = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": oversized_batch_replacement,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "BATCH_REQUEST_INVALID"
        )
        self.assertNotIn("bytes-replacement", response.text)
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

    def test_repreview_two_level_cas_failures_are_ordered_and_side_effect_free(self):
        batch = self._create_ready_batch(
            "https://repreview-cas.example/one",
            "https://repreview-cas.example/two",
            key="repreview-cas-001",
        )
        target = batch["items"][0]
        before = self._database_dump()
        calls_before = list(self.fetcher.calls)
        both_stale = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"] - 1,
                "expected_item_revision": target["item_revision"] - 1,
                "input_text": "https://repreview-cas.example/replacement",
            },
        )
        self.assertEqual(both_stale.status_code, 409, both_stale.text)
        self.assertEqual(
            both_stale.json()["error"],
            {
                "code": "BATCH_REVISION_CONFLICT",
                "message": "批次已被其他操作更新。",
                "current_revision": batch["revision"],
            },
        )
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

        item_stale = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"] - 1,
                "input_text": "https://repreview-cas.example/replacement",
            },
        )
        self.assertEqual(item_stale.status_code, 409, item_stale.text)
        self.assertEqual(
            item_stale.json()["error"],
            {
                "code": "BATCH_ITEM_REVISION_CONFLICT",
                "message": "批次项已被其他操作更新。",
                "current_revision": target["item_revision"],
            },
        )
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

    def test_commands_reject_invalid_json_and_surrogates_without_side_effects(self):
        batch = self._create_ready_batch(
            "https://unicode.example/one",
            "https://unicode.example/two",
            key="unicode-scalars-001",
        )
        target = batch["items"][0]
        before = self._database_dump()
        calls_before = list(self.fetcher.calls)

        review_payloads = []
        surrogate_title = self._review_payload(batch, target)
        surrogate_title["user_title"] = "\ud800sensitive-title"
        review_payloads.append(surrogate_title)
        surrogate_inspiration = self._review_payload(batch, target)
        surrogate_inspiration["inspiration"] = {
            "content": "\udfffsensitive-inspiration",
            "input_mode": "text",
            "transcription_status": "not_applicable",
        }
        review_payloads.append(surrogate_inspiration)
        for index, payload in enumerate(review_payloads):
            with self.subTest(review_surrogate=index):
                response = self.client.patch(
                    f"/api/v1/collection-import-batches/{batch['batch_id']}"
                    f"/items/{target['batch_item_id']}",
                    content=json.dumps(payload).encode("ascii"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "BATCH_REQUEST_INVALID",
                )
                self.assertNotIn("sensitive", response.text)
                self.assertEqual(self._database_dump(), before)
                self.assertEqual(self.fetcher.calls, calls_before)

        repreview_body = json.dumps(
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://unicode.example/\ud800sensitive-input",
            }
        ).encode("ascii")
        repreview = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            content=repreview_body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(repreview.status_code, 422, repreview.text)
        self.assertEqual(
            repreview.json()["error"]["code"], "BATCH_REQUEST_INVALID"
        )
        self.assertNotIn("sensitive", repreview.text)
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

        invalid_payloads = (
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": None,
            },
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "   ",
            },
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "x" * 10_001,
            },
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://valid.example/item",
                "preview_id": "client-forged-preview",
            },
            {
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://valid.example/item",
                "dirty": True,
            },
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(invalid=index):
                invalid = self.client.post(
                    f"/api/v1/collection-import-batches/{batch['batch_id']}"
                    f"/items/{target['batch_item_id']}/repreview",
                    json=payload,
                )
                self.assertEqual(invalid.status_code, 422, invalid.text)
                self.assertEqual(
                    invalid.json()["error"]["code"], "BATCH_REQUEST_INVALID"
                )
                self.assertEqual(self._database_dump(), before)
                self.assertEqual(self.fetcher.calls, calls_before)

        repeated_key = (
            '{"expected_batch_revision":%d,'
            '"expected_batch_revision":%d,'
            '"expected_item_revision":%d}'
            % (batch["revision"], batch["revision"], target["item_revision"])
        ).encode("utf-8")
        repeated = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            content=repeated_key,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(repeated.status_code, 422, repeated.text)
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

        too_large = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            content=b"x" * (128 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(too_large.status_code, 413, too_large.text)
        self.assertEqual(too_large.json()["error"]["code"], "BATCH_BODY_TOO_LARGE")
        unsupported = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(unsupported.status_code, 415, unsupported.text)
        self.assertEqual(self._database_dump(), before)
        self.assertEqual(self.fetcher.calls, calls_before)

    def test_repreview_generation_limit_and_missing_old_input_are_authoritative(self):
        batch = self._create_ready_batch(
            "https://authority.example/one",
            "https://authority.example/two",
            key="repreview-authority-001",
        )
        target = batch["items"][0]
        original_generation = target["preview_generation"]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE collection_import_batch_items
                   SET preview_generation=5 WHERE batch_item_id=?""",
                (target["batch_item_id"],),
            )
            db.commit()
        before_limit = self._database_dump()
        calls_before = list(self.fetcher.calls)
        limit = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
            },
        )
        self.assertEqual(limit.status_code, 409, limit.text)
        self.assertEqual(limit.json()["error"]["code"], "BATCH_ATTEMPT_LIMIT")
        self.assertEqual(self._database_dump(), before_limit)
        self.assertEqual(self.fetcher.calls, calls_before)

        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id = db.execute(
                "SELECT preview_id FROM collection_import_batch_items "
                "WHERE batch_item_id=?",
                (target["batch_item_id"],),
            ).fetchone()[0]
            db.execute(
                """UPDATE collection_import_batch_items
                   SET preview_generation=? WHERE batch_item_id=?""",
                (original_generation, target["batch_item_id"]),
            )
            db.execute(
                "DELETE FROM collection_previews WHERE preview_id=?",
                (preview_id,),
            )
            db.commit()
        before_missing = self._database_dump()
        missing = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
            },
        )
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertEqual(
            missing.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )
        self.assertEqual(self._database_dump(), before_missing)
        self.assertEqual(self.fetcher.calls, calls_before)

        release = threading.Event()
        entered = threading.Event()

        def block_replacement_claim(stage: str, _context: dict) -> None:
            if stage == "after_preview_claim":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test replacement gate timed out")

        coordinator = self.client.app.state.collection_import_coordinator
        coordinator.fault = block_replacement_claim
        replacement = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://authority.example/replacement",
            },
        )
        try:
            self.assertEqual(replacement.status_code, 202, replacement.text)
            self.assertEqual(replacement.json()["items"][0]["state"], "queued")
            self.assertEqual(
                replacement.json()["items"][0]["preview_generation"],
                original_generation,
            )
            self.assertTrue(entered.wait(5))
        finally:
            release.set()
            coordinator.fault = None
        self.assertTrue(coordinator.wait_idle(10))

    def test_repreview_unresolved_attempt_blocks_but_settled_attempt_does_not(self):
        batch = self._create_ready_batch(
            "https://attempt.example/one",
            "https://attempt.example/two",
            key="repreview-attempt-001",
        )
        target = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                """SELECT preview_id, preview_generation
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (target["batch_item_id"],),
            ).fetchone()
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision,
                       frozen_request_json, collection_request_hash,
                       idempotency_key_hash, attempt_phase, result,
                       claim_token, error_code, collection_item_id,
                       created_at, updated_at, settled_at
                   ) VALUES (?, ?, ?, ?, 1, NULL, ?, ?, 'call_started',
                             'none', NULL, NULL, NULL, ?, ?, NULL)""",
                (
                    "attempt-unresolved",
                    target["batch_item_id"],
                    row[0],
                    row[1],
                    "request-hash",
                    "key-hash",
                    "2026-08-31T00:00:00+00:00",
                    "2026-08-31T00:00:00+00:00",
                ),
            )
            db.commit()
        before_unresolved = self._database_dump()
        calls_before = list(self.fetcher.calls)
        blocked = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://attempt.example/replacement",
            },
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(
            blocked.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )
        self.assertEqual(self._database_dump(), before_unresolved)
        self.assertEqual(self.fetcher.calls, calls_before)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='settled', result='known_not_written',
                       settled_at=?, updated_at=? WHERE save_attempt_id=?""",
                (
                    "2026-08-31T00:01:00+00:00",
                    "2026-08-31T00:01:00+00:00",
                    "attempt-unresolved",
                ),
            )
            db.commit()

        release = threading.Event()
        entered = threading.Event()

        def block_settled_claim(stage: str, _context: dict) -> None:
            if stage == "after_preview_claim":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test settled gate timed out")

        coordinator = self.client.app.state.collection_import_coordinator
        coordinator.fault = block_settled_claim
        accepted = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{target['batch_item_id']}/repreview",
            json={
                "expected_batch_revision": batch["revision"],
                "expected_item_revision": target["item_revision"],
                "input_text": "https://attempt.example/replacement",
            },
        )
        try:
            self.assertEqual(accepted.status_code, 202, accepted.text)
            self.assertEqual(accepted.json()["items"][0]["state"], "queued")
            self.assertTrue(entered.wait(5))
        finally:
            release.set()
            coordinator.fault = None
        self.assertTrue(coordinator.wait_idle(10))

    def test_preparse_bounds_reject_without_persistence_or_fetch(self):
        headers = {"Idempotency-Key": "bounded-001"}
        unsupported = self.client.post(
            "/api/v1/collection-import-batches",
            content=b"{}",
            headers={**headers, "Content-Type": "text/plain"},
        )
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(
            unsupported.json()["error"]["code"],
            "BATCH_CONTENT_TYPE_UNSUPPORTED",
        )
        wrong_charset = self.client.post(
            "/api/v1/collection-import-batches",
            content=b"{}",
            headers={
                **headers,
                "Content-Type": "application/json; charset=gbk",
            },
        )
        self.assertEqual(wrong_charset.status_code, 415)

        too_large = self.client.post(
            "/api/v1/collection-import-batches",
            content=b"x" * (128 * 1024 + 1),
            headers={**headers, "Content-Type": "application/json"},
        )
        self.assertEqual(too_large.status_code, 413)
        self.assertEqual(too_large.json()["error"]["code"], "BATCH_BODY_TOO_LARGE")
        exact_limit = b'{"items":[]}' + b" " * (
            128 * 1024 - len(b'{"items":[]}')
        )
        exact = self.client.post(
            "/api/v1/collection-import-batches",
            content=exact_limit,
            headers={**headers, "Content-Type": "application/json"},
        )
        self.assertEqual(exact.status_code, 422)
        self.assertEqual(exact.json()["error"]["code"], "BATCH_REQUEST_INVALID")

        deeply_nested = (
            b'{"items":' + (b"[" * 50_000) + (b"]" * 50_000) + b"}"
        )
        self.assertLess(len(deeply_nested), 128 * 1024)
        before_deep_request = self._database_dump()
        deep_response = self.client.post(
            "/api/v1/collection-import-batches",
            content=deeply_nested,
            headers={
                "Idempotency-Key": "deeply-nested-001",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(deep_response.status_code, 422, deep_response.text)
        self.assertEqual(
            deep_response.json()["error"]["code"], "BATCH_REQUEST_INVALID"
        )
        self.assertEqual(self._database_dump(), before_deep_request)

        invalid_payloads = [
            {"items": [{"client_item_id": "one", "input_text": "https://a.test"}]},
            {
                "items": [
                    {"client_item_id": "same", "input_text": "https://a.test"},
                    {"client_item_id": "same", "input_text": "https://b.test"},
                ]
            },
            {
                "items": [
                    {
                        "client_item_id": f"many-{index}",
                        "input_text": f"https://{index}.test",
                    }
                    for index in range(11)
                ]
            },
            {
                "items": [
                    {
                        "client_item_id": f"utf8-{index}",
                        "input_text": "界" * 8_000,
                    }
                    for index in range(3)
                ]
            },
            {
                "items": [
                    {"client_item_id": "one", "input_text": "x" * 10_001},
                    {"client_item_id": "two", "input_text": "https://b.test"},
                ]
            },
            {
                "items": [
                    {"client_item_id": "bad id", "input_text": "https://a.test"},
                    {"client_item_id": "two", "input_text": "https://b.test"},
                ]
            },
        ]
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(index=index):
                response = self.client.post(
                    "/api/v1/collection-import-batches",
                    json=payload,
                    headers={"Idempotency-Key": f"invalid-{index}"},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "BATCH_REQUEST_INVALID"
                )

        missing_key = self.client.post(
            "/api/v1/collection-import-batches",
            json=_payload("https://a.test", "https://b.test"),
        )
        self.assertEqual(missing_key.status_code, 422)
        for table in (
            "collection_import_batches",
            "collection_import_batch_items",
            "collection_import_batch_idempotency",
            "collection_import_save_attempts",
        ):
            self.assertEqual(self._row_count(table), 0, table)
        self.assertEqual(self.fetcher.calls, [])

    def test_create_get_idempotency_and_active_are_bounded(self):
        payload = _payload("https://a.example/one", "https://b.example/two")
        response = self.client.post(
            "/api/v1/collection-import-batches",
            json=payload,
            headers={"Idempotency-Key": "create-001"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        created = response.json()
        self.assertEqual([item["position"] for item in created["items"]], [0, 1])
        self.assertNotIn("input_text", created["items"][0])
        self.assertNotIn("draft", created["items"][0])
        self.assertTrue(
            self.client.app.state.collection_import_coordinator.wait_idle(5)
        )

        active = self.client.get("/api/v1/collection-import-batches/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["batch_id"], created["batch_id"])
        restored = self.client.get(
            f"/api/v1/collection-import-batches/{created['batch_id']}"
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["status"], "awaiting_review")
        first = restored.json()["items"][0]
        detail = self.client.get(
            f"/api/v1/collection-import-batches/{created['batch_id']}"
            f"/items/{first['batch_item_id']}"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertTrue(detail.json()["input_available"])
        self.assertEqual(detail.json()["input_text"], payload["items"][0]["input_text"])
        self.assertIsNotNone(detail.json()["preview"])
        self.assertEqual(detail.json()["draft"]["personal_tags"], [])

        replay = self.client.post(
            "/api/v1/collection-import-batches",
            json=payload,
            headers={"Idempotency-Key": "create-001"},
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["batch_id"], created["batch_id"])
        reused = self.client.post(
            "/api/v1/collection-import-batches",
            json=_payload("https://a.example/changed", "https://b.example/two"),
            headers={"Idempotency-Key": "create-001"},
        )
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(reused.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")
        reordered = self.client.post(
            "/api/v1/collection-import-batches",
            json={"items": list(reversed(payload["items"]))},
            headers={"Idempotency-Key": "create-001"},
        )
        self.assertEqual(reordered.status_code, 409)
        self.assertEqual(
            reordered.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED"
        )
        other = self.client.post(
            "/api/v1/collection-import-batches",
            json=payload,
            headers={"Idempotency-Key": "create-002"},
        )
        self.assertEqual(other.status_code, 409)
        self.assertEqual(other.json()["error"]["code"], "BATCH_ACTIVE")
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute(
                """SELECT key_hash FROM collection_import_batch_idempotency"""
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(rows[0][0], "create-001")

    def test_not_found_errors_are_stable(self):
        batch = self.client.get("/api/v1/collection-import-batches/missing")
        self.assertEqual(batch.status_code, 404)
        self.assertEqual(batch.json()["error"]["code"], "BATCH_NOT_FOUND")
        item = self.client.get(
            "/api/v1/collection-import-batches/missing/items/missing"
        )
        self.assertEqual(item.status_code, 404)
        self.assertEqual(item.json()["error"]["code"], "BATCH_NOT_FOUND")

        missing_review = self.client.patch(
            "/api/v1/collection-import-batches/missing/items/missing",
            json=self._review_payload(
                {"revision": 1}, {"item_revision": 1}
            ),
        )
        self.assertEqual(missing_review.status_code, 404, missing_review.text)
        self.assertEqual(
            missing_review.json()["error"]["code"], "BATCH_NOT_FOUND"
        )
        missing_repreview = self.client.post(
            "/api/v1/collection-import-batches/missing/items/missing/repreview",
            json={
                "expected_batch_revision": 1,
                "expected_item_revision": 1,
            },
        )
        self.assertEqual(
            missing_repreview.status_code, 404, missing_repreview.text
        )
        self.assertEqual(
            missing_repreview.json()["error"]["code"], "BATCH_NOT_FOUND"
        )

    def test_one_invalid_link_fails_locally_without_blocking_other_preview(self):
        response = self.client.post(
            "/api/v1/collection-import-batches",
            json=_payload(
                "https://a.example/one https://b.example/two",
                "https://valid.example/item",
            ),
            headers={"Idempotency-Key": "partial-preview"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(
            self.client.app.state.collection_import_coordinator.wait_idle(5)
        )
        batch = self.client.get(
            f"/api/v1/collection-import-batches/{response.json()['batch_id']}"
        ).json()
        self.assertEqual(batch["status"], "awaiting_review")
        self.assertEqual(
            [item["state"] for item in batch["items"]], ["failed", "ready"]
        )
        self.assertEqual(
            batch["items"][0]["error_code"], "CAPTURE_LINK_AMBIGUOUS"
        )
        failed_detail = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{batch['items'][0]['batch_item_id']}"
        ).json()
        self.assertTrue(failed_detail["input_available"])
        self.assertIsNone(failed_detail["preview"])

    def test_confirm_atomically_freezes_full_requests_before_serial_save(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://confirm.example/one",
                "https://confirm.example/two",
                key="confirm-freeze-001",
            ),
            decisions=["save", "skip"],
        )
        save_item = batch["items"][0]
        detail = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{save_item['batch_item_id']}"
        ).json()
        entered = threading.Event()
        release = threading.Event()

        def block_after_claim(stage: str, _context: dict) -> None:
            if stage == "after_save_claim":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("save claim gate timed out")

        coordinator = self.client.app.state.collection_import_save_coordinator
        coordinator.fault = block_after_claim
        response = self._confirm(batch)
        try:
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["status"], "saving")
            self.assertEqual(
                [item["state"] for item in response.json()["items"]],
                ["save_queued", "skipped"],
            )
            self.assertTrue(entered.wait(5))
            self.assertEqual(self._row_count("library_items"), 0)
            with closing(sqlite3.connect(self.db_path)) as db:
                db.row_factory = sqlite3.Row
                attempt = db.execute(
                    """SELECT * FROM collection_import_save_attempts
                       WHERE batch_item_id=?""",
                    (save_item["batch_item_id"],),
                ).fetchone()
            self.assertIsNotNone(attempt)
            frozen = json.loads(attempt["frozen_request_json"])
            request = CollectionItemCreateRequest.model_validate(frozen)
            prepared = prepare_collection_create(request, detail["preview"])
            self.assertEqual(frozen["preview_id"], detail["preview"]["preview_id"])
            self.assertEqual(frozen["user_title"], "我的批量标题")
            self.assertEqual(
                frozen["organization_confirmation"]["organization_tags"],
                ["AI 编程"],
            )
            self.assertEqual(
                attempt["collection_request_hash"], prepared.request_hash
            )
            self.assertEqual(
                attempt["idempotency_key_hash"],
                collection_import_save_idempotency_key_hash(
                    batch["batch_id"],
                    save_item["batch_item_id"],
                    attempt["save_attempt_id"],
                ),
            )
            self.assertNotIn(attempt["idempotency_key_hash"], frozen.values())
            with closing(sqlite3.connect(self.db_path)) as db:
                revision_before_stale = db.execute(
                    """SELECT revision FROM collection_import_batches
                       WHERE batch_id=?""",
                    (batch["batch_id"],),
                ).fetchone()[0]
            self.assertFalse(
                CollectionImportRepository(self.repository).mark_save_call_started(
                    batch_id=batch["batch_id"],
                    batch_item_id=save_item["batch_item_id"],
                    save_attempt_id=attempt["save_attempt_id"],
                    claim_token="stale-claim-token",
                    now=datetime.now(UTC),
                )
            )
            with closing(sqlite3.connect(self.db_path)) as db:
                phase_after_stale = db.execute(
                    """SELECT attempt_phase FROM collection_import_save_attempts
                       WHERE save_attempt_id=?""",
                    (attempt["save_attempt_id"],),
                ).fetchone()[0]
                revision_after_stale = db.execute(
                    """SELECT revision FROM collection_import_batches
                       WHERE batch_id=?""",
                    (batch["batch_id"],),
                ).fetchone()[0]
            self.assertEqual(phase_after_stale, "claimed")
            self.assertEqual(revision_after_stale, revision_before_stale)
        finally:
            release.set()
            coordinator.fault = None
        self.assertTrue(coordinator.wait_idle(10))
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(
            [item["state"] for item in final["items"]], ["saved", "skipped"]
        )
        self.assertEqual(self._row_count("library_items"), 1)

    def test_confirm_request_contract_rejects_unsafe_shapes_without_writes(self):
        batch = self._create_ready_batch(
            "https://confirm-contract.example/one",
            "https://confirm-contract.example/two",
            key="confirm-contract-001",
        )
        path = (
            f"/api/v1/collection-import-batches/{batch['batch_id']}/confirm"
        )
        cases = [
            (
                {"content": b"{", "headers": {"Content-Type": "application/json"}},
                422,
                "BATCH_REQUEST_INVALID",
            ),
            ({"json": []}, 422, "BATCH_REQUEST_INVALID"),
            (
                {
                    "json": {
                        "expected_batch_revision": batch["revision"],
                        "extra": True,
                    }
                },
                422,
                "BATCH_REQUEST_INVALID",
            ),
            (
                {"json": {"expected_batch_revision": str(batch["revision"])}},
                422,
                "BATCH_REQUEST_INVALID",
            ),
            (
                {"json": {"expected_batch_revision": 0}},
                422,
                "BATCH_REQUEST_INVALID",
            ),
            (
                {
                    "content": json.dumps(
                        {"expected_batch_revision": batch["revision"]}
                    ),
                    "headers": {"Content-Type": "text/plain"},
                },
                415,
                "BATCH_CONTENT_TYPE_UNSUPPORTED",
            ),
        ]
        for kwargs, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code, kwargs=kwargs):
                response = self.client.post(path, **kwargs)
                self.assertEqual(response.status_code, expected_status, response.text)
                self.assertEqual(response.json()["error"]["code"], expected_code)
                self.assertEqual(
                    self._row_count("collection_import_save_attempts"), 0
                )
                self.assertEqual(self._row_count("library_items"), 0)

    def test_confirm_duplicate_attempt_ids_roll_back_with_safe_conflict(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://attempt-id.example/one",
                "https://attempt-id.example/two",
                key="attempt-id-conflict-001",
            )
        )
        service = self.client.app.state.collection_import_service
        original_factory = service.id_factory
        service.id_factory = lambda: "duplicate-save-attempt-id"
        try:
            response = self._confirm(batch)
        finally:
            service.id_factory = original_factory
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "BATCH_STATE_CONFLICT"
        )
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)
        self.assertEqual(self._row_count("library_items"), 0)
        unchanged = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(unchanged["revision"], batch["revision"])
        self.assertEqual(
            [item["state"] for item in unchanged["items"]],
            ["ready", "ready"],
        )
        self.assertEqual(
            [item["decision"] for item in unchanged["items"]],
            ["save", "save"],
        )

    def test_confirm_cas_review_ttl_and_attempt_limit_fail_without_partial_freeze(self):
        batch = self._create_ready_batch(
            "https://confirm-errors.example/one",
            "https://confirm-errors.example/two",
            key="confirm-errors-001",
        )
        incomplete = self._confirm(batch)
        self.assertEqual(incomplete.status_code, 409, incomplete.text)
        self.assertEqual(
            incomplete.json()["error"]["code"], "BATCH_REVIEW_INCOMPLETE"
        )
        stale = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}/confirm",
            json={"expected_batch_revision": batch["revision"] - 1},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "BATCH_REVISION_CONFLICT")
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)

        batch = self._review_all(batch, decisions=["save", "skip"])
        save_item = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id, generation, review_revision = db.execute(
                """SELECT preview_id, preview_generation, review_revision
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (save_item["batch_item_id"],),
            ).fetchone()
            db.execute(
                "UPDATE collection_previews SET expires_at=? WHERE preview_id=?",
                (
                    (datetime.now(UTC) + timedelta(seconds=240)).isoformat(),
                    preview_id,
                ),
            )
            db.commit()
        stale_before_ttl = self.client.post(
            f"/api/v1/collection-import-batches/{batch['batch_id']}/confirm",
            json={"expected_batch_revision": batch["revision"] - 1},
        )
        self.assertEqual(stale_before_ttl.status_code, 409, stale_before_ttl.text)
        self.assertEqual(
            stale_before_ttl.json()["error"]["code"],
            "BATCH_REVISION_CONFLICT",
        )
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)
        expiring = self._confirm(batch)
        self.assertEqual(expiring.status_code, 409, expiring.text)
        self.assertEqual(
            expiring.json()["error"]["code"], "BATCH_PREVIEW_EXPIRING"
        )
        self.assertEqual(self._row_count("collection_import_save_attempts"), 0)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE collection_previews SET expires_at=? WHERE preview_id=?",
                (
                    (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    preview_id,
                ),
            )
            now = datetime.now(UTC).isoformat()
            for index in range(5):
                db.execute(
                    """INSERT INTO collection_import_save_attempts(
                           save_attempt_id, batch_item_id, preview_id,
                           preview_generation, review_revision,
                           frozen_request_json, collection_request_hash,
                           idempotency_key_hash, attempt_phase, result,
                           created_at, updated_at, settled_at
                       ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'settled',
                                 'known_not_written', ?, ?, ?)""",
                    (
                        f"limit-attempt-{index}",
                        save_item["batch_item_id"],
                        preview_id,
                        generation,
                        max(1, review_revision),
                        f"hash-{index}",
                        f"key-{index}",
                        now,
                        now,
                        now,
                    ),
                )
            db.commit()
        limited = self._confirm(batch)
        self.assertEqual(limited.status_code, 409, limited.text)
        self.assertEqual(limited.json()["error"]["code"], "BATCH_ATTEMPT_LIMIT")
        self.assertEqual(self._row_count("collection_import_save_attempts"), 5)
        unchanged = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(
            [item["state"] for item in unchanged["items"]], ["ready", "ready"]
        )
        self.assertEqual(
            [item["decision"] for item in unchanged["items"]], ["save", "skip"]
        )
        self.assertEqual(self._row_count("library_items"), 0)

    def test_confirm_preview_guard_allows_exactly_three_hundred_seconds(self):
        class AcceptedWithoutDispatch:
            @staticmethod
            def accept_batch(_batch_id: str) -> bool:
                return True

        for remaining_seconds, expected_status in ((299, 409), (300, 202), (301, 202)):
            with self.subTest(remaining_seconds=remaining_seconds):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    path = root / "guard.sqlite3"
                    repository = SQLiteRepository(path)
                    pipeline = LocalFullPipeline(
                        repository,
                        UnconfiguredAsrProvider(),
                        DeterministicFullExtractor(),
                    )
                    fixed = datetime.now(UTC)
                    with TestClient(
                        create_app(
                            pipeline,
                            capture_fetcher=InstrumentedFetcher(),
                            upload_root=root / "uploads",
                            collection_import_clock=lambda fixed=fixed: fixed,
                        )
                    ) as client:
                        created = client.post(
                            "/api/v1/collection-import-batches",
                            json=_payload(
                                "https://guard.example/one",
                                "https://guard.example/two",
                            ),
                            headers={
                                "Idempotency-Key": f"guard-{remaining_seconds}"
                            },
                        )
                        self.assertEqual(created.status_code, 202, created.text)
                        self.assertTrue(
                            client.app.state.collection_import_coordinator.wait_idle(
                                10
                            )
                        )
                        current = client.get(
                            "/api/v1/collection-import-batches/"
                            f"{created.json()['batch_id']}"
                        ).json()
                        for index, decision in enumerate(["save", "skip"]):
                            item = current["items"][index]
                            reviewed = client.patch(
                                "/api/v1/collection-import-batches/"
                                f"{current['batch_id']}/items/"
                                f"{item['batch_item_id']}",
                                json=self._review_payload(
                                    current, item, decision=decision
                                ),
                            )
                            self.assertEqual(
                                reviewed.status_code, 200, reviewed.text
                            )
                            current = reviewed.json()
                        selected_id = current["items"][0]["batch_item_id"]
                        with closing(sqlite3.connect(path)) as db:
                            preview_id = db.execute(
                                """SELECT preview_id
                                   FROM collection_import_batch_items
                                   WHERE batch_item_id=?""",
                                (selected_id,),
                            ).fetchone()[0]
                            db.execute(
                                """UPDATE collection_previews SET expires_at=?
                                   WHERE preview_id=?""",
                                (
                                    (
                                        fixed
                                        + timedelta(seconds=remaining_seconds)
                                    ).isoformat(),
                                    preview_id,
                                ),
                            )
                            db.commit()
                        client.app.state.collection_import_service.save_coordinator = (
                            AcceptedWithoutDispatch()
                        )
                        confirmed = client.post(
                            "/api/v1/collection-import-batches/"
                            f"{current['batch_id']}/confirm",
                            json={
                                "expected_batch_revision": current["revision"]
                            },
                        )
                        self.assertEqual(
                            confirmed.status_code,
                            expected_status,
                            confirmed.text,
                        )
                        with closing(sqlite3.connect(path)) as db:
                            attempt_count = db.execute(
                                """SELECT COUNT(*)
                                   FROM collection_import_save_attempts"""
                            ).fetchone()[0]
                            library_count = db.execute(
                                "SELECT COUNT(*) FROM library_items"
                            ).fetchone()[0]
                        self.assertEqual(
                            attempt_count, 0 if remaining_seconds == 299 else 1
                        )
                        self.assertEqual(library_count, 0)

    def test_fifth_save_attempt_is_allowed_and_sixth_is_rejected(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://attempt-boundary.example/one",
                "https://attempt-boundary.example/two",
                key="attempt-boundary-001",
            ),
            decisions=["save", "skip"],
        )
        item = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            preview_id, generation, review_revision = db.execute(
                """SELECT preview_id, preview_generation, review_revision
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (item["batch_item_id"],),
            ).fetchone()
            now = datetime.now(UTC).isoformat()
            for index in range(4):
                db.execute(
                    """INSERT INTO collection_import_save_attempts(
                           save_attempt_id, batch_item_id, preview_id,
                           preview_generation, review_revision,
                           frozen_request_json, collection_request_hash,
                           idempotency_key_hash, attempt_phase, result,
                           created_at, updated_at, settled_at
                       ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, 'settled',
                                 'known_not_written', ?, ?, ?)""",
                    (
                        f"boundary-attempt-{index}",
                        item["batch_item_id"],
                        preview_id,
                        generation,
                        review_revision,
                        f"boundary-hash-{index}",
                        f"boundary-key-{index}",
                        now,
                        now,
                        now,
                    ),
                )
            db.commit()

        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create

        def reject_fifth(_request, _key):
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED", "test-only fifth attempt"
            )

        coordinator.collection_service.create = reject_fifth
        try:
            fifth = self._confirm(batch)
            self.assertEqual(fifth.status_code, 202, fifth.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.collection_service.create = original_create
        self.assertEqual(self._row_count("collection_import_save_attempts"), 5)
        after_fifth = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        failed = after_fifth["items"][0]
        self.assertEqual(failed["state"], "failed")
        reviewed = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{failed['batch_item_id']}",
            json=self._review_payload(after_fifth, failed, decision="save"),
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        sixth = self._confirm(reviewed.json())
        self.assertEqual(sixth.status_code, 409, sixth.text)
        self.assertEqual(sixth.json()["error"]["code"], "BATCH_ATTEMPT_LIMIT")
        self.assertEqual(self._row_count("collection_import_save_attempts"), 5)
        self.assertEqual(self._row_count("library_items"), 0)

    def test_two_save_coordinators_share_db_claim_and_drain_in_position_order(self):
        class AcceptedWithoutDispatch:
            @staticmethod
            def accept_batch(_batch_id: str) -> bool:
                return True

        batch = self._review_all(
            self._create_ready_batch(
                "https://coordinator-race.example/one",
                "https://coordinator-race.example/two",
                "https://coordinator-race.example/three",
                key="coordinator-race-001",
            )
        )
        preview_ids = [
            self.client.get(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
                f"/items/{item['batch_item_id']}"
            ).json()["preview"]["preview_id"]
            for item in batch["items"]
        ]
        service = self.client.app.state.collection_import_service
        first = self.client.app.state.collection_import_save_coordinator
        service.save_coordinator = AcceptedWithoutDispatch()
        confirmed = self._confirm(batch)
        self.assertEqual(confirmed.status_code, 202, confirmed.text)
        service.save_coordinator = first

        second_repository = CollectionImportRepository(
            SQLiteRepository(self.db_path)
        )
        second = CollectionImportSaveCoordinator(
            second_repository, first.collection_service
        )
        first_claim = first.repository.claim_save
        second_claim = second.repository.claim_save
        barrier = threading.Barrier(2)
        first_gate = True
        second_gate = True

        def gated_first_claim(**kwargs):
            nonlocal first_gate
            if first_gate:
                first_gate = False
                barrier.wait(5)
            return first_claim(**kwargs)

        def gated_second_claim(**kwargs):
            nonlocal second_gate
            if second_gate:
                second_gate = False
                barrier.wait(5)
            return second_claim(**kwargs)

        first.repository.claim_save = gated_first_claim
        second.repository.claim_save = gated_second_claim
        original_create = first.collection_service.create
        calls: list[str] = []
        active = 0
        max_active = 0
        call_lock = threading.Lock()

        def slow_create(request, key):
            nonlocal active, max_active
            with call_lock:
                calls.append(request.preview_id)
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.04)
                return original_create(request, key)
            finally:
                with call_lock:
                    active -= 1

        first.collection_service.create = slow_create
        try:
            self.assertTrue(first.accept_batch(batch["batch_id"]))
            self.assertTrue(second.accept_batch(batch["batch_id"]))
            self.assertTrue(first.wait_idle(15))
            self.assertTrue(second.wait_idle(15))
        finally:
            first.collection_service.create = original_create
            first.repository.claim_save = first_claim
            second.repository.claim_save = second_claim
            second.close()
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual([item["state"] for item in final["items"]], [
            "saved", "saved", "saved"
        ])
        self.assertEqual(calls, preview_ids)
        self.assertEqual(max_active, 1)
        self.assertEqual(self._row_count("library_items"), 3)

    def test_serial_partial_failure_retries_only_failed_item_with_new_attempt(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://serial.example/one",
                "https://serial.example/two",
                "https://serial.example/three",
                key="serial-partial-001",
            )
        )
        preview_ids = [
            self.client.get(
                f"/api/v1/collection-import-batches/{batch['batch_id']}"
                f"/items/{item['batch_item_id']}"
            ).json()["preview"]["preview_id"]
            for item in batch["items"]
        ]
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create
        calls: list[str] = []
        active = 0
        max_active = 0
        failed_once = False

        def fail_middle(request, key):
            nonlocal active, max_active, failed_once
            calls.append(request.preview_id)
            active += 1
            max_active = max(max_active, active)
            try:
                if request.preview_id == preview_ids[1] and not failed_once:
                    failed_once = True
                    raise PipelineError(
                        "COLLECTION_VALIDATION_FAILED", "known failure"
                    )
                return original_create(request, key)
            finally:
                active -= 1

        coordinator.collection_service.create = fail_middle
        first = self._confirm(batch)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertTrue(coordinator.wait_idle(10))
        first_final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(calls, preview_ids)
        self.assertEqual(max_active, 1)
        self.assertEqual(
            [item["state"] for item in first_final["items"]],
            ["saved", "failed", "saved"],
        )
        self.assertEqual(first_final["status"], "awaiting_review")
        self.assertEqual(first_final["items"][1]["decision"], "pending")
        self.assertEqual(
            first_final["items"][1]["terminal_reason"], "validation_failed"
        )
        self.assertEqual(self._row_count("library_items"), 2)

        failed = first_final["items"][1]
        reviewed = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{failed['batch_item_id']}",
            json=self._review_payload(first_final, failed, decision="save"),
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        second = self._confirm(reviewed.json())
        self.assertEqual(second.status_code, 202, second.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.collection_service.create = original_create
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual([item["state"] for item in final["items"]], [
            "saved", "saved", "saved"
        ])
        self.assertEqual(calls, [*preview_ids, preview_ids[1]])
        self.assertEqual(self._row_count("library_items"), 3)
        with closing(sqlite3.connect(self.db_path)) as db:
            attempts = db.execute(
                """SELECT batch_item_id, result, frozen_request_json
                   FROM collection_import_save_attempts
                   ORDER BY created_at, save_attempt_id"""
            ).fetchall()
        self.assertEqual(len(attempts), 4)
        failed_attempts = [
            row for row in attempts if row[0] == failed["batch_item_id"]
        ]
        self.assertEqual([row[1] for row in failed_attempts], [
            "known_not_written", "success"
        ])
        self.assertIsNone(failed_attempts[0][2])
        self.assertIsNotNone(failed_attempts[1][2])

    def test_unknown_save_outcome_stops_later_positions(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://unknown-save.example/one",
                "https://unknown-save.example/two",
                key="unknown-save-001",
            )
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        triggered = False

        def fail_after_call_started(stage: str, _context: dict) -> None:
            nonlocal triggered
            if stage == "after_save_call_started" and not triggered:
                triggered = True
                raise RuntimeError("uncertain local call window")

        coordinator.fault = fail_after_call_started
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.fault = None
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["outcome_unknown", "save_queued"],
        )
        self.assertEqual(self._row_count("library_items"), 0)
        with closing(sqlite3.connect(self.db_path)) as db:
            attempts = db.execute(
                """SELECT a.attempt_phase, a.result, a.claim_token
                   FROM collection_import_save_attempts a
                   JOIN collection_import_batch_items i
                     ON i.batch_item_id=a.batch_item_id
                   ORDER BY i.position"""
            ).fetchall()
        self.assertEqual(attempts[0], ("result_observed", "unknown", None))
        self.assertEqual(attempts[1][0:2], ("frozen", "none"))

    def test_explicit_skip_after_known_failure_retains_failure_provenance(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://skip-failure.example/one",
                "https://skip-failure.example/two",
                key="skip-failure-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create

        def reject_save(_request, _key):
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED", "known failure"
            )

        coordinator.collection_service.create = reject_save
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.collection_service.create = original_create
        failed_batch = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        failed = failed_batch["items"][0]
        skipped = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{failed['batch_item_id']}",
            json=self._review_payload(failed_batch, failed, decision="skip"),
        )
        self.assertEqual(skipped.status_code, 200, skipped.text)
        finished = self._confirm(skipped.json())
        self.assertEqual(finished.status_code, 202, finished.text)
        self.assertTrue(coordinator.wait_idle(10))
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed_with_issues")
        self.assertEqual([item["state"] for item in final["items"]], [
            "skipped", "skipped"
        ])
        self.assertEqual(
            final["items"][0]["terminal_reason"], "validation_failed"
        )
        self.assertEqual(self._row_count("collection_import_save_attempts"), 1)
        self.assertEqual(self._row_count("library_items"), 0)

    def test_durable_observed_result_settles_locally_after_crash(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://observed.example/one",
                "https://observed.example/two",
                key="observed-crash-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator

        def crash_after_observed(stage: str, _context: dict) -> None:
            if stage == "after_save_result_observed":
                raise SimulatedCollectionImportCrash()

        coordinator.fault = crash_after_observed
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        with closing(sqlite3.connect(self.db_path)) as db:
            before = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(before, ("result_observed", "success"))
        self.assertEqual(self._row_count("library_items"), 1)

        coordinator.fault = None
        CollectionImportRepository(self.repository).reconcile_startup(
            datetime.now(UTC)
        )
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(
            [item["state"] for item in final["items"]], ["saved", "skipped"]
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            after = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(after, ("settled", "success", None))
        self.assertEqual(self._row_count("library_items"), 1)

    def test_call_return_crash_reconciles_exact_idempotency_before_identity(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://call-return.example/one",
                "https://call-return.example/two",
                key="call-return-crash-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create
        create_calls = 0

        def counted_create(request, key):
            nonlocal create_calls
            create_calls += 1
            return original_create(request, key)

        def crash_after_return(stage: str, _context: dict) -> None:
            if stage == "after_collection_save_returned":
                raise SimulatedCollectionImportCrash()

        coordinator.collection_service.create = counted_create
        coordinator.fault = crash_after_return
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        with closing(sqlite3.connect(self.db_path)) as db:
            before = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(before, ("call_started", "none"))
        self.assertEqual(self._row_count("library_items"), 1)

        coordinator.fault = None
        coordinator.collection_service.create = original_create
        CollectionImportRepository(self.repository).reconcile_startup(
            datetime.now(UTC)
        )
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(
            [item["state"] for item in final["items"]], ["saved", "skipped"]
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("settled", "success"))
        self.assertEqual(self._row_count("library_items"), 1)
        self.assertEqual(create_calls, 1)

    def test_idempotency_key_reused_fails_closed_as_outcome_unknown(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://idempotency-mismatch.example/one",
                "https://idempotency-mismatch.example/two",
                key="idempotency-mismatch-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create

        def reject_reused_key(_request, _key):
            raise PipelineError(
                "IDEMPOTENCY_KEY_REUSED",
                "test-only mismatched server idempotency hash",
            )

        coordinator.collection_service.create = reject_reused_key
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.collection_service.create = original_create
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["outcome_unknown", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("result_observed", "unknown", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_worker_repository_fault_before_call_cas_settles_interrupted(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://worker-interrupted.example/one",
                "https://worker-interrupted.example/two",
                key="worker-interrupted-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_mark = coordinator.repository.mark_save_call_started

        def fail_mark_save_call_started(**_kwargs):
            raise sqlite3.OperationalError("test-only call-start persistence fault")

        coordinator.repository.mark_save_call_started = fail_mark_save_call_started
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.repository.mark_save_call_started = original_mark
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["interrupted", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("frozen", "none", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_worker_runtime_fault_after_claim_cas_settles_interrupted(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://worker-claim-fault.example/one",
                "https://worker-claim-fault.example/two",
                key="worker-claim-fault-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator

        def fail_after_claim(stage: str, _context: dict) -> None:
            if stage == "after_save_claim":
                raise RuntimeError("test-only worker fault after claim")

        coordinator.fault = fail_after_claim
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.fault = None
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["interrupted", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("frozen", "none", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_worker_claim_query_fault_interrupts_unclaimed_queue(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://claim-query-fault.example/one",
                "https://claim-query-fault.example/two",
                key="claim-query-fault-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_claim = coordinator.repository.claim_save

        def fail_claim(**_kwargs):
            raise sqlite3.OperationalError("test-only claim query fault")

        coordinator.repository.claim_save = fail_claim
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.repository.claim_save = original_claim
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["interrupted", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("frozen", "none", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_worker_pending_query_fault_interrupts_unclaimed_queue(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://pending-query-fault.example/one",
                "https://pending-query-fault.example/two",
                key="pending-query-fault-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_claim = coordinator.repository.claim_save
        original_pending = coordinator.repository.has_pending_save_work

        def lose_claim_race(**_kwargs):
            return None

        def fail_pending(_batch_id):
            raise sqlite3.OperationalError("test-only pending query fault")

        coordinator.repository.claim_save = lose_claim_race
        coordinator.repository.has_pending_save_work = fail_pending
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.repository.claim_save = original_claim
            coordinator.repository.has_pending_save_work = original_pending
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["interrupted", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("frozen", "none", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_worker_repository_fault_after_call_start_cas_settles_unknown(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://worker-unknown.example/one",
                "https://worker-unknown.example/two",
                key="worker-unknown-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create
        original_reconcile = coordinator.repository.reconcile_save_after_exception
        original_unknown = coordinator.repository.mark_save_outcome_unknown

        def uncertain_create(_request, _key):
            raise RuntimeError("test-only uncertain call")

        def fail_reconcile(**_kwargs):
            raise sqlite3.OperationalError("test-only reconcile fault")

        def fail_unknown(**_kwargs):
            raise sqlite3.OperationalError("test-only result persistence fault")

        coordinator.collection_service.create = uncertain_create
        coordinator.repository.reconcile_save_after_exception = fail_reconcile
        coordinator.repository.mark_save_outcome_unknown = fail_unknown
        try:
            confirmed = self._confirm(batch)
            self.assertEqual(confirmed.status_code, 202, confirmed.text)
            self.assertTrue(coordinator.wait_idle(10))
        finally:
            coordinator.collection_service.create = original_create
            coordinator.repository.reconcile_save_after_exception = (
                original_reconcile
            )
            coordinator.repository.mark_save_outcome_unknown = original_unknown
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["outcome_unknown", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("result_observed", "unknown", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_startup_identity_query_fault_is_best_effort_and_fail_closed(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://startup-query-fault.example/one",
                "https://startup-query-fault.example/two",
                key="startup-query-fault-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator

        def crash_after_call_started(stage: str, _context: dict) -> None:
            if stage == "after_save_call_started":
                raise SimulatedCollectionImportCrash()

        coordinator.fault = crash_after_call_started
        confirmed = self._confirm(batch)
        self.assertEqual(confirmed.status_code, 202, confirmed.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.fault = None

        original_identity_lookup = CollectionImportRepository._existing_collection_id

        def fail_identity_lookup(_db, _identity_url):
            raise sqlite3.OperationalError("test-only local identity query fault")

        CollectionImportRepository._existing_collection_id = staticmethod(
            fail_identity_lookup
        )
        try:
            CollectionImportRepository(self.repository).reconcile_startup(
                datetime.now(UTC)
            )
        finally:
            CollectionImportRepository._existing_collection_id = staticmethod(
                original_identity_lookup
            )
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "interrupted")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["outcome_unknown", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("result_observed", "unknown", None))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_startup_settle_fault_keeps_known_result_active_not_reviewable(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://startup-settle-fault.example/one",
                "https://startup-settle-fault.example/two",
                key="startup-settle-fault-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator

        def crash_after_observed(stage: str, _context: dict) -> None:
            if stage == "after_save_result_observed":
                raise SimulatedCollectionImportCrash()

        coordinator.fault = crash_after_observed
        confirmed = self._confirm(batch)
        self.assertEqual(confirmed.status_code, 202, confirmed.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.fault = None

        recovering = CollectionImportRepository(self.repository)

        def fail_settle(*_args, **_kwargs):
            raise sqlite3.OperationalError("test-only startup settle fault")

        recovering._settle_observed_save_on_connection = fail_settle
        recovering.reconcile_startup(datetime.now(UTC))
        unresolved = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(unresolved["status"], "saving")
        self.assertNotEqual(unresolved["status"], "awaiting_review")
        self.assertEqual(
            [item["state"] for item in unresolved["items"]],
            ["saving", "skipped"],
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result, claim_token
                   FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt[0:2], ("result_observed", "success"))
        self.assertIsNotNone(attempt[2])
        self.assertEqual(self._row_count("library_items"), 1)

    def test_known_failure_observed_before_crash_remains_known_not_written(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://known-observed.example/one",
                "https://known-observed.example/two",
                key="known-observed-crash-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create

        def reject_save(_request, _key):
            raise PipelineError(
                "COLLECTION_VALIDATION_FAILED", "known failure"
            )

        def crash_after_observed(stage: str, _context: dict) -> None:
            if stage == "after_save_result_observed":
                raise SimulatedCollectionImportCrash()

        coordinator.collection_service.create = reject_save
        coordinator.fault = crash_after_observed
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        with closing(sqlite3.connect(self.db_path)) as db:
            before = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(before, ("result_observed", "known_not_written"))

        coordinator.collection_service.create = original_create
        coordinator.fault = None
        CollectionImportRepository(self.repository).reconcile_startup(
            datetime.now(UTC)
        )
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "awaiting_review")
        self.assertEqual(final["items"][0]["state"], "failed")
        self.assertEqual(final["items"][0]["decision"], "pending")
        self.assertEqual(
            final["items"][0]["error_code"], "COLLECTION_VALIDATION_FAILED"
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            after = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(after, ("settled", "known_not_written"))
        self.assertEqual(self._row_count("library_items"), 0)

    def test_exception_reconciliation_uses_identity_after_idempotency_miss(self):
        batch = self._review_all(
            self._create_ready_batch(
                "https://identity-fallback.example/one",
                "https://identity-fallback.example/two",
                key="identity-fallback-001",
            ),
            decisions=["save", "skip"],
        )
        coordinator = self.client.app.state.collection_import_save_coordinator
        original_create = coordinator.collection_service.create

        def external_write_then_uncertain(request, _key):
            original_create(request, "independent-external-key")
            raise RuntimeError("lost local return")

        coordinator.collection_service.create = external_write_then_uncertain
        response = self._confirm(batch)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(coordinator.wait_idle(10))
        coordinator.collection_service.create = original_create
        final = self.client.get(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
        ).json()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(
            [item["state"] for item in final["items"]],
            ["already_exists", "skipped"],
        )
        self.assertEqual(self._row_count("library_items"), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            attempt = db.execute(
                """SELECT attempt_phase, result FROM collection_import_save_attempts"""
            ).fetchone()
        self.assertEqual(attempt, ("settled", "exists"))

    def test_failed_save_shell_requires_latest_settled_known_not_written(self):
        batch = self._create_ready_batch(
            "https://retry-proof.example/one",
            "https://retry-proof.example/two",
            key="retry-proof-001",
        )
        item = batch["items"][0]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE collection_import_batch_items
                   SET state='failed', error_stage='save', error_code='SAVE_FAILED',
                       terminal_reason='save_failed'
                   WHERE batch_item_id=?""",
                (item["batch_item_id"],),
            )
            preview_id, generation = db.execute(
                """SELECT preview_id, preview_generation
                   FROM collection_import_batch_items WHERE batch_item_id=?""",
                (item["batch_item_id"],),
            ).fetchone()
            db.commit()
        save_without_proof = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{item['batch_item_id']}",
            json=self._review_payload(batch, item, decision="save"),
        )
        self.assertEqual(save_without_proof.status_code, 409, save_without_proof.text)

        now = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """INSERT INTO collection_import_save_attempts(
                       save_attempt_id, batch_item_id, preview_id,
                       preview_generation, review_revision,
                       frozen_request_json, collection_request_hash,
                       idempotency_key_hash, attempt_phase, result,
                       created_at, updated_at
                   ) VALUES ('unknown-proof', ?, ?, ?, 1, NULL, 'hash', 'key',
                             'result_observed', 'unknown', ?, ?)""",
                (item["batch_item_id"], preview_id, generation, now, now),
            )
            db.commit()
        skip_unknown = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{item['batch_item_id']}",
            json=self._review_payload(batch, item, decision="skip"),
        )
        self.assertEqual(skip_unknown.status_code, 409, skip_unknown.text)

        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                """UPDATE collection_import_save_attempts
                   SET attempt_phase='settled', result='success', settled_at=?
                   WHERE save_attempt_id='unknown-proof'""",
                (now,),
            )
            db.commit()
        save_wrong_result = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{item['batch_item_id']}",
            json=self._review_payload(batch, item, decision="save"),
        )
        self.assertEqual(save_wrong_result.status_code, 409, save_wrong_result.text)
        skip_resolved = self.client.patch(
            f"/api/v1/collection-import-batches/{batch['batch_id']}"
            f"/items/{item['batch_item_id']}",
            json=self._review_payload(batch, item, decision="skip"),
        )
        self.assertEqual(skip_resolved.status_code, 200, skip_resolved.text)


class CollectionImportPreviewTests(unittest.TestCase):
    def _make_client(
        self,
        root: Path,
        repository: SQLiteRepository,
        fetcher: InstrumentedFetcher,
        *,
        fault=None,
    ) -> TestClient:
        pipeline = LocalFullPipeline(
            repository,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        return TestClient(
            create_app(
                pipeline,
                capture_fetcher=fetcher,
                upload_root=root / "uploads",
                collection_import_fault=fault,
            )
        )

    def test_preview_concurrency_dedupe_and_existing_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SQLiteRepository(root / "imports.sqlite3")
            fetcher = InstrumentedFetcher(delay=0.08)
            client_context = self._make_client(root, repository, fetcher)
            with client_context as client:
                single = client.post(
                    "/api/v1/collection-previews",
                    json={"input_text": "https://existing.example/item"},
                )
                self.assertEqual(single.status_code, 201, single.text)
                saved = client.post(
                    "/api/v1/collection-items",
                    headers={"Idempotency-Key": "existing-save"},
                    json={
                        "preview_id": single.json()["preview_id"],
                        "user_title": None,
                        "untitled_confirmed": False,
                        "organization_confirmation": {
                            "primary_category": "",
                            "secondary_category": "",
                            "organization_tags": [],
                        },
                        "personal_tags": [],
                        "inspiration": None,
                    },
                )
                self.assertEqual(saved.status_code, 201, saved.text)

                response = client.post(
                    "/api/v1/collection-import-batches",
                    json=_payload(
                        "https://same.example/path#first",
                        "https://same.example/path#second",
                        "https://existing.example/item",
                        "https://other.example/item",
                    ),
                    headers={"Idempotency-Key": "preview-groups"},
                )
                self.assertEqual(response.status_code, 202, response.text)
                batch_id = response.json()["batch_id"]
                self.assertTrue(
                    client.app.state.collection_import_coordinator.wait_idle(10)
                )
                batch = client.get(
                    f"/api/v1/collection-import-batches/{batch_id}"
                ).json()
                self.assertLessEqual(fetcher.max_active, 2)
                self.assertGreaterEqual(fetcher.max_active, 2)
                self.assertEqual(batch["items"][0]["state"], "ready")
                self.assertEqual(batch["items"][1]["state"], "duplicate_in_batch")
                self.assertEqual(
                    batch["items"][1]["duplicate_of_batch_item_id"],
                    batch["items"][0]["batch_item_id"],
                )
                self.assertEqual(batch["items"][2]["state"], "already_exists")
                self.assertEqual(
                    batch["items"][2]["collection_item_id"], saved.json()["id"]
                )

    def test_base_exception_preserves_claim_instead_of_faking_recoverable_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SQLiteRepository(root / "imports.sqlite3")
            fetcher = InstrumentedFetcher()

            def interrupt_after_claim(stage: str, _context: dict) -> None:
                if stage == "after_preview_claim":
                    raise KeyboardInterrupt("test-only process interruption")

            with self._make_client(
                root, repository, fetcher, fault=interrupt_after_claim
            ) as client:
                response = client.post(
                    "/api/v1/collection-import-batches",
                    json=_payload(
                        "https://interrupt.example/one",
                        "https://interrupt.example/two",
                    ),
                    headers={"Idempotency-Key": "interrupt-create"},
                )
                self.assertEqual(response.status_code, 202, response.text)
                batch_id = response.json()["batch_id"]
                self.assertTrue(
                    client.app.state.collection_import_coordinator.wait_idle(5)
                )
                batch = client.get(
                    f"/api/v1/collection-import-batches/{batch_id}"
                ).json()
                self.assertEqual([item["state"] for item in batch["items"]], [
                    "previewing",
                    "previewing",
                ])
                self.assertEqual(fetcher.calls, [])

    def test_startup_reconciles_reserved_preview_without_network_or_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "imports.sqlite3"
            repository = SQLiteRepository(path)
            fetcher = InstrumentedFetcher()

            def crash_after_preview(stage: str, _context: dict) -> None:
                if stage == "after_preview_persisted":
                    raise SimulatedCollectionImportCrash()

            with self._make_client(
                root, repository, fetcher, fault=crash_after_preview
            ) as client:
                response = client.post(
                    "/api/v1/collection-import-batches",
                    json=_payload(
                        "https://recover.example/one",
                        "https://recover.example/two",
                    ),
                    headers={"Idempotency-Key": "recover-create"},
                )
                self.assertEqual(response.status_code, 202)
                batch_id = response.json()["batch_id"]
                self.assertTrue(
                    client.app.state.collection_import_coordinator.wait_idle(5)
                )
                before = client.get(
                    f"/api/v1/collection-import-batches/{batch_id}"
                ).json()
                self.assertEqual(before["previewing"], 2)

            no_network = InstrumentedFetcher()
            reopened = SQLiteRepository(path)
            with self._make_client(root, reopened, no_network) as restored_client:
                restored = restored_client.get(
                    f"/api/v1/collection-import-batches/{batch_id}"
                )
                self.assertEqual(restored.status_code, 200, restored.text)
                self.assertEqual(restored.json()["status"], "awaiting_review")
                self.assertEqual(no_network.calls, [])
                self.assertEqual(
                    [item["state"] for item in restored.json()["items"]],
                    ["ready", "ready"],
                )


if __name__ == "__main__":
    unittest.main()
