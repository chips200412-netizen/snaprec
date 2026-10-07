from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import Segment
from backend.app.repositories.sqlite import SQLiteRepository, resource_key
from backend.app.services.focused import FocusedExtractionService
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
)


class SegmentedAsr:
    def transcribe(self, media_path: Path) -> list[Segment]:
        return [
            Segment(id="seg-1", start=0, end=2, text="这是核心观点。"),
            Segment(id="seg-2", start=2, end=5, text="首先执行第一步。"),
        ]


class PersonalNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "notes.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        self.pipeline = LocalFullPipeline(
            self.repo,
            SegmentedAsr(),
            DeterministicFullExtractor(),
            focused_extractor=DeterministicFocusedExtractor(),
        )
        self.client = TestClient(
            create_app(self.pipeline, upload_root=self.root / "uploads")
        )

    def tearDown(self):
        self.temp.cleanup()

    def _save(self, name: str = "notes.mp4"):
        path = self.root / name
        path.write_bytes(f"media:{name}".encode("utf-8"))
        return self.pipeline.process(path)[1]

    def _target(
        self,
        video_id: str,
        display_key: str,
        *,
        mode: str = "full",
        focus_query: str | None = None,
    ) -> dict:
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        matches = [
            item
            for item in detail.json()["annotation_targets"]
            if item["display_key"] == display_key
            and item["mode"] == mode
            and (focus_query is None or item["focus_query"] == focus_query)
        ]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def test_spark_upsert_read_delete_and_detail_snapshot(self):
        result = self._save()
        url = f"/api/v1/videos/{result.video_id}/spark"
        created = self.client.put(
            url,
            json={
                "content": "以后可以用于产品复盘",
                "author": "本地用户",
                "platform": "local_upload",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        first = created.json()["spark"]
        self.assertEqual(first["kind"], "spark")
        self.assertIsNone(first["target_key"])
        self.assertEqual(first["content"], "以后可以用于产品复盘")
        self.assertEqual(first["created_at"], first["updated_at"])
        first_list = self.client.get(
            "/api/v1/videos",
            params={"platform": "local_upload"},
        )
        self.assertEqual(first_list.status_code, 200, first_list.text)
        list_item = first_list.json()["items"][0]
        self.assertEqual(list_item["spark"], first)
        self.assertNotIn("personal_notes", list_item)
        self.assertNotIn("annotations", list_item)

        updated = self.client.put(
            url,
            json={
                "content": "更新后的闪念",
                "author": "本地用户",
                "platform": "local_upload",
            },
        )
        second = updated.json()["spark"]
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])
        updated_list = self.client.get(
            "/api/v1/videos",
            params={"platform": "local_upload"},
        )
        self.assertEqual(updated_list.json()["items"][0]["spark"], second)

        notes = self.client.get(
            f"/api/v1/videos/{result.video_id}/personal-notes",
            params={"platform": "local_upload"},
        )
        detail = self.client.get(
            f"/api/v1/videos/{result.video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(notes.status_code, 200)
        self.assertEqual(detail.json()["personal_notes"], notes.json())

        with closing(sqlite3.connect(self.db_path)) as db:
            core_before = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
        deleted = self.client.delete(
            url, params={"platform": "local_upload"}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(deleted.json()["spark"])
        deleted_list = self.client.get(
            "/api/v1/videos",
            params={"platform": "local_upload"},
        )
        self.assertIsNone(deleted_list.json()["items"][0]["spark"])
        with closing(sqlite3.connect(self.db_path)) as db:
            core_after = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
        self.assertEqual(core_after, core_before)

    def test_annotation_crud_validates_video_target_and_preserves_core_data(self):
        result = self._save()
        base = f"/api/v1/videos/{result.video_id}/annotations"
        claim_target = self._target(result.video_id, "key_points-0")
        step_target = self._target(result.video_id, "steps-0")
        claim = self.client.post(
            base,
            json={
                "target_key": claim_target["target_key"],
                "content": "这是我认可的结论",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(claim.status_code, 201, claim.text)
        first = claim.json()["annotations"][0]
        self.assertEqual(first["target_type"], "claim")

        step = self.client.post(
            base,
            json={
                "target_key": step_target["target_key"],
                "content": "明天尝试",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(step.status_code, 201, step.text)
        self.assertEqual(
            [item["target_type"] for item in step.json()["annotations"]],
            ["claim", "step"],
        )
        invalid = self.client.post(
            base,
            json={
                "target_key": "steps-0",
                "content": "旧的纯位置键不能绕过稳定目标校验",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_INPUT")

        changed = self.client.patch(
            f"{base}/{first['id']}",
            json={
                "content": "修改后的个人结论",
                "platform": "local_upload",
            },
        )
        changed_item = changed.json()["annotations"][0]
        self.assertEqual(changed_item["content"], "修改后的个人结论")
        self.assertEqual(changed_item["author"], "我")
        self.assertEqual(
            changed_item["target_key"], claim_target["target_key"]
        )

        other = self._save("other.mp4")
        cross_video_create = self.client.post(
            f"/api/v1/videos/{other.video_id}/annotations",
            json={
                "target_key": claim_target["target_key"],
                "content": "不能跨视频绑定",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(cross_video_create.status_code, 400)
        wrong_video = self.client.delete(
            f"/api/v1/videos/{other.video_id}/annotations/{first['id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(wrong_video.status_code, 404)

        with closing(sqlite3.connect(self.db_path)) as db:
            key = resource_key("local_upload", result.video_id)
            core_before = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
            transcript_before = db.execute(
                "SELECT text FROM transcript_segments WHERE resource_key=? "
                "ORDER BY position",
                (key,),
            ).fetchall()
        deleted = self.client.delete(
            f"{base}/{first['id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(len(deleted.json()["annotations"]), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            core_after = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
            transcript_after = db.execute(
                "SELECT text FROM transcript_segments WHERE resource_key=? "
                "ORDER BY position",
                (key,),
            ).fetchall()
        self.assertEqual(core_after, core_before)
        self.assertEqual(transcript_after, transcript_before)

    def test_focused_target_is_validated_inside_the_same_video(self):
        result = self._save()
        service = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        )
        service.extract(
            result.video_id,
            "核心观点",
            platform="local_upload",
        )
        service.extract(
            result.video_id,
            "第一步",
            platform="local_upload",
        )
        first_target = self._target(
            result.video_id,
            "focused-key-points-0",
            mode="focused",
            focus_query="核心观点",
        )
        second_target = self._target(
            result.video_id,
            "focused-key-points-0",
            mode="focused",
            focus_query="第一步",
        )
        self.assertNotEqual(
            first_target["target_key"], second_target["target_key"]
        )
        first_response = self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": first_target["target_key"],
                "content": "问题 A 的备注",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(first_response.status_code, 201, first_response.text)
        second_response = self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": second_target["target_key"],
                "content": "问题 B 的备注",
                "author": "我",
                "platform": "local_upload",
            },
        )
        annotations = second_response.json()["annotations"]
        self.assertEqual(
            {item["target_key"] for item in annotations},
            {first_target["target_key"], second_target["target_key"]},
        )
        self.assertEqual(
            {
                item["target_key"]: item["content"]
                for item in annotations
            },
            {
                first_target["target_key"]: "问题 A 的备注",
                second_target["target_key"]: "问题 B 的备注",
            },
        )

    def test_target_fingerprint_survives_reorder_but_not_replacement(self):
        result = self._save()
        original_target = self._target(result.video_id, "key_points-0")
        created = self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": original_target["target_key"],
                "content": "绑定原始结论",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        original_item = result.full_extraction.key_points[0]
        added_item = original_item.model_copy(
            update={"text": "这是后来新增的核心观点。"}
        )
        reordered_full = result.full_extraction.model_copy(
            update={"key_points": [added_item, original_item]}
        )
        self.repo.save_result(
            result.model_copy(update={"full_extraction": reordered_full})
        )
        moved_target = self._target(result.video_id, "key_points-1")
        self.assertEqual(
            moved_target["target_key"], original_target["target_key"]
        )

        replaced_full = result.full_extraction.model_copy(
            update={"key_points": [added_item]}
        )
        self.repo.save_result(
            result.model_copy(update={"full_extraction": replaced_full})
        )
        detail = self.client.get(
            f"/api/v1/videos/{result.video_id}",
            params={"platform": "local_upload"},
        ).json()
        current_keys = {
            item["target_key"] for item in detail["annotation_targets"]
        }
        self.assertNotIn(original_target["target_key"], current_keys)
        self.assertEqual(
            detail["personal_notes"]["annotations"][0]["target_key"],
            original_target["target_key"],
        )
        rejected = self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": original_target["target_key"],
                "content": "不能重新绑到替换后的内容",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(rejected.status_code, 400)

    def test_identical_extraction_items_receive_distinct_target_keys(self):
        result = self._save()
        duplicate_item = result.full_extraction.key_points[0]
        duplicated_full = result.full_extraction.model_copy(
            update={"key_points": [duplicate_item, duplicate_item]}
        )
        self.repo.save_result(
            result.model_copy(update={"full_extraction": duplicated_full})
        )

        detail = self.client.get(
            f"/api/v1/videos/{result.video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        targets = {
            item["display_key"]: item["target_key"]
            for item in detail.json()["annotation_targets"]
        }
        first_key = targets["key_points-0"]
        second_key = targets["key_points-1"]
        self.assertNotEqual(first_key, second_key)
        self.assertTrue(second_key.startswith(first_key + ":duplicate:"))

        for target_key, content in (
            (first_key, "第一条重复内容的备注"),
            (second_key, "第二条重复内容的备注"),
        ):
            created = self.client.post(
                f"/api/v1/videos/{result.video_id}/annotations",
                json={
                    "target_key": target_key,
                    "content": content,
                    "author": "我",
                    "platform": "local_upload",
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(
            {
                item["target_key"]
                for item in created.json()["annotations"]
            },
            {first_key, second_key},
        )

    def test_platform_is_required_when_public_video_id_is_ambiguous(self):
        result = self._save()
        bilibili_result = result.model_copy(
            update={
                "platform": "bilibili",
                "source_url": "https://www.bilibili.com/video/" + result.video_id,
                "canonical_url": "https://www.bilibili.com/video/" + result.video_id,
            }
        )
        self.repo.save_result(bilibili_result)
        ambiguous = self.client.put(
            f"/api/v1/videos/{result.video_id}/spark",
            json={"content": "平台必须消歧", "author": "我"},
        )
        self.assertEqual(ambiguous.status_code, 400)
        self.assertEqual(
            ambiguous.json()["error"]["code"], "AMBIGUOUS_VIDEO_ID"
        )
        explicit = self.client.put(
            f"/api/v1/videos/{result.video_id}/spark",
            json={
                "content": "只属于本地视频",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(explicit.status_code, 200)
        other = self.client.get(
            f"/api/v1/videos/{result.video_id}/personal-notes",
            params={"platform": "bilibili"},
        )
        self.assertIsNone(other.json()["spark"])

    def test_markdown_keeps_personal_content_in_a_separate_section(self):
        result = self._save()
        step_target = self._target(result.video_id, "steps-0")
        self.client.put(
            f"/api/v1/videos/{result.video_id}/spark",
            json={
                "content": "我的闪念",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": step_target["target_key"],
                "content": "我的步骤备注",
                "author": "我",
                "platform": "local_upload",
            },
        )
        exported = self.client.get(
            f"/api/v1/videos/{result.video_id}/export.md",
            params={"platform": "local_upload"},
        )
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.text.count("## 个人内容"), 1)
        self.assertIn("### 闪念", exported.text)
        self.assertIn("### 个人备注", exported.text)
        self.assertIn(
            f"`{step_target['target_key']}`：我的步骤备注",
            exported.text,
        )
        self.assertLess(
            exported.text.index("## 个人内容"),
            exported.text.index("## 完整字幕"),
        )

    def test_invalid_personal_text_and_unknown_resources_are_rejected(self):
        result = self._save()
        blank = self.client.put(
            f"/api/v1/videos/{result.video_id}/spark",
            json={
                "content": "   ",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.assertEqual(blank.status_code, 400)
        self.assertEqual(blank.json()["error"]["code"], "INVALID_INPUT")
        unknown = self.client.get(
            "/api/v1/videos/missing/personal-notes",
            params={"platform": "local_upload"},
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "NOT_FOUND")
        extra = self.client.post(
            f"/api/v1/videos/{result.video_id}/annotations",
            json={
                "target_key": "steps-0",
                "content": "x",
                "author": "我",
                "platform": "local_upload",
                "unexpected": True,
            },
        )
        self.assertEqual(extra.status_code, 422)


class PersonalNotesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v5.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _create_v5(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            SQLiteRepository._create_v3_schema(db)
            SQLiteRepository._migrate_v3_to_v4(db)
            SQLiteRepository._migrate_v4_to_v5(db)
            db.execute("PRAGMA user_version=5")
            db.commit()

    def test_v5_upgrade_is_idempotent_and_foreign_keys_are_valid(self):
        self._create_v5()
        SQLiteRepository(self.path)
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertTrue(
                {"video_sparks", "video_annotations"}.issubset(tables)
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v5_upgrade_rolls_back_completely_and_can_retry(self):
        self._create_v5()

        def fail_after_first_table(db):
            db.execute(
                """CREATE TABLE video_sparks (
                    id INTEGER PRIMARY KEY, resource_key TEXT UNIQUE
                )"""
            )
            raise RuntimeError("injected v6 migration failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v5_to_v6",
            staticmethod(fail_after_first_table),
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("video_sparks", tables)
            self.assertNotIn("video_annotations", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)

        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
