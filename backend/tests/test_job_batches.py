from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import Job, Segment
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.batches import JobBatchService
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import DeterministicFullExtractor


class NoopAsr:
    def transcribe(self, media_path: Path) -> list[Segment]:
        raise AssertionError("creating a batch must not invoke ASR")


class JobBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "notes.sqlite3"
        self.repo = SQLiteRepository(self.path)
        self.pipeline = LocalFullPipeline(
            self.repo,
            NoopAsr(),
            DeterministicFullExtractor(),
        )
        self.client = TestClient(
            create_app(self.pipeline, upload_root=self.root / "uploads")
        )

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def _job(
        self,
        job_id: str,
        *,
        status: str = "queued",
        progress: int = 0,
        retryable: bool = False,
        retry_count: int = 0,
        max_retries: int = 2,
        cancel_requested: bool = False,
    ) -> Job:
        job = Job(
            id=job_id,
            status=status,
            progress=progress,
            retryable=retryable,
            retry_count=retry_count,
            max_retries=max_retries,
            cancel_requested=cancel_requested,
        )
        self.assertTrue(self.repo.save_job(job))
        return job

    @staticmethod
    def _item(
        job_id: str,
        title: str | None = None,
        platform: str = "bilibili",
    ) -> dict:
        return {
            "job_id": job_id,
            "title": title or f"视频 {job_id}",
            "platform": platform,
        }

    def _create(self, *job_ids: str):
        return self.client.post(
            "/api/v1/job-batches",
            json={"items": [self._item(job_id) for job_id in job_ids]},
        )

    def _table_counts(self) -> tuple[int, int]:
        with closing(sqlite3.connect(self.path)) as db:
            batches = db.execute("SELECT COUNT(*) FROM job_batches").fetchone()[0]
            items = db.execute("SELECT COUNT(*) FROM job_batch_items").fetchone()[0]
        return batches, items

    def test_create_persists_existing_eligible_jobs_without_creating_jobs(self):
        self._job("queued")
        self._job("retryable", status="failed", progress=60, retryable=True)
        with closing(sqlite3.connect(self.path)) as db:
            before = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        response = self._create("queued", "retryable")

        self.assertEqual(response.status_code, 201, response.text)
        batch = response.json()
        self.assertEqual(batch["total"], 2)
        self.assertEqual(batch["active"], 1)
        self.assertEqual(batch["failed"], 1)
        self.assertEqual(batch["status"], "running")
        self.assertNotIn("progress", batch)
        self.assertEqual(
            [item["job"]["id"] for item in batch["items"]],
            ["queued", "retryable"],
        )
        with closing(sqlite3.connect(self.path)) as db:
            after = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(after, before)

        restored = SQLiteRepository(self.path).get_job_batch(batch["id"])
        self.assertIsNotNone(restored)
        self.assertEqual([item.job.id for item in restored.items], ["queued", "retryable"])

    def test_local_upload_job_can_join_a_batch_without_a_public_source_url(self):
        local_job = self._job("local-upload")
        platform_job = self._job("platform-link", status="resolving", progress=10)

        response = self.client.post(
            "/api/v1/job-batches",
            json={
                "items": [
                    self._item(
                        local_job.id,
                        "本地上传教程.mp4",
                        platform="local_upload",
                    ),
                    self._item(platform_job.id, "平台视频"),
                ]
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        batch = response.json()
        self.assertEqual(
            [
                (item["job"]["id"], item["platform"])
                for item in batch["items"]
            ],
            [
                ("local-upload", "local_upload"),
                ("platform-link", "bilibili"),
            ],
        )
        self.assertEqual(batch["total"], 2)
        self.assertEqual(batch["active"], 2)

    def test_rejects_size_duplicates_unknown_and_disallowed_atomically(self):
        service = JobBatchService(self.repo)
        self._job("one")
        self._job("two")
        with self.assertRaises(PipelineError) as too_small:
            service.create([("one", "一", "bilibili")])
        self.assertEqual(too_small.exception.code, "INVALID_INPUT")
        with self.assertRaises(PipelineError) as too_large:
            service.create(
                [(f"job-{index}", str(index), "bilibili") for index in range(11)]
            )
        self.assertEqual(too_large.exception.code, "BATCH_LIMIT_EXCEEDED")
        with self.assertRaises(PipelineError) as duplicate:
            service.create(
                [("one", "一", "bilibili"), ("one", "重复", "bilibili")]
            )
        self.assertEqual(duplicate.exception.code, "DUPLICATE_BATCH_ITEM")

        unknown = self._create("one", "missing")
        self.assertEqual(unknown.status_code, 404, unknown.text)
        self.assertEqual(unknown.json()["error"]["code"], "BATCH_JOB_NOT_FOUND")
        self.assertEqual(self._table_counts(), (0, 0))

        disallowed_cases = (
            ("completed", "completed", 100, False, 0, 2, False),
            ("warning", "completed_with_warnings", 100, False, 0, 2, False),
            ("cancelled", "cancelled", 0, False, 0, 2, True),
            ("hard-failed", "failed", 40, False, 0, 2, False),
            ("retry-limit", "failed", 40, True, 2, 2, False),
            ("cancelling", "resolving", 10, False, 0, 2, True),
        )
        for values in disallowed_cases:
            self._job(
                values[0],
                status=values[1],
                progress=values[2],
                retryable=values[3],
                retry_count=values[4],
                max_retries=values[5],
                cancel_requested=values[6],
            )
            response = self._create("two", values[0])
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(
                response.json()["error"]["code"], "BATCH_ITEM_NOT_ALLOWED"
            )
            self.assertEqual(self._table_counts(), (0, 0))

    def test_member_insert_failure_rolls_back_batch_and_all_members(self):
        self._job("valid")
        self._job("invalid-platform")

        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.create_job_batch(
                "atomic-failure",
                [
                    ("valid", "有效项", "bilibili"),
                    ("invalid-platform", "无效平台项", "unsupported"),
                ],
            )

        self.assertEqual(self._table_counts(), (0, 0))

    def test_status_is_derived_from_live_jobs_and_recent_batches_restore(self):
        first = self._job("first")
        second = self._job("second")
        created = self._create(first.id, second.id).json()
        self.assertEqual(created["status"], "queued")

        first.status = "resolving"
        first.progress = 10
        self.assertTrue(self.repo.save_job(first))
        running = self.client.get(
            f"/api/v1/job-batches/{created['id']}"
        ).json()
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["active"], 2)

        first.status = "completed"
        first.progress = 100
        self.assertTrue(self.repo.save_job(first))
        second.status = "completed_with_warnings"
        second.progress = 100
        self.assertTrue(self.repo.save_job(second))
        completed = SQLiteRepository(self.path).get_job_batch(created["id"])
        self.assertEqual(completed.status, "completed_with_issues")
        self.assertEqual(completed.completed, 2)
        self.assertEqual(completed.active, 0)

        self._job("third")
        self._job("fourth")
        latest = self._create("third", "fourth").json()
        listed = self.client.get("/api/v1/job-batches", params={"limit": 1})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([item["id"] for item in listed.json()], [latest["id"]])

    def test_cancel_validates_membership_and_isolates_each_job(self):
        first = self._job("first")
        second = self._job("second")
        self._job("outsider")
        batch = self._create(first.id, second.id).json()

        outsider = self.client.post(
            f"/api/v1/job-batches/{batch['id']}/jobs/outsider/cancel"
        )
        self.assertEqual(outsider.status_code, 404, outsider.text)
        self.assertEqual(
            outsider.json()["error"]["code"], "BATCH_JOB_NOT_MEMBER"
        )
        self.assertEqual(self.repo.get_job("outsider").status, "queued")

        first.status = "completed"
        first.progress = 100
        self.assertTrue(self.repo.save_job(first))
        terminal = self.client.post(
            f"/api/v1/job-batches/{batch['id']}/jobs/{first.id}/cancel"
        )
        self.assertEqual(terminal.status_code, 200, terminal.text)
        self.assertEqual(self.repo.get_job(first.id).status, "completed")
        self.assertEqual(self.repo.get_job(second.id).status, "queued")

        cancelled = self.client.post(
            f"/api/v1/job-batches/{batch['id']}/jobs/{second.id}/cancel"
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.repo.get_job(second.id).status, "cancelled")
        self.assertEqual(cancelled.json()["status"], "completed_with_issues")
        self.assertEqual(cancelled.json()["cancelled"], 1)

    def test_unknown_batch_is_not_found(self):
        response = self.client.get("/api/v1/job-batches/missing")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")


class JobBatchMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v7.sqlite3"
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TABLE job_batch_items")
            db.execute("DROP TABLE job_batches")
            db.execute("PRAGMA user_version=7")
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_v7_upgrade_is_idempotent_and_foreign_keys_are_valid(self):
        SQLiteRepository(self.path)
        SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue({"job_batches", "job_batch_items"}.issubset(tables))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v7_upgrade_rolls_back_completely_and_can_retry(self):
        original = SQLiteRepository._migrate_v7_to_v8

        def fail_midway(db):
            db.execute(
                "CREATE TABLE job_batches(id TEXT PRIMARY KEY, created_at TEXT)"
            )
            raise RuntimeError("injected v8 migration failure")

        with patch.object(
            SQLiteRepository,
            "_migrate_v7_to_v8",
            staticmethod(fail_midway),
        ):
            with self.assertRaises(RuntimeError):
                SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("job_batches", tables)
            self.assertNotIn("job_batch_items", tables)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)

        with patch.object(
            SQLiteRepository,
            "_migrate_v7_to_v8",
            staticmethod(original),
        ):
            SQLiteRepository(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
