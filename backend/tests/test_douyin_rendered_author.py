from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.adapters.base import ResolvedVideo
from backend.app.domain.models import CollectionPreviewRequest
from backend.app.services import rendered_cover
from backend.tests.test_douyin_metadata_regressions import adapter_for
from backend.tests.test_rendered_cover_probe import (
    COVER_URL, TARGET_ID, TARGET_URL, _Adapter, _ImageFetcher,
    _NoGenericFetch, _Probe, _Registry, _service,
    SQLiteRepository, LocalFullPipeline, UnconfiguredAsrProvider,
    DeterministicFullExtractor, create_app,
)


class MetadataProbe:
    def __init__(self, author="CF子豪", cover=COVER_URL):
        self.result = SimpleNamespace(author=author, cover_url=cover)
        self.calls = []

    def probe_metadata(self, *args):
        self.calls.append(args)
        return self.result

    def probe(self, *_args):
        raise AssertionError("structured capture must not run a second legacy probe")


class PublicAdapter(_Adapter):
    def __init__(self, author="", cover=""):
        super().__init__(cover=cover)
        self.author = author

    def get_metadata(self, resolved):
        result = super().get_metadata(resolved)
        result.author = self.author
        result.title = "纯武将"
        return result


def _payload(author="CF子豪", cover=COVER_URL):
    return {"aweme_detail": {
        "aweme_id": TARGET_ID,
        "author": {"nickname": author, "unique_id": "account123"},
        "video": {"cover": {"url_list": [cover]}},
        "music": {"author": "音乐作者"},
        "recommend": {"author": {"nickname": "推荐作者"}},
        "desc": "游戏玩家：霜牛",
    }}


def invalid_author_worker(sender, *_args):
    sender.send((rendered_cover.DouyinMetadataSupplement(author="坏\x00名字", cover_url=COVER_URL), None))
    sender.close()


def invalid_cover_worker(sender, *_args):
    sender.send((rendered_cover.DouyinMetadataSupplement(author="CF子豪", cover_url="https://evil.example/a.jpg"), None))
    sender.close()


class DouyinAuthorCaptureTests(unittest.TestCase):
    def test_single_probe_adds_nickname_and_cover_without_second_probe(self):
        probe = MetadataProbe()
        repo, service = _service(adapter=PublicAdapter(), probe=probe)
        first = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        second = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        self.assertEqual(first.metadata.author.value, "CF子豪")
        self.assertEqual(first.metadata.author.source, "platform_public")
        self.assertEqual(first.metadata.cover_url.value, COVER_URL)
        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(probe.calls, [("douyin", TARGET_URL, TARGET_ID)])
        self.assertEqual(repo.cache[TARGET_URL]["rendered_metadata_status"], "found")

    def test_only_missing_fields_are_filled_and_complete_skips_probe(self):
        for author, cover, expected_author, expected_calls in (
            ("Existing", "", "Existing", 1),
            ("", COVER_URL + "?existing=1", "CF子豪", 1),
            ("Existing", COVER_URL, "Existing", 0),
        ):
            with self.subTest(author=author, cover=cover):
                probe = MetadataProbe()
                _, service = _service(adapter=PublicAdapter(author, cover), probe=probe)
                result = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
                self.assertEqual(result.metadata.author.value, expected_author)
                self.assertEqual(result.metadata.cover_url.value, cover or COVER_URL)
                self.assertEqual(len(probe.calls), expected_calls)

    def test_unsafe_fields_are_independent_at_capture_boundary(self):
        for author, cover, reject_dns, expected_author, expected_cover in (
            ("CF子豪", "", False, "CF子豪", ""),
            ("CF子豪", COVER_URL, True, "CF子豪", ""),
            ("CF子豪", "https://evil.example/a.jpg", False, "CF子豪", ""),
            ("坏\x00名字", COVER_URL, False, "", COVER_URL),
            ("坏�名字", COVER_URL, False, "", COVER_URL),
            ("https://example.com/name", COVER_URL, False, "", COVER_URL),
            ("长" * 201, COVER_URL, False, "", COVER_URL),
            (7, COVER_URL, False, "", COVER_URL),
        ):
            with self.subTest(author=author, cover=cover, dns=reject_dns):
                probe = MetadataProbe(author, cover)
                _, service = _service(adapter=PublicAdapter(), probe=probe,
                    fetcher=_NoGenericFetch(reject=reject_dns))
                first = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
                service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
                self.assertEqual(first.metadata.author.value, expected_author)
                self.assertEqual(first.metadata.cover_url.value, expected_cover)
                self.assertEqual(len(probe.calls), 1)

    def test_legacy_fresh_cache_is_unchanged_until_refresh_or_expiry(self):
        probe = MetadataProbe()
        repo, service = _service(adapter=PublicAdapter(), probe=probe)
        request = CollectionPreviewRequest(input_text=TARGET_URL)
        original = service.preview(request, allow_rendered_cover=False)
        snapshot = copy.deepcopy(repo.cache)
        self.assertEqual(service.preview(request).metadata, original.metadata)
        self.assertEqual(repo.cache, snapshot)
        self.assertEqual(probe.calls, [])
        refreshed = service.preview(CollectionPreviewRequest(input_text=TARGET_URL, refresh_metadata=True))
        self.assertEqual(refreshed.metadata.author.value, "CF子豪")
        self.assertEqual(len(probe.calls), 1)
        repo.cache[TARGET_URL]["expires_at"] = "2000-01-01T00:00:00+00:00"
        service.preview(request)
        self.assertEqual(len(probe.calls), 2)
        service.preview(CollectionPreviewRequest(input_text=TARGET_URL, refresh_metadata=True),
                        allow_rendered_cover=False)
        self.assertEqual(len(probe.calls), 2)
        self.assertEqual(original.metadata.author.value, "")

    def test_legacy_string_probe_contract_is_preserved(self):
        probe = _Probe(COVER_URL)
        _, service = _service(adapter=PublicAdapter(), probe=probe)
        result = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        self.assertEqual(result.metadata.author.value, "")
        self.assertEqual(result.metadata.cover_url.value, COVER_URL)
        self.assertEqual(len(probe.calls), 1)
        probe = _Probe(error=AssertionError("legacy probe cannot add authors"))
        _, service = _service(adapter=PublicAdapter(cover=COVER_URL), probe=probe)
        service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        self.assertEqual(probe.calls, [])

    def test_save_rebuild_search_user_author_and_no_deep_data(self):
        for author, cover in (("CF子豪", COVER_URL), ("CF子豪", ""), ("", COVER_URL), ("", "")):
            with self.subTest(author=author, cover=cover), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                probe = MetadataProbe(author, cover)
                output = BytesIO()
                Image.new("RGB", (2, 2), (12, 34, 56)).save(output, "PNG")
                image_fetcher = _ImageFetcher(output.getvalue())

                def app(repo):
                    pipeline = LocalFullPipeline(repo, UnconfiguredAsrProvider(), DeterministicFullExtractor())
                    return create_app(pipeline,
                        resolution_service=SimpleNamespace(registry=_Registry(PublicAdapter())),
                        capture_fetcher=_NoGenericFetch(), rendered_cover_probe=probe,
                        cover_fetcher=image_fetcher, upload_root=root / "uploads",
                        inspiration_temp_root=root / "inspiration", cover_cache_root=root / "covers",
                        user_cover_root=root / "user-covers")

                repo = SQLiteRepository(root / "synthetic.sqlite3")
                with TestClient(app(repo)) as client:
                    response = client.post("/api/v1/collection-previews", json={"input_text": TARGET_URL})
                    self.assertEqual(response.status_code, 201, response.text)
                    preview = response.json()
                    self.assertEqual(preview["metadata"]["author"]["value"], author)
                    saved = client.post("/api/v1/collection-items", headers={"Idempotency-Key": "dy-author"},
                        json={"preview_id": preview["preview_id"], "user_author": "Personal Author"})
                    self.assertEqual(saved.status_code, 201, saved.text)
                    item = saved.json()
                    self.assertEqual(item["user_author"], "Personal Author")
                reopened = SQLiteRepository(root / "synthetic.sqlite3")
                with TestClient(app(reopened)) as client:
                    self.assertEqual(client.get(f"/api/v1/collection-items/{item['id']}").json(), item)
                    for query in (["CF子豪", "Personal Author"] if author else ["Personal Author"]):
                        found = client.get("/api/v1/collection-items", params={"query": query}).json()
                        self.assertEqual([row["id"] for row in found["items"]], [item["id"]])
                    if cover:
                        result = client.get(f"/api/v1/collection-items/{item['id']}/cover")
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(result.content, output.getvalue())
                    with reopened._connect() as db:
                        for table in ("jobs", "videos", "transcript_segments", "extractions"):
                            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(len(probe.calls), 1)


class DouyinAuthorExtractionTests(unittest.TestCase):
    def extract(self, payload):
        method = getattr(rendered_cover, "_extract_douyin_detail_metadata", None)
        self.assertTrue(callable(method), "missing bounded author extraction seam")
        return method(payload, TARGET_ID)

    def test_exact_detail_publisher_only_and_partial_without_video(self):
        payload = _payload()
        result = self.extract(payload)
        self.assertEqual((result.author, result.cover_url), ("CF子豪", COVER_URL))
        del payload["aweme_detail"]["video"]
        result = self.extract(payload)
        self.assertEqual((result.author, result.cover_url), ("CF子豪", ""))
        payload["aweme_detail"]["aweme_id"] = "123"
        self.assertIsNone(self.extract(payload))
        self.assertIsNone(self.extract({"recommend": _payload()["aweme_detail"]}))

    def test_nickname_validation_does_not_fall_back_to_account_or_other_authors(self):
        for author in (None, 7, {}, "", "坏\x00名字", "坏�名字", "https://example.com/u", "长" * 201):
            with self.subTest(author=author):
                result = self.extract(_payload(author))
                self.assertEqual(result.author, "")
                self.assertEqual(result.cover_url, COVER_URL)
        result = self.extract(_payload("  ＣＦ子豪 🌈  "))
        self.assertEqual(result.author, "CF子豪 🌈")

    def test_normal_adapter_does_not_substitute_unique_id_or_unsafe_nickname(self):
        adapter = adapter_for()
        for author in ({"unique_id": "not-display-name"}, {"nickname": "坏\x00名字"},
                       {"nickname": "CF子豪", "unique_id": "not-display-name"}):
            payload = _payload()
            payload["aweme_detail"]["author"] = author
            video = ResolvedVideo(platform="douyin", source_url=TARGET_URL, canonical_url=TARGET_URL,
                video_id=TARGET_ID, page_html='<script type="application/json">' + json.dumps(payload) + '</script>')
            result = adapter.get_metadata(video)
            self.assertEqual(result.author, "CF子豪" if author.get("nickname") == "CF子豪" else "")

    def test_single_browser_response_and_cleanup_identity_guards(self):
        for bad_request, bad_response, bad_final, fail_close, author, cover in (
            (False, False, False, False, "CF子豪", COVER_URL),
            (False, False, False, False, "CF子豪", ""),
            (True, False, False, False, "CF子豪", COVER_URL),
            (False, True, False, False, "CF子豪", COVER_URL),
            (False, False, True, False, "CF子豪", COVER_URL),
            (False, False, False, True, "CF子豪", COVER_URL),
        ):
            with self.subTest(request=bad_request, response=bad_response, final=bad_final, close=fail_close):
                payload = _payload(author, cover)
                if bad_response:
                    payload["aweme_detail"]["aweme_id"] = "123"
                body = json.dumps(payload).encode()
                response = SimpleNamespace(
                    url="https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + ("123" if bad_request else TARGET_ID),
                    status=200, all_headers=lambda: {"content-type": "application/json"}, body=lambda: body)
                callbacks = {}
                page = Mock()
                page.url = TARGET_URL.replace(TARGET_ID, "123") if bad_final else TARGET_URL
                page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
                page.goto.side_effect = lambda *_args, **_kwargs: callbacks["response"](response)
                page.wait_for_event.side_effect = TimeoutError("no further synthetic response")
                if fail_close:
                    page.close.side_effect = RuntimeError("synthetic cleanup failure")
                context, browser, driver = Mock(), Mock(), Mock()
                context.new_page.return_value = page
                browser.new_context.return_value = context
                driver.chromium.launch.return_value = browser
                probe = rendered_cover.PlaywrightRenderedCoverProbe(
                    playwright_starter=lambda: driver,
                    proxy_factory=lambda **_kwargs: nullcontext(SimpleNamespace(server_url="http://127.0.0.1:1")))
                method = getattr(probe, "probe_metadata", None)
                self.assertTrue(callable(method), "missing structured probe seam")
                result = method("douyin", TARGET_URL, TARGET_ID)
                if bad_request or bad_response or bad_final or fail_close:
                    self.assertIsNone(result)
                else:
                    self.assertEqual((result.author, result.cover_url), (author, cover))
                    page.wait_for_event.assert_not_called()
                page.goto.assert_called_once()
                browser.new_context.assert_called_once()
                context.new_page.assert_called_once()
                page.evaluate.assert_not_called()
                page.close.assert_called_once()
                browser.close.assert_called_once()
                driver.stop.assert_called_once()

    def test_parent_process_revalidates_each_field(self):
        for worker, expected in ((invalid_author_worker, ("", COVER_URL)), (invalid_cover_worker, ("CF子豪", ""))):
            with self.subTest(worker=worker):
                probe = rendered_cover.PlaywrightRenderedCoverProbe(isolated_worker_target=worker)
                method = getattr(probe, "probe_metadata", None)
                self.assertTrue(callable(method), "missing structured probe seam")
                result = method("douyin", TARGET_URL, TARGET_ID)
                self.assertIsNotNone(result)
                self.assertEqual((result.author, result.cover_url), expected)

    def _assert_late_identity_change_rejected(self, *, during_body):
        callbacks = {}
        page = Mock()
        page.url = TARGET_URL
        page.on.side_effect = lambda event, callback: callbacks.__setitem__(event, callback)
        body = json.dumps(_payload()).encode()

        def read_body():
            if during_body:
                page.url = TARGET_URL.replace(TARGET_ID, "123")
            return body

        response = SimpleNamespace(
            url="https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + TARGET_ID,
            status=200, all_headers=lambda: {"content-type": "application/json"}, body=read_body)

        def wait_response(*_args, **_kwargs):
            page.url = TARGET_URL.replace(TARGET_ID, "123")
            return response

        if during_body:
            page.goto.side_effect = lambda *_args, **_kwargs: callbacks["response"](response)
            page.wait_for_event.side_effect = AssertionError("already received target response")
        else:
            page.wait_for_event.side_effect = wait_response
        context, browser, driver = Mock(), Mock(), Mock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        driver.chromium.launch.return_value = browser
        probe = rendered_cover.PlaywrightRenderedCoverProbe(
            playwright_starter=lambda: driver,
            proxy_factory=lambda **_kwargs: nullcontext(SimpleNamespace(server_url="http://127.0.0.1:1")))
        result = probe.probe_metadata("douyin", TARGET_URL, TARGET_ID)
        self.assertIsNone(result)
        page.goto.assert_called_once()
        if not during_body:
            page.wait_for_event.assert_called_once()
        page.close.assert_called_once()
        context.close.assert_called_once()
        browser.close.assert_called_once()
        driver.stop.assert_called_once()

    def test_page_identity_changed_while_waiting_for_detail_is_rejected(self):
        self._assert_late_identity_change_rejected(during_body=False)

    def test_page_identity_changed_while_reading_detail_body_is_rejected(self):
        self._assert_late_identity_change_rejected(during_body=True)


if __name__ == "__main__":
    unittest.main()
