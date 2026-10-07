from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from backend.app.domain.models import CollectionPreviewRequest
from backend.app.services.capture import CaptureService
from backend.app.services.rendered_metadata import (
    PlaywrightRenderedMetadataProbe,
    RenderedMetadataSupplement,
    _bilibili_should_block_request,
    _extract_bilibili_document_metadata,
    _safe_author_candidate,
    _safe_bilibili_cover_candidate,
    _validate_bilibili_target,
)
from backend.app.services.safe_http import SafeHttpError


BVID = "BV1pE411A73a"
TARGET = f"https://www.bilibili.com/video/{BVID}"
COVER = "https://i0.hdslb.com/bfs/archive/target.jpg"


def _document(*, bvid=BVID, author="Target", cover=COVER, up="Wrong"):
    state = {
        "videoData": {
            "bvid": bvid,
            "owner": {"name": author} if author is not None else {},
            "pic": cover,
        },
        "upData": {"name": up},
    }
    return (
        "<html><head></head><body><script>window.__INITIAL_STATE__="
        + json.dumps(state)
        + ";</script></body></html>"
    ).encode()


class _Repository:
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
    def __init__(self, reject=False):
        self.reject = reject
        self.calls = []

    def resolve(self, url):
        self.calls.append(url)
        if self.reject:
            raise SafeHttpError("UNSAFE_URL")
        return object()


class _Fetcher:
    def __init__(self, reject=False):
        self.url_validator = _Validator(reject)

    def fetch(self, _url):
        raise AssertionError("known platform must not use generic fetch")


class _Adapter:
    platform = "bilibili"

    def __init__(self, *, author="", cover="", fail=False):
        self.author = author
        self.cover = cover
        self.fail = fail

    def match(self, url):
        return "bilibili.com" in url

    def resolve(self, url):
        if self.fail:
            raise RuntimeError("public page unavailable")
        return SimpleNamespace(
            platform="bilibili",
            canonical_url=TARGET,
            video_id=BVID,
            aliases=(url, TARGET),
        )

    def get_metadata(self, _resolved):
        return SimpleNamespace(
            title="Video",
            author=self.author,
            description="",
            tags=[],
            cover_url=self.cover,
            warnings=[],
        )


class _Registry:
    def __init__(self, adapter):
        self.adapter = adapter

    def matching(self, url):
        return self.adapter if self.adapter.match(url) else None


class _Probe:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def probe(self, platform, canonical_url, expected_identity):
        self.calls.append((platform, canonical_url, expected_identity))
        return self.result


def _service(adapter, probe, *, reject=False):
    repository = _Repository()
    fetcher = _Fetcher(reject)
    service = CaptureService(
        repository,
        fetcher=fetcher,
        adapter_registry=_Registry(adapter),
        rendered_metadata_probe=probe,
    )
    return repository, fetcher, service


class BilibiliRenderedMetadataPolicyTests(unittest.TestCase):
    def test_target_author_and_cover_contracts_are_exact(self):
        self.assertEqual(_validate_bilibili_target(TARGET, BVID), TARGET)
        for url, identity in (
            (TARGET + "?p=2", BVID),
            (TARGET.replace(BVID, "BV1other"), BVID),
            (f"https://bilibili.com/video/{BVID}", BVID),
            (f"http://www.bilibili.com/video/{BVID}", BVID),
            ("https://www.bilibili.com/video/av123", "av123"),
        ):
            with self.subTest(url=url):
                self.assertIsNone(_validate_bilibili_target(url, identity))

        self.assertEqual(_safe_author_candidate("  Target  Creator  "), "Target Creator")
        for author in (
            "",
            "https://space.bilibili.com/1",
            "bad\x00name",
            "bad\nname",
            "bad\tname",
            "x" * 201,
        ):
            self.assertIsNone(_safe_author_candidate(author))
        self.assertEqual(_safe_bilibili_cover_candidate(COVER), COVER)
        for cover in (
            "http://i0.hdslb.com/a.jpg",
            "https://i3.hdslb.com/a.jpg",
            "https://evil.i0.hdslb.com/a.jpg",
            "https://i0.hdslb.com/a.mp4",
        ):
            self.assertIsNone(_safe_bilibili_cover_candidate(cover))

    def test_document_extracts_only_matching_video_data(self):
        expected = RenderedMetadataSupplement(author="Target", cover_url=COVER)
        self.assertEqual(_extract_bilibili_document_metadata(_document(), BVID), expected)
        self.assertIsNone(
            _extract_bilibili_document_metadata(_document(bvid="BV1other"), BVID)
        )
        self.assertEqual(
            _extract_bilibili_document_metadata(
                _document(author=None, cover="https://i3.hdslb.com/a.jpg"), BVID
            ),
            RenderedMetadataSupplement(),
        )
        self.assertEqual(
            _extract_bilibili_document_metadata(
                _document(cover="http://i0.hdslb.com/bfs/archive/target.jpg"), BVID
            ),
            expected,
        )

    def test_bilibili_route_allows_only_exact_main_document(self):
        self.assertFalse(_bilibili_should_block_request("document", TARGET, BVID))
        for kind, url in (
            ("script", "https://s1.hdslb.com/app.js"),
            ("xhr", f"https://api.bilibili.com/x/view?bvid={BVID}"),
            ("image", COVER),
            ("document", "https://www.bilibili.com/"),
            ("document", TARGET + "?p=2"),
        ):
            self.assertTrue(_bilibili_should_block_request(kind, url, BVID))


class BilibiliRenderedMetadataCaptureTests(unittest.TestCase):
    def test_failed_public_page_is_recovered_and_cached(self):
        probe = _Probe(RenderedMetadataSupplement(author="Target", cover_url=COVER))
        repository, fetcher, service = _service(_Adapter(fail=True), probe)

        first = service.preview(CollectionPreviewRequest(input_text=TARGET))
        second = service.preview(CollectionPreviewRequest(input_text=TARGET))

        self.assertEqual(first.canonical_url, TARGET)
        self.assertEqual(first.metadata.author.value, "Target")
        self.assertEqual(first.metadata.author.source, "platform_public")
        self.assertEqual(first.metadata.cover_url.value, COVER)
        self.assertEqual(first.metadata.cover_url.source, "platform_public")
        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(probe.calls, [("bilibili", TARGET, BVID)])
        self.assertEqual(fetcher.url_validator.calls, [COVER])
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "found")

    def test_partial_result_only_fills_missing_field(self):
        probe = _Probe(RenderedMetadataSupplement(author="Target", cover_url=COVER))
        _, _, service = _service(_Adapter(author="Existing", cover=""), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "Existing")
        self.assertEqual(preview.metadata.cover_url.value, COVER)

        probe = _Probe(RenderedMetadataSupplement(author="Target", cover_url=""))
        repository, _, service = _service(_Adapter(author="", cover=COVER), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "Target")
        self.assertEqual(preview.metadata.cover_url.value, COVER)
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "found")

    def test_complete_public_fields_and_batch_never_probe(self):
        probe = _Probe(RenderedMetadataSupplement(author="Wrong", cover_url=COVER))
        _, _, service = _service(_Adapter(author="Existing", cover=COVER), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "Existing")
        self.assertEqual(probe.calls, [])

        _, _, service = _service(_Adapter(fail=True), probe)
        preview = service.preview(
            CollectionPreviewRequest(input_text=TARGET),
            allow_rendered_cover=False,
        )
        self.assertEqual(preview.metadata.author.value, "")
        self.assertEqual(probe.calls, [])

    def test_failed_non_exact_source_never_repackages_into_probe_target(self):
        for source in (
            TARGET.replace("https://", "http://"),
            TARGET + "?p=2",
            TARGET + "/",
        ):
            with self.subTest(source=source):
                probe = _Probe(
                    RenderedMetadataSupplement(author="Wrong", cover_url=COVER)
                )
                _, _, service = _service(_Adapter(fail=True), probe)
                preview = service.preview(CollectionPreviewRequest(input_text=source))
                self.assertEqual(preview.metadata.author.value, "")
                self.assertEqual(preview.metadata.cover_url.value, "")
                self.assertEqual(probe.calls, [])

    def test_unsafe_probe_fields_fail_independently_and_stay_saveable(self):
        probe = _Probe(
            RenderedMetadataSupplement(
                author="https://space.bilibili.com/1",
                cover_url="https://public.example/cover.jpg",
            )
        )
        repository, _, service = _service(_Adapter(fail=True), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "")
        self.assertEqual(preview.metadata.cover_url.value, "")
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "unavailable")
        self.assertTrue(any("作者/封面" in item for item in preview.metadata.warnings))


class BilibiliRenderedMetadataProbeTests(unittest.TestCase):
    def test_timeout_cannot_exceed_ten_seconds(self):
        with self.assertRaises(ValueError):
            PlaywrightRenderedMetadataProbe(timeout_seconds=10.1)


if __name__ == "__main__":
    unittest.main()
