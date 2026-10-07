from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.domain.models import Evidence, FocusedAnswer, Segment
from backend.app.repositories.sqlite import (
    SQLiteRepository,
    focus_query_hash,
    normalize_focus_query,
)
from backend.app.services.focused import FocusedExtractionService
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
    HttpFocusedExtractor,
)


class TranscriptAsr:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.calls = 0

    def transcribe(self, media_path: Path) -> list[Segment]:
        self.calls += 1
        return [
            Segment(id=f"seg-{index}", start=index - 1, end=index, text=text)
            for index, text in enumerate(self.lines, 1)
        ]


class CountingFocusedExtractor:
    def __init__(self):
        self.calls = 0
        self.delegate = DeterministicFocusedExtractor()

    def extract(self, *args, **kwargs):
        self.calls += 1
        return self.delegate.extract(*args, **kwargs)


class InvalidFocusedExtractor:
    def extract(self, *args, **kwargs):
        return FocusedAnswer(
            mention_status="explicit",
            direct_answer="视频明确提到：伪造内容",
            supporting_segments=[
                Evidence(
                    id="bad",
                    claim="伪造内容",
                    claim_type="creator_opinion",
                    evidence="字幕中不存在",
                    segment_ids=["seg-1"],
                    start_time=0,
                    end_time=1,
                    confidence=1,
                )
            ],
        )


class LocalFocusedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "sample.mp4"
        self.media.write_bytes(b"focused media")
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _save_video(
        self, lines: list[str], *, warnings: list[str] | None = None
    ) -> tuple[str, TranscriptAsr, LocalFullPipeline]:
        asr = TranscriptAsr(lines)
        pipeline = LocalFullPipeline(
            self.repo, asr, DeterministicFullExtractor()
        )
        _, result, _ = pipeline.process(self.media)
        if warnings:
            result.warnings = warnings
            self.repo.save_result(result)
        return result.video_id, asr, pipeline

    def test_explicit_status_and_evidence(self):
        video_id, _, _ = self._save_video(["收费标准是每月100元。"])
        result = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(video_id, "收费标准")
        answer = result.focused_answer
        self.assertEqual(answer.mention_status, "explicit")
        self.assertIs(answer.is_mentioned, True)
        self.assertEqual(answer.supporting_segments[0].evidence, "收费标准是每月100元。")
        self.assertEqual(answer.supporting_segments[0].start_time, 0)
        self.assertEqual(answer.key_points[0].evidence_refs, ["focus-ev-1"])

    def test_inferred_status_is_labelled(self):
        video_id, _, _ = self._save_video(["课程价格为100元。"])
        answer = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(video_id, "收费标准").focused_answer
        self.assertEqual(answer.mention_status, "inferred")
        self.assertIs(answer.is_mentioned, True)
        self.assertIn("根据上下文归纳", answer.key_points[0].text)
        self.assertEqual(
            answer.supporting_segments[0].claim_type, "model_inference"
        )

    def test_not_mentioned_uses_fixed_sentence(self):
        video_id, _, _ = self._save_video(["今天介绍摄影构图。"])
        answer = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(video_id, "收费标准").focused_answer
        self.assertEqual(answer.mention_status, "not_mentioned")
        self.assertIs(answer.is_mentioned, False)
        self.assertEqual(answer.direct_answer, "该视频没有明确讨论这个问题。")
        self.assertEqual(answer.supporting_segments, [])

    def test_unknown_when_transcript_is_incomplete(self):
        video_id, _, _ = self._save_video(
            ["字幕仅保留开头。"], warnings=["字幕残缺，无法确认完整内容。"]
        )
        answer = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(video_id, "收费标准").focused_answer
        self.assertEqual(answer.mention_status, "unknown_incomplete_transcript")
        self.assertIsNone(answer.is_mentioned)
        self.assertEqual(answer.supporting_segments, [])

    def test_new_focus_never_reads_media_or_runs_asr(self):
        video_id, asr, _ = self._save_video(["获客方法是先发布案例。"])
        calls_after_full = asr.calls
        self.media.unlink()
        result = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(video_id, "获客方法")
        self.assertEqual(result.extraction_mode, "focused")
        self.assertEqual(asr.calls, calls_after_full)

    def test_normalized_query_hash_reuses_result_and_preserves_first_original(self):
        video_id, _, _ = self._save_video(["收费 标准是100元。"])
        extractor = CountingFocusedExtractor()
        service = FocusedExtractionService(self.repo, extractor)
        first_query = "  收费　标准  "
        first = service.extract(video_id, first_query)
        second_query = "收费 标准"
        second = service.extract(video_id, second_query)
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(first.focus_query, first_query)
        self.assertEqual(second.focus_query, second_query)
        self.assertEqual(
            normalize_focus_query(first_query), normalize_focus_query(second_query)
        )
        self.assertEqual(focus_query_hash(first_query), focus_query_hash(second_query))
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            row = db.execute(
                """SELECT focus_query, result_json FROM extractions
                WHERE video_id=? AND mode='focused'""",
                (video_id,),
            ).fetchone()
        self.assertEqual(row[0], first_query)
        payload = json.loads(row[1])
        self.assertEqual(set(payload), {"focused_answer"})
        self.assertNotIn("raw_transcript", row[1])
        self.assertNotIn("clean_transcript", row[1])

    def test_invalid_evidence_rolls_back_focused_extraction(self):
        video_id, _, _ = self._save_video(["真实字幕。"])
        with self.assertRaises(PipelineError) as caught:
            FocusedExtractionService(
                self.repo, InvalidFocusedExtractor()
            ).extract(video_id, "真实")
        self.assertEqual(caught.exception.code, "EXTRACTION_FAILED")
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM extractions WHERE mode='focused'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_cross_video_claim_evidence_is_rejected_by_foreign_key(self):
        first_id, _, _ = self._save_video(["视频一收费100元。"])
        FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        ).extract(first_id, "收费")
        second_media = self.root / "second.mp4"
        second_media.write_bytes(b"second media")
        second = LocalFullPipeline(
            self.repo,
            TranscriptAsr(["视频二收费200元。"]),
            DeterministicFullExtractor(),
        ).process(second_media)[1]
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            db.execute("PRAGMA foreign_keys = ON")
            extraction_id, claim_id = db.execute(
                """SELECT c.extraction_id, c.id FROM claims AS c
                JOIN extractions AS e ON e.id=c.extraction_id
                WHERE e.video_id=? AND e.mode='focused'""",
                (first_id,),
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO claim_evidence
                    (extraction_id, claim_id, video_id, segment_id)
                    VALUES (?, ?, ?, ?)""",
                    (extraction_id, claim_id, second.video_id, "seg-1"),
                )

    def test_cached_focused_evidence_is_revalidated_against_transcript(self):
        video_id, _, _ = self._save_video(["收费标准是100元。"])
        service = FocusedExtractionService(
            self.repo, DeterministicFocusedExtractor()
        )
        service.extract(video_id, "收费")
        query_hash = focus_query_hash("收费")
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            row = db.execute(
                """SELECT id, result_json FROM extractions
                WHERE video_id=? AND mode='focused' AND query_hash=?""",
                (video_id, query_hash),
            ).fetchone()
            payload = json.loads(row[1])
            payload["focused_answer"]["supporting_segments"][0][
                "evidence"
            ] = "字幕中不存在但结构合法"
            db.execute(
                "UPDATE extractions SET result_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), row[0]),
            )
            db.commit()
        with self.assertRaises(PipelineError) as caught:
            service.extract(video_id, "收费")
        self.assertEqual(caught.exception.code, "EXTRACTION_FAILED")

    def test_api_schema_empty_query_and_missing_video(self):
        _, _, pipeline = self._save_video(["收费标准是100元。"])
        client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))
        blank = client.post(
            "/api/v1/videos/missing/extractions", json={"focus_query": " \t "}
        )
        self.assertEqual(blank.status_code, 400)
        self.assertEqual(blank.json()["error"]["code"], "INVALID_INPUT")
        missing = client.post(
            "/api/v1/videos/missing/extractions", json={"focus_query": "收费"}
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "NOT_FOUND")

    def test_api_response_has_strong_focused_schema(self):
        video_id, _, pipeline = self._save_video(["收费标准是100元。"])
        client = TestClient(create_app(pipeline, upload_root=self.root / "uploads"))
        response = client.post(
            f"/api/v1/videos/{video_id}/extractions",
            json={"focus_query": "收费标准"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()["focused_answer"]
        self.assertEqual(answer["mention_status"], "explicit")
        self.assertIs(answer["is_mentioned"], True)

    def test_http_focused_adapter_validates_four_state_schema(self):
        def handler(_):
            return httpx.Response(
                200,
                json={
                    "mention_status": "not_mentioned",
                    "is_mentioned": True,
                    "direct_answer": "该视频没有明确讨论这个问题。",
                    "key_points": [],
                    "supporting_segments": [],
                    "supplementary_context": [],
                    "missing_information": [],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        answer = HttpFocusedExtractor(
            "https://extract.invalid/focused", client=client
        ).extract("收费", "摄影", "摄影", [Segment(id="seg-1", text="摄影")])
        self.assertEqual(answer.mention_status, "not_mentioned")
        self.assertIs(answer.is_mentioned, False)


if __name__ == "__main__":
    unittest.main()
