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
from backend.app.domain.models import Segment
from backend.app.repositories.sqlite import SQLiteRepository, resource_key
from backend.app.services.focused import FocusedExtractionService
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
)


class CountingAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, media_path: Path) -> list[Segment]:
        self.calls += 1
        text = media_path.read_text(encoding="utf-8")
        return [Segment(id="seg-1", start=0, end=2, text=text)]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "notes.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        self.asr = CountingAsr()
        self.pipeline = LocalFullPipeline(
            self.repo,
            self.asr,
            DeterministicFullExtractor(),
            focused_extractor=DeterministicFocusedExtractor(),
        )
        self.client = TestClient(
            create_app(self.pipeline, upload_root=self.root / "uploads")
        )

    def tearDown(self):
        self.temp.cleanup()

    def _save(
        self,
        name: str,
        transcript: str,
        *,
        title: str = "",
        author: str = "",
        description: str = "",
        source_tags: list[str] | None = None,
        summary: str = "",
    ):
        path = self.root / name
        path.write_text(transcript, encoding="utf-8")
        result = self.pipeline.process(path)[1]
        result = result.model_copy(
            update={
                "title": title or result.title,
                "author": author,
                "description": description,
                "tags": source_tags or [],
                "summary": summary or result.summary,
            }
        )
        self.repo.save_result(result)
        return result, path

    def test_favorite_toggle_never_deletes_source_data(self):
        result, _ = self._save("favorite.mp4", "收费标准是每月100元。")
        FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(result.video_id, "收费", platform="local_upload")
        with closing(sqlite3.connect(self.db_path)) as db:
            before = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
        marked = self.client.post(
            f"/api/v1/videos/{result.video_id}/favorite",
            json={"favorite": True, "platform": "local_upload"},
        )
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertTrue(marked.json()["favorite"])
        unmarked = self.client.post(
            f"/api/v1/videos/{result.video_id}/favorite",
            json={"favorite": False, "platform": "local_upload"},
        )
        self.assertEqual(unmarked.status_code, 200, unmarked.text)
        self.assertFalse(unmarked.json()["favorite"])
        detail = self.client.get(
            f"/api/v1/videos/{result.video_id}?platform=local_upload"
        )
        self.assertEqual(detail.status_code, 200)
        with closing(sqlite3.connect(self.db_path)) as db:
            after = tuple(
                db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "videos",
                    "transcript_segments",
                    "extractions",
                    "claims",
                    "claim_evidence",
                )
            )
        self.assertEqual(after, before)

    def test_personal_tags_are_normalized_and_independent_from_source_tags(self):
        result, _ = self._save(
            "tags.mp4", "标签测试", source_tags=["平台标签"]
        )
        response = self.client.post(
            f"/api/v1/videos/{result.video_id}/tags",
            json={
                "operation": "add",
                "tags": [" AI ", "ＡＩ", "增长   方法"],
                "platform": "local_upload",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["personal_tags"], ["AI", "增长 方法"])
        self.assertEqual(payload["result"]["tags"], ["平台标签"])

        removed = self.client.post(
            f"/api/v1/videos/{result.video_id}/tags",
            json={
                "operation": "remove",
                "tags": ["ａｉ"],
                "platform": "local_upload",
            },
        )
        self.assertEqual(removed.json()["personal_tags"], ["增长 方法"])
        too_long = self.client.post(
            f"/api/v1/videos/{result.video_id}/tags",
            json={
                "operation": "add",
                "tags": ["x" * 65],
                "platform": "local_upload",
            },
        )
        self.assertEqual(too_long.status_code, 400)
        self.assertEqual(too_long.json()["error"]["code"], "INVALID_INPUT")
        too_many = self.client.post(
            f"/api/v1/videos/{result.video_id}/tags",
            json={
                "operation": "add",
                "tags": [f"t{i}" for i in range(51)],
                "platform": "local_upload",
            },
        )
        self.assertEqual(too_many.status_code, 422)

    def test_searches_all_fields_filters_and_treats_percent_underscore_literally(self):
        result, _ = self._save(
            "search.mp4",
            "原始字幕针 收费100元",
            title="标题针 100%_方法 Alpha  Beta",
            author="作者针",
            description="描述针",
            source_tags=["来源标签针"],
            summary="摘要针",
        )
        self._save("unrelated.mp4", "完全无关内容", title="普通视频")
        self.client.post(
            f"/api/v1/videos/{result.video_id}/tags",
            json={
                "operation": "add",
                "tags": ["个人标签针"],
                "platform": "local_upload",
            },
        )
        self.client.post(
            f"/api/v1/videos/{result.video_id}/favorite",
            json={"favorite": True, "platform": "local_upload"},
        )
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE videos SET clean_transcript=? WHERE resource_key=?",
                ("清洗字幕针", resource_key("local_upload", result.video_id)),
            )
            db.commit()

        for query in (
            "标题针",
            "作者针",
            "描述针",
            "来源标签针",
            "个人标签针",
            "原始字幕针",
            "清洗字幕针",
            "摘要针",
            "%_",
            "Ａｌｐｈａ　ＢＥＴＡ",
        ):
            response = self.client.get("/api/v1/videos", params={"query": query})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1, query)
        filtered = self.client.get(
            "/api/v1/videos",
            params={
                "platform": "local_upload",
                "tag": " 个人标签针 ",
                "favorite": "true",
            },
        )
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(
            self.client.get(
                "/api/v1/videos", params={"platform": "douyin"}
            ).json()["total"],
            0,
        )

    def test_search_pagination_has_unique_tie_breaker_and_compact_items(self):
        results = [
            self._save(f"{name}.mp4", f"字幕 {name}", title=name)[0]
            for name in ("c", "a", "b")
        ]
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE videos SET updated_at='same-time'")
            expected = [
                row[0].split(":", 1)[1]
                for row in db.execute(
                    "SELECT resource_key FROM videos ORDER BY resource_key ASC"
                ).fetchall()
            ]
            db.commit()
        pages = [
            self.client.get(
                "/api/v1/videos", params={"limit": 1, "offset": offset}
            ).json()
            for offset in range(3)
        ]
        self.assertEqual([page["items"][0]["video_id"] for page in pages], expected)
        self.assertTrue(all(page["total"] == 3 for page in pages))
        item = pages[0]["items"][0]
        for forbidden in (
            "raw_transcript",
            "clean_transcript",
            "segments",
            "evidence",
            "focused_answer",
            "resource_key",
        ):
            self.assertNotIn(forbidden, item)
        self.assertEqual(
            self.client.get(
                "/api/v1/videos", params={"limit": 0}
            ).status_code,
            422,
        )
        self.assertEqual(len(results), 3)

    def test_detail_and_history_are_stable_and_same_focus_is_idempotent(self):
        result, path = self._save(
            "history.mp4", "获客方法是先发案例。收费标准每月100元。"
        )
        calls_after_full = self.asr.calls
        path.unlink()
        first = self.client.post(
            f"/api/v1/videos/{result.video_id}/extractions",
            json={"focus_query": "获客方法", "platform": "local_upload"},
        )
        second = self.client.post(
            f"/api/v1/videos/{result.video_id}/extractions",
            json={"focus_query": "收费标准", "platform": "local_upload"},
        )
        repeated = self.client.post(
            f"/api/v1/videos/{result.video_id}/extractions",
            json={"focus_query": "  收费标准  ", "platform": "local_upload"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(self.asr.calls, calls_after_full)
        with closing(sqlite3.connect(self.db_path)) as db:
            count = db.execute(
                """SELECT COUNT(*) FROM extractions
                WHERE resource_key=? AND mode='focused'""",
                (resource_key("local_upload", result.video_id),),
            ).fetchone()[0]
        self.assertEqual(count, 2)

        detail = self.client.get(
            f"/api/v1/videos/{result.video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        payload = detail.json()
        self.assertEqual(payload["result"]["raw_transcript"], result.raw_transcript)
        self.assertEqual(
            [item["focus_query"] for item in payload["focused_history"]],
            ["收费标准", "获客方法"],
        )
        history = self.client.get(
            f"/api/v1/videos/{result.video_id}/extractions",
            params={"platform": "local_upload"},
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json(), payload["focused_history"])

    def test_markdown_export_and_stable_not_found_errors(self):
        result, _ = self._save("export.mp4", "导出字幕内容。", title="导出标题")
        exported = self.client.get(
            f"/api/v1/videos/{result.video_id}/export.md",
            params={"platform": "local_upload"},
        )
        self.assertEqual(exported.status_code, 200)
        self.assertIn("text/markdown", exported.headers["content-type"])
        self.assertIn("# 导出标题", exported.text)
        self.assertIn("导出字幕内容", exported.text)
        for method, path, kwargs in (
            ("get", "/api/v1/videos/missing", {}),
            (
                "post",
                "/api/v1/videos/missing/favorite",
                {"json": {"favorite": True}},
            ),
            (
                "post",
                "/api/v1/videos/missing/tags",
                {"json": {"operation": "add", "tags": ["x"]}},
            ),
        ):
            response = getattr(self.client, method)(path, **kwargs)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")


class LibraryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_database_and_repeated_migration_are_idempotent(self):
        path = self.root / "empty.sqlite3"
        SQLiteRepository(path)
        SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
            }
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("favorite", columns)
            self.assertTrue({"tags", "video_tags"}.issubset(tables))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v3_data_migrates_without_loss_and_can_migrate_again(self):
        path = self.root / "v3.sqlite3"
        payload = {
            "platform": "local_upload",
            "source_url": "local://v3",
            "canonical_url": "local://sha256/v3",
            "video_id": "v3",
            "author": "",
            "title": "v3 title",
            "description": "",
            "tags": [],
            "duration": 0,
            "cover_url": "",
            "subtitle_source": "none",
            "raw_transcript": "",
            "clean_transcript": "",
            "segments": [],
            "focus_query": "",
            "extraction_mode": "full",
            "summary": "",
            "full_extraction": {
                "key_points": [],
                "important_data": [],
                "cases_and_arguments": [],
                "steps": [],
                "risks": [],
                "quotes": [],
            },
            "evidence": [],
            "focused_answer": None,
            "warnings": ["字幕残缺"],
        }
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            SQLiteRepository._create_v3_schema(db)
            db.execute(
                """INSERT INTO videos
                (resource_key, platform, public_video_id, video_id, source_url,
                 title, metadata_json, subtitle_source, raw_transcript,
                 clean_transcript, warnings_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "local_upload:v3",
                    "local_upload",
                    "v3",
                    "v3",
                    "local://v3",
                    "v3 title",
                    json.dumps(payload, ensure_ascii=False),
                    "none",
                    "",
                    "",
                    '["字幕残缺"]',
                    "completed_with_warnings",
                    "old",
                    "old",
                ),
            )
            db.execute("PRAGMA user_version=3")
            db.commit()
        first = SQLiteRepository(path)
        second = SQLiteRepository(path)
        self.assertEqual(
            second.load_result("v3", platform="local_upload").title, "v3 title"
        )
        first.set_favorite("v3", True, platform="local_upload")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(
                db.execute(
                    "SELECT favorite FROM videos WHERE resource_key='local_upload:v3'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v3_failure_rolls_back_schema_data_and_can_retry(self):
        path = self.root / "v3-failure.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            SQLiteRepository._create_v3_schema(db)
            db.execute(
                """INSERT INTO videos
                (resource_key, platform, public_video_id, video_id, source_url,
                 title, metadata_json, subtitle_source, raw_transcript,
                 clean_transcript, warnings_json, status, created_at, updated_at)
                VALUES ('local_upload:sentinel', 'local_upload', 'sentinel',
                'sentinel', 'local://sentinel', 'sentinel title', '{}', 'none',
                '', '', '[]', 'completed', 'old', 'old')"""
            )
            db.execute("PRAGMA user_version=3")
            db.commit()

        def fail_after_alter(db):
            db.execute(
                "ALTER TABLE videos ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
            )
            raise RuntimeError("injected migration failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v3_to_v4",
            staticmethod(fail_after_alter),
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
            }
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("favorite", columns)
            self.assertNotIn("tags", tables)
            self.assertFalse(any(name.endswith("_v3") for name in tables))
            self.assertEqual(
                db.execute("SELECT title FROM videos").fetchone()[0],
                "sentinel title",
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)

        SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
            }
            self.assertIn("favorite", columns)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_legacy_failure_rolls_back_table_swap_and_can_retry(self):
        path = self.root / "legacy-failure.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(
                """
                CREATE TABLE videos (
                    video_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
                    source_url TEXT NOT NULL, title TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, subtitle_source TEXT NOT NULL,
                    raw_transcript TEXT NOT NULL, clean_transcript TEXT NOT NULL,
                    warnings_json TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE transcript_segments (
                    video_id TEXT NOT NULL, position INTEGER NOT NULL,
                    segment_id TEXT NOT NULL, start_time REAL, end_time REAL,
                    text TEXT NOT NULL, PRIMARY KEY(video_id, position),
                    UNIQUE(video_id, segment_id)
                );
                CREATE TABLE extractions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL,
                    mode TEXT NOT NULL, focus_query TEXT NOT NULL,
                    query_hash TEXT, result_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE claims (
                    id TEXT NOT NULL, extraction_id INTEGER NOT NULL,
                    claim TEXT NOT NULL, claim_type TEXT NOT NULL,
                    evidence TEXT NOT NULL, start_time REAL, end_time REAL,
                    confidence REAL NOT NULL, PRIMARY KEY(id, extraction_id)
                );
                CREATE TABLE claim_evidence (
                    extraction_id INTEGER NOT NULL, claim_id TEXT NOT NULL,
                    video_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                    PRIMARY KEY(extraction_id, claim_id, segment_id)
                );
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, progress INTEGER NOT NULL,
                    error_code TEXT, message TEXT NOT NULL, video_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO videos VALUES (
                    'legacy', 'local_upload', 'local://legacy', 'legacy title',
                    '{}', 'none', '', '', '[]', 'completed', 'old', 'old'
                );
                """
            )

        def fail_v4(_):
            raise RuntimeError("injected after legacy swap")

        with patch.object(
            SQLiteRepository, "_migrate_v3_to_v4", staticmethod(fail_v4)
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
            }
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertNotIn("resource_key", columns)
            self.assertFalse(any(name.endswith("_v3") for name in tables))
            self.assertEqual(
                db.execute("SELECT title FROM videos").fetchone()[0],
                "legacy title",
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

        SQLiteRepository(path)
        with closing(sqlite3.connect(path)) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(videos)").fetchall()
            }
            self.assertTrue({"resource_key", "favorite"}.issubset(columns))
            self.assertEqual(
                db.execute("SELECT title FROM videos").fetchone()[0],
                "legacy title",
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
