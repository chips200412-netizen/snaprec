from __future__ import annotations

import copy
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.api.main import create_app
from backend.app.domain.models import CollectionPreviewRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.capture import CaptureService
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.app.services.rendered_metadata import RenderedMetadataSupplement
from backend.app.services.safe_http import SafeImageFetchResult, SafePublicFetcher

from backend.app.services.xiaohongshu_embedded import extract_xhs_embedded_metadata


NOTE_ID = "64a01234567890abcdef1234"
OTHER_ID = "64b01234567890abcdef1234"
HTTP_COVER = "http://sns-webpic-qc.xhscdn.com/cover.webp?b=2&a=1"
HTTPS_COVER = HTTP_COVER.replace("http:", "https:", 1)


def _state(*, author="目标作者", cover=HTTP_COVER, note_type="normal"):
    return {"note": {"firstNoteId": NOTE_ID, "currentNoteId": NOTE_ID,
        "noteDetailMap": {NOTE_ID: {"note": {"noteId": NOTE_ID, "type": note_type,
            "user": {"nickname": author}, "imageList": [{"urlDefault": cover}]}}}}}


def _script(state=None):
    return "window.__INITIAL_STATE__=" + json.dumps(_state() if state is None else state) + ";"


def _html(state=None, *, script=None, attributes=""):
    value = _script(state) if script is None else script
    return f'<html><head><title>目标笔记</title></head><body>\n<!-- public page -->\n<script{attributes}>{value}</script>\n</body></html>'


def _app(root, repo, fetcher, probe, image_fetcher):
    return create_app(
        LocalFullPipeline(repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()),
        capture_fetcher=fetcher, rendered_metadata_probe=probe, cover_fetcher=image_fetcher,
        upload_root=root / "uploads", inspiration_temp_root=root / "audio",
        cover_cache_root=root / "covers", user_cover_root=root / "user-covers",
    )


class XiaohongshuEmbeddedTests(unittest.TestCase):
    def test_exact_target_normal_and_video_only_return_author_and_first_image(self):
        for kind in ("normal", "video"):
            state = _state(note_type=kind)
            state["user"] = {"nickname": "Wrong global user"}
            state["note"]["noteDetailMap"][OTHER_ID] = {"note": {"user": {"nickname": "Wrong"}}}
            result = extract_xhs_embedded_metadata(_html(state), NOTE_ID)
            self.assertEqual(result.author, "目标作者")
            self.assertEqual(result.cover_url, HTTPS_COVER)


class XiaohongshuEmbeddedApiTests(unittest.TestCase):
    def test_legacy_cache_refresh_expiry_and_batch_keep_request_boundaries(self):
        target = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
        cache = {}
        def upsert(payload, aliases):
            for alias in aliases:
                cache[alias] = copy.deepcopy(payload)
        repo = SimpleNamespace(
            get_collection_metadata_cache=lambda url: copy.deepcopy(cache.get(url)),
            upsert_collection_metadata_cache=upsert, create_collection_preview=Mock())
        body = ["<html><head><title>Old</title></head></html>"]
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, text=body[0], headers={"content-type": "text/html"})
        probe = SimpleNamespace(probe=Mock(return_value=None))
        with httpx.Client(transport=httpx.MockTransport(transport)) as http:
            fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ["8.8.8.8"])
            service = CaptureService(repo, fetcher=fetcher, rendered_metadata_probe=probe)
            request = CollectionPreviewRequest(input_text=target)
            old = service.preview(request, allow_rendered_cover=False)
            snapshot = copy.deepcopy(cache)
            body[0] = _html()
            fresh = service.preview(request)
            self.assertEqual(fresh.metadata, old.metadata)
            self.assertEqual(cache, snapshot)
            self.assertEqual(len(requests), 1)
            probe.probe.assert_not_called()
            refreshed = service.preview(CollectionPreviewRequest(input_text=target, refresh_metadata=True))
            self.assertEqual(refreshed.metadata.author.value, "目标作者")
            self.assertEqual(refreshed.metadata.cover_url.value, HTTPS_COVER)
            self.assertEqual(len(requests), 2)
            cache[target]["expires_at"] = "2000-01-01T00:00:00+00:00"
            body[0] = _html(_state(author="New author", cover=""))
            expired_batch = service.preview(request, allow_rendered_cover=False)
            self.assertEqual(expired_batch.metadata.author.value, "New author")
            self.assertEqual(len(requests), 3)
            self.assertEqual(old.metadata.author.value, "")
            probe.probe.assert_not_called()

    def test_capture_save_real_reopen_author_search_and_same_origin_image(self):
        source = "https://xhslink.cn/o/fixture"
        canonical = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=fixture%2F1&b=2&a=1"
        for state, has_author, has_cover, rendered in (
            (_state(), True, True, False), (_state(cover=""), True, False, False),
            (_state(author="", cover=""), False, False, False), (_state(cover=""), True, True, True),
            (_state(author=""), False, True, False),
        ):
            with self.subTest(author=has_author, cover=has_cover), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = SQLiteRepository(root / "synthetic.sqlite3")
                requests = []
                def transport(request):
                    requests.append(request)
                    if str(request.url) == source:
                        return httpx.Response(302, headers={"location": canonical, "content-type": "text/html"})
                    return httpx.Response(200, text=_html(state), headers={"content-type": "text/html"})
                http = httpx.Client(transport=httpx.MockTransport(transport))
                fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ["8.8.8.8"])
                probe = SimpleNamespace(probe=Mock(return_value=(RenderedMetadataSupplement(
                    cover_url=HTTPS_COVER, author_source="page_metadata", cover_source="open_graph"
                ) if rendered else None)))
                out = BytesIO()
                Image.new("RGB", (2, 2), (1, 2, 3)).save(out, "PNG")
                image_fetcher = SimpleNamespace(fetch=Mock(return_value=SafeImageFetchResult(
                    HTTPS_COVER, "image/png", out.getvalue(), (), "image/png")))
                with http, TestClient(_app(root, repo, fetcher, probe, image_fetcher)) as client:
                    response = client.post("/api/v1/collection-previews", json={"input_text": source})
                    self.assertEqual(response.status_code, 201, response.text)
                    preview = response.json()
                    self.assertEqual(preview["canonical_url"], canonical)
                    self.assertEqual(preview["metadata"]["author"]["value"], "目标作者" if has_author else "")
                    self.assertEqual(preview["metadata"]["author"]["source"], "platform_public" if has_author else "none")
                    self.assertEqual(preview["metadata"]["cover_url"]["value"], HTTPS_COVER if has_cover else "")
                    self.assertEqual(preview["metadata_status"], "recognized" if has_author or has_cover else "generic")
                    cached = client.post("/api/v1/collection-previews", json={"input_text": source}).json()
                    self.assertEqual(cached["metadata"], preview["metadata"])
                    self.assertEqual(len(requests), 2)
                    self.assertEqual(probe.probe.call_count, 0 if has_author and has_cover and not rendered else 1)
                    saved = client.post("/api/v1/collection-items", headers={"Idempotency-Key": "embedded"},
                        json={"preview_id": preview["preview_id"], "user_author": "Personal Author"})
                    self.assertEqual(saved.status_code, 201, saved.text)
                    item = saved.json()
                    self.assertEqual(item["user_author"], "Personal Author")
                    for request in requests:
                        self.assertNotIn("cookie", request.headers)
                        self.assertNotIn("authorization", request.headers)
                reopened_repo = SQLiteRepository(root / "synthetic.sqlite3")
                with TestClient(_app(root, reopened_repo, fetcher, probe, image_fetcher)) as client:
                    self.assertEqual(client.get(f"/api/v1/collection-items/{item['id']}").json(), item)
                    for query in (["Personal Author", "目标作者"] if has_author else ["Personal Author"]):
                        found = client.get("/api/v1/collection-items", params={"query": query}).json()
                        self.assertEqual([row["id"] for row in found["items"]], [item["id"]])
                    if has_cover:
                        image = client.get(f"/api/v1/collection-items/{item['id']}/cover")
                        self.assertEqual(image.status_code, 200)
                        self.assertEqual(image.content, out.getvalue())
                        self.assertEqual(image.headers["cross-origin-resource-policy"], "same-origin")
                        image_fetcher.fetch.assert_called_once_with(HTTPS_COVER)
                    with reopened_repo._connect() as db:
                        for table in ("jobs", "videos", "transcript_segments", "extractions"):
                            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_standard_values_are_not_overwritten_and_full_metadata_skips_embedded(self):
        from backend.app.adapters.xiaohongshu import XiaohongshuSourceAdapter
        body = _html().replace("</head>", '<meta name="author" content="Standard">'
                            f'<meta property="og:image" content="{HTTPS_COVER}"></head>')
        fetcher = SimpleNamespace(url_validator=SimpleNamespace(resolve=Mock()))
        adapter = XiaohongshuSourceAdapter(fetcher)
        resource = SimpleNamespace(page=SimpleNamespace(text=body), canonical_url=f"https://www.xiaohongshu.com/explore/{NOTE_ID}")
        with patch("backend.app.adapters.xiaohongshu.extract_xhs_embedded_metadata") as parser:
            metadata = adapter.get_public_metadata(resource, "now")
        parser.assert_not_called()
        self.assertEqual(metadata["author"]["value"], "Standard")
        self.assertEqual(metadata["author"]["source"], "page_metadata")

    def test_supplement_parser_exception_preserves_standard_metadata(self):
        from backend.app.adapters.xiaohongshu import XiaohongshuSourceAdapter
        resource = SimpleNamespace(page=SimpleNamespace(text=_html()),
            canonical_url=f"https://www.xiaohongshu.com/explore/{NOTE_ID}")
        with patch("backend.app.adapters.xiaohongshu.extract_xhs_embedded_metadata", side_effect=RuntimeError):
            metadata = XiaohongshuSourceAdapter(SimpleNamespace()).get_public_metadata(resource, "now")
        self.assertEqual(metadata["title"]["value"], "目标笔记")
        self.assertEqual(metadata["author"]["value"], "")

    def test_private_cover_dns_preserves_author_and_standard_title(self):
        from backend.app.adapters.xiaohongshu import XiaohongshuSourceAdapter
        resource = SimpleNamespace(page=SimpleNamespace(text=_html()),
            canonical_url=f"https://www.xiaohongshu.com/explore/{NOTE_ID}")
        with httpx.Client(transport=httpx.MockTransport(Mock())) as http:
            fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ["127.0.0.1"])
            metadata = XiaohongshuSourceAdapter(fetcher).get_public_metadata(resource, "now")
        self.assertEqual(metadata["title"]["value"], "目标笔记")
        self.assertEqual(metadata["author"]["value"], "目标作者")
        self.assertEqual(metadata["cover_url"]["value"], "")


class XiaohongshuEmbeddedBoundaryTests(unittest.TestCase):
    def test_identity_is_required_and_other_entries_are_never_substituted(self):
        variants = []
        for key in ("firstNoteId", "currentNoteId"):
            value = _state()
            value["note"][key] = OTHER_ID
            variants.append(value)
        for note_id in (None, OTHER_ID, NOTE_ID.upper()):
            value = _state()
            value["note"]["noteDetailMap"][NOTE_ID]["note"]["noteId"] = note_id
            variants.append(value)
        value = _state()
        value["note"]["noteDetailMap"][OTHER_ID] = value["note"]["noteDetailMap"].pop(NOTE_ID)
        variants.append(value)
        variants.append(_state(note_type="unknown"))
        for value in variants:
            self.assertIsNone(extract_xhs_embedded_metadata(_html(value), NOTE_ID))

    def test_only_real_direct_inline_single_assignment_scripts_are_accepted(self):
        script = _script()
        bad = (
            f'<body><!--<script>{script}</script>--></body>',
            f'<body><template><script>{script}</script></template></body>',
            f'<body><noscript><script>{script}</script></noscript></body>',
            f'<body><div><script>{script}</script></div></body>',
            _html(script="const text=" + json.dumps(script) + ";"),
            _html(script="function x(){" + script + "}"),
            _html(attributes=' src="/external.js"'),
            _html(attributes=' type="application/ld+json"'),
            _html() + f'<script>{script}</script>',
            _html(script=script + script),
            _html(script=script + "alert(1)"),
        )
        for value in bad:
            with self.subTest(value=value[:45]):
                self.assertIsNone(extract_xhs_embedded_metadata(value, NOTE_ID))

    def test_undefined_is_a_value_only_and_strings_are_unchanged(self):
        raw = _script(_state(author="undefined"))
        raw = raw.replace('"note": {', '"unused": undefined, "note": {', 1)
        result = extract_xhs_embedded_metadata(_html(script=raw), NOTE_ID)
        self.assertEqual(result.author, "undefined")
        for bad in ("undefined()", "NaN", "Infinity", "1e9999", "(() => 1)()"):
            value = raw.replace('"unused": undefined', f'"unused": {bad}')
            self.assertIsNone(extract_xhs_embedded_metadata(_html(script=value), NOTE_ID))
        self.assertIsNone(extract_xhs_embedded_metadata(
            _html(script=raw.replace('"unused": undefined', 'undefined: 1')), NOTE_ID))

    def test_duplicate_keys_and_resource_limits_fail_closed(self):
        raw = _script().replace('"nickname":', '"nickname":"wrong","nickname":', 1)
        self.assertIsNone(extract_xhs_embedded_metadata(_html(script=raw), NOTE_ID))
        for extra in ("[" * 33 + "0" + "]" * 33,
                      "[" + ",".join("0" for _ in range(16385)) + "]",
                      json.dumps("x" * (256 * 1024))):
            raw = _script().replace('"note": {', '"extra":' + extra + ',"note": {', 1)
            self.assertIsNone(extract_xhs_embedded_metadata(_html(script=raw), NOTE_ID))
        self.assertIsNone(extract_xhs_embedded_metadata("<" * 10001 + _html(), NOTE_ID))
        self.assertIsNone(extract_xhs_embedded_metadata("x" * (4 * 1024 * 1024 + 1), NOTE_ID))

    def test_unsafe_fields_are_independent_and_no_other_image_is_chosen(self):
        for author in (None, 7, {"name": "Wrong"}, "https://example.com/user", "a" * 201,
                       "A\x00B", "A\tB", "A\ufffdB"):
            result = extract_xhs_embedded_metadata(_html(_state(author=author)), NOTE_ID)
            self.assertEqual(result.author, "")
            self.assertEqual(result.cover_url, HTTPS_COVER)
        bad_covers = (None, 7, "https://picasso-static.xiaohongshu.com/logo.png",
            "http://evil.example/image.webp", "https://sns-avatar-qc.xhscdn.com/a.webp",
            "https://evil.sns-webpic-qc.xhscdn.com/a.webp", HTTP_COVER + "#x",
            "https://u:p@sns-webpic-qc.xhscdn.com/a.webp", HTTP_COVER.replace(".webp?", ".mp4?"),
            HTTP_COVER.replace(".webp?", ".%6dp4?"), HTTP_COVER.replace(".webp?", ".mpd?"),
            HTTP_COVER.replace(".webp?", ".%256dpd?"), HTTP_COVER + "a" * 2048,
            HTTP_COVER.replace("/cover", "/co\nver"), HTTP_COVER.replace(".com/", ".com:81/"))
        for cover in bad_covers:
            state = _state(cover=cover)
            state["note"]["noteDetailMap"][NOTE_ID]["note"]["imageList"].append({"urlDefault": HTTPS_COVER})
            result = extract_xhs_embedded_metadata(_html(state), NOTE_ID)
            self.assertEqual(result.cover_url, "")
            self.assertEqual(result.author, "目标作者")


if __name__ == "__main__":
    unittest.main()
