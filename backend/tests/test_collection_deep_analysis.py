from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import FullExtraction, Job, VideoResult
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)


class _Registry:
    @staticmethod
    def matching(url: str):
        return object() if "bilibili.com" in url else None


class _FakeResolutionService:
    def __init__(self, repository: SQLiteRepository, *, fail_once: bool = False):
        self.repository = repository
        self.registry = _Registry()
        self.fail_once = fail_once
        self.calls = 0

    def process(self, source_url: str, focus_query: str = "", job: Job | None = None):
        self.calls += 1
        assert job is not None
        self.repository.configure_job_request(
            job.id,
            "resolution",
            {"source_url": source_url, "focused": False, "focus_query_hash": ""},
        )
        if self.fail_once and self.calls == 1:
            job.status = "failed"
            job.error_code = "EXTRACTION_FAILED"
            job.message = "内容提取服务暂时失败。"
            job.retryable = True
            job.checkpoint = "clean"
            self.repository.save_job(job)
            raise PipelineError(job.error_code, job.message)
        video_id = "BVdeep"
        result = VideoResult(
            platform="bilibili",
            source_url=source_url,
            canonical_url=source_url,
            video_id=video_id,
            author="公开作者",
            title="公开素材",
            description="",
            tags=[],
            duration=0,
            cover_url="",
            subtitle_source="none" if not self.fail_once else "official",
            raw_transcript="" if not self.fail_once else "可验证字幕",
            clean_transcript="" if not self.fail_once else "可验证字幕",
            segments=[],
            focus_query="",
            extraction_mode="full",
            summary="" if not self.fail_once else "可验证字幕",
            full_extraction=FullExtraction(),
            evidence=[],
            warnings=(
                ["字幕残缺或不可用，无法判断视频内容；未使用标题或简介补充结论。"]
                if not self.fail_once
                else []
            ),
        )
        self.repository.save_result(result)
        job.video_id = video_id
        job.status = "completed_with_warnings" if result.warnings else "completed"
        job.progress = 100
        job.error_code = None
        job.message = ""
        job.retryable = False
        self.repository.save_job(job)
        return job, result, ""


class CollectionDeepAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "deep.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _client(self, service: _FakeResolutionService | None) -> tuple[TestClient, str]:
        preview_id = "preview-deep"
        source_url = "https://www.bilibili.com/video/BVdeep"
        self.repo.create_collection_preview(
            {
                "preview_id": preview_id,
                "original_input": source_url,
                "source_url": source_url,
                "canonical_url": source_url,
                "identity_url": source_url,
                "source_kind": "video",
                "platform": "bilibili",
                "metadata_status": "recognized",
                "metadata": {
                    "title": {
                        "value": "公开素材",
                        "source": "platform_public",
                        "fetched_at": "2026-08-24T00:00:00+00:00",
                    },
                    "author": {
                        "value": "公开作者",
                        "source": "platform_public",
                        "fetched_at": "2026-08-24T00:00:00+00:00",
                    },
                    "cover_url": {"value": "", "source": "none", "fetched_at": ""},
                    "source_copy": {"value": "", "source": "none", "fetched_at": ""},
                    "platform_tags": [],
                    "warnings": [],
                },
                "organization_suggestion": {
                    "primary_category": "",
                    "secondary_category": "",
                    "tags": [],
                    "basis": "public_metadata",
                    "method": "deterministic",
                    "status": "insufficient_metadata",
                },
                "created_at": "2026-08-24T00:00:00+00:00",
                "expires_at": "2099-08-24T00:00:00+00:00",
            }
        )
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        client = TestClient(
            create_app(
                pipeline,
                upload_root=self.root / "uploads",
                resolution_service=service,
            )
        )
        saved = client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "save-deep"},
            json={
                "preview_id": preview_id,
                "user_title": None,
                "organization_confirmation": {
                    "primary_category": "设计",
                    "secondary_category": "空间",
                    "organization_tags": ["自然光"],
                },
                "personal_tags": [],
                "inspiration": None,
            },
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        return client, saved.json()["id"]

    def test_unconfigured_service_is_truthful_and_side_effect_free(self):
        client, material_id = self._client(None)
        first = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        )
        second = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["state"], "unavailable")
        self.assertFalse(first.json()["can_start"])
        self.assertIsNone(first.json()["analysis_job_id"])

    def test_start_is_idempotent_and_no_subtitle_becomes_limited(self):
        service = _FakeResolutionService(self.repo)
        client, material_id = self._client(service)
        ready = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        ).json()
        self.assertEqual(ready["state"], "ready")
        self.assertTrue(ready["can_start"])

        started = client.post(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        )
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["state"], "queued")
        completed = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        ).json()
        self.assertEqual(completed["state"], "limited")
        self.assertEqual(completed["result_kind"], "limited")
        self.assertTrue(completed["can_view_result"])
        self.assertIn("字幕", completed["limitation"])

        replay = client.post(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["analysis_job_id"], completed["analysis_job_id"])
        self.assertEqual(service.calls, 1)

    def test_parallel_start_claims_one_persisted_job(self):
        service = _FakeResolutionService(self.repo)
        _client, material_id = self._client(service)

        def claim():
            return self.repo.get_or_create_collection_deep_job(
                material_id,
                "collection-default-v1",
                "parallel-task-key",
                "https://www.bilibili.com/video/BVdeep",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _index: claim(), range(2)))

        self.assertEqual(first[0].id, second[0].id)
        self.assertEqual(sorted([first[1], second[1]]), [False, True])

    def test_get_recovers_terminal_result_identity_without_writing(self):
        service = _FakeResolutionService(self.repo)
        client, material_id = self._client(service)
        job, _created = self.repo.get_or_create_collection_deep_job(
            material_id,
            "collection-default-v1",
            "crash-window-task-key",
            "https://www.bilibili.com/video/BVdeep",
        )
        service.process("https://www.bilibili.com/video/BVdeep", job=job)
        self.assertIsNone(
            self.repo.get_collection_item(material_id)["deep_analysis_resource_key"]
        )

        recovered = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        )

        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["state"], "limited")
        self.assertIsNone(
            self.repo.get_collection_item(material_id)["deep_analysis_resource_key"]
        )

    def test_retry_attempt_key_replays_without_a_second_attempt(self):
        service = _FakeResolutionService(self.repo, fail_once=True)
        client, material_id = self._client(service)
        client.post(f"/api/v1/collection-items/{material_id}/deep-analysis")
        failed = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        ).json()
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["can_retry"])
        self.assertEqual(failed["failed_stage"], "clean")

        retried = client.post(
            f"/api/v1/collection-items/{material_id}/deep-analysis/retry",
            headers={"Idempotency-Key": "retry-attempt-1"},
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()["state"], "queued")
        completed = client.get(
            f"/api/v1/collection-items/{material_id}/deep-analysis"
        ).json()
        self.assertEqual(completed["state"], "completed")

        replay = client.post(
            f"/api/v1/collection-items/{material_id}/deep-analysis/retry",
            headers={"Idempotency-Key": "retry-attempt-1"},
        )
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["state"], "completed")
        self.assertEqual(service.calls, 2)


if __name__ == "__main__":
    unittest.main()
