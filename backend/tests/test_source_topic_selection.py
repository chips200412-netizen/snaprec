"""CQ2 source-topic contract, using isolated synthetic SQLite databases only."""
from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from backend.app.domain.models import CollectionItemCreateRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.tests import test_collections as fixtures

collection_payload = fixtures.collection_payload
trusted_collection_payload = fixtures.trusted_collection_payload


class SourceTopicSelectionTests(unittest.TestCase):
    setUp = fixtures.CollectionApiTests.setUp
    tearDown = fixtures.CollectionApiTests.tearDown
    _seed_preview = fixtures.CollectionApiTests._seed_preview

    def seed_topics(self):
        trusted = trusted_collection_payload()
        trusted["metadata"]["platform_tags"] = [
            {"value": value, "source": "platform_public"}
            for value in [" ＡＩ  Tools ", "ai\ttools", "Straße", "STRASSE", "长" * 100]
        ]
        self._seed_preview("topics", trusted)
        return trusted["metadata"]["platform_tags"]

    def save(self, indices, *, key="cq2", preview="topics"):
        return self.client.post("/api/v1/collection-items",
            headers={"Idempotency-Key": key},
            json={**collection_payload(), "preview_id": preview,
                  "selected_source_topic_indices": indices})

    def test_subset_roundtrip_canonical_hash_and_expired_success_replay(self):
        topics = self.seed_topics()
        first = self.save([4, 0])
        self.assertEqual(first.status_code, 201, first.text)
        saved = first.json()
        self.assertEqual(saved["selected_source_topic_indices"], [0, 4])
        self.assertEqual(saved["metadata"]["platform_tags"], topics)
        self.assertEqual(self.client.get(f"/api/v1/collection-items/{saved['id']}").json(), saved)
        with closing(sqlite3.connect(self.db_path)) as db:
            raw = db.execute("SELECT payload_json FROM collection_previews WHERE preview_id='topics'").fetchone()[0]
            preview = json.loads(raw)
            preview["expires_at"] = "2000-01-01T00:00:00+00:00"
            db.execute("UPDATE collection_previews SET payload_json=? WHERE preview_id='topics'", (json.dumps(preview),))
            db.commit()
        self.assertEqual(self.save([0, 4]).json(), saved)
        changed = self.save([2])
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_invalid_indices_are_rejected_without_consuming_key(self):
        self.seed_topics()
        for values, status in [(None, 422), ([True], 422), ([1.0], 422),
                               ([-1], 422), (["0"], 422), ("0", 422),
                               ([1], 400), ([3], 400), ([5], 400),
                               ([0, 0], 400), ([0] * 1000, 400)]:
            with self.subTest(values=str(values)[:40]):
                response = self.save(values)
                self.assertEqual(response.status_code, status, response.text)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM library_items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM collection_idempotency_keys").fetchone()[0], 0)
        self.assertEqual(self.save([]).status_code, 201)

    def test_omission_stays_null_and_explicit_empty_never_matches_old_hash(self):
        self.seed_topics()
        old = self.client.post("/api/v1/collection-items", headers={"Idempotency-Key": "old"},
            json={**collection_payload(), "preview_id": "topics"})
        self.assertEqual(old.status_code, 201, old.text)
        self.assertIsNone(old.json()["selected_source_topic_indices"])
        self.assertEqual(self.save([], key="old").status_code, 409)
        self.assertEqual(self.save([0], key="old").status_code, 409)
        duplicate = self.save([0], key="new")
        self.assertEqual(duplicate.status_code, 409)
        self.assertIsNone(self.client.get(f"/api/v1/collection-items/{old.json()['id']}").json()["selected_source_topic_indices"])

    def test_selection_readonly_through_patch(self):
        self.seed_topics()
        saved = self.save([2]).json()
        payload = {"expected_revision": saved["revision"], "user_title": "编辑标题",
            "organization_confirmation": saved["organization_confirmation"],
            "personal_tags": saved["personal_tags"], "inspiration": None}
        url = f"/api/v1/collection-items/{saved['id']}"
        rejected = self.client.patch(url, json={**payload, "selected_source_topic_indices": []})
        self.assertEqual(rejected.status_code, 422)
        edited = self.client.patch(url, json=payload)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["selected_source_topic_indices"], [2])

    def test_empty_selection_saves_bookmark_without_metadata(self):
        trusted = trusted_collection_payload()
        trusted["metadata_status"] = "metadata_unavailable"
        for name in ["title", "author", "cover_url", "source_copy"]:
            trusted["metadata"][name] = {"value": "", "source": "none", "fetched_at": ""}
        trusted["metadata"]["platform_tags"] = []
        self._seed_preview("empty", trusted)
        invalid = self.save([0], preview="empty")
        self.assertEqual(invalid.status_code, 400)
        saved = self.save([], preview="empty")
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["selected_source_topic_indices"], [])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT selected_source_topic_indices_json FROM library_items").fetchone()[0], "[]")

    def test_creation_fault_rolls_back_selection_and_entire_graph(self):
        self.seed_topics()
        with patch("backend.app.repositories.sqlite.refresh_collection_document", side_effect=RuntimeError("synthetic fault")):
            with self.assertRaises(RuntimeError):
                self.save([0])
        with closing(sqlite3.connect(self.db_path)) as db:
            for table in ["library_items", "source_metadata", "collection_idempotency_keys"]:
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.save([0]).status_code, 201)

    def test_legacy_request_serialization_keeps_omission_for_batch_roundtrips(self):
        request = CollectionItemCreateRequest.model_validate(collection_payload())
        frozen = request.model_dump(mode="json")
        self.assertNotIn("selected_source_topic_indices", frozen)
        self.assertIsNone(CollectionItemCreateRequest.model_validate(frozen).selected_source_topic_indices)
        explicit = CollectionItemCreateRequest.model_validate({**collection_payload(), "selected_source_topic_indices": []})
        self.assertEqual(explicit.model_dump(mode="json")["selected_source_topic_indices"], [])
        schema = CollectionItemCreateRequest.model_json_schema()
        self.assertEqual(schema["properties"]["selected_source_topic_indices"]["type"], "array")
        self.assertNotIn("selected_source_topic_indices", schema["required"])

    def test_v16_expand_and_fault_preserve_old_rows_and_response_snapshots(self):
        old = self.client.post("/api/v1/collection-items", headers={"Idempotency-Key": "old"}, json=collection_payload()).json()
        with closing(sqlite3.connect(self.db_path)) as db:
            response = db.execute("SELECT response_json FROM collection_idempotency_keys").fetchone()[0]
            historic = json.loads(response)
            historic.pop("selected_source_topic_indices", None)
            db.execute("UPDATE collection_idempotency_keys SET response_json=?", (json.dumps(historic),))
            db.execute("ALTER TABLE library_items DROP COLUMN selected_source_topic_indices_json")
            db.execute("PRAGMA user_version=16")
            db.commit()
            before = list(db.iterdump())
            tables = ["library_items", "source_metadata", "organization_suggestions",
                      "organization_confirmations", "collection_personal_tags", "inspirations",
                      "collection_idempotency_keys", "collection_url_aliases",
                      "collection_search_documents", "collection_search_dirty"]
            snapshots = {
                table: ([column[1] for column in db.execute(f"PRAGMA table_info({table})")],
                        db.execute(f"SELECT * FROM {table}").fetchall())
                for table in tables
            }
        def fail(stage, db):
            if stage == "after_v17_ddl":
                raise RuntimeError("synthetic migration fault")
        with self.assertRaisesRegex(RuntimeError, "synthetic migration"):
            SQLiteRepository(self.db_path, migration_fault=fail)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(list(db.iterdump()), before)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 16)
        SQLiteRepository(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertIsNone(db.execute("SELECT selected_source_topic_indices_json FROM library_items").fetchone()[0])
            self.assertEqual(json.loads(db.execute("SELECT response_json FROM collection_idempotency_keys").fetchone()[0]), historic)
            for table, (columns, rows) in snapshots.items():
                self.assertEqual(db.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall(), rows)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        replay = self.client.post("/api/v1/collection-items", headers={"Idempotency-Key": "old"}, json=collection_payload())
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["id"], old["id"])
        self.assertIsNone(replay.json()["selected_source_topic_indices"])
