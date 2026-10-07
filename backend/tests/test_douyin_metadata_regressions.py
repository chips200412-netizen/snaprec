from __future__ import annotations

import json
import unittest
from itertools import permutations

import httpx

from backend.app.adapters.douyin import DouyinAdapter
from backend.app.services.pipeline import PipelineError


VIDEO_ID = "7351234567890123456"
CANONICAL = f"https://www.douyin.com/video/{VIDEO_ID}"


def adapter_for(handler=None):
    return DouyinAdapter(
        httpx.Client(transport=httpx.MockTransport(handler or (
            lambda _: httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        ))),
        dns_resolver=lambda _: ["8.8.8.8"],
    )


class DouyinMetadataRegressionTests(unittest.TestCase):
    def test_cq2_wrapper_field_permutations_do_not_leak_into_copy(self):
        body = "刷不到外企？教你一个办法。 @讨论对象 #大学生"
        adapter = adapter_for()
        for fields in permutations(("2.02", "O@X.md", ":0pm", "jpQ:/", "05/16")):
            with self.subTest(fields=fields):
                metadata = adapter.get_share_metadata(
                    " ".join(fields) + " " + body + " https://v.douyin.com/test/"
                )
                self.assertEqual(metadata.description, body)
                self.assertEqual(metadata.title, "刷不到外企？教你一个办法")
                self.assertEqual(metadata.author, "")
                self.assertEqual(metadata.tags, ["大学生"])

    def test_cq2_incomplete_or_inline_wrapper_like_text_is_preserved(self):
        for body in (
            "2.02 O@X.md :0pm 05/16 是本次测试数据",
            "05/16 上线计划", "2.02 版本发布", "O@X.md 代码解析",
            "如何理解符号 jpQ:/ :0pm 的含义",
            "标题 2.02 O@X.md :0pm jpQ:/ 05/16 仍是正文",
        ):
            with self.subTest(body=body):
                self.assertEqual(adapter_for().get_share_metadata(
                    body + " https://v.douyin.com/test/"
                ).description, body)

    def test_cq2_long_copy_uses_first_sentence_without_losing_description(self):
        body = "刷不到外企？" + "这是保留完整分享正文的后续内容。" * 12
        metadata = adapter_for().get_share_metadata(
            "2.02 O@X.md :0pm jpQ:/ 05/16 " + body + " https://v.douyin.com/test/"
        )
        self.assertEqual(metadata.title, "刷不到外企？")
        self.assertEqual(metadata.description, body)

    def test_cq2_long_first_sentence_has_bounded_explicit_truncation(self):
        for body in ("长" * 81, "长" * 90 + "。后续正文", "版本 v2.02 " + "长" * 90):
            with self.subTest(body=body):
                metadata = adapter_for().get_share_metadata(body + " https://v.douyin.com/test/")
                self.assertEqual(metadata.title, body[:79] + "…")
                self.assertEqual(len(metadata.title), 80)
                self.assertEqual(metadata.description, body)

    def test_cq2_short_copy_and_exact_title_limit_are_not_truncated(self):
        for body in ("短标题", "版本 v2.02 发布！后续很短", "长" * 80):
            metadata = adapter_for().get_share_metadata(body + " https://v.douyin.com/test/")
            self.assertEqual(metadata.title, body)
            self.assertEqual(metadata.description, body)

    def test_cq2_legacy_marker_adjacent_to_body_still_strips_only_marker(self):
        for prefix in ("x:/", "9.76 NWm:/", "2.02 O@X.md :0pm 05/16 jpQ:/"):
            metadata = adapter_for().get_share_metadata(prefix + "短标题 https://v.douyin.com/test/")
            self.assertEqual(metadata.description, "短标题")
            self.assertEqual(metadata.title, "短标题")

    def test_share_prefix_order_and_copy_preserve_mentions_and_topics(self):
        body = "网站部署上线来喽，分享3个方法~@抖音科技 # vibecoding # 网站上线"
        for prefix in (
            "9.76 NWm:/ :6pm W@Z.mD 11/11 ",
            "0.58 d@N.JI vfB:/ 05/06 :6pm ",
        ):
            metadata = adapter_for().get_share_metadata(
                prefix + body + " https://v.douyin.com/test/ 复制此链接，打开Dou音搜索，直接观看视频！"
            )
            self.assertEqual(metadata.author, "")
            self.assertEqual(metadata.title, "网站部署上线来喽，分享3个方法~")
            self.assertEqual(metadata.description, body)
            self.assertEqual(metadata.tags, ["vibecoding", "网站上线"])

    def test_only_explicit_author_structure_can_supply_share_author(self):
        metadata = adapter_for().get_share_metadata(
            "【小羊同学的作品】部署教程 @抖音科技 # 教程 https://v.douyin.com/test/"
        )
        self.assertEqual(metadata.author, "小羊同学")
        self.assertEqual(metadata.description, "部署教程 @抖音科技 # 教程")

    def test_legal_title_date_code_and_inline_marker_are_not_stripped(self):
        for body in ("11/11 上线计划", "W@Z.mD 是本期名称", "如何理解符号 x:/ 的含义"):
            metadata = adapter_for().get_share_metadata(
                body + " https://v.douyin.com/test/"
            )
            self.assertEqual(metadata.description, body)
        metadata = adapter_for().get_share_metadata(
            "x:/ 11/11 上线计划 https://v.douyin.com/test/"
        )
        self.assertEqual(metadata.description, "11/11 上线计划")

    def test_url_only_is_not_share_copy(self):
        for text in (CANONICAL, f"复制此链接 {CANONICAL}，打开Dou音搜索，直接观看视频！"):
            metadata = adapter_for().get_share_metadata(text)
            self.assertEqual(metadata.description, "")
            self.assertEqual(metadata.author, "")

    def test_modal_url_resolves_exact_video_and_preserves_source(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            payload = {"items": [
                {"aweme_id": "7999999999999999999", "desc": "其他视频", "author": {"nickname": "其他作者"}},
                {"aweme_id": VIDEO_ID, "desc": "目标视频", "author": {"nickname": "目标作者"}},
            ]}
            return httpx.Response(200, text=(
                '<script type="application/json">' + json.dumps(payload) + '</script>'
            ), headers={"content-type": "text/html"})

        adapter = adapter_for(handler)
        source = f"https://www.douyin.com/user/test-user?modal_id={VIDEO_ID}&from=share"
        self.assertTrue(adapter.match(source))
        self.assertEqual(adapter.identify(source), (VIDEO_ID, CANONICAL))
        video = adapter.resolve(source)
        self.assertEqual(calls, [CANONICAL])
        self.assertEqual(video.source_url, source)
        self.assertEqual(video.canonical_url, CANONICAL)
        self.assertEqual(adapter.get_metadata(video).author, "目标作者")

    def test_invalid_modal_identity_never_fetches_or_selects_payload(self):
        calls = []
        adapter = adapter_for(lambda request: calls.append(str(request.url)))
        for query in ("", "modal_id=", "modal_id=abc", "modal_id=123x",
                      "modal_id=１２３", "modal_id=123&modal_id=456", "modal_id=123&modal_id=123"):
            source = f"https://www.douyin.com/user/test-user?{query}"
            self.assertFalse(adapter.match(source))
            self.assertIsNone(adapter.identify(source))
            with self.assertRaises(PipelineError):
                adapter.resolve(source)
        self.assertEqual(calls, [])

    def test_modal_normalization_does_not_bypass_original_url_security(self):
        calls = []
        adapter = adapter_for(lambda request: calls.append(str(request.url)))
        for origin in ("https://secret@www.douyin.com", "https://www.douyin.com:444", "ftp://www.douyin.com"):
            with self.assertRaises(PipelineError):
                adapter.resolve(f"{origin}/user/test-user?modal_id={VIDEO_ID}")
        self.assertEqual(calls, [])

    def test_target_payload_fields_win_over_unrelated_page_meta(self):
        payload = {"aweme_id": VIDEO_ID, "desc": "目标正文", "author": {"nickname": "目标作者"},
                   "video": {"cover": {"url_list": ["https://p3.byteimg.com/target.jpg"]}},
                   "text_extra": [{"hashtag_name": "目标话题"}]}
        page = ('<meta property="og:title" content="页面标题">'
                '<meta property="og:description" content="页面描述">'
                '<meta name="author" content="账号页作者">'
                '<meta property="og:image" content="https://p3.byteimg.com/profile.jpg">'
                '<meta name="keywords" content="其他话题">'
                '<script type="application/json">' + json.dumps(payload) + '</script>')
        adapter = adapter_for(lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        metadata = adapter.get_metadata(adapter.resolve(CANONICAL))
        self.assertEqual(metadata.title, "目标正文")
        self.assertEqual(metadata.description, "目标正文")
        self.assertEqual(metadata.author, "目标作者")
        self.assertEqual(metadata.cover_url, "https://p3.byteimg.com/target.jpg")
        self.assertEqual(metadata.tags, ["目标话题"])

    def test_known_video_redirect_cannot_silently_change_identity(self):
        def handler(request):
            if str(request.url) == CANONICAL:
                return httpx.Response(302, headers={
                    "location": "https://www.douyin.com/video/7999999999999999999"
                })
            return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        adapter = adapter_for(handler)
        with self.assertRaises(PipelineError):
            adapter.resolve(CANONICAL)

    def test_short_link_modal_redirect_fetches_only_selected_video(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if request.url.host == "v.douyin.com":
                return httpx.Response(302, headers={"location":
                    f"https://www.douyin.com/user/test-user?modal_id={VIDEO_ID}"})
            return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
        adapter = adapter_for(handler)
        self.assertEqual(adapter.resolve("https://v.douyin.com/test/").video_id, VIDEO_ID)
        self.assertEqual(calls, ["https://v.douyin.com/test/", CANONICAL])

    def test_public_payload_for_different_identity_fails_closed(self):
        payload = {"aweme_id": "7999999999999999999", "desc": "其他视频"}
        page = ('<meta name="author" content="其他作者">'
                '<script type="application/json">' + json.dumps(payload) + '</script>')
        adapter = adapter_for(lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        with self.assertRaises(PipelineError):
            adapter.resolve(CANONICAL)

    def test_target_missing_fields_do_not_inherit_generic_page_metadata(self):
        payload = {"items": [{"aweme_id": VIDEO_ID, "video": {}},
                             {"aweme_id": "7999999999999999999", "desc": "其他视频"}]}
        page = ('<meta property="og:title" content="其他标题">'
                '<meta property="og:description" content="其他描述">'
                '<meta name="author" content="其他作者">'
                '<meta property="og:image" content="https://p3.byteimg.com/other.jpg">'
                '<meta name="keywords" content="其他话题">'
                '<script type="application/json">' + json.dumps(payload) + '</script>')
        adapter = adapter_for(lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        metadata = adapter.get_metadata(adapter.resolve(CANONICAL))
        self.assertEqual((metadata.title, metadata.description, metadata.author, metadata.cover_url), ("", "", "", ""))
        self.assertEqual(metadata.tags, [])

    def test_pure_meta_and_unidentified_nested_data_remain_supported(self):
        page = ('<meta property="og:title" content="页面标题">'
                '<meta name="author" content="页面作者">'
                '<script type="application/json">{"nested":{"author":{"nickname":"无身份数据"}}}</script>')
        adapter = adapter_for(lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        metadata = adapter.get_metadata(adapter.resolve(CANONICAL))
        self.assertEqual(metadata.title, "页面标题")
        self.assertEqual(metadata.author, "页面作者")
