from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import httpx

from backend.app.adapters import AdapterRegistry, BilibiliAdapter, DouyinAdapter
from backend.app.domain.models import Job
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import PipelineError
from backend.app.services.providers import (
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
)
from backend.app.services.resolution import ResolutionService


VIDEO_ID = "7351234567890123456"
REAL_SHARE_VIDEO_ID = "7670076848583331122"
REAL_SHARE_TEXT = (
    "0.58 d@N.JI vfB:/ 05/06 :6pm Hermes v0.20正式上线！Agent能力迈向新阶段！ "
    "Hermes v0.20正式发布！从AI助手到Agent平台，这次升级带来了哪些变化？ "
    "# Hermes # HermesAgent # Ai新星计划 # vibecoding大赏 @抖音科技 # Ai  "
    "https://v.douyin.com/dcJDbmf4_h8/ 复制此链接，打开Dou音搜索，直接观看视频！"
)


def _page(
    *,
    subtitles: list[dict] | None = None,
    subtitle_url: str = "",
    video_id: str = VIDEO_ID,
) -> str:
    item = {
        "aweme_id": video_id,
        "desc": "公开的 AI 获客方法",
        "author": {"nickname": "测试作者", "unique_id": "author-id"},
        "video": {
            "duration": 65000,
            "cover": {"url_list": ["https://p3.byteimg.com/cover.jpg"]},
        },
        "text_extra": [{"hashtag_name": "AI"}, {"hashtag_name": "获客"}],
    }
    if subtitles is not None:
        item["captions"] = subtitles
    if subtitle_url:
        item["subtitleInfos"] = [{"url": subtitle_url}]
    state = {"app": {"video": item}}
    return (
        '<meta property="og:title" content="公开的 AI 获客方法">'
        '<meta property="og:description" content="公开视频简介">'
        '<meta name="keywords" content="教程,AI">'
        f'<script id="RENDER_DATA" type="application/json">{quote(json.dumps(state, ensure_ascii=False))}</script>'
    )


def _multi_item_page(*, include_target: bool = True) -> str:
    recommended = {
        "aweme_id": "7999999999999999999",
        "desc": "推荐视频的错误标题",
        "author": {"nickname": "推荐作者"},
        "video": {"duration": 999000},
        "captions": [
            {"start": 0, "end": 2, "text": "推荐视频收费每月999元。"}
        ],
    }
    target = {
        "aweme_id": VIDEO_ID,
        "desc": "目标视频标题",
        "author": {"nickname": "目标作者"},
        "video": {"duration": 12000},
    }
    items = [recommended]
    if include_target:
        items.append(target)
    state = {"recommendations": items}
    return (
        '<meta property="og:title" content="页面目标标题">'
        '<meta property="og:description" content="页面目标简介">'
        f'<script id="RENDER_DATA" type="application/json">{quote(json.dumps(state, ensure_ascii=False))}</script>'
    )


class CountingDouyin:
    def __init__(self, *, subtitles: bool = True, remote_subtitle: bool = False):
        self.calls: list[str] = []
        self.subtitles = subtitles
        self.remote_subtitle = remote_subtitle

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={
                    "location": f"https://www.douyin.com/video/{VIDEO_ID}?from=share"
                },
            )
        if request.url.host == "subtitle.douyinvod.com":
            return httpx.Response(
                200,
                json={
                    "utterances": [
                        {"start_time": 0, "end_time": 3000, "text": "先确认目标客户。"},
                        {"start_time": 3000, "end_time": 7000, "text": "收费标准是每月300元。"},
                    ]
                },
                headers={"content-type": "application/json"},
            )
        inline = (
            [
                {"start": 0, "end": 3, "text": "先确认目标客户。"},
                {"start": 3, "end": 7, "text": "收费标准是每月300元。"},
            ]
            if self.subtitles and not self.remote_subtitle
            else None
        )
        subtitle_url = (
            "https://subtitle.douyinvod.com/caption.json"
            if self.subtitles and self.remote_subtitle
            else ""
        )
        return httpx.Response(
            200,
            text=_page(subtitles=inline, subtitle_url=subtitle_url),
            headers={"content-type": "text/html; charset=utf-8"},
        )


def _adapter(handler, *, dns=None) -> DouyinAdapter:
    return DouyinAdapter(
        httpx.Client(transport=httpx.MockTransport(handler)),
        dns_resolver=dns or (lambda _: ["8.8.8.8"]),
    )


class DouyinResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.temp.name) / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _service(self, platform: CountingDouyin | object) -> ResolutionService:
        adapter = platform if isinstance(platform, DouyinAdapter) else _adapter(platform)
        return ResolutionService(
            self.repo,
            AdapterRegistry([BilibiliAdapter(), adapter]),
            DeterministicFullExtractor(),
            DeterministicFocusedExtractor(),
        )

    def test_matches_only_supported_direct_and_short_links(self):
        adapter = _adapter(CountingDouyin())
        self.assertTrue(
            adapter.match(f"https://www.douyin.com/video/{VIDEO_ID}?from=share")
        )
        self.assertTrue(adapter.match("https://v.douyin.com/AbC123/"))
        self.assertFalse(adapter.match(f"https://douyin.com/video/{VIDEO_ID}"))
        self.assertFalse(adapter.match(f"https://evil.example/video/{VIDEO_ID}"))
        self.assertFalse(adapter.match("javascript:alert(1)"))

    def test_share_text_short_redirect_and_canonical_alias_reuse(self):
        platform = CountingDouyin()
        service = self._service(platform)
        job, result, _ = service.process(
            "复制这条链接 https://v.douyin.com/AbC123/，打开抖音观看"
        )
        self.assertEqual(job.status, "completed")
        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.video_id, VIDEO_ID)
        self.assertEqual(
            result.canonical_url, f"https://www.douyin.com/video/{VIDEO_ID}"
        )
        calls = len(platform.calls)
        cached = service.process(f"https://www.douyin.com/video/{VIDEO_ID}")[1]
        self.assertEqual(cached.platform, "douyin")
        self.assertEqual(len(platform.calls), calls)
        self.assertEqual(
            self.repo.load_video_alias("https://v.douyin.com/AbC123/"),
            ("douyin", VIDEO_ID),
        )

    def test_real_share_redirect_domain_and_copy_metadata_fallback(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            self.assertEqual(request.headers.get("accept-encoding"), "identity")
            if request.url.host == "v.douyin.com":
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://www.iesdouyin.com/share/video/"
                            f"{REAL_SHARE_VIDEO_ID}/?from=web_code_link"
                        )
                    },
                )
            if request.url.host == "www.iesdouyin.com":
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://www.douyin.com/video/"
                            f"{REAL_SHARE_VIDEO_ID}?previous_page=web_code_link"
                        )
                    },
                )
            return httpx.Response(
                200,
                text="<html><head></head><body></body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        video, metadata = self._service(_adapter(handler)).preview(REAL_SHARE_TEXT)

        self.assertEqual(video.video_id, REAL_SHARE_VIDEO_ID)
        self.assertEqual(
            video.canonical_url,
            f"https://www.douyin.com/video/{REAL_SHARE_VIDEO_ID}",
        )
        self.assertEqual(metadata.author, "")
        self.assertEqual(
            metadata.title,
            "Hermes v0.20正式上线！Agent能力迈向新阶段！ "
            "Hermes v0.20正式发布！从AI助手到Agent平台，这次升级带来了哪些变化？",
        )
        self.assertEqual(
            metadata.tags,
            ["Hermes", "HermesAgent", "Ai新星计划", "vibecoding大赏", "Ai"],
        )
        self.assertTrue(any("分享文案" in warning for warning in metadata.warnings))
        self.assertEqual(
            [httpx.URL(value).host for value in calls],
            ["v.douyin.com", "www.iesdouyin.com", "www.douyin.com"],
        )

    def test_metadata_inline_public_subtitle_and_full_extraction(self):
        result = self._service(CountingDouyin()).process(
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )[1]
        self.assertEqual(result.title, "公开的 AI 获客方法")
        self.assertEqual(result.author, "测试作者")
        self.assertEqual(result.description, "公开的 AI 获客方法")
        self.assertEqual(result.tags, ["AI", "获客"])
        self.assertEqual(result.duration, 65)
        self.assertEqual(result.subtitle_source, "public_page")
        self.assertIn("每月300元", result.raw_transcript)
        self.assertTrue(result.evidence)

    def test_public_metadata_is_not_overwritten_by_share_copy(self):
        _, metadata = self._service(CountingDouyin()).preview(
            "x:/ 公开页之外的标题 #分享话题 @分享作者 "
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )
        self.assertEqual(metadata.title, "公开的 AI 获客方法")
        self.assertEqual(metadata.author, "测试作者")
        self.assertEqual(metadata.tags, ["AI", "获客"])
        self.assertFalse(any("分享文案" in warning for warning in metadata.warnings))

    def test_real_three_hop_process_and_resolved_checkpoint_resume(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.host == "v.douyin.com":
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://www.iesdouyin.com/share/video/"
                            f"{REAL_SHARE_VIDEO_ID}/"
                        )
                    },
                )
            if request.url.host == "www.iesdouyin.com":
                return httpx.Response(
                    302,
                    headers={
                        "location": (
                            "https://www.douyin.com/video/"
                            f"{REAL_SHARE_VIDEO_ID}?previous_page=web_code_link"
                        )
                    },
                )
            return httpx.Response(
                200,
                text=_page(
                    video_id=REAL_SHARE_VIDEO_ID,
                    subtitles=[
                        {"start": 0, "end": 2, "text": "Hermes Agent 正式上线。"}
                    ],
                ),
                headers={"content-type": "text/html; charset=utf-8"},
            )

        adapter = _adapter(handler)
        service = self._service(adapter)
        result = service.process(REAL_SHARE_TEXT)[1]
        self.assertEqual(result.video_id, REAL_SHARE_VIDEO_ID)
        self.assertEqual(result.subtitle_source, "public_page")
        self.assertIn("Hermes Agent", result.raw_transcript)
        self.assertEqual(len(calls), 3)

        video = adapter.resolve("https://v.douyin.com/dcJDbmf4_h8/")
        calls.clear()
        job = Job(
            id="douyin-resolved-resume",
            status="queued",
            progress=10,
            video_id=REAL_SHARE_VIDEO_ID,
        )
        self.assertTrue(self.repo.save_job(job))
        self.repo.save_job_artifact(
            job.id,
            "resolved",
            {
                "video": {
                    "platform": video.platform,
                    "source_url": video.source_url,
                    "canonical_url": video.canonical_url,
                    "video_id": video.video_id,
                    "page_html": video.page_html,
                    "aliases": list(video.aliases),
                },
                "input_metadata": {
                    "author": "抖音科技",
                    "title": "分享文案标题",
                    "description": "",
                    "tags": ["Hermes"],
                    "duration": 0,
                    "cover_url": "",
                    "warnings": ["分享文案来源提示。"],
                },
            },
        )
        resumed = service.resume(job)[1]
        self.assertEqual(calls, [])
        self.assertEqual(resumed.title, "公开的 AI 获客方法")
        self.assertEqual(resumed.subtitle_source, "public_page")
        self.assertIn("Hermes Agent", resumed.raw_transcript)

    def test_public_json_subtitle_url_is_allowlisted_and_parsed(self):
        result = self._service(
            CountingDouyin(remote_subtitle=True)
        ).process(f"https://www.douyin.com/video/{VIDEO_ID}")[1]
        self.assertEqual(result.subtitle_source, "public_page")
        self.assertEqual(result.segments[1].start, 3)

    def test_focused_mode_uses_douyin_namespace_and_evidence(self):
        result = self._service(CountingDouyin()).process(
            f"https://www.douyin.com/video/{VIDEO_ID}", "收费标准"
        )[1]
        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.extraction_mode, "focused")
        self.assertEqual(result.focused_answer.mention_status, "explicit")
        self.assertIn(
            "每月300元", result.focused_answer.supporting_segments[0].evidence
        )
        stored = self.repo.load_result(VIDEO_ID, platform="douyin")
        self.assertEqual(stored.platform, "douyin")

    def test_missing_subtitle_is_limited_and_rechecked(self):
        platform = CountingDouyin(subtitles=False)
        service = self._service(platform)
        job, result, _ = service.process(
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )
        self.assertEqual(job.status, "completed_with_warnings")
        self.assertEqual(result.subtitle_source, "none")
        self.assertEqual(result.raw_transcript, "")
        self.assertEqual(result.summary, "")
        self.assertEqual(result.full_extraction.all_items(), [])
        self.assertEqual(result.evidence, [])
        self.assertTrue(any("本地媒体" in warning for warning in result.warnings))
        calls = len(platform.calls)
        service.process(f"https://www.douyin.com/video/{VIDEO_ID}")
        self.assertGreater(len(platform.calls), calls)

    def test_missing_subtitle_focused_mode_is_unknown_not_metadata_answer(self):
        result = self._service(CountingDouyin(subtitles=False)).process(
            f"https://www.douyin.com/video/{VIDEO_ID}", "有没有讲普通人如何入门"
        )[1]
        self.assertEqual(
            result.focused_answer.mention_status,
            "unknown_incomplete_transcript",
        )
        self.assertEqual(result.focused_answer.supporting_segments, [])
        self.assertNotIn("AI 获客方法", result.focused_answer.direct_answer)

    def test_recommended_video_cannot_pollute_target_metadata_or_subtitles(self):
        adapter = _adapter(
            lambda _: httpx.Response(
                200,
                text=_multi_item_page(),
                headers={"content-type": "text/html"},
            )
        )
        result = self._service(adapter).process(
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )[1]
        self.assertEqual(result.title, "目标视频标题")
        self.assertEqual(result.author, "目标作者")
        self.assertEqual(result.duration, 12)
        self.assertEqual(result.subtitle_source, "none")
        self.assertEqual(result.raw_transcript, "")
        self.assertNotIn("999", result.raw_transcript)

    def test_missing_target_item_does_not_fallback_to_recommended_video(self):
        adapter = _adapter(
            lambda _: httpx.Response(
                200,
                text=_multi_item_page(include_target=False),
                headers={"content-type": "text/html"},
            )
        )
        with self.assertRaises(PipelineError) as caught:
            self._service(adapter).process(
                f"https://www.douyin.com/video/{VIDEO_ID}"
            )
        self.assertEqual(caught.exception.code, "CONTENT_UNAVAILABLE")

    def test_redirect_host_private_dns_mime_and_size_are_rejected(self):
        adapter = _adapter(
            lambda _: httpx.Response(
                302, headers={"location": "http://127.0.0.1/private"}
            )
        )
        with self.assertRaises(PipelineError):
            adapter.resolve("https://v.douyin.com/unsafe/")

        for location in (
            f"https://evil.www.iesdouyin.com/share/video/{VIDEO_ID}",
            f"https://www.iesdouyin.com:444/share/video/{VIDEO_ID}",
        ):
            calls_before_redirect: list[str] = []
            lookalike = _adapter(
                lambda request, target=location: (
                    calls_before_redirect.append(str(request.url))
                    or httpx.Response(302, headers={"location": target})
                )
            )
            with self.assertRaises(PipelineError):
                lookalike.resolve("https://v.douyin.com/unsafe/")
            self.assertEqual(len(calls_before_redirect), 1)

        self.assertFalse(
            adapter.match(f"https://www.iesdouyin.com/share/video/{VIDEO_ID}")
        )

        calls: list[str] = []
        private = _adapter(
            lambda request: calls.append(str(request.url)) or httpx.Response(200),
            dns=lambda _: ["127.0.0.1"],
        )
        with self.assertRaises(PipelineError):
            private.resolve(f"https://www.douyin.com/video/{VIDEO_ID}")
        self.assertEqual(calls, [])

        wrong_mime = _adapter(
            lambda _: httpx.Response(
                200, text=_page(), headers={"content-type": "application/json"}
            )
        )
        with self.assertRaises(PipelineError):
            wrong_mime.resolve(f"https://www.douyin.com/video/{VIDEO_ID}")

        oversized = _adapter(
            lambda _: httpx.Response(
                200,
                content=b"x",
                headers={"content-length": str(4 * 1024 * 1024 + 1)},
            )
        )
        with self.assertRaises(PipelineError):
            oversized.resolve(f"https://www.douyin.com/video/{VIDEO_ID}")

    def test_subtitle_unlisted_host_and_non_json_degrade_safely(self):
        unlisted = _adapter(
            lambda _: httpx.Response(
                200,
                text=_page(subtitle_url="https://evil.example/subtitle.json"),
                headers={"content-type": "text/html"},
            )
        )
        result = self._service(unlisted).process(
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )[1]
        self.assertEqual(result.subtitle_source, "none")

        def handler(request: httpx.Request):
            if request.url.host == "subtitle.douyinvod.com":
                return httpx.Response(
                    200,
                    json={"body": [{"start": 0, "end": 1, "text": "不可采用"}]},
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200,
                text=_page(
                    subtitle_url="https://subtitle.douyinvod.com/subtitle.json"
                ),
                headers={"content-type": "text/html"},
            )

        result = self._service(_adapter(handler)).process(
            f"https://www.douyin.com/video/{VIDEO_ID}"
        )[1]
        self.assertEqual(result.subtitle_source, "none")

    def test_network_error_is_mapped_without_leaking_internal_detail(self):
        adapter = _adapter(
            lambda _: (_ for _ in ()).throw(httpx.ConnectError("secret endpoint"))
        )
        with self.assertRaises(PipelineError) as caught:
            self._service(adapter).process(
                f"https://www.douyin.com/video/{VIDEO_ID}"
            )
        self.assertEqual(caught.exception.code, "CONTENT_UNAVAILABLE")
        self.assertNotIn("secret endpoint", str(caught.exception))

    def test_cross_platform_supported_links_are_ambiguous(self):
        service = self._service(CountingDouyin())
        with self.assertRaises(PipelineError) as caught:
            service.process(
                f"https://www.douyin.com/video/{VIDEO_ID} "
                "https://www.bilibili.com/video/BV1abc123"
            )
        self.assertEqual(caught.exception.code, "AMBIGUOUS_LINKS")


if __name__ == "__main__":
    unittest.main()
