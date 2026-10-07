from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import FullExtraction, Segment
from backend.app.repositories.sqlite import SQLiteRepository, resource_key
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
)


class SplitAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, _media_path: Path) -> list[Segment]:
        self.calls += 1
        return [
            Segment(
                id="seg-step",
                start=2,
                end=6,
                text="首先填写申请，然后等待审核。",
            ),
            Segment(
                id="seg-risk",
                start=7,
                end=10,
                text="注意资料不能缺失，否则无法退款。",
            ),
            Segment(
                id="seg-price",
                start=11,
                end=13,
                text="收费标准是每月100元。",
            ),
        ]


class QuestionAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "notes.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        self.asr = SplitAsr()
        self.pipeline = LocalFullPipeline(
            self.repo,
            self.asr,
            DeterministicFullExtractor(),
            focused_extractor=DeterministicFocusedExtractor(),
        )
        self.client = TestClient(
            create_app(self.pipeline, upload_root=self.root / "uploads")
        )
        media = self.root / "question.mp4"
        media.write_bytes(b"media")
        self.result = self.pipeline.process(media)[1]

    def tearDown(self):
        self.temp.cleanup()

    def _question(self, value: str):
        return self.client.post(
            f"/api/v1/videos/{self.result.video_id}/questions",
            json={"question": value, "platform": "local_upload"},
        )

    def test_questions_use_saved_transcript_are_idempotent_and_restore(self):
        calls_after_parse = self.asr.calls
        with (
            patch(
                "backend.app.services.resolution.ResolutionService.process",
                autospec=True,
            ) as resolver_process,
            patch(
                "backend.app.adapters.local_upload.LocalUploadAdapter.resolve",
                autospec=True,
            ) as adapter_resolve,
            patch(
                "backend.app.services.media.RetainedMediaService.open_stream",
                autospec=True,
            ) as media_open,
        ):
            first = self._question("收费")
            repeated = self._question("  收费  ")

        resolver_process.assert_not_called()
        adapter_resolve.assert_not_called()
        media_open.assert_not_called()

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertEqual(first.json()["answer"]["mention_status"], "explicit")
        self.assertEqual(
            first.json()["answer"]["supporting_segments"][0]["segment_ids"],
            ["seg-price"],
        )
        self.assertEqual(self.asr.calls, calls_after_parse)

        detail = self.client.get(
            f"/api/v1/videos/{self.result.video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(len(detail.json()["question_history"]), 1)
        self.assertEqual(detail.json()["question_history"][0]["question"], "收费")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM questions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM question_evidence").fetchone()[0], 1)

    def test_questions_distinguish_not_mentioned_from_incomplete_transcript(self):
        absent = self._question("会员权益")
        self.assertEqual(absent.status_code, 200, absent.text)
        self.assertEqual(absent.json()["answer"]["mention_status"], "not_mentioned")
        self.assertEqual(
            absent.json()["answer"]["direct_answer"],
            "该视频没有明确讨论这个问题。",
        )

        incomplete = self.result.model_copy(
            update={
                "subtitle_source": "none",
                "raw_transcript": "",
                "clean_transcript": "",
                "segments": [],
                "summary": "",
                "full_extraction": FullExtraction(),
                "evidence": [],
                "warnings": ["字幕残缺，无法形成完整内容判断。"],
            }
        )
        self.repo.save_result(incomplete)
        unknown = self._question("退款")
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertEqual(
            unknown.json()["answer"]["mention_status"],
            "unknown_incomplete_transcript",
        )
        self.assertNotIn("退款条件是", unknown.json()["answer"]["direct_answer"])

    def test_export_modes_keep_sources_evidence_and_personal_content_explicit(self):
        detail = self.client.get(
            f"/api/v1/videos/{self.result.video_id}",
            params={"platform": "local_upload"},
        ).json()
        target = next(
            item for item in detail["annotation_targets"]
            if item["target_type"] == "step"
        )
        self.client.put(
            f"/api/v1/videos/{self.result.video_id}/spark",
            json={
                "content": "回头实践这个流程",
                "author": "我",
                "platform": "local_upload",
            },
        )
        self.client.post(
            f"/api/v1/videos/{self.result.video_id}/annotations",
            json={
                "target_key": target["target_key"],
                "content": "准备材料清单",
                "author": "我",
                "platform": "local_upload",
            },
        )

        steps = self.client.get(
            f"/api/v1/videos/{self.result.video_id}/export.md",
            params={
                "platform": "local_upload",
                "view": "steps",
                "include_personal": "false",
            },
        )
        self.assertEqual(steps.status_code, 200, steps.text)
        self.assertIn("## 方法或操作步骤", steps.text)
        self.assertIn("## 注意事项和风险", steps.text)
        self.assertIn("证据 [2s–6s]", steps.text)
        self.assertNotIn("收费标准是每月100元", steps.text)
        self.assertNotIn("## 个人内容", steps.text)
        self.assertNotIn("## 完整字幕", steps.text)

        personal = self.client.get(
            f"/api/v1/videos/{self.result.video_id}/export.md",
            params={
                "platform": "local_upload",
                "view": "steps",
                "include_personal": "true",
            },
        )
        self.assertIn("## 个人内容", personal.text)
        self.assertIn("回头实践这个流程", personal.text)
        self.assertIn("准备材料清单", personal.text)

        transcript = self.client.get(
            f"/api/v1/videos/{self.result.video_id}/export.md",
            params={"platform": "local_upload", "view": "transcript"},
        )
        self.assertIn("## 完整字幕", transcript.text)
        self.assertIn("收费标准是每月100元", transcript.text)
        self.assertNotIn("## 方法或操作步骤", transcript.text)

    def test_focused_export_selects_saved_hash_without_reextracting(self):
        focused = self.client.post(
            f"/api/v1/videos/{self.result.video_id}/extractions",
            json={"focus_query": "收费", "platform": "local_upload"},
        )
        self.assertEqual(focused.status_code, 200, focused.text)
        detail = self.client.get(
            f"/api/v1/videos/{self.result.video_id}",
            params={"platform": "local_upload"},
        ).json()
        query_hash = detail["focused_history"][0]["query_hash"]
        exported = self.client.get(
            f"/api/v1/videos/{self.result.video_id}/export.md",
            params={
                "platform": "local_upload",
                "view": "overview",
                "focus_query_hash": query_hash,
            },
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn("## 用户关注的问题", exported.text)
        self.assertIn("收费", exported.text)
        self.assertIn("## 字幕依据", exported.text)
        self.assertNotIn("## 完整字幕", exported.text)

        invalid = self.client.get(
            f"/api/v1/videos/{self.result.video_id}/export.md",
            params={
                "platform": "local_upload",
                "view": "overview",
                "focus_query_hash": "not-a-hash",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_INPUT")


class QuestionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "migration.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_v9_to_v10_failure_rolls_back_and_can_retry(self):
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE question_evidence")
            db.execute("DROP TABLE questions")
            db.execute("DROP TABLE conversations")
            db.execute("PRAGMA user_version=9")
            db.commit()

        def fail_after_first_table(db):
            db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected v10 failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v9_to_v10",
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
            self.assertNotIn("conversations", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)

        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertTrue(
                {"conversations", "questions", "question_evidence"}.issubset(tables)
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
