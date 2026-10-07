from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from backend.app.adapters.douyin import DouyinAdapter
from backend.app.domain.models import CollectionItemCreateRequest, CollectionPreviewRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.capture import CaptureService
from backend.app.services.collections import CollectionService


class _Validator:
    def resolve(self, url):
        return object()


class _NoGenericFetch:
    url_validator = _Validator()

    def fetch(self, url):
        raise AssertionError("Dedicated platform capture must not use generic fallback")


class _Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def matching(self, url):
        return self.adapter if self.adapter.match(url) else None


class CaptureQualityTests(unittest.TestCase):
    def test_cq2_preview_save_reread_and_shared_cache_remain_separate(self):
        source_url = "https://www.douyin.com/video/7351234567890123456"
        page = "<html></html>"
        calls = []

        def handler(request):
            calls.append(str(request.url))
            self.assertEqual(str(request.url), source_url)
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})

        with tempfile.TemporaryDirectory() as temp, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            repo = SQLiteRepository(Path(temp) / "synthetic-cq2.sqlite3")
            adapter = DouyinAdapter(client, dns_resolver=lambda _: ["8.8.8.8"])
            capture = CaptureService(repo, fetcher=_NoGenericFetch(), adapter_registry=_Registry(adapter))
            body = "刷不到外企？" + "完整正文不会被标题截断影响。" * 15 + " @讨论对象 #大学生"
            raw = "2.02 O@X.md :0pm jpQ:/ 05/16 " + body + " " + source_url
            preview = capture.preview(CollectionPreviewRequest(input_text=raw))
            self.assertEqual(preview.metadata.title.value, "刷不到外企？")
            self.assertEqual(preview.metadata.title.source, "share_text")
            self.assertEqual(preview.metadata.source_copy.value, body)
            self.assertEqual(preview.metadata.source_copy.source, "share_text")
            self.assertEqual(preview.metadata.author.value, "")
            service = CollectionService(repo)
            saved = service.create(CollectionItemCreateRequest(
                preview_id=preview.preview_id, user_title="我的标题", personal_tags=["我的标签"],
            ), "synthetic-cq2-create")
            reread = service.get(saved.id)
            self.assertEqual(reread.original_input, raw)
            self.assertEqual(reread.metadata, preview.metadata)
            self.assertEqual(reread.display_title, "我的标题")
            self.assertEqual(reread.personal_tags, ["我的标签"])

            plain = capture.preview(CollectionPreviewRequest(input_text=source_url))
            self.assertEqual(plain.metadata.title.value, "")
            self.assertEqual(plain.metadata.source_copy.value, "")
            self.assertEqual(plain.metadata.author.value, "")
            self.assertEqual(len(calls), 1)

            # Public values stay authoritative and are not shortened by the
            # share-only excerpt rule; refreshing never mutates a saved item.
            public_title = "公开标题" * 30
            page = (f'<meta property="og:title" content="{public_title}">'
                    '<meta name="author" content="公开作者">'
                    '<meta property="og:description" content="公开简介">')
            refreshed = capture.preview(CollectionPreviewRequest(input_text=raw, refresh_metadata=True))
            self.assertEqual(refreshed.metadata.title.value, public_title)
            self.assertEqual(refreshed.metadata.author.value, "公开作者")
            self.assertEqual(refreshed.metadata.author.source, "platform_public")
            self.assertEqual(refreshed.metadata.source_copy.value, "公开简介")
            self.assertEqual(service.get(saved.id), reread)
