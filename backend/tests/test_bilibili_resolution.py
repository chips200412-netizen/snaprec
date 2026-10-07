from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.app.adapters import AdapterRegistry, BilibiliAdapter
from backend.app.adapters.bilibili import _PinnedNetworkBackend
from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.repositories.sqlite import AmbiguousVideoIdError
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)
from backend.app.services.resolution import ResolutionService, extract_urls
from backend.app.services.recovery import JobRecoveryService


def _page(*, subtitle_url: str | None = "https://aisubtitle.hdslb.com/sub.json") -> str:
    subtitle = (
        {"subtitles": [{"subtitle_url": subtitle_url}]} if subtitle_url else {"subtitles": []}
    )
    initial = {
        "videoData": {
            "bvid": "BV1abc123",
            "aid": 12345,
            "title": "公开课程",
            "desc": "页面公开简介",
            "duration": 12,
            "pic": "https://i0.hdslb.com/cover.jpg",
            "owner": {"name": "测试作者"},
        },
        "tags": [{"tag_name": "AI"}, {"tag_name": "教程"}],
    }
    play = {"data": {"subtitle": subtitle}}
    return (
        '<meta property="og:title" content="公开课程">'
        '<meta name="keywords" content="AI,教程">'
        f"<script>window.__INITIAL_STATE__={json.dumps(initial, ensure_ascii=False)};</script>"
        f"<script>window.__playinfo__={json.dumps(play, ensure_ascii=False)};</script>"
    )


class CountingPlatform:
    def __init__(self, *, with_subtitles: bool = True):
        self.calls: list[str] = []
        self.with_subtitles = with_subtitles
        self.page_version = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.host == "b23.tv":
            return httpx.Response(
                302, headers={"location": "https://www.bilibili.com/video/BV1abc123/"}
            )
        if request.url.host == "aisubtitle.hdslb.com":
            return httpx.Response(
                200,
                json={
                    "body": [
                        {"from": 0, "to": 3, "content": "收费标准是每月100元。"},
                        {"from": 3, "to": 8, "content": "如果没有授权，不要继续操作。"},
                    ]
                },
            )
        subtitle = (
            "https://aisubtitle.hdslb.com/sub.json" if self.with_subtitles else None
        )
        return httpx.Response(
            200,
            text=_page(subtitle_url=subtitle),
            headers={"content-type": "text/html; charset=utf-8"},
        )


class FailOnceFullExtractor:
    def __init__(self):
        self.calls = 0
        self.delegate = DeterministicFullExtractor()

    def extract(self, raw, cleaned, segments):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary model outage")
        return self.delegate.extract(raw, cleaned, segments)


class FailingFocusedExtractor:
    def extract(self, *args, **kwargs):
        raise RuntimeError("focused model outage")


def _adapter(handler) -> BilibiliAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BilibiliAdapter(client, dns_resolver=lambda _: ["8.8.8.8"])


class BilibiliResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, adapter: BilibiliAdapter) -> ResolutionService:
        return ResolutionService(
            self.repo,
            AdapterRegistry([adapter]),
            DeterministicFullExtractor(),
            DeterministicFocusedExtractor(),
        )

    def _api(self, service: ResolutionService) -> TestClient:
        pipeline = LocalFullPipeline(
            self.repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()
        )
        return TestClient(create_app(pipeline, resolution_service=service))

    def test_extracts_url_from_share_text_and_strips_chinese_punctuation(self):
        self.assertEqual(
            extract_urls("复制链接：https://www.bilibili.com/video/BV1abc123/，谢谢"),
            ["https://www.bilibili.com/video/BV1abc123/"],
        )

    def test_adapter_matches_direct_bv_av_and_short_link(self):
        adapter = _adapter(CountingPlatform())
        self.assertTrue(adapter.match("https://www.bilibili.com/video/BV1abc123/?p=2#x"))
        self.assertTrue(adapter.match("https://www.bilibili.com/video/av12345"))
        self.assertTrue(adapter.match("https://b23.tv/abc"))
        self.assertFalse(adapter.match("https://evil.example/video/BV1abc123"))

    def test_retry_resumes_after_subtitles_without_platform_calls(self):
        platform = CountingPlatform()
        extractor = FailOnceFullExtractor()
        service = ResolutionService(
            self.repo,
            AdapterRegistry([_adapter(platform)]),
            extractor,
            DeterministicFocusedExtractor(),
        )
        with self.assertRaises(PipelineError) as caught:
            service.process("https://www.bilibili.com/video/BV1abc123")
        self.assertEqual(caught.exception.code, "EXTRACTION_FAILED")
        before_retry = list(platform.calls)
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            job_id = db.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        pipeline = LocalFullPipeline(
            self.repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()
        )
        retried = JobRecoveryService(
            self.repo, pipeline, service, sleeper=lambda _: None
        ).retry(job_id)
        self.assertEqual(retried.status, "completed")
        self.assertEqual(platform.calls, before_retry)
        self.assertEqual(extractor.calls, 2)

    def test_focused_failure_does_not_commit_base_or_terminal_success(self):
        platform = CountingPlatform()
        service = ResolutionService(
            self.repo,
            AdapterRegistry([_adapter(platform)]),
            DeterministicFullExtractor(),
            FailingFocusedExtractor(),
        )
        with self.assertRaises(PipelineError) as caught:
            service.process(
                "https://www.bilibili.com/video/BV1abc123",
                "收费标准是什么",
            )
        self.assertEqual(caught.exception.code, "EXTRACTION_FAILED")
        with closing(sqlite3.connect(self.root / "notes.sqlite3")) as db:
            status, retryable = db.execute(
                "SELECT status, retryable FROM jobs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            video_count = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
            focused_count = db.execute(
                "SELECT COUNT(*) FROM extractions WHERE mode='focused'"
            ).fetchone()[0]
        self.assertEqual(status, "failed")
        self.assertFalse(retryable)
        self.assertEqual(video_count, 0)
        self.assertEqual(focused_count, 0)

    def test_short_link_redirect_is_controlled_and_canonical(self):
        adapter = _adapter(CountingPlatform())
        result = adapter.resolve("https://b23.tv/abc")
        self.assertEqual(result.video_id, "BV1abc123")
        self.assertEqual(
            result.canonical_url, "https://www.bilibili.com/video/BV1abc123"
        )

    def test_gzip_page_is_decoded_once_and_metadata_remains_available(self):
        page = _page().encode("utf-8")
        compressed = gzip.compress(page)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                },
            )

        adapter = _adapter(handler)
        video = adapter.resolve("https://www.bilibili.com/video/BV1abc123")
        metadata = adapter.get_metadata(video)

        self.assertEqual(metadata.title, "公开课程")
        self.assertEqual(metadata.author, "测试作者")
        self.assertIn("AI", metadata.tags)

    def test_gzip_page_size_limit_applies_to_the_decoded_body(self):
        compressed = gzip.compress(b"x" * (4 * 1024 * 1024 + 1))

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed)),
                },
            )

        with self.assertRaises(PipelineError) as caught:
            _adapter(handler).resolve("https://www.bilibili.com/video/BV1abc123")

        self.assertEqual(caught.exception.code, "CONTENT_UNAVAILABLE")

    def test_short_link_alias_reuses_completed_resource(self):
        platform = CountingPlatform()
        service = self._service(_adapter(platform))
        first = service.process("https://b23.tv/abc")[1]
        calls_after_first = len(platform.calls)
        second = service.process("https://b23.tv/abc")[1]
        self.assertEqual(first.video_id, second.video_id)
        self.assertEqual(len(platform.calls), calls_after_first)

    def test_av_page_prefers_public_bvid_and_saves_both_aliases(self):
        platform = CountingPlatform()
        service = self._service(_adapter(platform))
        _, result, _ = service.process(
            "https://www.bilibili.com/video/av12345?from=share"
        )
        self.assertEqual(result.video_id, "BV1abc123")
        self.assertEqual(
            result.canonical_url, "https://www.bilibili.com/video/BV1abc123"
        )
        self.assertEqual(
            self.repo.load_video_alias("https://www.bilibili.com/video/av12345"),
            ("bilibili", "BV1abc123"),
        )
        calls = len(platform.calls)
        service.process("https://www.bilibili.com/video/av12345")
        self.assertEqual(len(platform.calls), calls)

    def test_redirect_to_unlisted_host_is_rejected_before_follow(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        adapter = _adapter(handler)
        with self.assertRaises(PipelineError) as caught:
            adapter.resolve("https://b23.tv/abc")
        self.assertEqual(caught.exception.code, "CONTENT_UNAVAILABLE")
        self.assertEqual(len(calls), 1)

    def test_pinned_backend_connects_to_validated_ip_not_hostname(self):
        class RecordingBackend:
            def __init__(self):
                self.host = None

            def connect_tcp(self, host, port, timeout, local_address, socket_options):
                self.host = host
                return object()

        recording = RecordingBackend()
        backend = _PinnedNetworkBackend(
            lambda host: ["8.8.8.8"], backend=recording
        )
        backend.connect_tcp("www.bilibili.com", 443)
        self.assertEqual(recording.host, "8.8.8.8")

    def test_private_dns_result_is_rejected_before_request(self):
        calls = []
        adapter = BilibiliAdapter(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: calls.append(str(request.url)) or httpx.Response(200)
                )
            ),
            dns_resolver=lambda _: ["127.0.0.1"],
        )
        with self.assertRaises(PipelineError):
            adapter.resolve("https://www.bilibili.com/video/BV1abc123")
        self.assertEqual(calls, [])

    def test_metadata_and_public_subtitle_full_flow_and_cache(self):
        platform = CountingPlatform()
        service = self._service(_adapter(platform))
        job, result, _ = service.process(
            "分享【课程】https://www.bilibili.com/video/BV1abc123/?from=share"
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.title, "公开课程")
        self.assertEqual(result.author, "测试作者")
        self.assertEqual(result.tags, ["AI", "教程"])
        self.assertEqual(result.subtitle_source, "public_page")
        self.assertEqual(result.segments[0].start, 0)
        self.assertTrue(result.evidence)
        calls_after_first = len(platform.calls)
        cached = service.process(
            "https://www.bilibili.com/video/BV1abc123/?different=1"
        )[1]
        self.assertEqual(cached.video_id, result.video_id)
        self.assertEqual(len(platform.calls), calls_after_first)

    def test_focused_flow_reuses_transcript_and_returns_evidence(self):
        service = self._service(_adapter(CountingPlatform()))
        _, result, _ = service.process(
            "https://www.bilibili.com/video/BV1abc123",
            "收费标准",
        )
        self.assertEqual(result.extraction_mode, "focused")
        self.assertEqual(result.focus_query, "收费标准")
        self.assertEqual(result.focused_answer.mention_status, "explicit")
        self.assertEqual(
            result.focused_answer.supporting_segments[0].evidence,
            "收费标准是每月100元。",
        )

    def test_missing_subtitle_is_limited_warning_and_retried(self):
        platform = CountingPlatform(with_subtitles=False)
        service = self._service(_adapter(platform))
        job, result, _ = service.process(
            "https://www.bilibili.com/video/BV1abc123?p=2"
        )
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(result.subtitle_source, "none")
        self.assertEqual(result.raw_transcript, "")
        self.assertEqual(result.summary, "")
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.full_extraction.all_items(), [])
        self.assertTrue(any("字幕残缺" in item for item in result.warnings))
        self.assertTrue(any("分P" in item for item in result.warnings))
        calls_after_first = len(platform.calls)
        service.process("https://www.bilibili.com/video/BV1abc123")
        self.assertGreater(len(platform.calls), calls_after_first)

    def test_short_link_resolved_part_is_warned(self):
        platform = CountingPlatform()

        def handler(request):
            if request.url.host == "b23.tv":
                return httpx.Response(
                    302,
                    headers={
                        "location": "https://www.bilibili.com/video/BV1abc123?p=2"
                    },
                )
            return platform(request)

        _, result, _ = self._service(_adapter(handler)).process("https://b23.tv/part")
        self.assertTrue(any("分P" in item for item in result.warnings))

    def test_missing_subtitle_focused_is_unknown_not_metadata_answer(self):
        service = self._service(_adapter(CountingPlatform(with_subtitles=False)))
        _, result, _ = service.process(
            "https://www.bilibili.com/video/BV1abc123", "收费标准"
        )
        self.assertEqual(result.focused_answer.mention_status, "unknown_incomplete_transcript")
        self.assertEqual(result.focused_answer.supporting_segments, [])

    def test_ambiguous_and_unsupported_inputs_have_stable_codes(self):
        service = self._service(_adapter(CountingPlatform()))
        with self.assertRaises(PipelineError) as ambiguous:
            service.process(
                "https://www.bilibili.com/video/BV1abc123 "
                "https://b23.tv/another"
            )
        self.assertEqual(ambiguous.exception.code, "AMBIGUOUS_LINKS")
        with self.assertRaises(PipelineError) as unsupported:
            service.process("https://example.com/video/1")
        self.assertEqual(unsupported.exception.code, "UNSUPPORTED_PLATFORM")

    def test_page_size_and_malformed_page_degrade_or_fail_safely(self):
        oversized = _adapter(
            lambda _: httpx.Response(
                200, content=b"x", headers={"content-length": str(4 * 1024 * 1024 + 1)}
            )
        )
        with self.assertRaises(PipelineError):
            oversized.resolve("https://www.bilibili.com/video/BV1abc123")

        service = self._service(
            _adapter(
                lambda _: httpx.Response(
                    200,
                    text="<broken",
                    headers={"content-type": "text/html; charset=utf-8"},
                )
            )
        )
        job, result, _ = service.process(
            "https://www.bilibili.com/video/BV1abc123"
        )
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(result.title, "")
        self.assertEqual(result.evidence, [])

    def test_oversize_subtitle_degrades_without_fabricating_content(self):
        def handler(request):
            if request.url.host == "aisubtitle.hdslb.com":
                return httpx.Response(
                    200,
                    content=b"x",
                    headers={"content-length": str(10 * 1024 * 1024 + 1)},
                )
            return httpx.Response(
                200, text=_page(), headers={"content-type": "text/html"}
            )

        job, result, _ = self._service(_adapter(handler)).process(
            "https://www.bilibili.com/video/BV1abc123"
        )
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(result.subtitle_source, "none")
        self.assertEqual(result.evidence, [])

    def test_network_exception_maps_to_stable_public_error(self):
        def handler(_):
            raise httpx.ConnectError("offline")

        with self.assertRaises(PipelineError) as caught:
            self._service(_adapter(handler)).process(
                "https://www.bilibili.com/video/BV1abc123"
            )
        self.assertEqual(caught.exception.code, "CONTENT_UNAVAILABLE")
        self.assertNotIn("offline", str(caught.exception))

    def test_page_and_subtitle_content_types_are_enforced(self):
        adapter = _adapter(
            lambda _: httpx.Response(
                200, text=_page(), headers={"content-type": "application/json"}
            )
        )
        with self.assertRaises(PipelineError):
            adapter.resolve("https://www.bilibili.com/video/BV1abc123")

        def handler(request):
            if request.url.host == "aisubtitle.hdslb.com":
                return httpx.Response(
                    200,
                    json={"body": [{"from": 0, "to": 1, "content": "不能采用"}]},
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200, text=_page(), headers={"content-type": "text/html"}
            )

        job, result, _ = self._service(_adapter(handler)).process(
            "https://www.bilibili.com/video/BV1abc123"
        )
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(result.subtitle_source, "none")

    def test_same_public_id_is_namespaced_by_platform(self):
        service = self._service(_adapter(CountingPlatform()))
        _, bili, _ = service.process(
            "https://www.bilibili.com/video/BV1abc123"
        )
        local = bili.model_copy(
            update={
                "platform": "local_upload",
                "source_url": "local://collision.mp4",
                "canonical_url": "local://sha256/BV1abc123",
            }
        )
        self.repo.save_result(local)
        self.assertEqual(
            self.repo.load_result("BV1abc123", platform="bilibili").platform,
            "bilibili",
        )
        self.assertEqual(
            self.repo.load_result("BV1abc123", platform="local_upload").platform,
            "local_upload",
        )
        with self.assertRaises(AmbiguousVideoIdError):
            self.repo.load_result("BV1abc123")
        response = self._api(service).post(
            "/api/v1/videos/BV1abc123/extractions",
            json={"focus_query": "收费"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "AMBIGUOUS_VIDEO_ID")

    def test_legacy_database_migrates_to_namespaced_resource_keys(self):
        path = self.root / "legacy.sqlite3"
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
                """
            )
            result = self._service(_adapter(CountingPlatform())).process(
                "https://www.bilibili.com/video/BV1abc123"
            )[1]
            payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
            db.execute(
                """INSERT INTO videos VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.video_id, result.platform, result.source_url, result.title,
                    payload, result.subtitle_source, result.raw_transcript,
                    result.clean_transcript, "[]", "completed", "now", "now",
                ),
            )
            db.execute(
                "INSERT INTO transcript_segments VALUES (?, 0, 'seg-1', 0, 1, '字幕')",
                (result.video_id,),
            )
            db.execute(
                """INSERT INTO extractions
                (video_id, mode, focus_query, query_hash, result_json, created_at)
                VALUES (?, 'full', '', NULL, ?, 'now')""",
                (result.video_id, payload),
            )
            db.execute("PRAGMA user_version=1")
            db.commit()
        migrated = SQLiteRepository(path)
        loaded = migrated.load_result("BV1abc123", platform="bilibili")
        self.assertEqual(loaded.video_id, "BV1abc123")
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
            row = db.execute(
                "SELECT resource_key, public_video_id FROM videos"
            ).fetchone()
            foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(row, ("bilibili:BV1abc123", "BV1abc123"))
        self.assertEqual(foreign_key_errors, [])

    def test_resolution_api_uses_strong_schema(self):
        client = self._api(self._service(_adapter(CountingPlatform())))
        preview = client.post(
            "/api/v1/resolution-preview",
            json={
                "input_text": "https://www.bilibili.com/video/BV1abc123",
                "focus_query": "",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["title"], "公开课程")
        self.assertEqual(preview.json()["author"], "测试作者")
        self.assertEqual(preview.json()["platform"], "bilibili")
        response = client.post(
            "/api/v1/resolutions",
            json={
                "input_text": "https://www.bilibili.com/video/BV1abc123",
                "focus_query": "",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["job"]["status"], "completed")
        self.assertEqual(payload["result"]["video_id"], "BV1abc123")
        invalid = client.post(
            "/api/v1/resolutions",
            json={"input_text": "x", "unexpected": True},
        )
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
