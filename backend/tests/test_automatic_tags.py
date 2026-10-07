from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import AutomaticTag, AutomaticTagging, Segment
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor
from backend.app.services.tagging import generate_automatic_tagging


class TextAsr:
    def transcribe(self, media_path: Path) -> list[Segment]:
        return [
            Segment(
                id="seg-1",
                start=0,
                end=2,
                text=media_path.read_text(encoding="utf-8"),
            )
        ]


class CountingTagger:
    generator_id = "counting"
    generator_version = "1"

    def __init__(self, name: str = "同名"):
        self.calls = 0
        self.name = name

    def generate(self, raw, clean, segments, extraction):
        self.calls += 1
        return [
            AutomaticTag(
                name=self.name,
                confidence=0.8,
                generation_method="deterministic",
            )
        ]


class FailingTagger(CountingTagger):
    def generate(self, raw, clean, segments, extraction):
        self.calls += 1
        raise RuntimeError("tagger unavailable")


class AutomaticTaggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _pipeline(self, tagger):
        return LocalFullPipeline(
            self.repo,
            TextAsr(),
            DeterministicFullExtractor(),
            auto_tagger=tagger,
        )

    def _save(self, pipeline, filename: str, text: str):
        path = self.root / filename
        path.write_text(text, encoding="utf-8")
        return pipeline.process(path)[1]

    def test_pipeline_generates_once_and_cached_video_reuses_result(self):
        tagger = CountingTagger("AI 编程")
        pipeline = self._pipeline(tagger)
        path = self.root / "cached.mp4"
        path.write_text("这段字幕讨论 AI 编程。", encoding="utf-8")

        first = pipeline.process(path)[1]
        second = pipeline.process(path)[1]

        self.assertEqual(tagger.calls, 1)
        self.assertEqual(first.automatic_tagging.status, "generated")
        self.assertEqual(
            second.automatic_tagging.model_dump(),
            first.automatic_tagging.model_dump(),
        )

    def test_no_transcript_skips_generator(self):
        tagger = CountingTagger()
        tagging = generate_automatic_tagging(
            tagger, "", "", [], DeterministicFullExtractor().extract("", "", [])[1]
        )
        self.assertEqual(tagging.status, "skipped_no_transcript")
        self.assertEqual(tagging.tags, [])
        self.assertEqual(tagger.calls, 0)

    def test_failure_is_warning_terminal_not_pipeline_failure(self):
        tagger = FailingTagger()
        pipeline = self._pipeline(tagger)
        result_job, result, _ = pipeline.process(
            self._write("failed.mp4", "字幕仍然可以阅读。")
        )
        self.assertEqual(result_job.status, "completed_with_warnings")
        self.assertEqual(result.automatic_tagging.status, "failed")
        self.assertTrue(result.automatic_tagging.warning)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_backfill_uses_stored_transcript_and_is_idempotent(self):
        tagger = CountingTagger()
        pipeline = self._pipeline(tagger)
        result = self._save(pipeline, "backfill.mp4", "这是一段已存字幕。")
        tagger.calls = 0
        with self.repo._connect() as db:
            db.execute(
                "DELETE FROM automatic_tag_runs WHERE resource_key=?",
                (f"local_upload:{result.video_id}",),
            )
        client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))

        first = client.post(
            f"/api/v1/videos/{result.video_id}/automatic-tags",
            json={"platform": "local_upload"},
        )
        second = client.post(
            f"/api/v1/videos/{result.video_id}/automatic-tags",
            json={"platform": "local_upload"},
        )

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(tagger.calls, 1)
        self.assertEqual(first.json()["automatic_tagging"]["status"], "generated")

    def test_three_tag_sources_filters_and_facets_do_not_collide(self):
        pipeline = self._pipeline(CountingTagger("自动默认"))
        records = [
            self._save(pipeline, "platform.mp4", "平台记录"),
            self._save(pipeline, "automatic.mp4", "自动记录"),
            self._save(pipeline, "personal.mp4", "个人记录"),
        ]
        platform_result = records[0].model_copy(update={"tags": ["同名"]})
        automatic_result = records[1].model_copy(
            update={
                "automatic_tagging": AutomaticTagging(
                    status="generated",
                    tags=[
                        AutomaticTag(
                            name="同名",
                            confidence=0.9,
                            generation_method="deterministic",
                        )
                    ],
                    generator_id="test",
                    generator_version="1",
                    transcript_hash="hash",
                    generated_at="now",
                )
            }
        )
        self.repo.save_result(platform_result)
        self.repo.save_result(automatic_result)
        client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))
        personal = records[2]
        response = client.post(
            f"/api/v1/videos/{personal.video_id}/tags",
            json={
                "operation": "add",
                "tags": ["同名"],
                "platform": "local_upload",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        expected = {
            "platform": records[0].video_id,
            "automatic": records[1].video_id,
            "personal": records[2].video_id,
        }
        for source, video_id in expected.items():
            page = client.get(
                "/api/v1/videos",
                params={"tag": "同名", "tag_source": source},
            )
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(
                [item["video_id"] for item in page.json()["items"]],
                [video_id],
            )
        unfiltered = client.get("/api/v1/videos", params={"limit": 1}).json()
        self.assertEqual(unfiltered["total"], 3)
        same_name_facets = [
            item
            for item in unfiltered["facets"]["tags"]
            if item["name"] == "同名"
        ]
        self.assertEqual(
            {item["source"] for item in same_name_facets},
            {"platform", "automatic", "personal"},
        )
        self.assertTrue(all(item["count"] == 1 for item in same_name_facets))

    def test_classification_persists_filters_and_facets_before_pagination(self):
        pipeline = self._pipeline(CountingTagger())
        first = self._save(pipeline, "one.mp4", "第一条")
        second = self._save(pipeline, "two.mp4", "第二条")
        client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))
        for result, secondary in ((first, "AI 编程"), (second, "自动化")):
            response = client.put(
                f"/api/v1/videos/{result.video_id}/classification",
                json={
                    "primary_category": "AI 与工具",
                    "secondary_category": secondary,
                    "platform": "local_upload",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        page = client.get(
            "/api/v1/videos",
            params={"primary_category": "AI 与工具", "limit": 1},
        ).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["facets"]["categories"][0]["count"], 2)
        self.assertEqual(
            {child["secondary_category"] for child in
             page["facets"]["categories"][0]["children"]},
            {"AI 编程", "自动化"},
        )
        filtered = client.get(
            "/api/v1/videos",
            params={
                "primary_category": "AI 与工具",
                "secondary_category": "AI 编程",
            },
        ).json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["video_id"], first.video_id)

    def test_v6_to_v7_failure_rolls_back_and_retries(self):
        path = self.root / "v6.sqlite3"
        SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            for table in (
                "video_automatic_tags",
                "automatic_tags",
                "automatic_tag_runs",
                "video_classifications",
            ):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=6")
            db.commit()

        original = SQLiteRepository._migrate_v6_to_v7

        def fail(db):
            db.execute(
                "CREATE TABLE automatic_tag_runs(resource_key TEXT PRIMARY KEY)"
            )
            raise RuntimeError("injected v7 failure")

        with patch.object(
            SQLiteRepository, "_migrate_v6_to_v7", staticmethod(fail)
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("automatic_tag_runs", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
        with patch.object(
            SQLiteRepository, "_migrate_v6_to_v7", staticmethod(original)
        ):
            SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
