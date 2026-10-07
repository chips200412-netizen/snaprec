from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import httpx
import uvicorn

from backend.app.api.main import create_app
from backend.app.domain.models import Job, Segment
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import DeterministicFullExtractor
from backend.app.services.recovery import (
    JobRecoveryService,
    TEMP_MARKER,
    TemporaryMediaService,
)


class CountingAsr:
    def __init__(self, *, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def transcribe(self, media_path: Path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return [Segment(id="seg-1", start=0, end=1, text="真实字幕。")]


class BlockingAsr:
    def __init__(self):
        self.started = Event()
        self.release = Event()

    def transcribe(self, media_path: Path):
        self.started.set()
        self.release.wait(timeout=5)
        return [Segment(id="seg-1", start=0, end=1, text="真实字幕。")]


class FakeResolution:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository
        self.calls = 0

    def process(self, input_text: str, focus_query: str = "", job=None):
        self.calls += 1
        job.status = "completed"
        job.progress = 100
        job.checkpoint = "result_saved"
        self.repository.save_job(job)
        return job, None, ""


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")
        self.asr = CountingAsr()
        self.pipeline = LocalFullPipeline(
            self.repo,
            self.asr,
            DeterministicFullExtractor(),
            temp_root=self.root / "jobs",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_job_progress_is_monotonic_and_terminal_is_immutable(self):
        job = Job(id="state", status="queued", progress=0)
        self.repo.save_job(job)
        job.status, job.progress = "transcribing", 45
        self.repo.save_job(job)
        job.status, job.progress = "cleaning", 40
        self.repo.save_job(job)
        stale = self.repo.get_job(job.id)
        self.assertEqual(stale.progress, 45)
        self.assertEqual(stale.status, "transcribing")
        job.status, job.progress = "completed", 100
        self.repo.save_job(job)
        job.status, job.error_code = "failed", "EXTRACTION_FAILED"
        self.repo.save_job(job)
        persisted = self.repo.get_job(job.id)
        self.assertEqual(persisted.status, "completed")
        self.assertIsNone(persisted.error_code)

    def test_startup_recovery_is_persisted_and_idempotent(self):
        job = Job(id="zombie", status="extracting", progress=75)
        self.repo.save_job(job)
        self.repo.configure_job_request(
            job.id, "resolution", {"input_text": "https://example.invalid"}
        )
        service = JobRecoveryService(
            self.repo,
            self.pipeline,
            None,
            sleeper=lambda _: None,
            clock=lambda: datetime.now(UTC) + timedelta(minutes=10),
        )
        recovered = service.recover_startup()
        self.assertEqual([item.id for item in recovered], ["zombie"])
        persisted = self.repo.get_job(job.id)
        self.assertEqual(persisted.status, "failed")
        self.assertEqual(persisted.error_code, "PROCESS_INTERRUPTED")
        self.assertTrue(persisted.retryable)
        self.assertEqual(service.recover_startup(), [])

    def test_startup_recovery_does_not_fail_fresh_worker(self):
        job = Job(id="fresh", status="extracting", progress=75)
        self.repo.save_job(job)
        service = JobRecoveryService(
            self.repo,
            self.pipeline,
            None,
            sleeper=lambda _: None,
            heartbeat_timeout_seconds=300,
        )
        self.assertEqual(service.recover_startup(), [])
        self.assertEqual(self.repo.get_job(job.id).status, "extracting")

    def test_cancel_api_persists_terminal_state_and_blocks_late_worker_update(self):
        client = TestClient(
            create_app(
                self.pipeline,
                upload_root=self.root / "uploads",
                orphan_max_age_seconds=999999,
            )
        )
        job = Job(id="cancel-me", status="transcribing", progress=45)
        self.repo.save_job(job)
        response = client.post(f"/api/v1/jobs/{job.id}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["error_code"], "CANCELLED")
        job.status, job.progress = "extracting", 75
        self.repo.save_job(job)
        self.assertEqual(self.repo.get_job(job.id).status, "cancelled")

    def test_resolution_retry_has_bounded_backoff_and_attempt_count(self):
        fake = FakeResolution(self.repo)
        delays = []
        service = JobRecoveryService(
            self.repo,
            self.pipeline,
            fake,
            sleeper=delays.append,
            base_delay_seconds=2,
        )
        job = Job(
            id="retry-link",
            status="failed",
            progress=30,
            error_code="CONTENT_UNAVAILABLE",
            retryable=True,
        )
        self.repo.save_job(job)
        self.repo.configure_job_request(
            job.id,
            "resolution",
            {
                "source_url": "https://www.bilibili.com/video/BV1abc123",
                "focused": False,
            },
        )
        retried = service.retry(job.id)
        self.assertEqual(delays, [2])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(retried.retry_count, 1)
        self.assertEqual(retried.status, "completed")
        with self.assertRaises(PipelineError) as caught:
            service.retry(job.id)
        self.assertEqual(caught.exception.code, "RETRY_NOT_ALLOWED")

        exhausted = Job(
            id="exhausted",
            status="failed",
            progress=10,
            retry_count=2,
            max_retries=2,
            retryable=True,
        )
        self.repo.save_job(exhausted)
        with self.assertRaises(PipelineError) as caught:
            service.retry(exhausted.id)
        self.assertEqual(caught.exception.code, "RETRY_LIMIT_EXCEEDED")

    def test_retry_api_uses_persisted_request_and_returns_terminal_job(self):
        fake = FakeResolution(self.repo)
        delays = []
        client = TestClient(
            create_app(
                self.pipeline,
                upload_root=self.root / "uploads",
                resolution_service=fake,
                retry_sleeper=delays.append,
                retry_base_delay_seconds=3,
            )
        )
        job = Job(
            id="api-retry",
            status="failed",
            progress=20,
            error_code="CONTENT_UNAVAILABLE",
            retryable=True,
        )
        self.repo.save_job(job)
        self.repo.configure_job_request(
            job.id,
            "resolution",
            {
                "source_url": "https://www.bilibili.com/video/BV1abc123",
                "focused": False,
            },
        )
        response = client.post(f"/api/v1/jobs/{job.id}/retry")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["retry_count"], 1)
        self.assertEqual(delays, [3])

    def test_retry_claim_is_single_winner_under_concurrency(self):
        fake = FakeResolution(self.repo)
        service = JobRecoveryService(
            self.repo, self.pipeline, fake, sleeper=lambda _: None
        )
        job = Job(
            id="concurrent-retry",
            status="failed",
            progress=20,
            error_code="CONTENT_UNAVAILABLE",
            retryable=True,
        )
        self.repo.save_job(job)
        self.repo.configure_job_request(
            job.id,
            "resolution",
            {
                "source_url": "https://www.bilibili.com/video/BV1abc123",
                "focused": False,
            },
        )

        def retry_once():
            try:
                return service.retry(job.id).status
            except PipelineError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: retry_once(), range(2)))
        self.assertEqual(outcomes.count("completed"), 1)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self.repo.get_job(job.id).retry_count, 1)

    def test_cancel_before_atomic_result_commit_leaves_no_result(self):
        media = self.root / "atomic.mp4"
        media.write_bytes(b"atomic")
        _, existing, _ = self.pipeline.process(media)
        result = existing.model_copy(
            update={"video_id": "atomic-race", "canonical_url": "local://atomic-race"}
        )
        job = Job(id="atomic-job", status="queued", progress=0)
        self.repo.save_job(job)
        job.status, job.progress = "saving", 90
        self.assertTrue(self.repo.save_job(job))
        cancelled = self.repo.request_job_cancel(job.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertFalse(self.repo.commit_job_result(job, result))
        self.assertIsNone(
            self.repo.load_result("atomic-race", platform="local_upload")
        )

    def test_resolution_job_first_returns_persistent_job_id(self):
        fake = FakeResolution(self.repo)
        client = TestClient(
            create_app(
                self.pipeline,
                upload_root=self.root / "uploads",
                resolution_service=fake,
                retry_sleeper=lambda _: None,
            )
        )
        response = client.post(
            "/api/v1/resolution-jobs",
            json={
                "input_text": "https://www.bilibili.com/video/BV1abc123",
                "focus_query": "",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        self.assertEqual(self.repo.get_job(job_id).status, "completed")

    def test_upload_job_first_and_real_cancel_race(self):
        blocking = BlockingAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            blocking,
            DeterministicFullExtractor(),
            temp_root=self.root / "blocking-jobs",
        )
        media = self.root / "cancel-race.mp4"
        media.write_bytes(b"media")
        job = Job(id="upload-job-id", status="queued", progress=0)
        self.repo.save_job(job)
        outcome = []

        def run():
            try:
                pipeline.process(media, job=job)
            except PipelineError as exc:
                outcome.append(exc.code)

        worker = Thread(target=run)
        worker.start()
        self.assertTrue(blocking.started.wait(timeout=5))
        client = TestClient(
            create_app(
                pipeline,
                upload_root=self.root / "uploads",
                orphan_max_age_seconds=999999,
            )
        )
        response = client.post(f"/api/v1/jobs/{job.id}/cancel")
        self.assertEqual(response.status_code, 200, response.text)
        blocking.release.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["CANCELLED"])
        self.assertEqual(self.repo.get_job(job.id).status, "cancelled")
        self.assertIsNone(self.repo.get_result(job.video_id or "missing"))

        normal = TestClient(
            create_app(
                self.pipeline,
                upload_root=self.root / "normal-uploads",
                orphan_max_age_seconds=999999,
            )
        ).post(
            "/api/v1/upload-jobs",
            files={"file": ("ok.mp4", b"media", "video/mp4")},
        )
        self.assertEqual(normal.status_code, 202, normal.text)
        self.assertIsNotNone(self.repo.get_job(normal.json()["id"]))

    def test_upload_job_first_returns_before_blocking_asr_over_real_http(self):
        blocking = BlockingAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            blocking,
            DeterministicFullExtractor(),
            temp_root=self.root / "http-jobs",
        )
        app = create_app(
            pipeline,
            upload_root=self.root / "http-uploads",
            orphan_max_age_seconds=999999,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        thread = Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.started)
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5
            ) as client:
                started = time.monotonic()
                response = client.post(
                    "/api/v1/upload-jobs",
                    files={"file": ("blocked.mp4", b"media", "video/mp4")},
                )
                elapsed = time.monotonic() - started
                self.assertEqual(response.status_code, 202, response.text)
                self.assertLess(elapsed, 1.0)
                job_id = response.json()["id"]
                self.assertTrue(blocking.started.wait(timeout=5))
                cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertEqual(cancelled.json()["status"], "cancelled")
                blocking.release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    persisted = self.repo.get_job(job_id)
                    if persisted is not None and persisted.status == "cancelled":
                        break
                    time.sleep(0.02)
                persisted = self.repo.get_job(job_id)
                self.assertEqual(persisted.status, "cancelled")
                self.assertIsNone(
                    self.repo.load_result(
                        persisted.video_id or "missing",
                        platform="local_upload",
                    )
                )
        finally:
            blocking.release.set()
            server.should_exit = True
            thread.join(timeout=5)

    def test_local_retry_requires_real_saved_result_checkpoint(self):
        missing = Job(
            id="missing-media",
            status="failed",
            progress=45,
            error_code="PROCESS_INTERRUPTED",
            retryable=True,
            video_id="missing",
            checkpoint="transcribing",
        )
        self.repo.save_job(missing)
        self.repo.configure_job_request(
            missing.id,
            "local_upload",
            {"video_id": "missing", "platform": "local_upload"},
        )
        service = JobRecoveryService(
            self.repo, self.pipeline, None, sleeper=lambda _: None
        )
        with self.assertRaises(PipelineError) as caught:
            service.retry(missing.id)
        self.assertEqual(caught.exception.code, "MEDIA_UNAVAILABLE")
        persisted = self.repo.get_job(missing.id)
        self.assertEqual(persisted.status, "failed")
        self.assertFalse(persisted.retryable)

        media = self.root / "cached.mp4"
        media.write_bytes(b"cached")
        _, result, _ = self.pipeline.process(media)
        calls = self.asr.calls
        cached = Job(
            id="cached-retry",
            status="failed",
            progress=90,
            error_code="PROCESS_INTERRUPTED",
            retryable=True,
            video_id=result.video_id,
            checkpoint="result_saved",
        )
        self.repo.save_job(cached)
        self.repo.configure_job_request(
            cached.id,
            "local_upload",
            {"video_id": result.video_id, "platform": "local_upload"},
        )
        media.unlink()
        retried = service.retry(cached.id)
        self.assertEqual(retried.status, "completed")
        self.assertEqual(self.asr.calls, calls)

    def test_primary_failure_survives_workdir_cleanup_failure(self):
        media = self.root / "failure.mp4"
        media.write_bytes(b"failure")
        pipeline = LocalFullPipeline(
            self.repo,
            CountingAsr(fail=True),
            DeterministicFullExtractor(),
            temp_root=self.root / "failed-jobs",
        )
        with patch(
            "backend.app.services.pipeline.shutil.rmtree",
            side_effect=OSError("locked"),
        ):
            with self.assertRaises(PipelineError) as caught:
                pipeline.process(media)
        self.assertEqual(caught.exception.code, "ASR_FAILED")
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            status, code, warnings = db.execute(
                """SELECT status, error_code, warnings_json FROM jobs
                ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual((status, code), ("failed", "ASR_FAILED"))
        self.assertIn("临时媒体清理失败", warnings)

    def test_upload_failure_cleans_request_and_pipeline_directories(self):
        upload_root = self.root / "uploads"
        pipeline = LocalFullPipeline(
            self.repo,
            CountingAsr(fail=True),
            DeterministicFullExtractor(),
            temp_root=self.root / "pipeline-tmp",
        )
        client = TestClient(
            create_app(
                pipeline,
                upload_root=upload_root,
                orphan_max_age_seconds=999999,
            )
        )
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("failure.mp4", b"media", "video/mp4")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "ASR_FAILED")
        self.assertEqual(list(upload_root.iterdir()), [])
        self.assertEqual(list((self.root / "pipeline-tmp").iterdir()), [])

    def test_orphan_cleanup_requires_marker_age_safe_root_and_records_failure(self):
        pipeline_root = self.root / "pipeline-orphans"
        upload_root = self.root / "upload-orphans"
        outside = self.root / "outside"
        for root in (pipeline_root, upload_root, outside):
            root.mkdir()

        def owned(root: Path, name: str, old: bool = True):
            path = root / name
            path.mkdir()
            owner_id = f"owner-{name}"
            self.repo.save_job(
                Job(id=owner_id, status="failed", progress=1)
            )
            (path / TEMP_MARKER).write_text(
                f"job:{owner_id}", encoding="utf-8"
            )
            stamp = time.time() - (1000 if old else 0)
            os.utime(path, (stamp, stamp))
            return path

        old_pipeline = owned(pipeline_root, "video-job-old")
        old_upload = owned(upload_root, "upload-old")
        fresh = owned(pipeline_root, "video-job-fresh", old=False)
        unmarked = pipeline_root / "video-job-unmarked"
        unmarked.mkdir()
        other = owned(pipeline_root, "someone-else")
        link = pipeline_root / "video-job-link"
        symlink_created = False
        try:
            link.symlink_to(outside, target_is_directory=True)
            symlink_created = True
        except OSError:
            pass

        warnings = TemporaryMediaService(
            self.repo,
            [
                (pipeline_root, ("video-job-",)),
                (upload_root, ("upload-",)),
            ],
            max_age_seconds=100,
        ).cleanup_orphans()
        self.assertFalse(old_pipeline.exists())
        self.assertFalse(old_upload.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unmarked.exists())
        self.assertTrue(other.exists())
        if symlink_created:
            self.assertTrue(link.exists())
            self.assertTrue(any("不安全" in item for item in warnings))
            self.assertTrue(outside.exists())

        failing = owned(upload_root, "upload-fail")
        warnings = TemporaryMediaService(
            self.repo,
            [(upload_root, ("upload-",))],
            max_age_seconds=0,
            remover=lambda _: (_ for _ in ()).throw(OSError("locked")),
        ).cleanup_orphans()
        self.assertTrue(failing.exists())
        self.assertTrue(any("清理失败" in item for item in warnings))
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            codes = {
                row[0] for row in db.execute("SELECT code FROM system_warnings")
            }
        self.assertIn("ORPHAN_CLEANUP_FAILED", codes)


class RecoveryMigrationTests(unittest.TestCase):
    def test_v4_to_v5_failure_is_atomic_and_retryable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "v4.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                SQLiteRepository._create_v3_schema(db)
                SQLiteRepository._migrate_v3_to_v4(db)
                db.execute(
                    """INSERT INTO jobs
                    (id,status,progress,error_code,message,video_id,created_at,updated_at)
                    VALUES ('sentinel','failed',10,'X','kept',NULL,'old','old')"""
                )
                db.execute("PRAGMA user_version=4")
                db.commit()

            def fail_midway(db):
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
                raise RuntimeError("injected v5 failure")

            with patch.object(
                SQLiteRepository,
                "_migrate_v4_to_v5",
                staticmethod(fail_midway),
            ):
                with self.assertRaises(RuntimeError):
                    SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(jobs)")
                }
                self.assertNotIn("retry_count", columns)
                self.assertEqual(
                    db.execute("SELECT message FROM jobs").fetchone()[0], "kept"
                )
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(jobs)")
                }
                self.assertTrue(
                    {"retry_count", "cancel_requested", "checkpoint"}.issubset(columns)
                )
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
