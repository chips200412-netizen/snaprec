"""R2.2 Xiaohongshu collection behavior at the public HTTP/SQLite seam.

All platform responses and DNS answers are synthetic. These tests do not claim
that unauthenticated live notes are available from any particular device/region.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from html import escape
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.app.services.safe_http import SafePublicFetcher


NOTE_ID = "64a01234567890abcdef1234"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
PUBLIC_IP = "8.8.8.8"
OTHER_ID = "64b01234567890abcdef1234"
CDN_HOST = "sns-webpic-qc.xhscdn.com"


class _ChunkedBody(httpx.SyncByteStream):
    def __iter__(self):
        yield b"x" * (4 * 1024 * 1024)
        yield b"x"


class XiaohongshuCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="r22-xhs-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "capture.sqlite3")
        self.requests: list[httpx.Request] = []
        self.dns_calls: list[str] = []
        self.routes: dict[str, httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]] = {}
        self.answers: dict[str, list[str]] = {}
        self.dns_override = None

        def dns(host: str):
            self.dns_calls.append(host)
            if self.dns_override is not None:
                return self.dns_override(host)
            return self.answers.get(host, [PUBLIC_IP])

        def handler(request: httpx.Request):
            self.requests.append(request)
            result = self.routes.get(
                str(request.url),
                httpx.Response(200, text="<html><head></head></html>",
                               headers={"content-type": "text/html; charset=utf-8"}),
            )
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return result(request)
            # Refreshes get a fresh response stream, not the previously closed one.
            return httpx.Response(result.status_code, content=result.content,
                                  headers=result.headers)

        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.http.close)
        self.fetcher = SafePublicFetcher(client=self.http, dns_resolver=dns)
        self.asr = Mock(spec=UnconfiguredAsrProvider)
        self.extractor = Mock(spec=DeterministicFullExtractor)
        self.subtitle_provider = Mock()
        self.focused_extractor = Mock()
        self.auto_tagger = Mock()
        self.client = self.make_client()

    def make_client(self, *, repository=None, resolution_service=None, fetcher=None):
        pipeline = LocalFullPipeline(
            repository or self.repo, self.asr, self.extractor,
            temp_root=self.root / "pipeline",
            subtitle_provider=self.subtitle_provider,
            focused_extractor=self.focused_extractor,
            auto_tagger=self.auto_tagger,
        )
        client = TestClient(create_app(
            pipeline, capture_fetcher=fetcher or self.fetcher,
            resolution_service=resolution_service,
            upload_root=self.root / "uploads",
            inspiration_temp_root=self.root / "inspiration",
        ))
        self.addCleanup(client.close)
        return client

    def preview(self, text: str, *, refresh: bool = False):
        response = self.client.post(
            "/api/v1/collection-previews",
            json={"input_text": text, "refresh_metadata": refresh},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def html(self, url: str, body: str):
        self.routes[url] = httpx.Response(
            200, text=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    def redirect(self, source: str, target: str):
        self.routes[source] = httpx.Response(
            302, headers={"location": target, "content-type": "text/html"}
        )

    def save(self, preview, *, key="save", **values):
        response = self.client.post(
            "/api/v1/collection-items", headers={"Idempotency-Key": key},
            json={"preview_id": preview["preview_id"], **values},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def assert_unavailable(self, preview, *, canonical=""):
        self.assertEqual(preview["metadata_status"], "metadata_unavailable")
        self.assertEqual(preview["platform"], "xiaohongshu")
        self.assertEqual(preview["source_kind"], "webpage")
        self.assertEqual(preview["canonical_url"], canonical)
        for field in ("title", "author", "cover_url"):
            self.assertEqual(preview["metadata"][field]["value"], "")
            self.assertEqual(preview["metadata"][field]["source"], "none")
        self.assertEqual(preview["metadata"]["platform_tags"], [])
        self.assertTrue(preview["metadata"]["warnings"])

    def assert_no_deep_effects(self):
        for provider in (
            self.asr, self.extractor, self.subtitle_provider,
            self.focused_extractor, self.auto_tagger,
        ):
            self.assertEqual(provider.mock_calls, [])
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            for table in (
                "jobs", "videos", "transcript_segments", "extractions",
                "job_artifacts", "collection_deep_analysis_jobs",
                "collection_deep_retry_attempts",
            ):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)

    def test_short_link_soft404_does_not_become_material_identity_or_metadata(self):
        """R2-XHS-SAFETY-001 / R2-XHS-FALLBACK-001: HTTP 200 is not a note."""
        source = "https://xhslink.com/a/fixtureSoft404"
        final = "https://www.xiaohongshu.com/404"
        self.redirect(source, final)
        self.html(final, '<html><head><title>小红书 - 你的生活指南</title>'
                         '<meta name="description" content="欢迎探索小红书">'
                         '</head></html>')

        preview = self.preview(source)

        self.assertEqual(preview["canonical_url"], "")
        self.assertEqual(preview["identity_url"], source)
        self.assertEqual(preview["source_url"], source)
        self.assertEqual(preview["platform"], "xiaohongshu")
        self.assertEqual(preview["metadata_status"], "metadata_unavailable")
        self.assertEqual(preview["metadata"]["title"]["value"], "")
        self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
        self.assertTrue(preview["metadata"]["warnings"])

        saved = self.save(preview, user_title="手动保留的笔记")
        self.assertEqual(saved["source_url"], source)
        self.assertEqual(saved["canonical_url"], "")
        self.assertEqual(self.client.get(
            f"/api/v1/collection-items/{saved['id']}"
        ).json(), saved)
        self.assertIsNone(self.repo.get_collection_metadata_cache(final))

    def test_direct_note_shapes_preserve_original_url_and_ordered_query(self):
        """R2-XHS-INPUT-001: desktop/mobile are webpage sources, not video jobs."""
        for host, path in (
            ("www.xiaohongshu.com", f"/explore/{NOTE_ID}"),
            ("xiaohongshu.com", f"/discovery/item/{NOTE_ID}/"),
        ):
            with self.subTest(host=host, path=path):
                query = "xsec_token=fixture%2Ftoken&b=2&a=1&a=0"
                raw = f"HTTPS://{host.upper()}:443{path}?{query}#fragment"
                canonical = f"https://{host}{path}?{query}"
                self.html(canonical, "<html><head><title>公开笔记</title></head></html>")
                preview = self.preview(f"阅读 {raw}")
                self.assertEqual(preview["original_input"], f"阅读 {raw}")
                self.assertEqual(preview["source_url"], raw)
                self.assertEqual(preview["canonical_url"], canonical)
                self.assertEqual(preview["identity_url"], canonical)
                self.assertEqual(preview["platform"], "xiaohongshu")
                self.assertEqual(preview["source_kind"], "webpage")
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(str(self.requests[-1].url), canonical)
        self.assert_no_deep_effects()

    def test_url_query_suffixes_survive_plain_shared_and_explicitly_wrapped_input(self):
        """R2-XHS-INPUT-001: URL syntax is not disposable prose punctuation."""
        forms = (
            "{url}",
            " \t{url}\n ",
            "分享 {url}",
            "分享 {url} 继续记录",
            "分享 ({url})",
            '分享 "{url}"',
            "分享 '{url}'",
        )
        for suffix in ("!", ")", ".", ",", ";", ":", "?", "%21", "%29"):
            # Keep both repeated query keys and parentheses inside the URL. For
            # a trailing ')' plus an outer '(URL)', only the outer ')' is prose.
            source = f"{NOTE_URL}?b=2&a=(one)&a=1&xsec_token=abc{suffix}"
            self.html(source, "<html><head><title>原样参数笔记</title></head></html>")
            for form in forms:
                with self.subTest(suffix=suffix, form=form):
                    input_text = form.format(url=source)
                    before = len(self.requests)
                    preview = self.preview(input_text, refresh=True)
                    self.assertEqual(preview["original_input"], input_text)
                    self.assertEqual(preview["source_url"], source)
                    self.assertEqual(preview["canonical_url"], source)
                    self.assertEqual(preview["identity_url"], source)
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(preview["metadata"]["title"]["value"], "原样参数笔记")
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertEqual(str(self.requests[-1].url), source)
                    self.assertEqual(
                        self.repo.get_collection_metadata_cache(source)["identity_url"],
                        source,
                    )
        self.assert_no_deep_effects()

    def test_distinct_query_suffixes_keep_separate_cache_and_collection_identities(self):
        """R2-XHS-CACHE-001 / R2-XHS-COLLECTION-001: abc! and abc are distinct."""
        bang = f"{NOTE_URL}?b=2&a=1&xsec_token=abc!"
        plain = f"{NOTE_URL}?b=2&a=1&xsec_token=abc"
        self.html(bang, "<html><head><title>带叹号公开笔记</title></head></html>")
        self.html(plain, "<html><head><title>无叹号公开笔记</title></head></html>")
        previews = {}
        for source, expected_title in ((bang, "带叹号公开笔记"), (plain, "无叹号公开笔记")):
            with self.subTest(source=source):
                preview = self.preview(source)
                previews[source] = preview
                self.assertEqual(preview["source_url"], source)
                self.assertEqual(preview["identity_url"], source)
                self.assertEqual(preview["canonical_url"], source)
                self.assertEqual(preview["metadata"]["title"]["value"], expected_title)

        self.assertEqual([str(request.url) for request in self.requests], [bang, plain])
        before = (len(self.requests), len(self.dns_calls))
        for source in (bang, plain):
            cache = self.repo.get_collection_metadata_cache(source)
            self.assertEqual(cache["identity_url"], source)
            self.assertEqual(cache["metadata"]["title"], previews[source]["metadata"]["title"])
            cached = self.preview(f"再次分享 {source}")
            self.assertEqual(cached["source_url"], source)
            self.assertEqual(cached["identity_url"], source)
            self.assertEqual(cached["metadata"]["title"], previews[source]["metadata"]["title"])
            self.assertNotEqual(cached["preview_id"], previews[source]["preview_id"])

        saved_bang = self.save(previews[bang], key="query-bang", user_title="叹号入口素材")
        saved_plain = self.save(previews[plain], key="query-plain", user_title="普通入口素材")
        self.assertNotEqual(saved_bang["id"], saved_plain["id"])
        for source, saved in ((bang, saved_bang), (plain, saved_plain)):
            detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), saved)
            self.assertEqual(detail.json()["source_url"], source)
            self.assertEqual(detail.json()["identity_url"], source)
            deep = self.client.get(f"/api/v1/collection-items/{saved['id']}/deep-analysis")
            self.assertEqual(deep.status_code, 200, deep.text)
            self.assertEqual(deep.json()["state"], "unavailable")

        for query, expected_ids in (
            ("xsec_token=abc!", {saved_bang["id"]}),
            ("普通入口素材", {saved_plain["id"]}),
            # Search is literal containment, so the shorter token matches both.
            ("xsec_token=abc", {saved_bang["id"], saved_plain["id"]}),
        ):
            with self.subTest(query=query):
                found = self.client.get("/api/v1/collection-items", params={
                    "query": query, "platform": "xiaohongshu",
                })
                self.assertEqual(found.status_code, 200, found.text)
                self.assertEqual({item["id"] for item in found.json()["items"]}, expected_ids)
                self.assertEqual(found.json()["total"], len(expected_ids))
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assert_no_deep_effects()

    def test_unicode_query_punctuation_is_preserved_in_shared_input_and_saved_identities(self):
        """R2-XHS-INPUT-001 / R2-XHS-CACHE-001: Unicode query text stays intact."""
        for punctuation in ("！", "）", "，", "。", "：", "】", "（", "；", "？"):
            source = f"{NOTE_URL}?b=2&xsec_token=A{punctuation}B&a=1"
            wire_url = str(httpx.URL(source))
            self.html(wire_url, "<html><head><title>Unicode参数笔记</title></head></html>")
            for shared in (False, True):
                with self.subTest(punctuation=punctuation, shared=shared):
                    text = f"分享原文 {source}" if shared else source
                    preview = self.preview(text, refresh=True)
                    self.assertEqual(preview["original_input"], text)
                    self.assertEqual(preview["source_url"], source)
                    self.assertEqual(preview["canonical_url"], source)
                    self.assertEqual(preview["identity_url"], source)
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(preview["metadata"]["title"]["value"], "Unicode参数笔记")
                    self.assertEqual(preview["metadata"]["source_copy"]["value"],
                                     "分享原文" if shared else "")
                    self.assertEqual(str(self.requests[-1].url), wire_url)
                    cache = self.repo.get_collection_metadata_cache(source)
                    self.assertEqual(cache["identity_url"], source)
                    self.assertEqual(cache["metadata"]["source_copy"]["value"], "")

            # An explicit whole-URL or wrapping boundary also removes any
            # ambiguity about a final Unicode character belonging to the query.
            tail_source = f"{NOTE_URL}?tail=1&b=2&xsec_token=A{punctuation}"
            wire_tail = str(httpx.URL(tail_source))
            self.html(wire_tail, "<html><head><title>Unicode尾字符笔记</title></head></html>")
            for form in ("{url}", "分享 ({url})", '分享 "{url}"'):
                with self.subTest(punctuation=punctuation, form=form):
                    preview = self.preview(form.format(url=tail_source), refresh=True)
                    self.assertEqual(preview["source_url"], tail_source)
                    self.assertEqual(preview["canonical_url"], tail_source)
                    self.assertEqual(preview["identity_url"], tail_source)
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(str(self.requests[-1].url), wire_tail)
                    self.assertNotIn("xsec_token", preview["metadata"]["source_copy"]["value"])
                    self.assertNotIn(f"A{punctuation}", preview["metadata"]["source_copy"]["value"])

        saved_items = {}
        for suffix in ("B", "C"):
            source = f"{NOTE_URL}?pair=1&b=2&xsec_token=A！{suffix}&a=1"
            wire_url = str(httpx.URL(source))
            self.html(wire_url, f"<html><head><title>Unicode入口{suffix}</title></head></html>")
            preview = self.preview(f"我的分享 {source}")
            self.assertEqual(preview["source_url"], source)
            self.assertEqual(preview["identity_url"], source)
            self.assertEqual(preview["metadata"]["source_copy"]["value"], "我的分享")
            self.assertEqual(str(self.requests[-1].url), wire_url)
            before = (len(self.requests), len(self.dns_calls))
            cached = self.preview(source)
            self.assertEqual(cached["metadata"]["source_copy"]["value"], "")
            self.assertEqual(cached["metadata"]["title"], preview["metadata"]["title"])
            self.assertEqual(cached["canonical_url"], source)
            self.assertEqual(cached["identity_url"], source)
            saved = self.save(preview, key=f"unicode-{suffix}", user_title=f"中文参数入口{suffix}")
            self.assertEqual(saved["source_url"], source)
            self.assertEqual(saved["canonical_url"], source)
            self.assertEqual(saved["identity_url"], source)
            detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), saved)
            self.assertEqual((len(self.requests), len(self.dns_calls)), before)
            saved_items[suffix] = saved

        self.assertNotEqual(saved_items["B"]["id"], saved_items["C"]["id"])
        before = (len(self.requests), len(self.dns_calls))
        for suffix, saved in saved_items.items():
            found = self.client.get("/api/v1/collection-items", params={
                "query": f"xsec_token=A！{suffix}", "platform": "xiaohongshu",
            })
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
            self.assertEqual(found.json()["total"], 1)
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)

        # Ordinary sentence punctuation outside a no-query link remains prose.
        self.html(NOTE_URL, "<html><head><title>普通中文分享</title></head></html>")
        ordinary = self.preview(f"分享 {NOTE_URL}。")
        self.assertEqual(ordinary["source_url"], NOTE_URL)
        self.assertEqual(ordinary["canonical_url"], NOTE_URL)
        self.assertEqual(ordinary["metadata"]["source_copy"]["value"], "分享")
        self.assert_no_deep_effects()

    def test_nested_url_query_values_are_data_not_additional_capture_inputs(self):
        """R2-XHS-INPUT-001 / R2-XHS-CACHE-001: only the outer URL is fetched."""
        forms = ("{url}", "分享 {url}", "分享 ({url})", '分享 "{url}"')
        saved_items = {}
        for suffix in ("a", "b"):
            nested = f"https://example.com/{suffix}"
            source = f"{NOTE_URL}?next={nested}&x=1"
            wire_url = str(httpx.URL(source))
            self.html(wire_url, f"<html><head><title>NestedOuter{suffix}</title></head></html>")
            for form in forms:
                with self.subTest(suffix=suffix, form=form):
                    text = form.format(url=source)
                    preview = self.preview(text, refresh=True)
                    self.assertEqual(preview["original_input"], text)
                    self.assertEqual(preview["source_url"], source)
                    self.assertEqual(preview["canonical_url"], source)
                    self.assertEqual(preview["identity_url"], source)
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(preview["metadata"]["title"]["value"], f"NestedOuter{suffix}")
                    self.assertEqual(str(self.requests[-1].url), wire_url)
                    self.assertNotIn(nested, preview["metadata"]["source_copy"]["value"])

            before = (len(self.requests), len(self.dns_calls))
            cached = self.preview(source)
            self.assertEqual(cached["canonical_url"], source)
            self.assertEqual(cached["identity_url"], source)
            self.assertEqual(cached["metadata"]["title"], preview["metadata"]["title"])
            self.assertEqual(cached["metadata"]["source_copy"]["value"], "")
            self.assertEqual(self.repo.get_collection_metadata_cache(source)["identity_url"], source)
            saved = self.save(preview, key=f"nested-url-{suffix}", user_title=f"嵌套参数入口{suffix}")
            self.assertEqual(saved["source_url"], source)
            self.assertEqual(saved["canonical_url"], source)
            self.assertEqual(saved["identity_url"], source)
            detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), saved)
            self.assertEqual((len(self.requests), len(self.dns_calls)), before)
            saved_items[suffix] = saved

        self.assertNotEqual(saved_items["a"]["id"], saved_items["b"]["id"])
        before = (len(self.requests), len(self.dns_calls))
        for suffix, saved in saved_items.items():
            found = self.client.get("/api/v1/collection-items", params={
                "query": f"next=https://example.com/{suffix}", "platform": "xiaohongshu",
            })
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
            self.assertEqual(found.json()["total"], 1)
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)

        # A nested scheme in a fragment also remains input data. The ordinary
        # canonical contract still removes the fragment before any HTTP request.
        canonical = f"{NOTE_URL}?fragment_case=1"
        fragment_source = f"{canonical}#next=https://example.com/fragment"
        self.html(canonical, "<html><head><title>NestedFragment</title></head></html>")
        for form in forms:
            with self.subTest(fragment_form=form):
                preview = self.preview(form.format(url=fragment_source), refresh=True)
                self.assertEqual(preview["source_url"], fragment_source)
                self.assertEqual(preview["canonical_url"], canonical)
                self.assertEqual(preview["identity_url"], canonical)
                self.assertEqual(str(self.requests[-1].url), canonical)

        # Independent path URLs retain the pre-existing ambiguity contract, even
        # when prose punctuation rather than whitespace separates the links.
        before = (len(self.requests), len(self.dns_calls))
        for text in (
            f"{NOTE_URL} https://example.com/independent",
            f"{NOTE_URL},https://example.com/independent",
            f"{NOTE_URL}，https://example.com/independent",
            f"{NOTE_URL}。https://example.com/independent",
            f"分享 {NOTE_URL}。另见https://example.com/independent",
            f"{NOTE_URL}?next=https://example.com/a&x=1 https://another.example/independent",
        ):
            with self.subTest(independent_input=text):
                response = self.client.post("/api/v1/collection-previews", json={"input_text": text})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], "CAPTURE_LINK_AMBIGUOUS")
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assertEqual({request.url.host for request in self.requests}, {"www.xiaohongshu.com"})
        self.assertEqual(set(self.dns_calls), {"www.xiaohongshu.com"})
        self.assert_no_deep_effects()

    def test_supported_short_link_shapes_resolve_to_a_note(self):
        for path in ("/a_1-Z", "/a/a_1-Z", "/m/a_1-Z", "/o/a_1-Z"):
            with self.subTest(path=path):
                source = f"https://xhslink.com{path}"
                final = f"{NOTE_URL}?from={path.count('/')}"
                self.redirect(source, final)
                self.html(final, "<html><head><title>短链笔记</title></head></html>")
                preview = self.preview(source)
                self.assertEqual(preview["canonical_url"], final)
                self.assertEqual(preview["platform"], "xiaohongshu")
                self.assertEqual(preview["metadata"]["title"]["value"], "短链笔记")
                self.assertEqual(self.repo.get_collection_metadata_cache(source)["identity_url"], final)

    def test_verified_cn_short_link_preserves_query_cache_and_bookmark(self):
        source = "https://xhslink.cn/o/currentNote"
        final = f"{NOTE_URL}?xsec_token=fixture%2Fvalue&xsec_source=share&b=2&a=1"
        self.redirect(source, final)
        self.html(final, '<head><title>Current note</title>'
                  f'<meta property="og:image" content="https://{CDN_HOST}/cover.webp"></head>')
        preview = self.preview(source)
        self.assertEqual(preview["platform"], "xiaohongshu")
        self.assertEqual(preview["canonical_url"], final)
        self.assertEqual(preview["metadata"]["cover_url"]["value"], f"https://{CDN_HOST}/cover.webp")
        self.assertEqual([str(request.url) for request in self.requests], [source, final])
        for request in self.requests:
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
        before = (len(self.requests), len(self.dns_calls))
        self.assertEqual(self.preview(source)["metadata"], preview["metadata"])
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        saved = self.client.post("/api/v1/collection-items", headers={"Idempotency-Key": "cn-share"},
                                 json={"preview_id": preview["preview_id"]})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["source_url"], source)
        self.assert_no_deep_effects()

    def test_cn_short_link_rejects_disallowed_redirect_and_private_dns(self):
        source = "https://xhslink.cn/o/wrongHost"
        self.redirect(source, "https://evil.example/note")
        self.assert_unavailable(self.preview(source))
        self.assertEqual([str(request.url) for request in self.requests], [source])
        self.requests.clear()
        self.answers["xhslink.cn"] = ["127.0.0.1"]
        self.assert_unavailable(self.preview("https://xhslink.cn/o/privateDns"))
        self.assertEqual(self.requests, [])

    def test_other_platform_routes_are_bookmarks_without_fetching_site_titles(self):
        routes = (
            "https://www.xiaohongshu.com/", "https://xiaohongshu.com/explore",
            "https://www.xiaohongshu.com/404", "https://www.xiaohongshu.com/login",
            "https://www.xiaohongshu.com/user/profile/123",
            f"{NOTE_URL}/nested", "https://www.xiaohongshu.com/explore/not-a-note",
            "https://xhslink.com/", "https://xhslink.com/a/with/delimiters",
            "https://xhslink.com/a/%2f", "https://xhslink.com/a/" + "z" * 129,
            "https://xhslink.cn/", "https://xhslink.cn/o/a/b", "https://xhslink.cn/o/%2f",
            "https://xhslink.cn/a/unverified", "https://xhslink.cn/plainUnverified",
        )
        for source in routes:
            with self.subTest(source=source):
                self.html(source, "<html><head><title>平台壳标题</title></head></html>")
                preview = self.preview(source)
                self.assert_unavailable(preview)
                self.assertEqual(preview["identity_url"], source)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dns_calls, [])

    def test_unverified_and_lookalike_hosts_remain_ordinary_web(self):
        for host in (
            "xiaohongshu.com.example.org", "evil.xiaohongshu.com",
            "www.xhslink.com", "xhslink.net", "www.xhslink.cn", "xhslink.cn.evil.example",
        ):
            with self.subTest(host=host):
                source = f"https://{host}/explore/{NOTE_ID}"
                self.html(source, "<html><head><title>普通网页</title></head></html>")
                preview = self.preview(source)
                self.assertEqual(preview["platform"], "web")
                self.assertEqual(preview["metadata_status"], "generic")

    def test_multiple_links_missing_protocol_and_app_scheme_do_not_fetch(self):
        for text in (
            f"{NOTE_URL} https://xhslink.com/a/second",
            f"{NOTE_URL}，https://xhslink.com/a/second",
            f"www.xiaohongshu.com/explore/{NOTE_ID}",
            f"xhsdiscover://item/{NOTE_ID}",
        ):
            with self.subTest(text=text):
                response = self.client.post(
                    "/api/v1/collection-previews", json={"input_text": text}
                )
                self.assertGreaterEqual(response.status_code, 400)
                self.assertLess(response.status_code, 500)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dns_calls, [])

    def test_safe_chain_strips_ambient_credentials_and_caches_all_safe_aliases(self):
        source = "https://xhslink.com/a/start"
        middle = "https://xhslink.com/m/middle"
        final = f"{NOTE_URL}?b=2&a=1"
        self.redirect(source, middle)
        self.redirect(middle, final)
        self.html(final, "<html><head><title>安全短链</title></head></html>")
        self.http.headers["Authorization"] = "Bearer fixture-auth-secret"
        self.http.cookies.set("session", "fixture-cookie-secret")
        self.routes[source].headers["set-cookie"] = "session=redirect-cookie"

        first = self.preview(source)
        self.assertEqual([str(request.url) for request in self.requests], [source, middle, final])
        for request in self.requests:
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            self.assertEqual(request.headers["accept-encoding"], "identity")
            self.assertLessEqual(request.extensions["timeout"]["connect"], 5)
            self.assertLessEqual(request.extensions["timeout"]["read"], 15)
        dns_count = len(self.dns_calls)
        for alias in (middle, final, source):
            cached = self.preview(alias)
            self.assertEqual(cached["identity_url"], final)
            self.assertEqual(cached["metadata"], first["metadata"])
            self.assertNotEqual(cached["preview_id"], first["preview_id"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.dns_calls), dns_count)
        saved = self.save(first)
        with closing(sqlite3.connect(self.repo.database_path)) as db:
            aliases = {row[0] for row in db.execute(
                "SELECT alias_url FROM collection_url_aliases WHERE collection_item_id=?",
                (saved["id"],),
            )}
        self.assertEqual(aliases, {source, final})

    def test_redirect_safety_failures_stop_before_unsafe_request_and_no_generic_retry(self):
        targets = (
            f"https://not-xhs.example/explore/{NOTE_ID}",
            f"https://evil.xiaohongshu.com/explore/{NOTE_ID}",
            f"http://127.0.0.1/explore/{NOTE_ID}",
            f"https://user:secret@www.xiaohongshu.com/explore/{NOTE_ID}",
            f"https://www.xiaohongshu.com:444/explore/{NOTE_ID}",
            f"ftp://www.xiaohongshu.com/explore/{NOTE_ID}",
        )
        for index, target in enumerate(targets):
            with self.subTest(target=target):
                source = f"https://xhslink.com/a/safety{index}"
                self.redirect(source, target)
                before = len(self.requests)
                preview = self.preview(source)
                self.assert_unavailable(preview)
                self.assertEqual(len(self.requests) - before, 1)
                self.assertIsNone(self.repo.get_collection_metadata_cache(target))

    def test_all_dns_addresses_must_be_public_before_request(self):
        for answers in (["127.0.0.1"], [PUBLIC_IP, "10.0.0.1"], []):
            with self.subTest(answers=answers):
                self.answers["www.xiaohongshu.com"] = answers
                preview = self.preview(NOTE_URL, refresh=True)
                self.assert_unavailable(preview)
        self.assertEqual(self.requests, [])

    def test_redirect_dns_rebinding_is_rechecked_even_on_the_same_host(self):
        source = "https://xhslink.com/a/first"
        target = "https://xhslink.com/m/second"
        answers = iter(([PUBLIC_IP], [PUBLIC_IP, "192.168.1.5"]))
        self.dns_override = lambda _host: next(answers)
        self.redirect(source, target)
        preview = self.preview(source)
        self.assert_unavailable(preview)
        self.assertEqual([str(request.url) for request in self.requests], [source])
        self.assertEqual(self.dns_calls, ["xhslink.com", "xhslink.com"])
        self.assertIsNone(self.repo.get_collection_metadata_cache(target))

    def test_redirect_limit_and_wrong_mime_fall_back_without_aliases(self):
        for index in range(5):
            self.redirect(f"https://xhslink.com/a/hop{index}", f"https://xhslink.com/a/hop{index + 1}")
        preview = self.preview("https://xhslink.com/a/hop0")
        self.assert_unavailable(preview)
        self.assertEqual(len(self.requests), 4)
        self.assertIsNone(self.repo.get_collection_metadata_cache("https://xhslink.com/a/hop1"))
        for status, headers in (
            (200, {"content-type": "application/json"}),
            (200, {}),
            (302, {"location": NOTE_URL, "content-type": "application/json"}),
            (302, {"location": NOTE_URL}),
        ):
            with self.subTest(status=status, headers=headers):
                self.routes[NOTE_URL] = httpx.Response(status, content=b"{}", headers=headers)
                before = len(self.requests)
                preview = self.preview(NOTE_URL, refresh=True)
                self.assert_unavailable(preview)
                self.assertEqual(len(self.requests) - before, 1)

    def test_declared_streamed_size_and_timeout_are_bounded(self):
        self.routes[NOTE_URL] = httpx.Response(
            200, content=b"x", headers={"content-type": "text/html",
                                       "content-length": str(4 * 1024 * 1024 + 1)}
        )
        self.assert_unavailable(self.preview(NOTE_URL))
        self.routes[NOTE_URL] = lambda _request: httpx.Response(
            200, stream=_ChunkedBody(), headers={"content-type": "text/html"}
        )
        self.assert_unavailable(self.preview(NOTE_URL, refresh=True))
        self.routes[NOTE_URL] = httpx.ReadTimeout("fixture-private-error")
        timeout = self.preview(NOTE_URL, refresh=True)
        self.assert_unavailable(timeout)
        self.assertNotIn("fixture-private-error", json.dumps(timeout))
        short_budget = SafePublicFetcher(
            client=self.http, dns_resolver=lambda _host: [PUBLIC_IP],
            total_timeout_seconds=1,
            monotonic_clock=iter((0.0, 2.0)).__next__,
        )
        client = self.make_client(fetcher=short_budget)
        before = len(self.requests)
        response = client.post("/api/v1/collection-previews", json={
            "input_text": NOTE_URL, "refresh_metadata": True,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assert_unavailable(response.json())
        self.assertEqual(len(self.requests), before)

    def test_cross_note_or_non_note_hop_never_creates_final_alias(self):
        other = f"https://www.xiaohongshu.com/explore/{OTHER_ID}"
        self.redirect(NOTE_URL, other)
        self.html(other, "<html><head><title>另一篇笔记</title></head></html>")
        self.assert_unavailable(self.preview(NOTE_URL))
        self.assertIsNone(self.repo.get_collection_metadata_cache(other))
        source = "https://xhslink.com/a/hoplogin"
        intermediate = "https://www.xiaohongshu.com/login"
        self.redirect(source, intermediate)
        self.redirect(intermediate, other)
        self.assert_unavailable(self.preview(source))
        self.assertIsNone(self.repo.get_collection_metadata_cache(intermediate))
        self.assertIsNone(self.repo.get_collection_metadata_cache(other))

    def test_public_head_metadata_has_honest_provenance_stable_tags_and_safe_cover(self):
        source = f"{NOTE_URL}?xsec_token=fixture-token&b=2&a=1"
        self.html(source, f'''<html><head>
            <title>HTML 后备标题</title><meta property="og:title" content="公开设计笔记">
            <meta name="author" content="后备作者"><meta property="article:author" content="公开作者">
            <meta property="og:description" content="公开页面描述">
            <meta property="og:image" content="https://{CDN_HOST}/cover.jpg">
            <meta name="keywords" content="设计, 灵感,设计, ,">
            <meta property="article:tag" content="灵感"><meta property="article:tag" content="配色">
            <meta property="article:tag" content="配色"><meta property="article:tag" content=" ">
            <meta property="og:url" content="{NOTE_URL}">
            <link rel="canonical" href="https://other.example/wrong">
            <script>window.__INITIAL_STATE__={{"title":"私有脚本标题","video":"https://media.example/source.mp4"}}</script>
            </head><body><svg><title>SVG 标题</title></svg>
            <meta property="og:title" content="正文伪标题"></body></html>''')
        preview = self.preview(source)
        self.assertEqual(preview["canonical_url"], source)
        self.assertEqual(preview["metadata_status"], "generic")
        self.assertEqual(preview["metadata"]["title"]["value"], "公开设计笔记")
        self.assertEqual(preview["metadata"]["title"]["source"], "open_graph")
        # CQ2-B: conflicting standard authors cannot be attributed by guessing
        # precedence. Other public fields and their provenance remain usable.
        self.assertEqual(preview["metadata"]["author"]["value"], "")
        self.assertEqual(preview["metadata"]["author"]["source"], "none")
        self.assertEqual(preview["metadata"]["author"]["fetched_at"], "")
        self.assertEqual(preview["metadata"]["source_copy"]["source"], "page_description")
        self.assertEqual(preview["metadata"]["source_copy"]["value"], "公开页面描述")
        self.assertEqual(preview["metadata"]["cover_url"]["source"], "open_graph")
        self.assertEqual(preview["metadata"]["cover_url"]["value"], f"https://{CDN_HOST}/cover.jpg")
        self.assertEqual(preview["metadata"]["platform_tags"], [
            {"value": tag, "source": "page_metadata"} for tag in ("设计", "灵感", "配色")
        ])
        times = {preview["metadata"][field]["fetched_at"]
                 for field in ("title", "cover_url", "source_copy")}
        self.assertEqual(len(times), 1)
        self.assertTrue(next(iter(times)))
        self.assertEqual(len(self.requests), 1)
        self.assertIn(CDN_HOST, self.dns_calls)
        self.assert_no_deep_effects()

    def test_html_fallback_fields_and_relative_cover_use_final_note_url(self):
        source = "https://xhslink.com/m/html"
        self.redirect(source, NOTE_URL)
        self.html(NOTE_URL, '''<html><head><title>HTML 标题</title>
            <meta name="author" content="HTML 作者"><meta name="description" content="HTML 描述">
            <meta property="og:image" content="/fixture/cover.jpg"></head></html>''')
        preview = self.preview(source)
        self.assertEqual(preview["metadata_status"], "generic")
        self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
        self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
        self.assertEqual(preview["metadata"]["source_copy"]["source"], "page_description")
        self.assertEqual(preview["metadata"]["cover_url"]["value"],
                         "https://www.xiaohongshu.com/fixture/cover.jpg")
        self.assertEqual(len(self.requests), 2)

    def test_unsafe_cover_only_degrades_cover_not_other_public_fields(self):
        values = (
            "https://sns-webpic-qn.xhscdn.com/cover.jpg",
            "https://evil.sns-webpic-qc.xhscdn.com/cover.jpg",
            "https://fe-static.xhscdn.com/logo.png",
            f"https://user:secret@{CDN_HOST}/cover.jpg",
            f"https://{CDN_HOST}:444/cover.jpg",
            "http://127.0.0.1/private", "javascript:alert(1)", "data:image/png,abc",
        )
        for cover in values:
            with self.subTest(cover=cover):
                self.html(NOTE_URL, f'''<html><head><title>可保留标题</title>
                    <meta property="og:image" content="{cover}"></head></html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["metadata"]["title"]["value"], "可保留标题")
                self.assertEqual(preview["metadata"]["cover_url"]["value"], "")
                self.assertEqual(preview["metadata"]["cover_url"]["source"], "none")
                self.assertTrue(preview["metadata"]["warnings"])
                self.assertNotIn(cover, json.dumps(preview["metadata"]["warnings"]))
        self.answers[CDN_HOST] = [PUBLIC_IP, "127.0.0.1"]
        self.html(NOTE_URL, f'''<html><head><title>公网字段保留</title>
            <meta property="og:image" content="https://{CDN_HOST}/cover.jpg"></head></html>''')
        preview = self.preview(NOTE_URL, refresh=True)
        self.assertEqual(preview["metadata"]["title"]["value"], "公网字段保留")
        self.assertEqual(preview["metadata"]["cover_url"]["value"], "")
        self.assertEqual(len(self.requests), len(values) + 1)

    def test_malformed_cover_percent_escapes_only_degrade_cover(self):
        """R2-XHS-META-001: a bad optional URL cannot erase a good note."""
        expected_warning = None
        covers = (
            f"https://{CDN_HOST}/bad%ZZ.jpg",
            f"https://{CDN_HOST}/trailing%",
            f"https://{CDN_HOST}/short%2",
        )
        for cover in covers:
            with self.subTest(cover=cover):
                self.html(NOTE_URL, f'''<html><head><title>正常笔记标题</title>
                    <meta name="author" content="正常公开作者">
                    <meta name="description" content="正常公开说明">
                    <meta property="og:image" content="{cover}"></head></html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["canonical_url"], NOTE_URL)
                self.assertEqual(preview["identity_url"], NOTE_URL)
                self.assertEqual(preview["metadata"]["title"]["value"], "正常笔记标题")
                self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["author"]["value"], "正常公开作者")
                self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "正常公开说明")
                self.assertEqual(preview["metadata"]["cover_url"]["value"], "")
                self.assertEqual(preview["metadata"]["cover_url"]["source"], "none")
                warning = preview["metadata"]["warnings"]
                self.assertTrue(warning)
                self.assertNotIn(cover, json.dumps(warning, ensure_ascii=False))
                if expected_warning is None:
                    expected_warning = warning
                self.assertEqual(warning, expected_warning)
        self.assertEqual(len(self.requests), len(covers))

    def test_implicit_body_boundary_stops_head_metadata_without_explicit_close_tags(self):
        """R2-XHS-META-001: omitted head/body tags do not make body data public metadata."""
        for boundary in (
            "<p>body starts here</p>",
            "<div>body starts here</div>",
            "<svg><title>正文 SVG 标题</title></svg>",
            "正文非空白文本",
        ):
            with self.subTest(boundary=boundary):
                self.html(NOTE_URL, f'''<html><head><title>正常 head 标题</title>
                    <meta name="author" content="正常 head 作者">
                    <meta name="keywords" content="正常head标签">
                    {boundary}
                    <meta name="description" content="正文伪装说明">
                    <meta property="og:author" content="正文伪装作者">
                    <meta property="og:title" content="正文伪装标题">
                    <title>正文第二标题</title>
                    <meta property="article:tag" content="正文伪装标签">
                    <meta property="og:image" content="https://{CDN_HOST}/body-cover.jpg">
                    </html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["canonical_url"], NOTE_URL)
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                self.assertEqual(preview["metadata"]["source_copy"]["source"], "none")
                self.assertEqual(preview["metadata"]["title"]["value"], "正常 head 标题")
                self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["author"]["value"], "正常 head 作者")
                self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["platform_tags"], [
                    {"value": "正常head标签", "source": "page_metadata"},
                ])
                self.assertEqual(preview["metadata"]["cover_url"]["value"], "")
                self.assertEqual(preview["metadata"]["cover_url"]["source"], "none")
        self.assertEqual(len(self.requests), 4)
        self.assertNotIn(CDN_HOST, self.dns_calls)

    def test_inert_head_content_does_not_hide_later_legitimate_head_metadata(self):
        """R2-XHS-META-001: inert content is not a body boundary or metadata."""
        inert_content = f'''<div>inert body-like text</div>
            <meta property="og:title" content="inert 假标题">
            <meta property="og:author" content="inert 假作者">
            <meta property="og:description" content="inert 假描述">
            <meta name="keywords" content="inert假标签">
            <meta property="article:tag" content="inert附加标签">
            <meta property="og:image" content="https://{CDN_HOST}/inert-cover.jpg">'''
        for tag in ("script", "style", "template", "noscript"):
            with self.subTest(tag=tag):
                self.html(NOTE_URL, f'''<html><head>
                    <{tag}>{inert_content}</{tag}>
                    <title>后续正常 head 标题</title>
                    <meta name="author" content="后续正常 head 作者">
                    <meta name="description" content="后续正常 head 描述">
                    <meta name="keywords" content="后续正常标签">
                    <meta property="og:image" content="https://{CDN_HOST}/legitimate-cover.jpg">
                    </head><body>普通正文</body></html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["canonical_url"], NOTE_URL)
                self.assertEqual(preview["metadata"]["title"]["value"], "后续正常 head 标题")
                self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["author"]["value"], "后续正常 head 作者")
                self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "后续正常 head 描述")
                self.assertEqual(preview["metadata"]["platform_tags"], [
                    {"value": "后续正常标签", "source": "page_metadata"},
                ])
                self.assertEqual(preview["metadata"]["cover_url"]["value"],
                                 f"https://{CDN_HOST}/legitimate-cover.jpg")
                self.assertNotIn("inert", json.dumps(preview["metadata"], ensure_ascii=False))
        self.assertEqual(len(self.requests), 4)

    def test_raw_text_inside_template_cannot_release_inert_metadata(self):
        """R2-XHS-META-001: literal closing tags in raw text cannot escape a template."""
        cases = []
        for tag in ("textarea", "xmp", "iframe", "noembed"):
            for visible in (False, True):
                public_tail = "<title>PUBLIC_TITLE</title>" if visible else ""
                markup = f'''<html><head><template><{tag}>
                    </template><meta property="og:title" content="INERT_TITLE">
                    <meta name="description" content="INERT_COPY">
                    <meta name="keywords" content="AI,INERT_TAG">
                    </{tag}></template>{public_tail}</head><body>正文</body></html>'''
                cases.append((f"{tag}-{visible}", markup, "PUBLIC_TITLE" if visible else ""))
        # A plaintext element does not recognize even its own closing tag: the
        # apparent public fields after it remain inert through EOF.
        cases.extend((
            ("plaintext-eof", '''<html><head><template><plaintext></plaintext></template>
                <meta property="og:title" content="INERT_TITLE">
                <title>PUBLIC_TITLE</title></head></html>''', ""),
            ("double-escaped-script", '''<html><head><script><!--<script></script>
                <meta property="og:title" content="INERT_TITLE"></script></head></html>''', ""),
            ("simple-script", '''<html><head><script>const inert = 1;</script>
                <title>PUBLIC_TITLE</title></head></html>''', "PUBLIC_TITLE"),
            ("ordinary-comment", '''<html><head><!-- ordinary comment
                <meta property="og:title" content="INERT_TITLE"> -->
                <title>PUBLIC_TITLE</title></head></html>''', "PUBLIC_TITLE"),
            ("commented-script", '''<html><head><script><!-- ordinary comment --></script>
                <title>PUBLIC_TITLE</title></head></html>''', "PUBLIC_TITLE"),
        ))
        for label, markup, expected_title in cases:
            with self.subTest(case=label):
                source = f"{NOTE_URL}?rawtext={label}"
                self.html(source, markup)
                preview = self.preview(source)
                self.assertEqual(preview["metadata"]["title"]["value"], expected_title)
                self.assertEqual(preview["canonical_url"], source)
                if expected_title:
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                else:
                    self.assert_unavailable(preview, canonical=source)
                    self.assertEqual(preview["organization_suggestion"]["status"], "insufficient_metadata")
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                self.assertEqual(preview["metadata"]["platform_tags"], [])
                self.assertEqual(preview["organization_suggestion"]["tags"], [])
                self.assertNotIn("INERT_", json.dumps(preview, ensure_ascii=False))
                cache = self.repo.get_collection_metadata_cache(source)
                self.assertNotIn("INERT_", json.dumps(cache, ensure_ascii=False))
                before = (len(self.requests), len(self.dns_calls))
                cached = self.preview(source)
                self.assertEqual(cached["metadata"], preview["metadata"])
                saved = self.save(preview, key=f"raw-{label}",
                                  user_title=f"原始文本隔离 {label}")
                self.assertEqual(saved["metadata"]["title"]["value"], expected_title)
                self.assertNotIn("INERT_", json.dumps(saved, ensure_ascii=False))
                detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json(), saved)
                leaked = self.client.get("/api/v1/collection-items", params={"query": "INERT_"})
                self.assertEqual(leaked.status_code, 200, leaked.text)
                self.assertEqual(leaked.json()["total"], 0)
                self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assert_no_deep_effects()

    def test_pseudo_raw_text_and_comment_endings_do_not_release_metadata(self):
        """R2-XHS-META-001: only HTML-valid text/comment endings resume metadata."""
        cases = []
        for label, make_end in (
            ("slash-space", lambda tag: f"</ {tag}>"),
            ("slash-tab", lambda tag: f"</\t{tag}>"),
            ("slash-newline", lambda tag: f"</\n{tag}>"),
            ("tag-nbsp", lambda tag: f"</{tag}\u00a0>"),
        ):
            for tag in ("script", "style"):
                markup = f'''<html><head><title>Legitimate</title><{tag}>
                    {make_end(tag)}<meta property="og:title" content="SPOOF">
                    </{tag}><meta name="author" content="PUBLIC_AUTHOR"></head></html>'''
                cases.append((f"{tag}-{label}", markup, "Legitimate", "page_metadata", "PUBLIC_AUTHOR", ""))
            # The literal template end also stays inside textarea until its
            # genuinely valid closing tag, so it cannot expose the fake meta.
            markup = f'''<html><head><title>Legitimate</title><template><textarea>
                {make_end('textarea')}</template><meta property="og:title" content="SPOOF">
                </textarea></template><meta name="author" content="PUBLIC_AUTHOR"></head></html>'''
            cases.append((f"textarea-{label}", markup, "Legitimate", "page_metadata", "PUBLIC_AUTHOR", ""))

            literal_title = f'Legitimate{make_end("title")}<meta property="og:author" content="SPOOF">'
            markup = f'''<html><head><title>{literal_title}</title>
                <meta name="description" content="PUBLIC_COPY"></head></html>'''
            cases.append((f"title-{label}", markup, literal_title, "page_metadata", "", "PUBLIC_COPY"))

        for label, pseudo_end in (("space", "-- >"), ("tab", "--\t>"), ("newline", "--\n>")):
            markup = f'''<html><head><title>Legitimate</title><!-- ordinary comment {pseudo_end}
                <meta property="og:title" content="SPOOF"> -->
                <meta name="author" content="PUBLIC_AUTHOR"></head></html>'''
            cases.append((f"comment-pseudo-{label}", markup, "Legitimate", "page_metadata", "PUBLIC_AUTHOR", ""))
        for label, actual_end in (("canonical", "-->"), ("bang", "--!>")):
            markup = f'''<html><head><!-- ordinary comment {actual_end}
                <meta property="og:title" content="PUBLIC_TITLE"></head></html>'''
            cases.append((f"comment-real-{label}", markup, "PUBLIC_TITLE", "open_graph", "", ""))

        for label, markup, expected_title, title_source, expected_author, expected_copy in cases:
            with self.subTest(case=label):
                source = f"{NOTE_URL}?pseudo_end={label}"
                self.html(source, markup)
                preview = self.preview(source)
                self.assertEqual(preview["canonical_url"], source)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["metadata"]["title"]["value"],
                                 " ".join(expected_title.split()))
                self.assertEqual(preview["metadata"]["title"]["source"], title_source)
                # RCDATA may legitimately contain the literal word SPOOF as part
                # of its title text; it must not become a separate author/meta.
                self.assertEqual(preview["metadata"]["author"]["value"], expected_author)
                self.assertEqual(preview["metadata"]["author"]["source"],
                                 "page_metadata" if expected_author else "none")
                self.assertEqual(preview["metadata"]["source_copy"]["value"], expected_copy)
                self.assertEqual(preview["metadata"]["platform_tags"], [])
                self.assertEqual(preview["metadata"]["cover_url"]["value"], "")
                cache = self.repo.get_collection_metadata_cache(source)
                self.assertEqual(cache["metadata"]["title"], preview["metadata"]["title"])
                self.assertEqual(cache["metadata"]["author"], preview["metadata"]["author"])
        self.assert_no_deep_effects()

    def test_pseudo_template_endings_never_publish_inert_metadata(self):
        """R2-XHS-META-001: ordinary template ends also require real HTML syntax."""
        for index, pseudo_end in enumerate(("</ template>", "</template\u00a0>")):
            for visible in (False, True):
                with self.subTest(ending=pseudo_end, public_tail=visible):
                    source = f"{NOTE_URL}?template_boundary={index}-{int(visible)}"
                    public_tail = (
                        '<title>PUBLIC_TITLE</title>'
                        '<meta name="author" content="PUBLIC_AUTHOR">'
                        if visible else ""
                    )
                    self.html(source, f'''<html><head><template>{pseudo_end}
                        <meta property="og:title" content="INERT_TEMPLATE_TITLE">
                        <meta name="author" content="INERT_TEMPLATE_AUTHOR">
                        <meta name="description" content="INERT_TEMPLATE_COPY">
                        <meta name="keywords" content="INERT_TEMPLATE_TAG">
                        </template>{public_tail}</head></html>''')

                    preview = self.preview(source)
                    self.assertEqual(preview["source_url"], source)
                    self.assertEqual(preview["canonical_url"], source)
                    self.assertEqual(preview["identity_url"], source)
                    if visible:
                        self.assertEqual(preview["metadata_status"], "generic")
                        self.assertEqual(preview["metadata"]["title"]["value"], "PUBLIC_TITLE")
                        self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                        self.assertEqual(preview["metadata"]["author"]["value"], "PUBLIC_AUTHOR")
                        self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
                    else:
                        self.assert_unavailable(preview, canonical=source)
                        self.assertEqual(preview["organization_suggestion"]["status"],
                                         "insufficient_metadata")
                    self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                    self.assertEqual(preview["metadata"]["platform_tags"], [])
                    self.assertEqual(preview["organization_suggestion"]["tags"], [])
                    self.assertNotIn("INERT_TEMPLATE_", json.dumps(preview, ensure_ascii=False))
                    cache = self.repo.get_collection_metadata_cache(source)
                    self.assertIsNotNone(cache)
                    self.assertEqual(cache["metadata"], preview["metadata"])
                    self.assertNotIn("INERT_TEMPLATE_", json.dumps(cache, ensure_ascii=False))

                    before = (len(self.requests), len(self.dns_calls))
                    cached = self.preview(source)
                    self.assertEqual(cached["metadata"], preview["metadata"])
                    saved = self.save(preview, key=f"template-boundary-{index}-{visible}",
                                      user_title=f"模板边界保留 {index}-{visible}")
                    self.assertEqual(saved["metadata"], preview["metadata"])
                    detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
                    self.assertEqual(detail.status_code, 200, detail.text)
                    self.assertEqual(detail.json(), saved)
                    self.assertNotIn("INERT_TEMPLATE_", json.dumps(detail.json(), ensure_ascii=False))
                    leaked = self.client.get("/api/v1/collection-items",
                                             params={"query": "INERT_TEMPLATE_"})
                    self.assertEqual(leaked.status_code, 200, leaked.text)
                    self.assertEqual(leaked.json()["total"], 0)
                    self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assertEqual(len(self.requests), 4)
        self.assert_no_deep_effects()

    def test_html5_head_recovery_and_duplicate_attributes_keep_real_source_fields(self):
        self.html(NOTE_URL, '''<html><head><title>HTML5 source title</title>
            <meta name="author" content="FIRST_AUTHOR" content="DUPLICATE_SPOOF">
            <meta name="unrelated" name="author" content="DUPLICATE_NAME_SPOOF">
            </head><meta name="description" content="AFTER_HEAD_REAL">
            <body><meta property="og:title" content="BODY_SPOOF"></body></html>''')
        preview = self.preview(NOTE_URL)
        self.assertEqual(preview["metadata_status"], "generic")
        self.assertEqual(preview["metadata"]["title"]["value"], "HTML5 source title")
        self.assertEqual(preview["metadata"]["author"]["value"], "FIRST_AUTHOR")
        self.assertEqual(preview["metadata"]["source_copy"]["value"], "AFTER_HEAD_REAL")
        self.assertNotIn("SPOOF", json.dumps(preview, ensure_ascii=False))
        self.assertEqual(len(self.requests), 1)

    def test_over_complex_html_falls_back_without_blocking_bookmark_save(self):
        source = f"{NOTE_URL}?complex_page=over-limit"
        self.html(source, '<head><title>Not trusted after limit</title></head>' + '<' * 10_001)
        preview = self.preview(source)
        self.assert_unavailable(preview)
        self.assertEqual(preview["source_url"], source)
        saved = self.save(preview, key="complex-bookmark", user_title="手动保留复杂页面")
        before = (len(self.requests), len(self.dns_calls))
        self.assertEqual(self.client.get(
            f"/api/v1/collection-items/{saved['id']}"
        ).json(), saved)
        found = self.client.get("/api/v1/collection-items", params={"query": "手动保留复杂页面"})
        self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        # A moderately large, valid head still exposes its real public fields.
        below = f"{NOTE_URL}?complex_page=below-limit"
        self.html(below, '<head><title>Bounded public head</title>' +
                  '<meta name="unrelated" content="value">' * 3_000 + '</head>')
        self.assertEqual(self.preview(below)["metadata"]["title"]["value"], "Bounded public head")
        self.assert_no_deep_effects()

    def test_title_text_and_omitted_head_cannot_create_metadata_fields(self):
        for opening in ("<title>", "<title/>"):
            with self.subTest(opening=opening):
                self.html(NOTE_URL, f'''<html>{opening}公开 &amp; 标题
                    <meta property="og:author" content="标题内伪作者"></title>
                    <meta name="author" content="真实 head 作者">
                    <p>正文开始</p><head>
                    <meta name="description" content="正文内伪 head 描述">
                    </head></html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assertEqual(preview["metadata_status"], "generic")
                self.assertEqual(preview["canonical_url"], NOTE_URL)
                self.assertEqual(preview["metadata"]["title"]["value"],
                    '公开 & 标题 <meta property="og:author" content="标题内伪作者">')
                self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["author"]["value"], "真实 head 作者")
                self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
        self.assertEqual(len(self.requests), 2)

    def test_body_private_script_and_html_redirects_are_not_metadata_or_requests(self):
        self.html(NOTE_URL, f'''<html><head>
            <meta http-equiv="refresh" content="0;url=https://other.example/login">
            <link rel="canonical" href="https://other.example/rewritten">
            <script>window.location='https://other.example/redirect';
            window.__INITIAL_STATE__={{"title":"私有标题","desc":"私有正文",
            "tags":["私有标签"],"video":"https://media.example/video.mp4"}};</script>
            </head><body><title>正文标题</title><meta property="og:title" content="伪标题">
            <svg><title>SVG 标题</title></svg><p>正文不是来源元信息。</p></body></html>''')
        preview = self.preview(NOTE_URL)
        self.assert_unavailable(preview, canonical=NOTE_URL)
        self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
        self.assertEqual([str(request.url) for request in self.requests], [NOTE_URL])
        self.assertEqual(preview["organization_suggestion"]["status"], "insufficient_metadata")

    def test_gate_shell_and_wrong_og_identity_have_no_public_metadata(self):
        for title in ("小红书 - 你的生活指南", "登录 - 小红书", "安全验证", "页面不存在", "404"):
            with self.subTest(title=title):
                self.html(NOTE_URL, f'''<html><head><title>{title}</title>
                    <meta property="og:description" content="站点描述不是笔记正文">
                    <meta property="og:image" content="https://{CDN_HOST}/shell.jpg">
                    </head></html>''')
                preview = self.preview(NOTE_URL, refresh=True)
                self.assert_unavailable(preview, canonical=NOTE_URL)
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
        self.html(NOTE_URL, f'''<html><head><title>看似正常笔记</title>
            <meta property="og:url" content="https://www.xiaohongshu.com/explore/{OTHER_ID}">
            </head></html>''')
        preview = self.preview(NOTE_URL, refresh=True)
        self.assert_unavailable(preview, canonical=NOTE_URL)
        saved = self.save(preview, user_title="手动补标题")
        self.assertEqual(saved["display_title"], "手动补标题")
        self.assertEqual(saved["metadata"]["title"]["value"], "")

    def test_explicit_login_and_http_error_titles_are_saveable_bookmark_fallbacks(self):
        """R2-XHS-FALLBACK-001: exact access/error shells are not content titles."""
        cases = (
            ("登录 / 注册", "title"),
            ("小红书 - 登录 / 注册", "title"),
            ("403 Forbidden", "title"),
            ("401 Unauthorized", "title"),
            ("404 Not Found", "title"),
            ("500 Internal Server Error", "title"),
            ("503 Service Unavailable", "title"),
            ("注册 / 登录", "title"),
            ("小红书｜注册／登录", "title"),
            ("登录／注册 - 小红书", "title"),
            ("小红书 - 登录 / 注册", "og:title"),
            ("403 Forbidden", "og:title"),
        )
        for index, (title, carrier) in enumerate(cases):
            with self.subTest(title=title, carrier=carrier):
                source = f"{NOTE_URL}?access_title={index}"
                title_markup = (f"<title>{title}</title>" if carrier == "title" else
                                f'<meta property="og:title" content="{title}">')
                self.html(source, f'''<html><head>{title_markup}
                    <meta name="author" content="GATE_SHELL_AUTHOR">
                    <meta name="description" content="GATE_SHELL_COPY">
                    <meta name="keywords" content="GATE_SHELL_TAG">
                    <meta property="og:image" content="https://{CDN_HOST}/shell.jpg">
                    </head></html>''')
                preview = self.preview(source)
                self.assert_unavailable(preview, canonical=source)
                self.assertEqual(preview["source_url"], source)
                self.assertEqual(preview["identity_url"], source)
                self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                self.assertEqual(preview["metadata"]["source_copy"]["source"], "none")
                self.assertEqual(preview["organization_suggestion"]["status"], "insufficient_metadata")
                self.assertEqual(preview["organization_suggestion"]["tags"], [])
                self.assertNotIn("GATE_SHELL_", json.dumps(preview, ensure_ascii=False))
                cache = self.repo.get_collection_metadata_cache(source)
                self.assertIsNotNone(cache)
                self.assertEqual(cache["metadata"], preview["metadata"])

                before = (len(self.requests), len(self.dns_calls))
                self.assertEqual(self.preview(source)["metadata"], preview["metadata"])
                user_title = f"手动保留门禁书签{index:02d}"
                saved = self.save(preview, key=f"access-title-{index}", user_title=user_title)
                self.assertEqual(saved["display_title"], user_title)
                self.assertEqual(saved["metadata"], preview["metadata"])
                self.assertEqual(saved["source_url"], source)
                self.assertEqual(saved["identity_url"], source)
                detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json(), saved)
                found = self.client.get("/api/v1/collection-items", params={"query": user_title})
                self.assertEqual(found.status_code, 200, found.text)
                self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
                leaked = self.client.get("/api/v1/collection-items", params={"query": "GATE_SHELL_"})
                self.assertEqual(leaked.status_code, 200, leaked.text)
                self.assertEqual(leaked.json()["total"], 0)
                self.assertEqual((len(self.requests), len(self.dns_calls)), before)

        # These are meaningful note titles about the same words, not gates. A
        # substring blocklist would erase them even though no access wall exists.
        for index, title in enumerate(("登录/注册页面设计笔记", "解决403 Forbidden的经验")):
            for carrier in ("title", "og:title"):
                with self.subTest(content_title=title, carrier=carrier):
                    source = f"{NOTE_URL}?content_title={index}&carrier={carrier}"
                    markup = (f"<title>{title}</title>" if carrier == "title" else
                              f'<meta property="og:title" content="{title}">')
                    self.html(source, f"<html><head>{markup}</head></html>")
                    preview = self.preview(source)
                    self.assertEqual(preview["metadata_status"], "generic")
                    self.assertEqual(preview["canonical_url"], source)
                    self.assertEqual(preview["metadata"]["title"]["value"], title)
                    self.assertEqual(preview["metadata"]["title"]["source"],
                                     "page_metadata" if carrier == "title" else "open_graph")
        self.assertEqual(len(self.requests), len(cases) + 4)
        self.assert_no_deep_effects()

    def test_all_standard_http_error_titles_are_rejected_at_the_preview_boundary(self):
        """R2-XHS-FALLBACK-001: reason-phrase punctuation uses one title contract."""
        count = 0
        for status in HTTPStatus:
            if status.value < 400:
                continue
            phrase = f"{status.value} {status.phrase}"
            for title in (phrase, f"小红书 - {phrase}", f"{phrase}｜小红书"):
                for carrier in ("title", "og:title"):
                    with self.subTest(status=status.value, title=title, carrier=carrier):
                        source = f"{NOTE_URL}?standard_error={count}"
                        count += 1
                        value = escape(title)
                        markup = (f"<title>{value}</title>" if carrier == "title" else
                                  f'<meta property="og:title" content="{value}">')
                        self.html(source, f'<head>{markup}<meta name="description" '
                                          'content="STANDARD_ERROR_SHELL"></head>')
                        preview = self.preview(source)
                        self.assert_unavailable(preview, canonical=source)
                        self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                        self.assertEqual(preview["metadata"]["source_copy"]["source"], "none")
                        self.assertNotIn("STANDARD_ERROR_SHELL", json.dumps(preview))
                        cache = self.repo.get_collection_metadata_cache(source)
                        self.assertEqual(cache["metadata"], preview["metadata"])
                        self.assertEqual(cache["metadata_status"], "metadata_unavailable")
        self.assertEqual(len(self.requests), count)
        self.assert_no_deep_effects()

    def test_hyphenated_http_error_shells_cannot_bypass_save_confirmation_or_pollute_search(self):
        """R2-XHS-FALLBACK-001: a soft 414 stays a bookmark through real storage."""
        saved_records = []
        titles = ("414 Request-URI Too Long", "小红书 - 414 Request-URI Too Long",
                  "４１４　Ｒｅｑｕｅｓｔ－ＵＲＩ　Ｔｏｏ　Ｌｏｎｇ｜小红书")
        for index, title in enumerate(titles):
            for carrier in ("title", "og:title"):
                with self.subTest(title=title, carrier=carrier):
                    source = f"{NOTE_URL}?hyphenated_error={index}&carrier={carrier}"
                    markup = (f"<title>{escape(title)}</title>" if carrier == "title" else
                              f'<meta property="og:title" content="{escape(title)}">')
                    self.html(source, f'<head>{markup}<meta name="description" '
                                      'content="R22_414_ERROR_COPY"></head>')
                    preview = self.preview(source)
                    self.assert_unavailable(preview, canonical=source)
                    self.assertEqual(preview["metadata"]["source_copy"]["value"], "")
                    self.assertEqual(preview["organization_suggestion"]["status"],
                                     "insufficient_metadata")
                    before = (len(self.requests), len(self.dns_calls))
                    cached = self.preview(source)
                    self.assertEqual(cached["metadata"], preview["metadata"])
                    key = f"hyphenated-{index}-{carrier}"
                    refused = self.client.post(
                        "/api/v1/collection-items", headers={"Idempotency-Key": key},
                        json={"preview_id": preview["preview_id"]},
                    )
                    self.assertGreaterEqual(refused.status_code, 400)
                    self.assertLess(refused.status_code, 500)
                    user_title = f"保留链接手动标题{index}{carrier}"
                    saved = self.save(preview, key=key, user_title=user_title)
                    self.assertEqual(saved["metadata"], preview["metadata"])
                    self.assertEqual(saved["display_title"], user_title)
                    self.assertEqual(saved["source_url"], source)
                    self.assertEqual(self.save(preview, key=key, user_title=user_title), saved)
                    saved_records.append(saved)
                    self.assertEqual((len(self.requests), len(self.dns_calls)), before)

        restored = self.make_client(repository=SQLiteRepository(self.repo.database_path))
        before = (len(self.requests), len(self.dns_calls))
        for saved in saved_records:
            detail = restored.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), saved)
            found = restored.get("/api/v1/collection-items", params={"query": saved["display_title"]})
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
        leaked = restored.get("/api/v1/collection-items", params={"query": "R22_414_ERROR_COPY"})
        self.assertEqual(leaked.status_code, 200, leaked.text)
        self.assertEqual(leaked.json()["total"], 0)
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)

        title = "解决 414 Request-URI Too Long 的笔记"
        for carrier in ("title", "og:title"):
            source = f"{NOTE_URL}?hyphenated_content={carrier}"
            markup = (f"<title>{title}</title>" if carrier == "title" else
                      f'<meta property="og:title" content="{title}">')
            self.html(source, f"<head>{markup}</head>")
            preview = self.preview(source)
            self.assertEqual(preview["metadata_status"], "generic")
            self.assertEqual(preview["metadata"]["title"]["value"], title)
        self.assert_no_deep_effects()

    def test_share_text_is_per_preview_and_never_cached_or_used_as_public_suggestion(self):
        source = "https://xhslink.com/a/shareonly"
        self.redirect(source, NOTE_URL)
        self.html(NOTE_URL, "<html><head></head></html>")
        first = self.preview(f"编程教程甲 {source}")
        second = self.preview(f"生活灵感乙 {NOTE_URL}")
        plain = self.preview(source)
        self.assert_unavailable(first, canonical=NOTE_URL)
        self.assertEqual(first["metadata"]["source_copy"]["value"], "编程教程甲")
        self.assertEqual(second["metadata"]["source_copy"]["value"], "生活灵感乙")
        self.assertEqual(plain["metadata"]["source_copy"]["value"], "")
        self.assertEqual(first["metadata"]["source_copy"]["source"], "share_text")
        self.assertEqual(second["metadata"]["source_copy"]["source"], "share_text")
        self.assertEqual(plain["metadata"]["source_copy"]["source"], "none")
        for preview in (first, second, plain):
            self.assertEqual(preview["organization_suggestion"]["status"], "insufficient_metadata")
            self.assertEqual(preview["organization_suggestion"]["tags"], [])
        cache = self.repo.get_collection_metadata_cache(source)
        self.assertEqual(cache["metadata"]["source_copy"]["source"], "none")
        self.assertNotIn("编程教程甲", json.dumps(cache, ensure_ascii=False))
        self.assertNotIn("生活灵感乙", json.dumps(cache, ensure_ascii=False))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len({item["preview_id"] for item in (first, second, plain)}), 3)
        refused = self.client.post("/api/v1/collection-items", headers={"Idempotency-Key": "share"},
                                   json={"preview_id": first["preview_id"]})
        self.assertGreaterEqual(refused.status_code, 400)
        saved = self.save(first, key="share", untitled_confirmed=True)
        self.assertEqual(saved["display_title"], "未命名收藏")
        self.assertEqual(saved["metadata"]["source_copy"]["value"], "编程教程甲")

    def test_public_description_wins_over_each_share_text_on_cache_hits(self):
        self.html(NOTE_URL, '<html><head><title>公开标题</title>'
                           '<meta name="description" content="公开描述"></head></html>')
        first = self.preview(f"第一次私人分享 {NOTE_URL}")
        second = self.preview(f"第二次私人分享 {NOTE_URL}")
        self.assertEqual(first["metadata"], second["metadata"])
        self.assertEqual(first["metadata"]["source_copy"]["value"], "公开描述")
        self.assertEqual(first["metadata"]["source_copy"]["source"], "page_description")
        self.assertEqual(len(self.requests), 1)

    def test_refresh_revalidates_and_old_preview_remains_saveable_and_unchanged(self):
        source = "https://xhslink.com/a/refresh"
        self.redirect(source, NOTE_URL)
        self.html(NOTE_URL, "<html><head><title>旧公开标题</title></head></html>")
        first = self.preview(f"旧分享文字 {source}")
        before = self.repo.get_collection_preview(first["preview_id"])
        self.redirect(source, "https://not-allowed.example/private")
        refreshed = self.preview(f"新分享文字 {source}", refresh=True)
        self.assert_unavailable(refreshed)
        self.assertEqual(refreshed["metadata"]["source_copy"]["value"], "新分享文字")
        self.assertNotEqual(first["preview_id"], refreshed["preview_id"])
        self.assertEqual(self.repo.get_collection_preview(first["preview_id"]), before)
        saved = self.save(first)
        self.assertEqual(saved["canonical_url"], NOTE_URL)
        self.assertEqual(saved["metadata"], first["metadata"])
        self.assertEqual(len(self.requests), 3)

    def test_existing_fresh_cache_keeps_old_fields_until_explicit_refresh(self):
        source = "https://www.xiaohongshu.com/user/profile/legacy"
        fetched = "2026-01-01T00:00:00+00:00"
        cache = {
            "identity_url": source, "canonical_url": source,
            "source_kind": "webpage", "platform": "xiaohongshu", "metadata_status": "generic",
            "metadata": {
                "title": {"value": "历史已抓取字段", "source": "page_metadata", "fetched_at": fetched},
                "author": {"value": "", "source": "none", "fetched_at": ""},
                "cover_url": {"value": "", "source": "none", "fetched_at": ""},
                "source_copy": {"value": "旧公开说明", "source": "page_description", "fetched_at": fetched},
                "platform_tags": [], "warnings": [],
            },
            "fetched_at": fetched,
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
        self.repo.upsert_collection_metadata_cache(cache, [source])
        first = self.preview(source)
        self.assertEqual(first["metadata"], cache["metadata"])
        self.assertEqual(first["canonical_url"], source)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.dns_calls, [])
        refreshed = self.preview(source, refresh=True)
        self.assert_unavailable(refreshed)
        self.assertEqual(self.requests, [])
        saved = self.save(first)
        self.assertEqual(saved["metadata"], cache["metadata"])

    def test_expired_cache_gets_new_public_fields_but_not_old_preview_mutation(self):
        self.html(NOTE_URL, "<html><head><title>第一版公开标题</title></head></html>")
        old = self.preview(NOTE_URL)
        cache = self.repo.get_collection_metadata_cache(NOTE_URL)
        cache["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        self.repo.upsert_collection_metadata_cache(cache, [NOTE_URL])
        self.html(NOTE_URL, "<html><head><title>第二版公开标题</title></head></html>")
        refreshed = self.preview(NOTE_URL)
        self.assertEqual(refreshed["metadata"]["title"]["value"], "第二版公开标题")
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(refreshed["metadata"]["title"]["fetched_at"], old["metadata"]["title"]["fetched_at"])
        self.assertEqual(self.repo.get_collection_preview(old["preview_id"])["metadata"], old["metadata"])

    def test_idempotent_save_reopen_get_and_search_preserve_all_owned_fields(self):
        self.html(NOTE_URL, '<html><head><title>配色笔记</title>'
                           '<meta name="author" content="公开作者">'
                           '<meta name="description" content="页面配色思路">'
                           '<meta name="keywords" content="来源标签"></head></html>')
        preview = self.preview(f"收藏分享原文 {NOTE_URL}")
        payload = {
            "preview_id": preview["preview_id"], "user_title": "我的设计参考",
            "organization_confirmation": {"primary_category": "创意与设计", "secondary_category": "配色",
                                           "organization_tags": ["整理标签"]},
            "personal_tags": ["个人标签"],
            "inspiration": {"content": "用于下次海报的独特灵感", "input_mode": "text",
                            "transcription_status": "not_applicable"},
        }
        response = self.client.post("/api/v1/collection-items", json=payload,
                                    headers={"Idempotency-Key": "full-save"})
        self.assertEqual(response.status_code, 201, response.text)
        saved = response.json()
        replay = self.client.post("/api/v1/collection-items", json=payload,
                                  headers={"Idempotency-Key": "full-save"})
        self.assertEqual(replay.json(), saved)
        conflict = self.client.post("/api/v1/collection-items", json={**payload, "user_title": "不可覆盖"},
                                    headers={"Idempotency-Key": "second-save"})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"], "COLLECTION_EXISTS")
        reused = self.client.post("/api/v1/collection-items", json={**payload, "user_title": "不可覆盖"},
                                  headers={"Idempotency-Key": "full-save"})
        self.assertEqual(reused.status_code, 409, reused.text)
        self.assertEqual(reused.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(saved["metadata"], preview["metadata"])
        self.assertEqual(saved["organization_confirmation"], payload["organization_confirmation"])
        self.assertEqual(saved["personal_tags"], ["个人标签"])
        self.assertEqual(saved["inspiration"]["content"], payload["inspiration"]["content"])
        before = (len(self.requests), len(self.dns_calls))
        reopened = SQLiteRepository(self.repo.database_path)
        client = self.make_client(repository=reopened)
        detail = client.get(f"/api/v1/collection-items/{saved['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json(), saved)
        for query in ("独特灵感", "公开作者", "来源标签", "整理标签", "个人标签", "配色笔记"):
            with self.subTest(query=query):
                found = client.get("/api/v1/collection-items", params={"query": query, "platform": "xiaohongshu"})
                self.assertEqual(found.status_code, 200, found.text)
                self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
                self.assertEqual(found.json()["total"], 1)
                self.assertNotIn("inspiration", found.json()["items"][0])
        for source, tag in (("platform", "来源标签"), ("organization", "整理标签"), ("personal", "个人标签")):
            found = client.get("/api/v1/collection-items", params={
                "query": "设计", "platform": "xiaohongshu", "tag_source": source, "tag": tag,
                "primary_category": "创意与设计", "secondary_category": "配色",
            })
            self.assertEqual(found.json()["total"], 1, found.text)
        excluded = client.get("/api/v1/collection-items", params={"platform": "bilibili"})
        self.assertEqual(excluded.json()["total"], 0)
        self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assert_no_deep_effects()

    def test_old_registry_and_deep_providers_stay_unused_with_or_without_configuration(self):
        self.html(NOTE_URL, "<html><head><title>没有深度副作用</title></head></html>")
        service = Mock()
        service.registry.matching.side_effect = AssertionError("XHS must not enter video registry")
        for index, resolution in enumerate((None, service)):
            with self.subTest(configured=resolution is not None):
                self.client = self.make_client(resolution_service=resolution)
                source = f"{NOTE_URL}?boundary={index}"
                self.html(source, "<html><head><title>没有深度副作用</title></head></html>")
                preview = self.preview(source)
                self.assertEqual(preview["metadata_status"], "generic")
                saved = self.save(preview, key=f"boundary-{index}")
                before = (len(self.requests), len(self.dns_calls))
                self.client.get(f"/api/v1/collection-items/{saved['id']}")
                self.client.get("/api/v1/collection-items", params={"query": "深度副作用"})
                deep = self.client.get(f"/api/v1/collection-items/{saved['id']}/deep-analysis")
                self.assertEqual(deep.status_code, 200, deep.text)
                self.assertEqual(deep.json()["state"], "unavailable")
                self.assertFalse(deep.json()["can_start"])
                self.assertIsNone(deep.json()["analysis_job_id"])
                self.assertEqual((len(self.requests), len(self.dns_calls)), before)
                self.assert_no_deep_effects()
        self.assertEqual(service.mock_calls, [])

    def test_transport_errors_do_not_expose_sensitive_details_in_public_warnings(self):
        marker = "fixture-sensitive-transport-marker"
        source = f"{NOTE_URL}?xsec_token=fixture-query-marker"
        self.routes[source] = httpx.ConnectError(marker)
        preview = self.preview(f"私人分享内容 {source}")
        self.assert_unavailable(preview)
        self.assertNotIn(marker, json.dumps(preview, ensure_ascii=False))
        warnings = json.dumps(preview["metadata"]["warnings"], ensure_ascii=False)
        self.assertNotIn("fixture-query-marker", warnings)
        self.assertNotIn("私人分享内容", warnings)

    def test_generic_page_read_and_close_failures_remain_saveable_bookmarks(self):
        """Inherited CAPTURE-FALLBACK: cleanup errors must not defeat bookmark fallback."""
        class FailingCloseStream(httpx.SyncByteStream):
            def __init__(self, fail_read):
                self.fail_read = fail_read
                self.close_attempts = 0

            def __iter__(self):
                if self.fail_read:
                    yield b"<html><head><title>PARTIAL_SHOULD_NOT_LEAK"
                    raise httpx.ReadError("fixture-private-read-marker")
                yield b"{}"

            def close(self):
                self.close_attempts += 1
                raise httpx.CloseError("fixture-private-close-marker")

        for label, content_type, fail_read in (
            ("wrong-mime-and-close", "application/json", False),
            ("read-and-close", "text/html; charset=utf-8", True),
        ):
            with self.subTest(case=label):
                source = f"https://ordinary-page.example/{label}"
                stream = FailingCloseStream(fail_read)
                self.routes[source] = lambda _request, body=stream, mime=content_type: httpx.Response(
                    200, headers={"content-type": mime}, stream=body
                )
                before_requests = len(self.requests)
                preview = self.preview(source)
                self.assertEqual(preview["metadata_status"], "metadata_unavailable")
                self.assertEqual(preview["platform"], "web")
                self.assertEqual(preview["source_kind"], "webpage")
                self.assertEqual(preview["source_url"], source)
                self.assertEqual(preview["identity_url"], source)
                self.assertEqual(preview["canonical_url"], "")
                for field in ("title", "author", "cover_url", "source_copy"):
                    self.assertEqual(preview["metadata"][field]["value"], "")
                    self.assertEqual(preview["metadata"][field]["source"], "none")
                self.assertEqual(preview["metadata"]["platform_tags"], [])
                self.assertTrue(preview["metadata"]["warnings"])
                self.assertNotIn("fixture-private-", json.dumps(preview, ensure_ascii=False))
                self.assertNotIn("PARTIAL_SHOULD_NOT_LEAK", json.dumps(preview, ensure_ascii=False))
                self.assertGreaterEqual(stream.close_attempts, 1)
                self.assertEqual(len(self.requests), before_requests + 1)
                self.assertEqual(str(self.requests[-1].url), source)

                before = (len(self.requests), len(self.dns_calls))
                self.assertEqual(self.preview(source)["metadata"], preview["metadata"])
                user_title = f"网络失败手动书签 {label}"
                saved = self.save(preview, key=label, user_title=user_title)
                self.assertEqual(saved["display_title"], user_title)
                self.assertEqual(saved["metadata"], preview["metadata"])
                self.assertEqual(saved["source_url"], source)
                self.assertEqual(saved["canonical_url"], "")
                detail = self.client.get(f"/api/v1/collection-items/{saved['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json(), saved)
                found = self.client.get("/api/v1/collection-items", params={"query": user_title})
                self.assertEqual(found.status_code, 200, found.text)
                self.assertEqual([item["id"] for item in found.json()["items"]], [saved["id"]])
                self.assertEqual((len(self.requests), len(self.dns_calls)), before)
        self.assert_no_deep_effects()

    def test_ordinary_external_short_link_cannot_bypass_platform_identity_checks(self):
        for index, target in enumerate(("https://www.xiaohongshu.com/404", NOTE_URL)):
            with self.subTest(target=target):
                source = f"https://ordinary-short.example/link{index}"
                self.redirect(source, target)
                self.html(target, "<html><head><title>不得通过通用解析器读取</title></head></html>")
                preview = self.preview(source)
                self.assertEqual(preview["metadata_status"], "metadata_unavailable")
                self.assertEqual(preview["platform"], "web")
                self.assertEqual(preview["source_url"], source)
                self.assertEqual(preview["identity_url"], source)
                self.assertEqual(preview["canonical_url"], "")
                self.assertEqual(preview["metadata"]["title"]["value"], "")
                self.assertIsNone(self.repo.get_collection_metadata_cache(target))


if __name__ == "__main__":
    unittest.main()
