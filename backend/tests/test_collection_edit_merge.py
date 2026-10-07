from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import (
    CollectionRevisionConflictError,
    SQLiteRepository,
)
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)
from backend.tests.test_collections import trusted_collection_payload


def editable_snapshot(item: dict) -> dict:
    inspiration = item["inspiration"]
    return {
        "user_title": item["user_title"],
        "user_author": item["user_author"],
        "organization_confirmation": deepcopy(item["organization_confirmation"]),
        "personal_tags": list(item["personal_tags"]),
        "inspiration": None
        if inspiration is None
        else {
            "content": inspiration["content"],
            "input_mode": inspiration["input_mode"],
            "transcription_status": inspiration["transcription_status"],
        },
    }


class CollectionEditMergeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "collection-edit.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        self.trusted = trusted_collection_payload()
        self.repo.create_collection_preview(
            {
                "preview_id": "r25-preview",
                "original_input": self.trusted["original_input"],
                "source_url": self.trusted["source_url"],
                "canonical_url": self.trusted["canonical_url"],
                "identity_url": "https://example.com/watch?a=1",
                "source_kind": self.trusted["source_kind"],
                "platform": self.trusted["platform"],
                "metadata_status": self.trusted["metadata_status"],
                "metadata": self.trusted["metadata"],
                "organization_suggestion": self.trusted["organization_suggestion"],
                "created_at": "2026-08-20T00:00:00+00:00",
                "expires_at": "2099-08-20T00:00:00+00:00",
            }
        )
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        self.client = TestClient(
            create_app(pipeline, upload_root=self.root / "uploads")
        )
        created = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "r25-create"},
            json={
                "preview_id": "r25-preview",
                "user_title": self.trusted["user_title"],
                "organization_confirmation": self.trusted[
                    "organization_confirmation"
                ],
                "personal_tags": self.trusted["personal_tags"],
                "inspiration": self.trusted["inspiration"],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.item = created.json()

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def patch_payload(self, item: dict | None = None, **updates) -> dict:
        source = self.item if item is None else item
        payload = {
            "expected_revision": source["revision"],
            **editable_snapshot(source),
        }
        payload.update(updates)
        return payload

    def immutable_source_snapshot(self) -> dict:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            item = dict(
                db.execute(
                    """SELECT source_kind, platform, original_input, source_url,
                              canonical_url, identity_url, metadata_status,
                              deep_analysis_resource_key, created_at
                       FROM library_items WHERE id=?""",
                    (self.item["id"],),
                ).fetchone()
            )
            return {
                "item": item,
                "metadata": dict(
                    db.execute(
                        "SELECT * FROM source_metadata WHERE collection_item_id=?",
                        (self.item["id"],),
                    ).fetchone()
                ),
                "suggestion": dict(
                    db.execute(
                        "SELECT * FROM organization_suggestions WHERE collection_item_id=?",
                        (self.item["id"],),
                    ).fetchone()
                ),
                "aliases": [
                    tuple(row)
                    for row in db.execute(
                        """SELECT alias_url, collection_item_id, created_at
                           FROM collection_url_aliases
                           WHERE collection_item_id=? ORDER BY alias_url""",
                        (self.item["id"],),
                    )
                ],
                "idempotency": dict(
                    db.execute(
                        """SELECT idempotency_key, request_hash, collection_item_id,
                                  response_json, created_at
                           FROM collection_idempotency_keys
                           WHERE collection_item_id=?""",
                        (self.item["id"],),
                    ).fetchone()
                ),
            }

    def transactional_snapshot(self) -> dict:
        tables = (
            "library_items",
            "source_metadata",
            "organization_suggestions",
            "organization_confirmations",
            "collection_personal_tags",
            "inspirations",
            "collection_url_aliases",
            "collection_idempotency_keys",
            "collection_search_documents",
            "collection_search_dirty",
            "user_cover_assets",
            "collection_user_covers",
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            return {
                table: [
                    dict(row)
                    for row in db.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                ]
                for table in tables
            }

    def test_patch_updates_only_user_graph_and_refreshes_search_facets_and_list(self):
        """R2-EDIT-FIELDS-001 / R2-EDIT-SEARCH-001 / R2-EDIT-BOUNDARY-001."""
        immutable_before = self.immutable_source_snapshot()
        old_inspiration = self.item["inspiration"]
        response = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                user_title="  新标题  ",
                organization_confirmation={
                    "primary_category": "　技术　",
                    "secondary_category": "ＡＩ",
                    "organization_tags": ["  Foo   Bar ", "ｆｏｏ bar", "新增"],
                },
                personal_tags=["　个人　", "个人", "Another"],
                inspiration={
                    "content": "新的最终灵感",
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                },
            ),
        )
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["user_title"], "新标题")
        self.assertEqual(updated["display_title"], "新标题")
        self.assertEqual(
            updated["organization_confirmation"],
            {
                "primary_category": "技术",
                "secondary_category": "AI",
                "organization_tags": ["Foo Bar", "新增"],
            },
        )
        self.assertEqual(updated["personal_tags"], ["个人", "Another"])
        self.assertEqual(updated["inspiration"]["id"], old_inspiration["id"])
        self.assertEqual(
            updated["inspiration"]["created_at"], old_inspiration["created_at"]
        )
        self.assertEqual(updated["inspiration"]["content"], "新的最终灵感")
        self.assertNotEqual(updated["updated_at"], self.item["updated_at"])

        restored = self.client.get(
            f"/api/v1/collection-items/{self.item['id']}"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json(), updated)
        listing = self.client.get("/api/v1/collection-items")
        self.assertEqual(listing.status_code, 200, listing.text)
        list_item = listing.json()["items"][0]
        self.assertEqual(
            set(list_item),
            {
                "id",
                "display_title",
                "platform",
                "primary_category",
                "cover_url",
                "source_author",
                "user_author",
                "has_user_cover",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(list_item["display_title"], "新标题")

        for query in ("新标题", "新的最终灵感", "Foo Bar", "Another"):
            result = self.client.get(
                "/api/v1/collection-items", params={"query": query}
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["total"], 1, query)
        self.assertEqual(
            self.client.get(
                "/api/v1/collection-items", params={"query": "用户标题"}
            ).json()["total"],
            0,
        )
        filtered = self.client.get(
            "/api/v1/collection-items",
            params={
                "primary_category": "技术",
                "secondary_category": "AI",
                "tag": "foo bar",
                "tag_source": "organization",
            },
        ).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["facets"]["categories"][0]["primary_category"], "技术")
        self.assertIn(
            ("Foo Bar", "organization", 1),
            {
                (entry["name"], entry["source"], entry["count"])
                for entry in filtered["facets"]["tags"]
            },
        )
        self.assertEqual(self.immutable_source_snapshot(), immutable_before)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM collection_search_dirty"
                ).fetchone()[0],
                0,
            )

    def test_user_author_patch_uses_cas_and_clear_falls_back_without_mutating_source(self):
        """INT-ACC-CQ3-001/004/007; CQ3-CONSISTENCY-001."""
        source_before = self.immutable_source_snapshot()
        supplemented = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(user_author="  我的  作者  "),
        )
        self.assertEqual(supplemented.status_code, 200, supplemented.text)
        body = supplemented.json()
        self.assertEqual(body["user_author"], "我的 作者")
        self.assertEqual(body["revision"], self.item["revision"] + 1)
        self.assertEqual(body["metadata"]["author"], self.item["metadata"]["author"])
        for query in ("我的 作者", "来源作者"):
            result = self.client.get(
                "/api/v1/collection-items", params={"query": query}
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["total"], 1, query)

        omitted = self.patch_payload(item=body, user_title="只改标题")
        omitted.pop("user_author")
        kept = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}", json=omitted
        )
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(kept.json()["user_author"], "我的 作者")

        cleared = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(item=kept.json(), user_author=None),
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["user_author"])
        self.assertEqual(
            self.client.get(
                "/api/v1/collection-items", params={"query": "我的 作者"}
            ).json()["total"],
            0,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/collection-items", params={"query": "来源作者"}
            ).json()["total"],
            1,
        )
        self.assertEqual(self.immutable_source_snapshot(), source_before)

    def test_semantic_noop_does_not_write_revision_timestamps_index_or_facets(self):
        """R2-EDIT-NOOP-001."""
        state_before = self.transactional_snapshot()
        list_before = self.client.get("/api/v1/collection-items").json()
        payload = self.patch_payload(
            user_title="  用户标题  ",
            organization_confirmation={
                "primary_category": "　知识　",
                "secondary_category": " 文章 ",
                "organization_tags": [" 稍后读 ", "稍后读"],
            },
            personal_tags=[" 研究 ", "ＡＩ", "ai"],
        )
        with patch(
            "backend.app.repositories.sqlite.refresh_collection_document"
        ) as refresh:
            response = self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}", json=payload
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.item)
        refresh.assert_not_called()
        self.assertEqual(self.transactional_snapshot(), state_before)
        self.assertEqual(self.client.get("/api/v1/collection-items").json(), list_before)

    def test_stale_cas_returns_minimal_stable_conflict_and_never_mixes_writes(self):
        """R2-EDIT-CAS-001."""
        first = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(user_title="客户端 A"),
        )
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                user_title="客户端 B",
                personal_tags=["不得混入"],
                inspiration=None,
            ),
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        error = stale.json()["error"]
        self.assertEqual(
            set(error),
            {
                "code",
                "message",
                "collection_item_id",
                "expected_revision",
                "current_revision",
            },
        )
        self.assertEqual(error["code"], "COLLECTION_REVISION_CONFLICT")
        self.assertEqual(error["expected_revision"], 1)
        self.assertEqual(error["current_revision"], 2)
        current = self.client.get(
            f"/api/v1/collection-items/{self.item['id']}"
        ).json()
        self.assertEqual(current["user_title"], "客户端 A")
        self.assertNotIn("不得混入", current["personal_tags"])
        self.assertIsNotNone(current["inspiration"])

    def test_two_concurrent_repository_writers_have_one_cas_winner(self):
        """R2-EDIT-CAS-001 exercises the real SQLite writer lock."""
        base = editable_snapshot(self.item)
        choices = []
        for marker in ("A", "B"):
            choices.append(
                {
                    **deepcopy(base),
                    "user_title": f"并发 {marker}",
                    "organization_confirmation": {
                        "primary_category": f"分类 {marker}",
                        "secondary_category": f"子类 {marker}",
                        "organization_tags": [f"整理 {marker}"],
                    },
                    "personal_tags": [f"个人 {marker}"],
                    "inspiration": {
                        "content": f"灵感 {marker}",
                        "input_mode": "text",
                        "transcription_status": "not_applicable",
                    },
                }
            )

        def save(payload: dict):
            try:
                result = self.repo.update_collection_item_user_fields(
                    self.item["id"], 1, payload
                )
                return "ok", result
            except CollectionRevisionConflictError as exc:
                return "conflict", exc.current_revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, choices))
        self.assertEqual(sorted(status for status, _ in outcomes), ["conflict", "ok"])
        self.assertIn(("conflict", 2), outcomes)
        winner = next(result for status, result in outcomes if status == "ok")
        self.assertEqual(winner["revision"], 2)
        authoritative = self.repo.get_collection_item(self.item["id"])
        self.assertEqual(editable_snapshot(authoritative), editable_snapshot(winner))
        self.assertIn(editable_snapshot(winner), choices)

    def test_every_mutation_stage_rolls_back_user_graph_index_dirty_and_facets(self):
        """R2-EDIT-ATOMIC-001."""
        desired = editable_snapshot(self.item)
        desired.update(
            {
                "user_title": "原子新标题",
                "organization_confirmation": {
                    "primary_category": "原子分类",
                    "secondary_category": "原子子类",
                    "organization_tags": ["原子整理"],
                },
                "personal_tags": ["原子个人"],
                "inspiration": {
                    "content": "原子灵感",
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                },
            }
        )
        baseline = self.transactional_snapshot()
        facets_before = self.repo.search_collection_items(
            query="",
            platform=None,
            primary_category="",
            secondary_category="",
            tag="",
            tag_source=None,
            limit=24,
            after=None,
        )
        helper_names = (
            "_update_collection_root",
            "_replace_collection_confirmation",
            "_replace_collection_personal_tags",
            "_replace_collection_inspiration",
        )
        for helper_name in helper_names:
            with self.subTest(stage=helper_name):
                original = getattr(SQLiteRepository, helper_name)

                def fail_after_write(*args, _original=original, **kwargs):
                    _original(*args, **kwargs)
                    raise RuntimeError("injected atomic failure")

                with patch.object(
                    SQLiteRepository,
                    helper_name,
                    staticmethod(fail_after_write),
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        self.repo.update_collection_item_user_fields(
                            self.item["id"], 1, desired
                        )
                self.assertEqual(self.transactional_snapshot(), baseline)
                self.assertEqual(
                    self.repo.search_collection_items(
                        query="",
                        platform=None,
                        primary_category="",
                        secondary_category="",
                        tag="",
                        tag_source=None,
                        limit=24,
                        after=None,
                    ),
                    facets_before,
                )

        with patch(
            "backend.app.repositories.sqlite.refresh_collection_document",
            side_effect=RuntimeError("injected search refresh failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "search refresh"):
                self.repo.update_collection_item_user_fields(
                    self.item["id"], 1, desired
                )
        self.assertEqual(self.transactional_snapshot(), baseline)
        self.assertEqual(
            self.repo.search_collection_items(
                query="",
                platform=None,
                primary_category="",
                secondary_category="",
                tag="",
                tag_source=None,
                limit=24,
                after=None,
            ),
            facets_before,
        )

    def test_inspiration_update_delete_and_recreate_preserve_then_rotate_identity(self):
        """R2-EDIT-FIELDS-001 inspiration lifecycle."""
        original = self.item["inspiration"]
        changed = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                inspiration={
                    "content": "修改后灵感",
                    "input_mode": "text",
                    "transcription_status": "not_applicable",
                }
            ),
        ).json()
        self.assertEqual(changed["inspiration"]["id"], original["id"])
        self.assertEqual(changed["inspiration"]["created_at"], original["created_at"])
        cleared = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(changed, inspiration=None),
        ).json()
        self.assertIsNone(cleared["inspiration"])
        recreated = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                cleared,
                inspiration={
                    "content": "重新新建",
                    "input_mode": "voice",
                    "transcription_status": "completed",
                },
            ),
        ).json()
        self.assertNotEqual(recreated["inspiration"]["id"], original["id"])
        self.assertEqual(recreated["revision"], 4)

    def test_patch_schema_not_found_and_repository_validation_have_stable_boundaries(self):
        """R2-EDIT-BOUNDARY-001."""
        extra = self.patch_payload()
        extra["source_url"] = "https://attacker.invalid/forged"
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}", json=extra
            ).status_code,
            422,
        )
        nested_extra = self.patch_payload()
        nested_extra["inspiration"]["source_audio_url"] = (
            "https://attacker.invalid/source-audio"
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}",
                json=nested_extra,
            ).status_code,
            422,
        )
        missing = self.patch_payload()
        missing.pop("inspiration")
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}", json=missing
            ).status_code,
            422,
        )
        incomplete_confirmation = self.patch_payload()
        incomplete_confirmation["organization_confirmation"] = {}
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}",
                json=incomplete_confirmation,
            ).status_code,
            422,
        )
        string_revision = self.patch_payload()
        string_revision["expected_revision"] = "1"
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}",
                json=string_revision,
            ).status_code,
            422,
        )
        invalid = self.patch_payload(personal_tags=["   "])
        self.assertEqual(
            self.client.patch(
                f"/api/v1/collection-items/{self.item['id']}", json=invalid
            ).status_code,
            422,
        )
        missing_item = self.client.patch(
            "/api/v1/collection-items/missing", json=self.patch_payload()
        )
        self.assertEqual(missing_item.status_code, 404, missing_item.text)
        self.assertEqual(missing_item.json()["error"]["code"], "NOT_FOUND")
        state_before = self.transactional_snapshot()
        rejected = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                inspiration={
                    "content": "尚未完成的语音候选",
                    "input_mode": "voice",
                    "transcription_status": "not_applicable",
                }
            ),
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(
            rejected.json()["error"]["code"], "COLLECTION_VALIDATION_FAILED"
        )
        self.assertEqual(set(rejected.json()["error"]), {"code", "message"})
        self.assertEqual(self.transactional_snapshot(), state_before)

    def test_duplicate_post_is_zero_write_until_explicit_patch_snapshot(self):
        """R2-MERGE-IDENTITY-001 / R2-MERGE-EXPLICIT-001."""
        before = self.transactional_snapshot()
        duplicate = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "r25-duplicate"},
            json={
                "preview_id": "r25-preview",
                "user_title": "候选标题",
                "organization_confirmation": {
                    "primary_category": "候选分类",
                    "secondary_category": "",
                    "organization_tags": ["候选整理"],
                },
                "personal_tags": ["候选个人"],
                "inspiration": None,
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["error"]["code"], "COLLECTION_EXISTS")
        self.assertEqual(self.transactional_snapshot(), before)
        merged = self.client.patch(
            f"/api/v1/collection-items/{self.item['id']}",
            json=self.patch_payload(
                user_title="用户标题",
                organization_confirmation={
                    "primary_category": "知识",
                    "secondary_category": "文章",
                    "organization_tags": ["稍后读", "候选整理"],
                },
                personal_tags=["研究", "AI", "候选个人"],
            ),
        )
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(
            editable_snapshot(merged.json()),
            {
                "user_title": "用户标题",
                "user_author": None,
                "organization_confirmation": {
                    "primary_category": "知识",
                    "secondary_category": "文章",
                    "organization_tags": ["稍后读", "候选整理"],
                },
                "personal_tags": ["研究", "AI", "候选个人"],
                "inspiration": editable_snapshot(self.item)["inspiration"],
            },
        )

    def test_old_create_idempotency_snapshot_without_revision_replays_as_revision_one(self):
        """R2-EDIT-OUTCOME-001 plus create replay compatibility."""
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                """SELECT response_json FROM collection_idempotency_keys
                   WHERE idempotency_key='r25-create'"""
            ).fetchone()
            old_response = json.loads(row[0])
            old_response.pop("revision", None)
            db.execute(
                """UPDATE collection_idempotency_keys SET response_json=?
                   WHERE idempotency_key='r25-create'""",
                (json.dumps(old_response, ensure_ascii=False),),
            )
            db.commit()
        replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "r25-create"},
            json={
                "preview_id": "r25-preview",
                "user_title": self.trusted["user_title"],
                "organization_confirmation": self.trusted[
                    "organization_confirmation"
                ],
                "personal_tags": self.trusted["personal_tags"],
                "inspiration": self.trusted["inspiration"],
            },
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()["revision"], 1)
        self.assertEqual(replay.json()["id"], self.item["id"])


class CollectionRevisionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v14.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def build_v14_database(self) -> str:
        repo = SQLiteRepository(self.path)
        created = repo.create_collection_item(
            trusted_collection_payload(), "v14-key", "v14-request"
        )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE library_items DROP COLUMN revision")
            stored = json.loads(
                db.execute(
                    """SELECT response_json FROM collection_idempotency_keys
                       WHERE idempotency_key='v14-key'"""
                ).fetchone()[0]
            )
            stored.pop("revision", None)
            db.execute(
                """UPDATE collection_idempotency_keys SET response_json=?
                   WHERE idempotency_key='v14-key'""",
                (json.dumps(stored, ensure_ascii=False),),
            )
            db.execute("PRAGMA user_version=14")
            db.commit()
        return created["id"]

    def test_v14_to_v15_adds_revision_one_and_get_list_create_replay_remain_compatible(self):
        item_id = self.build_v14_database()
        migrated = SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(library_items)").fetchall()
            }
            self.assertIn("revision", columns)
            self.assertEqual(
                db.execute(
                    "SELECT revision FROM library_items WHERE id=?", (item_id,)
                ).fetchone()["revision"],
                1,
            )
        restored = migrated.get_collection_item(item_id)
        self.assertEqual(restored["revision"], 1)
        listed = migrated.search_collection_items(
            query="",
            platform=None,
            primary_category="",
            secondary_category="",
            tag="",
            tag_source=None,
            limit=24,
            after=None,
        )
        self.assertEqual(
            set(listed["items"][0]),
            {
                "id",
                "display_title",
                "platform",
                "primary_category",
                "cover_url",
                "source_author",
                "user_author",
                "has_user_cover",
                "created_at",
                "updated_at",
            },
        )

    def test_v14_raw_confirmation_get_and_original_create_hash_replay_are_compatible(self):
        historical_confirmation = {
            "primary_category": " 旧 一级 ",
            "secondary_category": "　",
            "organization_tags": ["   ", "重复", "重复", "旧合法值"],
        }
        trusted = trusted_collection_payload()
        trusted["organization_confirmation"] = deepcopy(historical_confirmation)
        trusted["personal_tags"] = ["研究", "AI"]
        trusted["untitled_confirmed"] = False
        canonical = json.dumps(
            trusted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        original_request_hash = hashlib.sha256(canonical).hexdigest()

        repo = SQLiteRepository(self.path)
        created = repo.create_collection_item(
            trusted,
            "v14-original-key",
            original_request_hash,
        )
        repo.create_collection_preview(
            {
                "preview_id": "v14-original-preview",
                "original_input": trusted["original_input"],
                "source_url": trusted["source_url"],
                "canonical_url": trusted["canonical_url"],
                "identity_url": "https://example.com/watch?a=1",
                "source_kind": trusted["source_kind"],
                "platform": trusted["platform"],
                "metadata_status": trusted["metadata_status"],
                "metadata": trusted["metadata"],
                "organization_suggestion": trusted["organization_suggestion"],
                "created_at": "2026-08-20T00:00:00+00:00",
                "expires_at": "2099-08-20T00:00:00+00:00",
            }
        )
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE library_items DROP COLUMN revision")
            response = json.loads(
                db.execute(
                    """SELECT response_json FROM collection_idempotency_keys
                       WHERE idempotency_key='v14-original-key'"""
                ).fetchone()[0]
            )
            response.pop("revision", None)
            db.execute(
                """UPDATE collection_idempotency_keys SET response_json=?
                   WHERE idempotency_key='v14-original-key'""",
                (json.dumps(response, ensure_ascii=False),),
            )
            db.execute("PRAGMA user_version=14")
            db.commit()

        migrated = SQLiteRepository(self.path)
        pipeline = LocalFullPipeline(
            migrated,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        with TestClient(
            create_app(pipeline, upload_root=self.temp.name)
        ) as client:
            restored = client.get(
                f"/api/v1/collection-items/{created['id']}"
            )
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(
                restored.json()["organization_confirmation"],
                historical_confirmation,
            )
            self.assertEqual(restored.json()["revision"], 1)

            replay = client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": "v14-original-key"},
                json={
                    "preview_id": "v14-original-preview",
                    "user_title": trusted["user_title"],
                    "organization_confirmation": historical_confirmation,
                    "personal_tags": ["研究", " AI ", "ＡＩ"],
                    "inspiration": trusted["inspiration"],
                },
            )
            self.assertEqual(replay.status_code, 201, replay.text)
            self.assertEqual(replay.json()["id"], created["id"])
            self.assertEqual(replay.json()["revision"], 1)
            self.assertEqual(
                replay.json()["organization_confirmation"],
                historical_confirmation,
            )

    def test_v15_failure_rolls_back_column_user_version_and_search_then_retries(self):
        item_id = self.build_v14_database()
        with closing(sqlite3.connect(self.path)) as db:
            document_before = db.execute(
                "SELECT indexed_text FROM collection_search_documents"
            ).fetchone()[0]

        def fail_after_alter(db: sqlite3.Connection) -> None:
            db.execute(
                "ALTER TABLE library_items "
                "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
            raise RuntimeError("injected v15 migration failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v14_to_v15",
            staticmethod(fail_after_alter),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected v15"):
                SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(library_items)")
            }
            self.assertNotIn("revision", columns)
            self.assertEqual(
                db.execute(
                    "SELECT indexed_text FROM collection_search_documents"
                ).fetchone()[0],
                document_before,
            )

        recovered = SQLiteRepository(self.path)
        self.assertEqual(recovered.get_collection_item(item_id)["revision"], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)


if __name__ == "__main__":
    unittest.main()
