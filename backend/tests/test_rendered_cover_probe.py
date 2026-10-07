from __future__ import annotations

import copy
import json
import socket
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from backend.tests.rendered_cover_worker_fixtures import (
    containment_unavailable_worker,
    stalled_listener_worker,
)
from backend.app.api.main import create_app
from backend.app.domain.models import CollectionPreviewRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.capture import CaptureService
from backend.app.services.collection_imports import CollectionImportPreviewCoordinator
from backend.app.services.rendered_cover import (
    PlaywrightRenderedCoverProbe,
    PinnedConnectProxy,
    _cover_from_detail_response,
    _detail_response_matches,
    _document_response_is_download,
    _extract_douyin_detail_cover,
    _parse_connect_target,
    _should_block_request,
    _validate_douyin_target,
)
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.app.services.safe_http import SafeHttpError, SafeImageFetchResult


TARGET_ID = "7667239585550583290"
TARGET_URL = f"https://www.douyin.com/video/{TARGET_ID}"
COVER_URL = "https://p9-pc-sign.douyinpic.com/image-cut-tos-priv/cover.jpeg"


class _MemoryRepository:
    def __init__(self):
        self.cache = {}
        self.previews = {}

    def get_collection_metadata_cache(self, url):
        return copy.deepcopy(self.cache.get(url))

    def upsert_collection_metadata_cache(self, payload, aliases):
        for alias in aliases:
            self.cache[alias] = copy.deepcopy(payload)

    def create_collection_preview(self, payload):
        self.previews[payload["preview_id"]] = copy.deepcopy(payload)


class _Validator:
    def __init__(self, *, reject=False):
        self.reject = reject
        self.calls = []

    def resolve(self, url):
        self.calls.append(url)
        if self.reject:
            raise SafeHttpError("UNSAFE_URL")
        return object()


class _NoGenericFetch:
    def __init__(self, *, reject=False):
        self.url_validator = _Validator(reject=reject)

    def fetch(self, url):
        raise AssertionError("known platform capture must not use generic fetch")


class _Adapter:
    def __init__(self, *, cover=""):
        self.cover = cover

    def match(self, url):
        return "douyin.com" in url

    def resolve(self, url):
        return SimpleNamespace(
            platform="douyin",
            canonical_url=TARGET_URL,
            video_id=TARGET_ID,
            aliases=(url, TARGET_URL),
        )

    def get_metadata(self, resolved):
        return SimpleNamespace(
            title="",
            author="",
            description="",
            tags=[],
            cover_url=self.cover,
            warnings=["抖音公开页面未提供标题。"],
        )


class _Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def matching(self, url):
        return self.adapter if self.adapter.match(url) else None


class _Probe:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def probe(self, platform, canonical_url, expected_identity):
        self.calls.append((platform, canonical_url, expected_identity))
        if self.error is not None:
            raise self.error
        return self.result


class _ImageFetcher:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return SafeImageFetchResult(
            final_url=url,
            media_type="image/png",
            body=self.body,
            redirects=(),
            content_type="image/png",
        )


def _service(*, adapter, probe, fetcher=None):
    repo = _MemoryRepository()
    return repo, CaptureService(
        repo,
        fetcher=fetcher or _NoGenericFetch(),
        adapter_registry=_Registry(adapter),
        rendered_cover_probe=probe,
    )


class RenderedCoverCaptureTests(unittest.TestCase):
    def test_missing_douyin_cover_uses_rendered_public_cover_and_cache(self):
        probe = _Probe(COVER_URL)
        fetcher = _NoGenericFetch()
        _, service = _service(adapter=_Adapter(), probe=probe, fetcher=fetcher)

        first = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        second = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))

        self.assertEqual(first.metadata.cover_url.value, COVER_URL)
        self.assertEqual(first.metadata.cover_url.source, "platform_public")
        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(probe.calls, [("douyin", TARGET_URL, TARGET_ID)])
        self.assertEqual(fetcher.url_validator.calls, [COVER_URL])

    def test_existing_platform_cover_never_starts_probe(self):
        probe = _Probe(error=AssertionError("probe must not run"))
        _, service = _service(adapter=_Adapter(cover=COVER_URL), probe=probe)

        preview = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))

        self.assertEqual(preview.metadata.cover_url.value, COVER_URL)
        self.assertEqual(probe.calls, [])

    def test_explicit_batch_scope_never_starts_probe(self):
        probe = _Probe(error=AssertionError("batch must not start a browser"))
        _, service = _service(adapter=_Adapter(), probe=probe)

        preview = service.preview(
            CollectionPreviewRequest(input_text=TARGET_URL),
            preview_id="batch-owned-preview",
            allow_rendered_cover=False,
        )

        self.assertEqual(preview.metadata.cover_url.value, "")
        self.assertEqual(probe.calls, [])

    def test_batch_coordinator_always_disables_rendered_probe(self):
        class Repository:
            def __init__(self):
                self.claimed = False
                self.published = []

            def claim_preview(self, **_kwargs):
                if self.claimed:
                    return None
                self.claimed = True
                return {
                    "batch_item_id": "batch-item",
                    "preview_id": "batch-preview",
                    "preview_generation": 1,
                    "claim_token": "claim-token",
                    "input_text": TARGET_URL,
                }

            def publish_preview_success(self, **kwargs):
                self.published.append(kwargs)
                return True

        class Capture:
            def __init__(self):
                self.calls = []

            def preview(self, request, *, preview_id, allow_rendered_cover):
                self.calls.append((request.input_text, preview_id, allow_rendered_cover))
                return SimpleNamespace(preview_id=preview_id)

        repository = Repository()
        capture = Capture()
        coordinator = CollectionImportPreviewCoordinator(
            repository, capture, id_factory=lambda: "generated"
        )
        try:
            coordinator._drain_batch("batch")
        finally:
            coordinator.close()
        self.assertEqual(capture.calls, [(TARGET_URL, "batch-preview", False)])
        self.assertEqual(len(repository.published), 1)

    def test_probe_failure_or_rejected_url_stays_saveable(self):
        cases = (
            (_Probe(error=RuntimeError("private transport detail")), _NoGenericFetch()),
            (_Probe(COVER_URL), _NoGenericFetch(reject=True)),
            (_Probe("https://public.example/cover.jpg"), _NoGenericFetch()),
            (_Probe(None), _NoGenericFetch()),
        )
        for probe, fetcher in cases:
            with self.subTest(probe=probe, rejected=fetcher.url_validator.reject):
                _, service = _service(adapter=_Adapter(), probe=probe, fetcher=fetcher)
                preview = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
                self.assertEqual(preview.metadata.cover_url.value, "")
                self.assertEqual(preview.metadata.cover_url.source, "none")
                self.assertEqual(preview.platform, "douyin")
                self.assertTrue(
                    any("自动封面暂不可用" in warning for warning in preview.metadata.warnings)
                )
                self.assertNotIn(
                    "private transport detail", " ".join(preview.metadata.warnings)
                )

    def test_fresh_failed_probe_cache_does_not_relaunch_browser(self):
        probe = _Probe(None)
        _, service = _service(adapter=_Adapter(), probe=probe)

        first = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))
        second = service.preview(CollectionPreviewRequest(input_text=TARGET_URL))

        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(len(probe.calls), 1)

    def test_preview_to_same_origin_cover_endpoint_is_complete(self):
        output = BytesIO()
        Image.new("RGB", (2, 2), (22, 44, 66)).save(output, format="PNG")
        png = output.getvalue()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SQLiteRepository(root / "collections.sqlite3")
            pipeline = LocalFullPipeline(
                repository,
                UnconfiguredAsrProvider(),
                DeterministicFullExtractor(),
            )
            probe = _Probe(COVER_URL)
            image_fetcher = _ImageFetcher(png)
            app = create_app(
                pipeline,
                resolution_service=SimpleNamespace(registry=_Registry(_Adapter())),
                capture_fetcher=_NoGenericFetch(),
                rendered_cover_probe=probe,
                cover_fetcher=image_fetcher,
                upload_root=root / "uploads",
                inspiration_temp_root=root / "inspiration",
                cover_cache_root=root / "cover-cache",
                user_cover_root=root / "user-covers",
            )
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/collection-previews",
                    json={"input_text": TARGET_URL},
                )
                self.assertEqual(response.status_code, 201, response.text)
                preview = response.json()
                self.assertEqual(preview["metadata"]["cover_url"]["value"], COVER_URL)
                cover = client.get(
                    f"/api/v1/collection-previews/{preview['preview_id']}/cover"
                )
                self.assertEqual(cover.status_code, 200, cover.text)
                self.assertEqual(cover.content, png)
                self.assertEqual(cover.headers["content-type"], "image/png")
                self.assertEqual(
                    cover.headers["cross-origin-resource-policy"], "same-origin"
                )
            self.assertEqual(probe.calls, [("douyin", TARGET_URL, TARGET_ID)])
            self.assertEqual(image_fetcher.calls, [COVER_URL])


class RenderedCoverPolicyTests(unittest.TestCase):
    def test_target_identity_is_exact_and_douyin_only(self):
        self.assertEqual(_validate_douyin_target(TARGET_URL, TARGET_ID), TARGET_URL)
        for url, identity in (
            (f"https://www.douyin.com/video/{int(TARGET_ID) + 1}", TARGET_ID),
            (f"https://evil.example/video/{TARGET_ID}", TARGET_ID),
            (f"http://www.douyin.com/video/{TARGET_ID}", TARGET_ID),
            (f"https://www.douyin.com/user/{TARGET_ID}", TARGET_ID),
            (TARGET_URL, "not-digits"),
        ):
            with self.subTest(url=url):
                self.assertIsNone(_validate_douyin_target(url, identity))

    def test_page_detail_response_requires_exact_identity_and_bounded_json(self):
        detail_url = (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            f"?aweme_id={TARGET_ID}"
        )
        self.assertTrue(_detail_response_matches(detail_url, TARGET_ID))
        self.assertFalse(
            _detail_response_matches(
                detail_url.replace(TARGET_ID, str(int(TARGET_ID) + 1)), TARGET_ID
            )
        )
        self.assertFalse(
            _detail_response_matches(
                f"https://evil.example/aweme/v1/web/aweme/detail/?aweme_id={TARGET_ID}",
                TARGET_ID,
            )
        )
        payload = {
            "aweme_detail": {
                "aweme_id": TARGET_ID,
                "video": {"cover": {"url_list": [COVER_URL]}},
            }
        }
        self.assertEqual(_extract_douyin_detail_cover(payload, TARGET_ID), COVER_URL)
        self.assertIsNone(
            _extract_douyin_detail_cover(
                {"aweme_detail": payload["aweme_detail"] | {"aweme_id": "123"}},
                TARGET_ID,
            )
        )

        class Response:
            def __init__(
                self,
                body,
                content_type="application/json",
                status=200,
                content_length=None,
            ):
                self._body = body
                self._content_type = content_type
                self.status = status
                self._content_length = (
                    str(len(body)) if content_length is None else content_length
                )

            def all_headers(self):
                return {
                    "content-type": self._content_type,
                    "content-length": self._content_length,
                }

            def body(self):
                return self._body

        encoded = json.dumps(payload).encode()
        self.assertEqual(
            _cover_from_detail_response(Response(encoded), TARGET_ID), COVER_URL
        )
        self.assertIsNone(
            _cover_from_detail_response(
                Response(encoded, "text/html; profile=notjson"), TARGET_ID
            )
        )
        self.assertIsNone(
            _cover_from_detail_response(Response(encoded, status=500), TARGET_ID)
        )
        self.assertIsNone(_cover_from_detail_response(Response(b"{"), TARGET_ID))
        self.assertIsNone(
            _cover_from_detail_response(Response(b" " * 1_000_001), TARGET_ID)
        )
        self.assertEqual(
            _cover_from_detail_response(
                Response(encoded, content_length=""), TARGET_ID
            ),
            COVER_URL,
        )
        self.assertIsNone(
            _cover_from_detail_response(
                Response(encoded, content_length="1000001"), TARGET_ID
            )
        )

    def test_document_download_is_rejected_from_response_headers(self):
        base = {
            "resourceType": "Document",
            "responseStatusCode": 200,
            "responseHeaders": [
                {"name": "Content-Type", "value": "text/html; charset=utf-8"}
            ],
        }
        self.assertFalse(_document_response_is_download(base))
        self.assertFalse(
            _document_response_is_download(base | {"responseStatusCode": 302})
        )
        self.assertTrue(
            _document_response_is_download(
                base
                | {
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "application/octet-stream"}
                    ]
                }
            )
        )
        self.assertTrue(
            _document_response_is_download(
                base
                | {
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "text/html"},
                        {
                            "name": "Content-Disposition",
                            "value": "attachment; filename=page.html",
                        },
                    ]
                }
            )
        )

    def test_request_policy_blocks_bytes_and_credentials(self):
        for resource_type, url in (
            ("image", COVER_URL),
            ("media", "https://v26-web.douyinvod.com/a.mp4"),
            ("font", "https://lf.example/font.woff2"),
            ("xhr", "https://v26-web.douyinvod.com/range"),
            ("fetch", "https://example.com/live.m3u8?token=redacted"),
            ("fetch", "https://www.douyin.com/aweme/v1/play/?video_id=secret"),
            ("xhr", "https://www.douyin.com/passport/general/login_guiding_strategy/"),
            ("document", COVER_URL),
            ("other", "https://p3.byteimg.com/cover-without-extension"),
            ("document", "https://www.douyin.com/file.zip"),
        ):
            with self.subTest(resource_type=resource_type, url=url):
                self.assertTrue(_should_block_request(resource_type, url))
        self.assertFalse(
            _should_block_request("script", "https://lf-douyin-pc-web.douyinstatic.com/app.js")
        )
        self.assertFalse(_should_block_request("document", TARGET_URL, TARGET_ID))
        self.assertFalse(
            _should_block_request(
                "xhr",
                f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={TARGET_ID}",
                TARGET_ID,
            )
        )
        self.assertTrue(
            _should_block_request(
                "xhr", "https://www.douyin.com/aweme/v1/web/aweme/detail/", TARGET_ID
            )
        )

        class Request:
            resource_type = "script"
            url = "https://lf-douyin-pc-web.douyinstatic.com/app.js"
            headers = {
                "cookie": "private",
                "authorization": "secret",
                "referer": TARGET_URL,
                "accept": "*/*",
            }

        class Route:
            request = Request()

            def __init__(self):
                self.aborted = False
                self.headers = None
                self.fulfilled = False

            def abort(self, _reason):
                self.aborted = True

            def continue_(self, *, headers):
                self.headers = headers

            def fulfill(self, **_kwargs):
                self.fulfilled = True

        limits = []
        handler = PlaywrightRenderedCoverProbe._route_handler(
            lambda: limits.append(True), TARGET_ID
        )
        routes = [Route() for _ in range(257)]
        for route in routes:
            handler(route)
        self.assertFalse(routes[255].aborted)
        self.assertTrue(routes[256].aborted)
        self.assertEqual(limits, [True])
        self.assertEqual(routes[0].headers, {"accept": "*/*"})

        image_route = Route()
        image_route.request = SimpleNamespace(
            resource_type="image", url=COVER_URL, headers={}
        )
        PlaywrightRenderedCoverProbe._route_handler(
            expected_identity=TARGET_ID
        )(image_route)
        self.assertFalse(image_route.fulfilled)
        self.assertTrue(image_route.aborted)

    def test_connect_proxy_rejects_invalid_or_private_targets_and_closes_listener(self):
        self.assertEqual(
            _parse_connect_target(b"CONNECT www.douyin.com:443 HTTP/1.1\r\n"),
            "www.douyin.com",
        )
        self.assertIsNone(
            _parse_connect_target(b"CONNECT evil.example:443 HTTP/1.1\r\n")
        )
        connector_calls = []
        proxy = PinnedConnectProxy(
            dns_resolver=lambda _host: ["127.0.0.1"],
            connector=lambda address, timeout: connector_calls.append((address, timeout)),
        )
        with proxy:
            host, port = proxy._server.server_address
            with socket.create_connection((host, port), timeout=1) as client:
                client.sendall(
                    b"CONNECT www.douyin.com:443 HTTP/1.1\r\nHost: www.douyin.com\r\n\r\n"
                )
                client.settimeout(1)
                self.assertEqual(client.recv(1), b"")
        self.assertEqual(connector_calls, [])
        with socket.socket() as check:
            self.assertNotEqual(check.connect_ex((host, port)), 0)

    def test_probe_concurrency_is_fail_soft(self):
        probe = PlaywrightRenderedCoverProbe(timeout_seconds=1)
        self.assertTrue(probe._slot.acquire(blocking=False))
        try:
            self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        finally:
            probe._slot.release()

    def test_missing_playwright_runtime_is_fail_soft_and_closes_proxy(self):
        proxy = PinnedConnectProxy(dns_resolver=lambda _host: ["127.0.0.1"])
        stages = []

        def unavailable():
            raise ImportError("playwright is unavailable")

        probe = PlaywrightRenderedCoverProbe(
            timeout_seconds=1,
            playwright_starter=unavailable,
            proxy_factory=lambda **_kwargs: proxy,
            diagnostic_sink=stages.append,
        )
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        self.assertEqual(stages, ["failed-driver"])
        self.assertIsNone(proxy._server)
        self.assertIsNone(proxy._thread)

    def test_detail_response_precedes_video_and_cleanup_failure_downgrades(self):
        payload = json.dumps({
            "aweme_detail": {
                "aweme_id": TARGET_ID,
                "video": {"cover": {"url_list": [COVER_URL]}},
            }
        }).encode()

        class Response:
            url = (
                "https://www.douyin.com/aweme/v1/web/aweme/detail/"
                f"?aweme_id={TARGET_ID}"
            )
            status = 200

            def all_headers(self):
                return {
                    "content-type": "application/json",
                    "content-length": str(len(payload)),
                }

            def body(self):
                return payload

        class Page:
            url = TARGET_URL

            def __init__(self, fail_close):
                self.fail_close = fail_close
                self.response_callback = None

            def on(self, event, callback):
                if event == "response":
                    self.response_callback = callback

            def goto(self, *_args, **_kwargs):
                self.response_callback(Response())

            def wait_for_timeout(self, _timeout):
                raise AssertionError("an available detail response must not wait")

            def wait_for_function(self, *_args, **_kwargs):
                raise AssertionError("an available detail response must precede video DOM")

            def evaluate(self, _script):
                raise AssertionError("detail success must not inspect DOM candidates")

            def close(self):
                if self.fail_close:
                    raise RuntimeError("close failed")

        class Context:
            def __init__(self, fail_close):
                self.page = Page(fail_close)
                self.cdp = SimpleNamespace(
                    on=lambda *_args: None,
                    send=lambda *_args: None,
                    detach=lambda: None,
                )

            def set_default_timeout(self, _timeout):
                pass

            def route(self, *_args):
                pass

            def new_page(self):
                return self.page

            def new_cdp_session(self, _page):
                return self.cdp

            def on(self, *_args):
                pass

            def close(self):
                pass

        class Browser:
            def __init__(self, fail_close):
                self.context = Context(fail_close)

            def new_context(self, **_kwargs):
                return self.context

            def close(self):
                pass

        class Driver:
            def __init__(self, fail_close):
                self.browser = Browser(fail_close)
                self.chromium = SimpleNamespace(launch=lambda **_kwargs: self.browser)

            def stop(self):
                pass

        class Proxy:
            server_url = "http://127.0.0.1:1"

            def __init__(self, delay=0):
                self.delay = delay

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                time.sleep(self.delay)
                return None

        for fail_close, proxy_delay, timeout, expected, expected_stage in (
            (False, 0, 1, COVER_URL, []),
            (True, 0, 1, None, ["failed-cleanup"]),
            (False, 0.1, 0.05, None, ["watchdog-timeout"]),
        ):
            with self.subTest(fail_close=fail_close, proxy_delay=proxy_delay):
                stages = []
                probe = PlaywrightRenderedCoverProbe(
                    timeout_seconds=timeout,
                    playwright_starter=lambda: Driver(fail_close),
                    proxy_factory=lambda **_kwargs: Proxy(proxy_delay),
                    diagnostic_sink=stages.append,
                )
                self.assertEqual(
                    probe.probe("douyin", TARGET_URL, TARGET_ID), expected
                )
                self.assertEqual(stages, expected_stage)

    def test_watchdog_bounds_caller_and_keeps_slot_until_worker_cleanup(self):
        started = threading.Event()
        release = threading.Event()
        proxy = PinnedConnectProxy(dns_resolver=lambda _host: ["127.0.0.1"])
        stages = []

        def stalled():
            started.set()
            release.wait(timeout=1)
            raise ImportError("stalled runtime")

        probe = PlaywrightRenderedCoverProbe(
            timeout_seconds=0.05,
            playwright_starter=stalled,
            proxy_factory=lambda **_kwargs: proxy,
            diagnostic_sink=stages.append,
        )
        before = time.monotonic()
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        elapsed = time.monotonic() - before
        self.assertTrue(started.is_set())
        self.assertLess(elapsed, 0.2)
        self.assertEqual(stages, ["watchdog-timeout"])
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        release.set()
        self.assertTrue(probe._slot.acquire(timeout=1))
        probe._slot.release()
        self.assertIsNone(proxy._server)

    def test_process_isolation_terminates_stalled_worker_listener(self):
        stages = []
        probe = PlaywrightRenderedCoverProbe(
            timeout_seconds=1,
            diagnostic_sink=stages.append,
            process_isolation=True,
            isolated_worker_target=stalled_listener_worker,
        )
        before = time.monotonic()
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        self.assertLess(time.monotonic() - before, 2.5)
        listener_stage = next(
            stage for stage in stages if stage.startswith("test-listener:")
        )
        port = int(listener_stage.partition(":")[2])
        with socket.socket() as check:
            self.assertNotEqual(check.connect_ex(("127.0.0.1", port)), 0)
        self.assertFalse(probe._disabled.is_set())

    def test_unavailable_process_containment_permanently_disables_probe(self):
        stages = []
        probe = PlaywrightRenderedCoverProbe(
            timeout_seconds=1,
            diagnostic_sink=stages.append,
            process_isolation=True,
            isolated_worker_target=containment_unavailable_worker,
        )
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        self.assertTrue(probe._disabled.is_set())
        self.assertIsNone(probe.probe("douyin", TARGET_URL, TARGET_ID))
        self.assertEqual(stages, ["containment-unavailable"])

    def test_timeout_cannot_exceed_ten_second_contract(self):
        with self.assertRaises(ValueError):
            PlaywrightRenderedCoverProbe(timeout_seconds=10.1)


if __name__ == "__main__":
    unittest.main()
