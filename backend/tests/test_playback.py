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
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import DeterministicFullExtractor


class CountingAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, _media_path: Path):
        self.calls += 1
        return [Segment(id="seg-1", start=0, end=1, text="第一步准备。")]


class FailingAsr:
    def transcribe(self, _media_path: Path):
        raise RuntimeError("offline")


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")
        self.asr = CountingAsr()
        self.media_root = self.root / "retained"
        self.pipeline = LocalFullPipeline(
            self.repo,
            self.asr,
            DeterministicFullExtractor(),
            temp_root=self.root / "pipeline-tmp",
            retained_media_root=self.media_root,
        )
        self.client = TestClient(
            create_app(self.pipeline, upload_root=self.root / "uploads")
        )

    def tearDown(self):
        self.temp.cleanup()

    def _upload(
        self,
        *,
        name: str = "lesson.mp4",
        content: bytes = b"0123456789",
        retain: bool = False,
        endpoint: str = "/api/v1/uploads",
        content_type: str = "application/x-msdownload",
    ):
        response = self.client.post(
            endpoint,
            data={"retain_media": "true" if retain else "false"},
            files={"file": (name, content, content_type)},
        )
        self.assertIn(response.status_code, {200, 202}, response.text)
        return response.json()

    def test_default_upload_does_not_retain_media(self):
        payload = self._upload()
        detail = self.client.get(
            f"/api/v1/videos/{payload['result']['video_id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["playback"]["reason"], "not_retained")
        self.assertEqual(list(self.media_root.iterdir()), [])
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM retained_media").fetchone()[0], 0)

    def test_opt_in_upload_exposes_safe_full_and_single_range_streams(self):
        payload = self._upload(retain=True)
        video_id = payload["result"]["video_id"]
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        )
        playback = detail.json()["playback"]
        self.assertEqual(playback["availability"], "available")
        self.assertEqual(playback["mime_type"], "video/mp4")
        self.assertNotIn(str(self.root), detail.text)
        self.assertNotIn("lesson.mp4", playback["stream_url"])

        full = self.client.get(playback["stream_url"])
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(full.content, b"0123456789")
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(full.headers["content-length"], "10")
        self.assertEqual(full.headers["cache-control"], "private, no-store")
        self.assertEqual(full.headers["x-content-type-options"], "nosniff")
        self.assertEqual(full.headers["content-type"], "video/mp4")

        cases = {
            "bytes=2-5": (b"2345", "bytes 2-5/10"),
            "bytes=6-": (b"6789", "bytes 6-9/10"),
            "bytes=-3": (b"789", "bytes 7-9/10"),
        }
        for header, (expected, content_range) in cases.items():
            with self.subTest(header=header):
                response = self.client.get(
                    playback["stream_url"], headers={"Range": header}
                )
                self.assertEqual(response.status_code, 206, response.text)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.headers["content-range"], content_range)
                self.assertEqual(
                    response.headers["content-length"], str(len(expected))
                )

        for header in (
            "bytes=20-30",
            "bytes=7-2",
            "bytes=0-1,3-4",
            "bytes=-0",
            "items=0-1",
            "bytes=a-b",
        ):
            with self.subTest(invalid=header):
                response = self.client.get(
                    playback["stream_url"], headers={"Range": header}
                )
                self.assertEqual(response.status_code, 416, response.text)
                self.assertEqual(response.headers["content-range"], "bytes */10")
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_cached_reupload_can_opt_in_without_running_asr_again(self):
        first = self._upload(retain=False)
        self.assertEqual(self.asr.calls, 1)
        second = self._upload(retain=True)
        self.assertEqual(second["result"]["video_id"], first["result"]["video_id"])
        self.assertEqual(self.asr.calls, 1)
        detail = self.client.get(
            f"/api/v1/videos/{first['result']['video_id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.json()["playback"]["availability"], "available")

        self._upload(retain=False)
        detail = self.client.get(
            f"/api/v1/videos/{first['result']['video_id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.json()["playback"]["availability"], "available")

    def test_job_first_upload_can_retain_media(self):
        job = self._upload(retain=True, endpoint="/api/v1/upload-jobs")
        persisted = self.client.get(f"/api/v1/jobs/{job['id']}").json()
        self.assertIn(persisted["status"], {"completed", "completed_with_warnings"})
        detail = self.client.get(
            f"/api/v1/videos/{persisted['video_id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.json()["playback"]["availability"], "available")
        self.assertEqual(list((self.root / "uploads").iterdir()), [])
        self.assertEqual(list((self.root / "pipeline-tmp").iterdir()), [])

    def test_failed_or_cancelled_processing_does_not_retain_media(self):
        failed_root = self.root / "failed-retained"
        failed_pipeline = LocalFullPipeline(
            self.repo,
            FailingAsr(),
            DeterministicFullExtractor(),
            temp_root=self.root / "failed-tmp",
            retained_media_root=failed_root,
        )
        failed_client = TestClient(
            create_app(failed_pipeline, upload_root=self.root / "failed-uploads")
        )
        failed = failed_client.post(
            "/api/v1/uploads",
            data={"retain_media": "true"},
            files={"file": ("failed.mp4", b"failed", "video/mp4")},
        )
        self.assertEqual(failed.status_code, 400, failed.text)
        self.assertEqual(failed.json()["error"]["code"], "ASR_FAILED")
        self.assertEqual(list(failed_root.iterdir()), [])

        cancelled_root = self.root / "cancelled-retained"
        cancelled_pipeline = LocalFullPipeline(
            self.repo,
            CountingAsr(),
            DeterministicFullExtractor(),
            temp_root=self.root / "cancelled-tmp",
            retained_media_root=cancelled_root,
        )
        source = self.root / "cancelled.mp4"
        source.write_bytes(b"cancelled")
        with self.assertRaises(PipelineError) as caught:
            cancelled_pipeline.process(
                source,
                cancel_requested=lambda: True,
                retain_media=True,
            )
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.assertEqual(list(cancelled_root.iterdir()), [])
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM retained_media").fetchone()[0], 0)

    def test_unsupported_browser_format_is_retained_but_not_streamed(self):
        payload = self._upload(name="lesson.mkv", retain=True)
        video_id = payload["result"]["video_id"]
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        )
        playback = detail.json()["playback"]
        self.assertEqual(playback["availability"], "unavailable")
        self.assertEqual(playback["kind"], "retained_local")
        self.assertEqual(playback["reason"], "unsupported_browser_format")
        stream = self.client.get(
            f"/api/v1/videos/{video_id}/media",
            params={"platform": "local_upload"},
        )
        self.assertEqual(stream.status_code, 400, stream.text)
        self.assertEqual(stream.json()["error"]["code"], "MEDIA_UNAVAILABLE")

    def test_platform_source_never_exposes_or_proxies_media(self):
        payload = self._upload()
        local = self.repo.load_result(
            payload["result"]["video_id"], platform="local_upload"
        )
        platform_result = local.model_copy(
            update={
                "platform": "bilibili",
                "video_id": "BV1playback",
                "source_url": "https://www.bilibili.com/video/BV1playback",
                "canonical_url": "https://www.bilibili.com/video/BV1playback",
            }
        )
        self.repo.save_result(platform_result)
        detail = self.client.get(
            "/api/v1/videos/BV1playback", params={"platform": "bilibili"}
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["playback"]["reason"], "unsupported_source")
        self.assertEqual(detail.json()["playback"]["stream_url"], "")
        stream = self.client.get(
            "/api/v1/videos/BV1playback/media", params={"platform": "bilibili"}
        )
        self.assertEqual(stream.status_code, 400, stream.text)
        self.assertEqual(stream.json()["error"]["code"], "MEDIA_UNAVAILABLE")

    def test_delete_is_idempotent_and_preserves_video_knowledge(self):
        payload = self._upload(retain=True)
        video_id = payload["result"]["video_id"]
        self.client.post(
            f"/api/v1/videos/{video_id}/favorite",
            json={"favorite": True, "platform": "local_upload"},
        )
        self.client.post(
            f"/api/v1/videos/{video_id}/tags",
            json={
                "operation": "add",
                "tags": ["教程"],
                "platform": "local_upload",
            },
        )
        self.client.put(
            f"/api/v1/videos/{video_id}/classification",
            json={
                "primary_category": "知识",
                "secondary_category": "教程",
                "platform": "local_upload",
            },
        )
        self.client.put(
            f"/api/v1/videos/{video_id}/spark",
            json={
                "content": "保留这个想法",
                "author": "我",
                "platform": "local_upload",
            },
        )
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        ).json()
        target = detail["annotation_targets"][0]["target_key"]
        self.client.post(
            f"/api/v1/videos/{video_id}/annotations",
            json={
                "target_key": target,
                "content": "个人批注",
                "author": "我",
                "platform": "local_upload",
            },
        )
        tables = (
            "videos",
            "transcript_segments",
            "extractions",
            "claims",
            "video_tags",
            "automatic_tag_runs",
            "video_classifications",
            "video_sparks",
            "video_annotations",
            "jobs",
        )
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            before = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

        first = self.client.delete(
            f"/api/v1/videos/{video_id}/media",
            params={"platform": "local_upload"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["reason"], "not_retained")
        second = self.client.delete(
            f"/api/v1/videos/{video_id}/media",
            params={"platform": "local_upload"},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["reason"], "not_retained")
        self.assertEqual(list(self.media_root.iterdir()), [])
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            after = {
                table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }
            self.assertEqual(db.execute("SELECT COUNT(*) FROM retained_media").fetchone()[0], 0)
        self.assertEqual(after, before)

    def test_corrupt_storage_key_is_fail_closed_and_path_is_not_exposed(self):
        payload = self._upload(retain=True)
        video_id = payload["result"]["video_id"]
        outside = self.root / "outside.txt"
        outside.write_bytes(b"secret")
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            db.execute(
                "UPDATE retained_media SET storage_key='../outside.txt'"
            )
            db.commit()
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["playback"]["reason"], "missing_file")
        self.assertNotIn(str(outside), detail.text)
        stream = self.client.get(
            f"/api/v1/videos/{video_id}/media",
            params={"platform": "local_upload"},
        )
        self.assertEqual(stream.status_code, 400, stream.text)
        self.assertEqual(outside.read_bytes(), b"secret")

    def test_missing_owned_file_is_reported_and_stale_record_can_be_deleted(self):
        payload = self._upload(retain=True)
        video_id = payload["result"]["video_id"]
        retained_files = list(self.media_root.iterdir())
        self.assertEqual(len(retained_files), 1)
        retained_files[0].unlink()
        detail = self.client.get(
            f"/api/v1/videos/{video_id}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.json()["playback"]["reason"], "missing_file")
        self.assertEqual(detail.json()["playback"]["kind"], "retained_local")
        deleted = self.client.delete(
            f"/api/v1/videos/{video_id}/media",
            params={"platform": "local_upload"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["reason"], "not_retained")
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM retained_media").fetchone()[0], 0)


class PlaybackMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v8.sqlite3"
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE retained_media")
            db.execute("PRAGMA user_version=8")
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_v8_upgrade_is_idempotent_and_foreign_keys_are_valid(self):
        SQLiteRepository(self.path)
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_media'"
                ).fetchone()
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v8_upgrade_rolls_back_completely_and_can_retry(self):
        original = SQLiteRepository._migrate_v8_to_v9

        def fail_midway(db):
            db.execute(
                "CREATE TABLE retained_media(resource_key TEXT PRIMARY KEY)"
            )
            raise RuntimeError("injected v9 failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v8_to_v9",
            staticmethod(fail_midway),
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retained_media'"
                ).fetchone()
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 8)

        with patch.object(
            SQLiteRepository,
            "_migrate_v8_to_v9",
            staticmethod(original),
        ):
            SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
