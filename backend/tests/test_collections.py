from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import FullExtraction, VideoResult
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)


def trusted_collection_payload(*, title: str = "用户标题", source_url: str = "https://Example.com:443/watch?a=1#part") -> dict:
    return {
        "source_kind": "webpage",
        "platform": "web",
        "original_input": f"分享文本 {source_url}",
        "source_url": source_url,
        "canonical_url": "https://EXAMPLE.com:443/watch?a=1#canonical",
        "metadata_status": "generic",
        "user_title": title,
        "metadata": {
            "title": {"value": "来源标题", "source": "open_graph", "fetched_at": "2026-08-20T00:00:00Z"},
            "author": {"value": "来源作者", "source": "open_graph", "fetched_at": "2026-08-20T00:00:00Z"},
            "cover_url": {"value": "https://cdn.example/cover.jpg", "source": "open_graph", "fetched_at": "2026-08-20T00:00:00Z"},
            "source_copy": {"value": "公开页面说明", "source": "page_description", "fetched_at": "2026-08-20T00:00:00Z"},
            "platform_tags": [{"value": "公开标签", "source": "platform_public"}],
            "warnings": [],
        },
        "organization_suggestion": {
            "primary_category": "阅读",
            "secondary_category": "网页",
            "tags": ["待整理"],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "generated",
        },
        "organization_confirmation": {
            "primary_category": "知识",
            "secondary_category": "文章",
            "organization_tags": ["稍后读"],
        },
        "personal_tags": ["研究", " AI ", "ＡＩ"],
        "inspiration": {
            "content": "这是我的灵感。",
            "input_mode": "text",
            "transcription_status": "not_applicable",
        },
    }


def collection_payload(*, title: str = "用户标题") -> dict:
    trusted = trusted_collection_payload(title=title)
    return {
        "preview_id": "preview-default",
        "user_title": trusted["user_title"],
        "organization_confirmation": trusted["organization_confirmation"],
        "personal_tags": trusted["personal_tags"],
        "inspiration": trusted["inspiration"],
    }


def legacy_video(video_id: str, source_url: str, *, title: str) -> VideoResult:
    return VideoResult(
        platform="bilibili",
        source_url=source_url,
        canonical_url=source_url,
        video_id=video_id,
        author="旧作者",
        title=title,
        description="旧来源文案",
        tags=["旧平台标签"],
        duration=0,
        cover_url="",
        subtitle_source="none",
        raw_transcript="",
        clean_transcript="",
        segments=[],
        focus_query="",
        extraction_mode="full",
        summary="",
        full_extraction=FullExtraction(),
        evidence=[],
        warnings=[],
    )


class CollectionApiTests(unittest.TestCase):
    def _seed_preview(self, preview_id: str, trusted: dict) -> None:
        self.repo.create_collection_preview({
            "preview_id": preview_id,
            "original_input": trusted["original_input"],
            "source_url": trusted["source_url"],
            "canonical_url": trusted["canonical_url"],
            "identity_url": "https://example.com/watch?a=1",
            "source_kind": trusted["source_kind"],
            "platform": trusted["platform"],
            "metadata_status": trusted["metadata_status"],
            "metadata": trusted["metadata"],
            "created_at": "2026-08-20T00:00:00+00:00",
            "expires_at": "2099-08-20T00:00:00+00:00",
        })

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "collections.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        trusted = trusted_collection_payload()
        self._seed_preview("preview-default", trusted)
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        self.client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))

    def tearDown(self):
        self.temp.cleanup()

    def test_collection_save_is_atomic_and_get_restores_complete_graph(self):
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "save-001"},
            json=collection_payload(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        saved = response.json()
        self.assertEqual(saved["identity_url"], "https://example.com/watch?a=1")
        self.assertEqual(saved["display_title"], "用户标题")
        self.assertEqual(saved["personal_tags"], ["研究", "AI"])
        self.assertEqual(saved["inspiration"]["content"], "这是我的灵感。")

        restored = self.client.get(f"/api/v1/collection-items/{saved['id']}")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json(), saved)
        with closing(sqlite3.connect(self.db_path)) as db:
            counts = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "library_items",
                    "source_metadata",
                    "organization_suggestions",
                    "organization_confirmations",
                    "inspirations",
                    "collection_idempotency_keys",
                    "collection_url_aliases",
                    "collection_personal_tags",
                )
            }
        self.assertEqual(counts["library_items"], 1)
        self.assertTrue(all(value >= 1 for value in counts.values()))

    def test_create_persists_manual_author_without_overwriting_source_and_replays(self):
        """INT-ACC-CQ3-001/004; CQ3-CONSISTENCY-001."""
        payload = {**collection_payload(), "user_author": "  我的  作者  "}
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "cq3-author-create"},
            json=payload,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["user_author"], "我的 作者")
        self.assertEqual(first.json()["metadata"]["author"]["value"], "来源作者")
        replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "cq3-author-create"},
            json=payload,
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first.json())

    def test_create_normalizes_editable_organization_fields_before_hash_and_write(self):
        payload = collection_payload()
        payload["user_title"] = f"  {'题' * 500}  "
        payload["organization_confirmation"] = {
            "primary_category": "　" + ("A\u030a" * 64) + "　",
            "secondary_category": "  创意　工具  ",
            "organization_tags": ["  Foo   Bar  "] * 51 + [" 新增 "],
        }
        payload["personal_tags"] = [" ＡＩ "] * 51
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "normalized-create"},
            json=payload,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["user_title"], "题" * 500)
        self.assertEqual(
            first.json()["organization_confirmation"],
            {
                "primary_category": "Å" * 64,
                "secondary_category": "创意 工具",
                "organization_tags": ["Foo Bar", "新增"],
            },
        )
        self.assertEqual(first.json()["personal_tags"], ["AI"])

        same_raw_replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "normalized-create"},
            json=payload,
        )
        self.assertEqual(same_raw_replay.status_code, 201, same_raw_replay.text)
        self.assertEqual(same_raw_replay.json(), first.json())

        canonical = collection_payload()
        canonical["user_title"] = first.json()["user_title"]
        canonical["organization_confirmation"] = first.json()[
            "organization_confirmation"
        ]
        canonical["personal_tags"] = first.json()["personal_tags"]
        replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "normalized-create"},
            json=canonical,
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first.json())

    def test_idempotency_replays_same_payload_and_rejects_changed_payload(self):
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "same-key"},
            json=collection_payload(),
        )
        replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "same-key"},
            json=collection_payload(),
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first.json())

        changed = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "same-key"},
            json=collection_payload(title="另一个标题"),
        )
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 1)

    def test_successful_idempotent_replay_survives_preview_expiry(self):
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "expiry-replay-key"},
            json=collection_payload(),
        )
        self.assertEqual(first.status_code, 201, first.text)
        with closing(sqlite3.connect(self.db_path)) as db:
            row = db.execute(
                "SELECT payload_json FROM collection_previews WHERE preview_id=?",
                ("preview-default",),
            ).fetchone()
            preview = json.loads(row[0])
            preview["expires_at"] = "2000-01-01T00:00:00+00:00"
            db.execute(
                """UPDATE collection_previews
                   SET payload_json=?, expires_at=? WHERE preview_id=?""",
                (
                    json.dumps(preview, ensure_ascii=False),
                    preview["expires_at"],
                    "preview-default",
                ),
            )
            db.commit()
        replay = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "expiry-replay-key"},
            json=collection_payload(),
        )
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first.json())

    def test_new_key_for_existing_identity_returns_existing_item_without_overwrite(self):
        first = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "first-key"},
            json=collection_payload(),
        )
        duplicate = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "second-key"},
            json=collection_payload(title="不得覆盖"),
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["error"]["code"], "COLLECTION_EXISTS")
        self.assertEqual(duplicate.json()["error"]["collection_item_id"], first.json()["id"])
        restored = self.client.get(f"/api/v1/collection-items/{first.json()['id']}")
        self.assertEqual(restored.json()["user_title"], "用户标题")

    def test_collection_graph_rolls_back_if_any_child_write_fails(self):
        original = SQLiteRepository._insert_collection_graph

        def fail_after_graph(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected collection graph failure")

        with patch.object(
            SQLiteRepository,
            "_insert_collection_graph",
            staticmethod(fail_after_graph),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.repo.create_collection_item(
                    trusted_collection_payload(), "rollback-key", "request-hash"
                )
        with closing(sqlite3.connect(self.db_path)) as db:
            for table in (
                "library_items",
                "source_metadata",
                "organization_suggestions",
                "organization_confirmations",
                "inspirations",
                "collection_idempotency_keys",
                "collection_url_aliases",
                "collection_personal_tags",
            ):
                self.assertEqual(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                    table,
                )

    def test_concurrent_same_key_replays_one_committed_collection(self):
        payload = trusted_collection_payload()

        def save():
            return self.repo.create_collection_item(
                payload, "concurrent-key", "same-request-hash"
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertEqual(results[0], results[1])
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM collection_idempotency_keys").fetchone()[0],
                1,
            )
    def test_untitled_collection_requires_explicit_confirmation(self):
        trusted = trusted_collection_payload(title="")
        trusted["metadata"] = {
            "title": {"value": "", "source": "none", "fetched_at": ""},
            "author": {"value": "", "source": "none", "fetched_at": ""},
            "cover_url": {"value": "", "source": "none", "fetched_at": ""},
            "source_copy": {"value": "", "source": "none", "fetched_at": ""},
            "platform_tags": [], "warnings": [],
        }
        trusted["metadata_status"] = "metadata_unavailable"
        self._seed_preview("preview-untitled", trusted)
        payload = collection_payload(title="")
        payload["preview_id"] = "preview-untitled"
        rejected = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "untitled-rejected"},
            json=payload,
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        payload["untitled_confirmed"] = True
        accepted = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "untitled-accepted"},
            json=payload,
        )
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()["display_title"], "未命名收藏")

    def test_create_rejects_client_source_fields_and_unconfirmed_voice_draft(self):
        forged = collection_payload()
        forged["source_url"] = "https://attacker.example/forged"
        forged["metadata"] = {"title": {"value": "伪造", "source": "legacy_import"}}
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "forged-legacy"},
            json=forged,
        )
        self.assertEqual(response.status_code, 422, response.text)

        for index, invalid_tag in enumerate(("   ", "x" * 65)):
            invalid = collection_payload()
            invalid["personal_tags"] = [invalid_tag]
            response = self.client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": f"invalid-tag-{index}"},
                json=invalid,
            )
            self.assertEqual(response.status_code, 422, response.text)

        for index, invalid_tag in enumerate(("   ", "x" * 65)):
            invalid = collection_payload()
            invalid["organization_confirmation"]["organization_tags"] = [
                invalid_tag
            ]
            response = self.client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": f"invalid-organization-tag-{index}"},
                json=invalid,
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(
                response.json()["error"]["code"], "COLLECTION_VALIDATION_FAILED"
            )

        too_many_personal = collection_payload()
        too_many_personal["personal_tags"] = [f"个人-{index}" for index in range(51)]
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "too-many-final-personal-tags"},
            json=too_many_personal,
        )
        self.assertEqual(response.status_code, 422, response.text)

        too_many_organization = collection_payload()
        too_many_organization["organization_confirmation"]["organization_tags"] = [
            f"整理-{index}" for index in range(51)
        ]
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "too-many-final-organization-tags"},
            json=too_many_organization,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "COLLECTION_VALIDATION_FAILED"
        )

        voice_draft = collection_payload()
        voice_draft["inspiration"] = {
            "content": "尚未合并的草稿",
            "input_mode": "voice",
            "transcription_status": "draft",
        }
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "voice-draft"},
            json=voice_draft,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_create_rejects_missing_and_expired_preview(self):
        missing = collection_payload()
        missing["preview_id"] = "missing"
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "empty-generic"},
            json=missing,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "PREVIEW_NOT_FOUND")
        trusted = trusted_collection_payload()
        self.repo.create_collection_preview({
            "preview_id": "preview-expired",
            "original_input": trusted["original_input"],
            "source_url": trusted["source_url"],
            "canonical_url": trusted["canonical_url"],
            "identity_url": "https://example.com/watch?a=1",
            "source_kind": trusted["source_kind"],
            "platform": trusted["platform"],
            "metadata_status": trusted["metadata_status"],
            "metadata": trusted["metadata"],
            "created_at": "2000-01-01T00:00:00+00:00",
            "expires_at": "2000-01-02T00:00:00+00:00",
        })
        expired = collection_payload()
        expired["preview_id"] = "preview-expired"
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "expired"},
            json=expired,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "PREVIEW_EXPIRED")

    def test_status_requires_a_nonblank_value_with_matching_public_provenance(self):
        trusted = trusted_collection_payload(title="")
        trusted["metadata"] = {
            "title": {"value": "", "source": "platform_public", "fetched_at": ""},
            "author": {"value": "", "source": "none", "fetched_at": ""},
            "cover_url": {"value": "", "source": "none", "fetched_at": ""},
            "source_copy": {"value": "", "source": "none", "fetched_at": ""},
            "platform_tags": [],
            "warnings": [],
        }
        trusted["metadata_status"] = "recognized"
        self._seed_preview("preview-empty-recognized", trusted)
        payload = collection_payload(title="用户补充标题")
        payload["preview_id"] = "preview-empty-recognized"
        response = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "empty-recognized"},
            json=payload,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["error"]["code"], "COLLECTION_VALIDATION_FAILED"
        )


class CollectionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "migration.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _build_v10(self) -> SQLiteRepository:
        with patch.object(
            SQLiteRepository,
            "_migrate_v10_to_v11",
            staticmethod(lambda db: None),
        ), patch("backend.app.repositories.sqlite.migrate_collection_search"):
            repo = SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version=10")
            db.commit()
        return repo

    def test_v10_to_v11_backfills_only_unique_favorites_and_preserves_old_api(self):
        repo = self._build_v10()
        unique = legacy_video("BVunique", "https://example.com/unique#one", title="唯一收藏")
        not_favorite = legacy_video("BVplain", "https://example.com/plain", title="未收藏")
        collision_a = legacy_video("BVcollisionA", "https://EXAMPLE.com:443/same#one", title="冲突甲")
        collision_b = legacy_video("BVcollisionB", "https://example.com/same#two", title="冲突乙")
        alias_a = legacy_video(
            "BValiasA", "https://example.com/shared-short", title="别名冲突甲"
        ).model_copy(update={"canonical_url": "https://example.com/canonical-a"})
        alias_b = legacy_video(
            "BValiasB", "https://example.com/shared-short", title="别名冲突乙"
        ).model_copy(update={"canonical_url": "https://example.com/canonical-b"})
        for result in (
            unique,
            not_favorite,
            collision_a,
            collision_b,
            alias_a,
            alias_b,
        ):
            repo.save_result(result)
        for result in (unique, collision_a, collision_b, alias_a, alias_b):
            self.assertTrue(repo.set_favorite(result.video_id, True, platform="bilibili"))
        repo.update_video_tags(unique.video_id, [" 旧标签 "], operation="add", platform="bilibili")
        repo.set_classification(unique.video_id, "旧分类", "旧子类", platform="bilibili")
        repo.upsert_spark(unique.video_id, "旧闪念", "用户", platform="bilibili")

        migrated = SQLiteRepository(self.path)
        migrated.migrate()
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            rows = db.execute("SELECT * FROM library_items").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["deep_analysis_resource_key"], "bilibili:BVunique")
            self.assertEqual(rows[0]["identity_url"], "https://example.com/unique")
            metadata = db.execute("SELECT * FROM source_metadata").fetchone()
            self.assertEqual(metadata["title_source"], "legacy_import")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_personal_tags").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inspirations").fetchone()[0], 1)
            warning = db.execute(
                "SELECT message FROM system_warnings WHERE code='COLLECTION_BACKFILL_IDENTITY_CONFLICT'"
            ).fetchone()
            self.assertIsNotNone(warning)
            self.assertNotIn("example.com", warning["message"])
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM system_warnings WHERE code='COLLECTION_BACKFILL_IDENTITY_CONFLICT'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(migrated.load_result("BVunique", platform="bilibili").title, "唯一收藏")

    def test_v10_long_platform_tag_restores_and_unsafe_cover_is_discarded(self):
        repo = self._build_v10()
        legacy = legacy_video(
            "BVlegacyfields", "https://example.com/legacy-fields", title="旧字段收藏"
        ).model_copy(
            update={
                "tags": ["旧" * 65, "", "   "],
                "cover_url": "javascript:alert(1)",
            }
        )
        repo.save_result(legacy)
        repo.set_favorite(legacy.video_id, True, platform="bilibili")

        migrated = SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            item_id = db.execute("SELECT id FROM library_items").fetchone()[0]
        pipeline = LocalFullPipeline(
            migrated,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        client = TestClient(
            create_app(pipeline, upload_root=Path(self.temp.name) / "uploads")
        )
        restored = client.get(f"/api/v1/collection-items/{item_id}")
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["metadata"]["platform_tags"][0]["value"], "旧" * 65)
        self.assertEqual(restored.json()["metadata"]["cover_url"]["value"], "")
        self.assertIn(
            "历史封面链接未通过安全校验，已忽略。",
            restored.json()["metadata"]["warnings"],
        )
        self.assertIn(
            "历史空平台标签已忽略。",
            restored.json()["metadata"]["warnings"],
        )

    def test_v10_favorite_without_usable_metadata_is_marked_unavailable(self):
        repo = self._build_v10()
        legacy = legacy_video(
            "BVempty", "https://example.com/empty-metadata", title=""
        ).model_copy(
            update={
                "author": "   ",
                "description": "",
                "tags": ["   "],
                "cover_url": "",
            }
        )
        repo.save_result(legacy)
        repo.set_favorite(legacy.video_id, True, platform="bilibili")

        migrated = SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            item = db.execute("SELECT id, metadata_status FROM library_items").fetchone()
        restored = migrated.get_collection_item(item["id"])
        self.assertEqual(item["metadata_status"], "metadata_unavailable")
        self.assertEqual(restored["display_title"], "未命名收藏")
        self.assertEqual(restored["metadata"]["platform_tags"], [])
        self.assertIn(
            "历史空平台标签已忽略。", restored["metadata"]["warnings"]
        )

    def test_v11_failure_rolls_back_schema_backfill_and_user_version_then_retries(self):
        repo = self._build_v10()
        result = legacy_video("BVrollback", "https://example.com/rollback", title="回滚收藏")
        repo.save_result(result)
        repo.set_favorite(result.video_id, True, platform="bilibili")
        original = SQLiteRepository._migrate_v10_to_v11

        def fail(db):
            db.execute("CREATE TABLE library_items(id TEXT PRIMARY KEY)")
            raise RuntimeError("injected v11 migration failure")

        with patch.object(SQLiteRepository, "_migrate_v10_to_v11", staticmethod(fail)):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("library_items", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)

        with patch.object(SQLiteRepository, "_migrate_v10_to_v11", staticmethod(original)):
            recovered = SQLiteRepository(self.path)
        self.assertEqual(recovered.load_result("BVrollback", platform="bilibili").title, "回滚收藏")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 1)

    def test_v10_foreign_key_violation_blocks_v11_and_rolls_back(self):
        self._build_v10()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "INSERT INTO tags(normalized_name, display_name, created_at) VALUES ('orphan', 'orphan', 'now')"
            )
            db.execute(
                "INSERT INTO video_tags(resource_key, normalized_name, created_at) VALUES ('missing:video', 'orphan', 'now')"
            )
            db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "foreign_key_check"):
            SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("library_items", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 10)

    def test_repeated_v11_migration_skips_late_legacy_favorite_conflicting_with_new_item(self):
        old_repo = self._build_v10()
        legacy = legacy_video(
            "BVlate", "https://example.com/watch?a=1", title="稍后旧收藏"
        )
        old_repo.save_result(legacy)

        repo = SQLiteRepository(self.path)
        created = repo.create_collection_item(
            trusted_collection_payload(), "new-item-key", "new-item-hash"
        )
        self.assertTrue(repo.set_favorite("BVlate", True, platform="bilibili"))

        reopened = SQLiteRepository(self.path)
        self.assertEqual(reopened.get_collection_item(created["id"])["id"], created["id"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0], 1)
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM system_warnings WHERE code='COLLECTION_BACKFILL_IDENTITY_CONFLICT'"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
