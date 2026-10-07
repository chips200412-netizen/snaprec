from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.api.main import create_app
from backend.app.domain.models import Evidence, ExtractionItem, FullExtraction, Segment
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.cleaning import clean_transcript, protected_tokens
from backend.app.services.evidence import EvidenceValidationError
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import (
    DeterministicFullExtractor,
    HttpAsrProvider,
    HttpFullExtractor,
    SidecarSubtitleProvider,
)


class FakeAsr:
    def __init__(self):
        self.calls = 0

    def transcribe(self, media_path: Path) -> list[Segment]:
        self.calls += 1
        return [
            Segment(id="seg-1", start=0.0, end=3.0, text="第一步必须在2026年7月投入100元。"),
            Segment(id="seg-2", start=3.0, end=6.0, text="如果没有授权，不要继续操作。"),
        ]


class InvalidExtractor:
    def extract(self, raw, clean, segments):
        ev = Evidence(
            id="ev-1", claim="伪造", claim_type="creator_opinion",
            evidence="字幕中没有", segment_ids=["seg-1"],
            start_time=0, end_time=3, confidence=0.9,
        )
        item = ExtractionItem(
            text="伪造", item_type="key_point", claim_type="creator_opinion",
            evidence_refs=["ev-1"], confidence=0.9,
        )
        return "摘要", FullExtraction(key_points=[item]), [ev]


class FailingAsr:
    def transcribe(self, media_path: Path):
        raise RuntimeError("provider unavailable")


class LocalFullPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "sample.mp4"
        self.media.write_bytes(b"fake media bytes")
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_local_flow_persists_and_exports(self):
        asr = FakeAsr()
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        pipeline = LocalFullPipeline(
            self.repo, asr, DeterministicFullExtractor(), temp_root=jobs_root
        )

        job, result, markdown = pipeline.process(self.media)

        self.assertEqual(job.status, "completed")
        self.assertEqual(job.progress, 100)
        self.assertEqual(result.extraction_mode, "full")
        self.assertEqual(result.subtitle_source, "asr")
        self.assertEqual(asr.calls, 1)
        self.assertLessEqual(len(result.summary), 100)
        self.assertEqual(protected_tokens(result.raw_transcript), protected_tokens(result.clean_transcript))
        self.assertIsNotNone(self.repo.get_result(result.video_id))
        self.assertIn("## 完整字幕", markdown)
        self.assertIn("字幕来源：asr", markdown)
        self.assertIn("证据 [0s–3s]", markdown)
        self.assertEqual(list(jobs_root.iterdir()), [])
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0], 1)

    def test_cleaner_preserves_critical_tokens(self):
        examples = [
            "嗯，日期是2026-07-28。",
            "金额是￥99.5元和100美元。",
            "每次需要3个，持续2小时。",
            "如果没有授权，否则不能继续。",
            "第一步准备，第二步执行。",
            "不要删掉两个两个重复数字2和2。",
        ]
        for original in examples:
            with self.subTest(original=original):
                self.assertEqual(
                    sorted(protected_tokens(original)),
                    sorted(protected_tokens(clean_transcript(original))),
                )

    def test_invalid_evidence_fails_and_temp_is_cleaned(self):
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        pipeline = LocalFullPipeline(self.repo, FakeAsr(), InvalidExtractor(), jobs_root)

        with self.assertRaises(PipelineError) as caught:
            pipeline.process(self.media)

        self.assertEqual(caught.exception.code, "INVALID_EXTRACTION")
        self.assertEqual(list(jobs_root.iterdir()), [])
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            job = db.execute("SELECT status, error_code FROM jobs").fetchone()
            self.assertEqual(job, ("failed", "INVALID_EXTRACTION"))

    def test_asr_failure_has_stable_error_and_temp_is_cleaned(self):
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        pipeline = LocalFullPipeline(
            self.repo, FailingAsr(), DeterministicFullExtractor(), jobs_root
        )
        with self.assertRaises(PipelineError) as caught:
            pipeline.process(self.media)
        self.assertEqual(caught.exception.code, "ASR_FAILED")
        self.assertEqual(list(jobs_root.iterdir()), [])

    def test_sidecar_provider_runs_through_isolated_staging(self):
        Path(f"{self.media}.txt").write_text(
            "0|2|第一步准备10元。\n2|4|不要遗漏日期2026年7月。", encoding="utf-8"
        )
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        asr = FakeAsr()
        _, result, _ = LocalFullPipeline(
            self.repo, asr, DeterministicFullExtractor(), jobs_root,
            subtitle_provider=SidecarSubtitleProvider(),
        ).process(self.media)
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.subtitle_source, "user_upload")
        self.assertTrue(
            all(item.start is None and item.end is None for item in result.segments)
        )
        self.assertEqual(asr.calls, 0)
        self.assertEqual(list(jobs_root.iterdir()), [])

    def test_content_hash_cache_reconstructs_result_without_asr(self):
        first_asr = FakeAsr()
        pipeline = LocalFullPipeline(self.repo, first_asr, DeterministicFullExtractor())
        _, original, _ = pipeline.process(self.media)
        second_asr = FakeAsr()
        _, cached, _ = LocalFullPipeline(
            self.repo, second_asr, DeterministicFullExtractor()
        ).process(self.media)
        self.assertEqual(first_asr.calls, 1)
        self.assertEqual(second_asr.calls, 0)
        self.assertEqual(cached, original)

    def test_explicit_user_subtitle_overrides_cached_asr_result(self):
        first_asr = FakeAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            first_asr,
            DeterministicFullExtractor(),
            subtitle_provider=SidecarSubtitleProvider(),
        )
        _, first, _ = pipeline.process(self.media)
        self.assertEqual(first.subtitle_source, "asr")
        Path(f"{self.media}.srt").write_text(
            "1\n00:00:01,250 --> 00:00:03,500\n用户修正字幕包含200元。\n",
            encoding="utf-8",
        )
        second_asr = FakeAsr()
        _, corrected, _ = LocalFullPipeline(
            self.repo,
            second_asr,
            DeterministicFullExtractor(),
            subtitle_provider=SidecarSubtitleProvider(),
        ).process(self.media)
        self.assertEqual(corrected.subtitle_source, "user_upload")
        self.assertIn("用户修正字幕", corrected.raw_transcript)
        self.assertEqual(corrected.segments[0].start, 1.25)
        self.assertEqual(corrected.segments[0].end, 3.5)
        self.assertEqual(second_asr.calls, 0)
        persisted = self.repo.load_result(corrected.video_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.subtitle_source, "user_upload")

    def test_claims_and_segment_links_are_persisted(self):
        _, result, _ = LocalFullPipeline(
            self.repo, FakeAsr(), DeterministicFullExtractor()
        ).process(self.media)
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
                len(result.evidence),
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0],
                sum(len(item.segment_ids) for item in result.evidence),
            )

    def test_runtime_schema_rejects_invalid_confidence_and_inference(self):
        with self.assertRaises(ValidationError):
            Evidence(
                id="e", claim="普通陈述", claim_type="model_inference",
                evidence="原文", segment_ids=["s"], confidence=1.2,
            )
        with self.assertRaises(ValidationError):
            ExtractionItem(
                text="普通陈述", item_type="key_point", claim_type="model_inference",
                evidence_refs=["e"], confidence=0.5,
            )

    def test_cleanup_failure_is_not_silent(self):
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        pipeline = LocalFullPipeline(
            self.repo, FakeAsr(), DeterministicFullExtractor(), jobs_root
        )
        with patch("backend.app.services.pipeline.shutil.rmtree", side_effect=OSError("locked")):
            with self.assertRaises(PipelineError) as caught:
                pipeline.process(self.media)
        self.assertEqual(caught.exception.code, "CLEANUP_FAILED")

    def test_unsupported_type_has_stable_error_and_failed_job(self):
        bad = self.root / "sample.exe"
        bad.write_bytes(b"x")
        with self.assertRaises(PipelineError) as caught:
            LocalFullPipeline(
                self.repo, FakeAsr(), DeterministicFullExtractor()
            ).process(bad)
        self.assertEqual(caught.exception.code, "UNSUPPORTED_MEDIA_TYPE")

    def test_cancelled_job_is_terminal_and_temp_is_cleaned(self):
        jobs_root = self.root / "jobs"
        jobs_root.mkdir()
        pipeline = LocalFullPipeline(
            self.repo, FakeAsr(), DeterministicFullExtractor(), jobs_root
        )
        with self.assertRaises(PipelineError) as caught:
            pipeline.process(self.media, cancel_requested=lambda: True)
        self.assertEqual(caught.exception.code, "CANCELLED")
        self.assertEqual(list(jobs_root.iterdir()), [])
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            job = db.execute("SELECT status, error_code FROM jobs").fetchone()
            self.assertEqual(job, ("cancelled", "CANCELLED"))

    def test_migrations_are_repeatable(self):
        self.repo.migrate()
        self.repo.migrate()

    def test_pipeline_rejects_file_over_configured_limit(self):
        pipeline = LocalFullPipeline(
            self.repo,
            FakeAsr(),
            DeterministicFullExtractor(),
            max_media_bytes=4,
        )
        with self.assertRaises(PipelineError) as caught:
            pipeline.process(self.media)
        self.assertEqual(caught.exception.code, "UPLOAD_TOO_LARGE")

    def test_upload_api_accepts_media_and_user_subtitle(self):
        upload_root = self.root / "uploads"
        asr = FakeAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            asr,
            DeterministicFullExtractor(),
            subtitle_provider=SidecarSubtitleProvider(),
        )
        client = TestClient(
            create_app(pipeline, upload_root=upload_root, max_upload_bytes=1024)
        )
        response = client.post(
            "/api/v1/uploads",
            files={
                "file": ("lesson.mp4", b"media", "video/mp4"),
                "subtitle": (
                    "lesson.txt",
                    "第一步准备100元。\n如果没有授权，不要继续。",
                    "text/plain",
                ),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["result"]["title"], "lesson")
        self.assertEqual(payload["result"]["subtitle_source"], "user_upload")
        self.assertEqual(asr.calls, 0)
        self.assertEqual(list(upload_root.iterdir()), [])
        job_response = client.get(f"/api/v1/jobs/{payload['job']['id']}")
        self.assertEqual(job_response.status_code, 200)
        self.assertEqual(job_response.json()["status"], "completed")

    def test_sync_upload_preserves_and_parses_srt_timestamps(self):
        upload_root = self.root / "srt-uploads"
        asr = FakeAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            asr,
            DeterministicFullExtractor(),
            subtitle_provider=SidecarSubtitleProvider(),
        )
        client = TestClient(create_app(pipeline, upload_root=upload_root))
        response = client.post(
            "/api/v1/uploads",
            files={
                "file": ("lesson.mp4", b"media-srt", "video/mp4"),
                "subtitle": (
                    "../../lesson.SRT",
                    "1\r\n00:00:01,250 --> 00:00:03,500\r\n第一步准备。\r\n\r\n"
                    "2\r\n00:01:02,000 --> 00:01:04,750\r\n不要跳过。\r\n",
                    "application/x-subrip",
                ),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        segments = response.json()["result"]["segments"]
        self.assertEqual(asr.calls, 0)
        self.assertEqual(
            [(item["start"], item["end"]) for item in segments],
            [(1.25, 3.5), (62.0, 64.75)],
        )
        self.assertEqual(list(upload_root.iterdir()), [])

    def test_async_upload_preserves_and_parses_webvtt_timestamps(self):
        upload_root = self.root / "vtt-uploads"
        asr = FakeAsr()
        pipeline = LocalFullPipeline(
            self.repo,
            asr,
            DeterministicFullExtractor(),
            subtitle_provider=SidecarSubtitleProvider(),
        )
        client = TestClient(create_app(pipeline, upload_root=upload_root))
        response = client.post(
            "/api/v1/upload-jobs",
            files={
                "file": ("lesson.webm", b"media-vtt", "video/webm"),
                "subtitle": (
                    "lesson.vtt",
                    "WEBVTT\n\nintro\n00:00:02.000 --> 00:00:05.125 align:start\n"
                    "第一行\n第二行\n\n00:10.000 --> 00:12.500\n下一步\n",
                    "text/vtt",
                ),
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        job = client.get(f"/api/v1/jobs/{response.json()['id']}").json()
        self.assertIn(job["status"], {"completed", "completed_with_warnings"})
        detail = client.get(
            f"/api/v1/videos/{job['video_id']}",
            params={"platform": "local_upload"},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        segments = detail.json()["result"]["segments"]
        self.assertEqual(asr.calls, 0)
        self.assertEqual(
            [(item["start"], item["end"]) for item in segments],
            [(2.0, 5.125), (10.0, 12.5)],
        )
        self.assertEqual(segments[0]["text"], "第一行\n第二行")
        self.assertEqual(list(upload_root.iterdir()), [])

    def test_upload_rejects_unapproved_subtitle_extension_and_cleans(self):
        upload_root = self.root / "bad-subtitle-uploads"
        client = TestClient(
            create_app(
                LocalFullPipeline(
                    self.repo, FakeAsr(), DeterministicFullExtractor()
                ),
                upload_root=upload_root,
            )
        )
        for endpoint in ("/api/v1/uploads", "/api/v1/upload-jobs"):
            with self.subTest(endpoint=endpoint):
                response = client.post(
                    endpoint,
                    files={
                        "file": ("lesson.mp4", b"media", "video/mp4"),
                        "subtitle": ("lesson.ass", "unsafe", "text/plain"),
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "UNSUPPORTED_MEDIA_TYPE"
                )
                self.assertEqual(list(upload_root.iterdir()), [])

    def test_upload_api_rejects_oversize_and_cleans_request_dir(self):
        upload_root = self.root / "uploads"
        client = TestClient(
            create_app(
                LocalFullPipeline(
                    self.repo, FakeAsr(), DeterministicFullExtractor()
                ),
                upload_root=upload_root,
                max_upload_bytes=3,
            )
        )
        response = client.post(
            "/api/v1/uploads",
            files={"file": ("large.mp4", b"four", "video/mp4")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "UPLOAD_TOO_LARGE")
        self.assertEqual(list(upload_root.iterdir()), [])

    def test_claim_evidence_rejects_unknown_segment(self):
        _, result, _ = LocalFullPipeline(
            self.repo, FakeAsr(), DeterministicFullExtractor()
        ).process(self.media)
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            db.execute("PRAGMA foreign_keys = ON")
            extraction_id = db.execute(
                "SELECT id FROM extractions WHERE video_id=?", (result.video_id,)
            ).fetchone()[0]
            claim_id = db.execute(
                "SELECT id FROM claims WHERE extraction_id=?", (extraction_id,)
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO claim_evidence
                    (extraction_id, claim_id, video_id, segment_id)
                    VALUES (?, ?, ?, ?)""",
                    (extraction_id, claim_id, result.video_id, "missing"),
                )

    def test_http_asr_and_extractor_adapters_validate_responses(self):
        def asr_handler(_):
            return httpx.Response(
                200,
                json={
                    "segments": [
                        {"start": 0, "end": 1.5, "text": "第一步准备10元。"}
                    ]
                },
            )

        asr_client = httpx.Client(transport=httpx.MockTransport(asr_handler))
        segments = HttpAsrProvider(
            "https://asr.invalid/transcribe", client=asr_client
        ).transcribe(self.media)
        self.assertEqual(segments[0].text, "第一步准备10元。")

        def extraction_handler(_):
            return httpx.Response(
                200,
                json={
                    "summary": "准备10元",
                    "full_extraction": {
                        "important_data": [
                            {
                                "text": "准备10元",
                                "item_type": "important_data",
                                "claim_type": "creator_opinion",
                                "evidence_refs": ["ev-1"],
                                "confidence": 1,
                            }
                        ]
                    },
                    "evidence": [
                        {
                            "id": "ev-1",
                            "claim": "第一步准备10元。",
                            "claim_type": "creator_opinion",
                            "evidence": "第一步准备10元。",
                            "segment_ids": ["seg-1"],
                            "start_time": 0,
                            "end_time": 1.5,
                            "confidence": 1,
                        }
                    ],
                },
            )

        extraction_client = httpx.Client(
            transport=httpx.MockTransport(extraction_handler)
        )
        summary, full, evidence = HttpFullExtractor(
            "https://llm.invalid/extract", client=extraction_client
        ).extract("第一步准备10元。", "第一步准备10元。", segments)
        self.assertEqual(summary, "准备10元")
        self.assertEqual(full.important_data[0].evidence_refs, ["ev-1"])
        self.assertEqual(evidence[0].segment_ids, ["seg-1"])


if __name__ == "__main__":
    unittest.main()
