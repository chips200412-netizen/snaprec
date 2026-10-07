from __future__ import annotations

import tempfile
import unittest
from itertools import permutations
from pathlib import Path

import httpx

from backend.app.adapters.douyin import DouyinAdapter
from backend.app.domain.models import CollectionItemCreateRequest, CollectionPreviewRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.capture import CaptureService
from backend.app.services.collections import CollectionService
from backend.tests.test_capture_quality import _NoGenericFetch, _Registry


URL = "https://www.douyin.com/video/7351234567890123456"
SHORT_URL = "https://v.douyin.com/test/"
BODY = "问--荡漾是种颜色吗！出自哪里？"
COPY = BODY + " #情侣日常#生活 🐧 @讨论对象"
PREFIX = "6.97 复制打开抖音，看看【飞飞鱼是大哥的作品】 "
FIELDS = ("xSl:/", "k@P.Xz", "09/19", ":6pm")


class PhoneShareTitleTests(unittest.TestCase):
    def setUp(self):
        self.client = httpx.Client(transport=httpx.MockTransport(
            lambda _: self.fail("Pure share parsing must not fetch")
        ))
        self.addCleanup(self.client.close)
        self.adapter = DouyinAdapter(self.client, dns_resolver=lambda _: self.fail("No DNS"))

    def test_phone_copy_removes_invitation_and_all_24_suffix_orders(self):
        for fields in permutations(FIELDS):
            with self.subTest(fields=fields):
                metadata = self.adapter.get_share_metadata(
                    PREFIX + COPY + " " + SHORT_URL + " " + " ".join(fields)
                )
                self.assertEqual(metadata.title, BODY + " 🐧")
                self.assertEqual(metadata.description, COPY)
                self.assertEqual(metadata.author, "飞飞鱼是大哥")
                self.assertEqual(metadata.tags, ["情侣日常", "生活"])

    def test_invitation_without_metric_and_old_prefix_are_supported(self):
        for prefix in ("", "6.97 ", "2.02 O@X.md :0pm jpQ:/ 05/16 "):
            with self.subTest(prefix=prefix):
                metadata = self.adapter.get_share_metadata(
                    prefix + "复制打开抖音，看看【作者🐧的作品】" + BODY + " " + URL
                )
                self.assertEqual(metadata.title, BODY)
                self.assertEqual(metadata.author, "作者🐧")
        legacy = self.adapter.get_share_metadata("2.02 O@X.md :0pm jpQ:/ 05/16 " + BODY + " " + URL)
        self.assertEqual(legacy.description, BODY)

    def test_suffix_needs_exact_douyin_url_boundary(self):
        tail = " ".join(FIELDS)
        for url in ("https://example.com/test/", "https://v.douyin.com.example.com/test/",
                    "https://v.douyin.com@example.com/test/", "https://guest@v.douyin.com/test/",
                    "https://example.com@v.douyin.com/test/", "https://v.douyin.com:444/test/"):
            with self.subTest(url=url):
                self.assertEqual(self.adapter.get_share_metadata(BODY + " " + url + " " + tail).description,
                                 BODY + " " + tail)
        for raw in (BODY + " " + tail, BODY + " " + tail + " " + SHORT_URL):
            with self.subTest(raw=raw):
                self.assertIn(tail, self.adapter.get_share_metadata(raw).description)
        # Without whitespace the marker is part of the URL under the existing
        # URL extractor; the remaining incomplete suffix must still survive.
        self.assertEqual(self.adapter.get_share_metadata(BODY + " " + SHORT_URL + tail).description,
                         BODY + " " + " ".join(FIELDS[1:]))

    def test_partial_duplicate_or_extended_suffix_is_preserved(self):
        tails = [" ".join(FIELDS[:index] + FIELDS[index + 1:]) for index in range(4)]
        tails += [" ".join(FIELDS) + " 后续正文", "后续正文 " + " ".join(FIELDS),
                  " ".join(FIELDS) + " xSl:/", "xSl:/ k@P.Xz 09/19 09/19",
                  "xSl:/ k@P.Xz 09/19 :6pm。", "xSl:/ k@P.Xz 09/19 :6pm 更多:内容"]
        for tail in tails:
            with self.subTest(tail=tail):
                self.assertEqual(self.adapter.get_share_metadata(BODY + " " + URL + " " + tail).description,
                                 BODY + " " + tail)

    def test_inline_prose_and_malformed_author_are_preserved(self):
        bodies = ["09/19 上线计划", "6.97 版本发布", "k@P.Xz 代码解析", "时间 :6pm 是示例",
                  "正文 " + PREFIX + BODY, "6.97 复制打开抖音，看看【的作品】正文",
                  "复制打开抖音，看看【作者的作品正文", "复制打开抖音，看看【作者的作品】】正文",
                  "复制打开抖音，看看【作者【别名】的作品】正文",
                  "复制打开抖音，看看【   的作品】正文", "复制打开抖音，看看【@的作品】正文",
                  "复制打开抖音，看看【作者\n名字的作品】正文",
                  "复制打开抖音，看看【" + "字" * 81 + "的作品】正文"]
        for body in bodies:
            with self.subTest(body=body):
                metadata = self.adapter.get_share_metadata(body + " " + URL)
                self.assertEqual(metadata.description, " ".join(body.split()))
                self.assertEqual(metadata.author, "")

    def test_share_excerpt_preserves_full_copy_and_emoji(self):
        body = BODY + "🐧后续完整正文。" * 20 + " #情侣日常"
        metadata = self.adapter.get_share_metadata(PREFIX + body + " " + URL + " " + " ".join(FIELDS))
        self.assertEqual(metadata.title, "问--荡漾是种颜色吗！")
        self.assertEqual(metadata.description, body)
        self.assertLessEqual(len(metadata.title), 80)

    def test_capture_cache_public_priority_and_saved_snapshots_survive_rebuild(self):
        page = '<html><head><meta name="author" content="公开作者"></head></html>'
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})

        with tempfile.TemporaryDirectory() as temp, httpx.Client(transport=httpx.MockTransport(handler)) as client:
            database = Path(temp) / "phone-share-synthetic.sqlite3"
            repo = SQLiteRepository(database)
            capture = CaptureService(repo, fetcher=_NoGenericFetch(), adapter_registry=_Registry(
                DouyinAdapter(client, dns_resolver=lambda _: ["8.8.8.8"])
            ))
            raw = PREFIX + COPY + " " + URL + " " + " ".join(FIELDS)
            preview = capture.preview(CollectionPreviewRequest(input_text=raw))
            self.assertEqual(preview.metadata.title.value, BODY + " 🐧")
            self.assertEqual(preview.metadata.title.source, "share_text")
            self.assertEqual(preview.metadata.author.value, "公开作者")
            self.assertEqual(preview.metadata.author.source, "platform_public")
            self.assertEqual(preview.metadata.source_copy.value, COPY)
            first_snapshot = repo.get_collection_preview(preview.preview_id)
            saved = CollectionService(repo).create(CollectionItemCreateRequest(
                preview_id=preview.preview_id, user_title="用户标题", personal_tags=["个人标签"],
                selected_source_topic_indices=[0],
            ), "phone-share-save")
            other = capture.preview(CollectionPreviewRequest(input_text=
                "复制打开抖音，看看【其他作者的作品】另一正文 #另话题 " + URL + " " + " ".join(FIELDS)
            ))
            self.assertEqual(other.metadata.title.value, "另一正文")
            self.assertEqual(other.metadata.source_copy.value, "另一正文 #另话题")
            self.assertEqual(other.metadata.author.value, "公开作者")
            plain = capture.preview(CollectionPreviewRequest(input_text=URL))
            self.assertEqual(plain.metadata.title.value, "")
            self.assertEqual(plain.metadata.source_copy.value, "")
            self.assertEqual(plain.metadata.platform_tags, [])
            self.assertEqual(len(calls), 1)

            page = ('<html><head><meta property="og:title" content="公开标题">'
                    '<meta name="author" content="新的公开作者">'
                    '<meta property="og:description" content="公开简介"></head></html>')
            refreshed = capture.preview(CollectionPreviewRequest(input_text=raw, refresh_metadata=True))
            self.assertEqual(refreshed.metadata.title.value, "公开标题")
            self.assertEqual(refreshed.metadata.source_copy.value, "公开简介")
            self.assertEqual(refreshed.metadata.author.value, "新的公开作者")
            del capture, repo  # Repository connections close after each operation.
            reopened = SQLiteRepository(database)
            reread = CollectionService(reopened).get(saved.id)
            self.assertEqual(reread, saved)
            self.assertEqual(reread.original_input, raw)
            self.assertEqual(reread.display_title, "用户标题")
            self.assertEqual(reread.metadata, preview.metadata)
            self.assertEqual(reread.selected_source_topic_indices, [0])
            self.assertEqual(reopened.get_collection_preview(preview.preview_id), first_snapshot)
