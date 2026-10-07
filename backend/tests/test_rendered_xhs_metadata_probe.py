from __future__ import annotations

import copy
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.app.services.safe_http import SafeImageFetchResult, SafePublicFetcher

from backend.app.domain.models import CollectionPreviewRequest, SourceMetadata
from backend.app.services.capture import CaptureService
from backend.app.services.rendered_metadata import (
    PlaywrightRenderedMetadataProbe,
    RenderedMetadataSupplement,
    _extract_xiaohongshu_document_metadata,
    _safe_xiaohongshu_cover_candidate,
    _validate_xiaohongshu_target,
    _xiaohongshu_document_url_matches,
    _xiaohongshu_should_block_request,
)
from backend.app.services.safe_http import SafeHttpError


NOTE_ID = "64a01234567890abcdef1234"
TARGET = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=abc%2F1&b=2"
TARGET_SLASH = f"https://www.xiaohongshu.com/explore/{NOTE_ID}/?xsec_token=abc%2F1&b=2"
COVER = "https://sns-webpic-qc.xhscdn.com/target.webp"


def _document(*, author="Target", cover=COVER, og_url=TARGET, title="Public note"):
    values = [f'<title>{title}</title>']
    if author is not None:
        values.append(f'<meta name="author" content="{author}">')
    if cover is not None:
        values.append(f'<meta property="og:image" content="{cover}">')
    if og_url is not None:
        values.append(f'<meta property="og:url" content="{og_url}">')
    return ("<html><head>" + "".join(values) + "</head><body></body></html>").encode()


def _metadata(*, author="", cover=""):
    return SourceMetadata.model_validate({
        "title": {"value": "Public note", "source": "page_metadata", "fetched_at": "now"},
        "author": {
            "value": author,
            "source": "page_metadata" if author else "none",
            "fetched_at": "now" if author else "",
        },
        "cover_url": {
            "value": cover,
            "source": "open_graph" if cover else "none",
            "fetched_at": "now" if cover else "",
        },
        "source_copy": {"value": "", "source": "none", "fetched_at": ""},
        "platform_tags": [],
        "warnings": [],
    }).model_dump(mode="json")


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

    def resolve(self, url, **_kwargs):
        self.calls.append(url)
        if self.reject:
            raise SafeHttpError("UNSAFE_URL")
        return object()


class _Fetcher:
    def __init__(self, reject=False):
        self.url_validator = _Validator(reject)


class _ImageFetcher:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return SafeImageFetchResult(final_url=url, media_type="image/png", body=self.body,
                                    redirects=(), content_type="image/png")


class _Adapter:
    def __init__(self, *, author="", cover="", fail=False, canonical=TARGET):
        self.author = author
        self.cover = cover
        self.fail = fail
        self.canonical = canonical

    def match(self, url):
        return "xiaohongshu.com" in url or "xhslink.com" in url

    def resolve(self, url):
        if self.fail:
            raise ValueError("unavailable")
        return SimpleNamespace(
            platform="xiaohongshu",
            source_kind="webpage",
            canonical_url=self.canonical,
            aliases=(url, self.canonical),
        )

    def get_public_metadata(self, _resource, _fetched_at):
        return _metadata(author=self.author, cover=self.cover)


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


class _Response:
    def __init__(self, *, status=200, url=TARGET, content_type="text/html", body=None, length=None):
        self.status = status
        self.url = url
        self._body = _document() if body is None else body
        self._headers = {"content-type": content_type}
        if length is not None:
            self._headers["content-length"] = str(length)

    def all_headers(self):
        return self._headers

    def body(self):
        return self._body


def _service(adapter, probe, *, reject=False):
    repository = _Repository()
    fetcher = _Fetcher(reject)
    service = CaptureService(
        repository,
        fetcher=fetcher,
        source_adapter_registry=_Registry(adapter),
        rendered_metadata_probe=probe,
    )
    return repository, fetcher, service


class XiaohongshuRenderedMetadataPolicyTests(unittest.TestCase):
    def test_raw_author_controls_are_rejected_before_normalization(self):
        for author in ("A&#9;B", "A&#10;B", "&#9;Author", "Author&#10;", "A&#127;B",
                       "A\x00B", "A&#0;B", "A&#x0;B", "A\ufffdB"):
            with self.subTest(author=author):
                result = _extract_xiaohongshu_document_metadata(_document(author=author), TARGET, NOTE_ID)
                self.assertEqual(result.author, "")
                self.assertEqual(result.cover_url, COVER)
        for author in ("A\tB", "\nAuthor", "Author\r", "A\x7fB"):
            for value in (author, {"@type": "Person", "name": author}):
                script = json.dumps({"@context": "https://schema.org", "@type": "Article",
                                     "url": TARGET, "author": value})
                body = _document(author=None).replace(b"</head>", (
                    '<script type="application/ld+json">' + script + '</script></head>'
                ).encode())
                result = _extract_xiaohongshu_document_metadata(body, TARGET, NOTE_ID)
                self.assertEqual(result.author, "")
                self.assertEqual(result.cover_url, COVER)

    def test_document_route_rejects_same_url_child_and_unknown_frames(self):
        for platform, target, identity in (
            ("xiaohongshu", TARGET, NOTE_ID),
            ("bilibili", "https://www.bilibili.com/video/BV1target", "BV1target"),
        ):
            main_frame = object()
            handler = PlaywrightRenderedMetadataProbe._route_handler(
                platform=platform, target=target, expected_identity=identity,
                main_frame_getter=lambda: main_frame,
            )
            for frame, navigation, allowed in (
                (main_frame, True, True),
                (object(), True, False),
                (None, True, False),
                (main_frame, False, False),
            ):
                route = Mock()
                route.request = SimpleNamespace(
                    frame=frame, resource_type="document", url=target,
                    is_navigation_request=lambda: navigation, headers={},
                )
                handler(route)
                self.assertEqual(route.continue_.call_count, int(allowed))
                self.assertEqual(route.abort.call_count, int(not allowed))

    def test_target_keeps_query_and_rejects_unproven_shapes(self):
        self.assertEqual(_validate_xiaohongshu_target(TARGET, NOTE_ID), TARGET)
        for url, identity in (
            (TARGET.replace("https://", "http://"), NOTE_ID),
            (TARGET + "#fragment", NOTE_ID),
            (TARGET.replace("www.xiaohongshu.com", "xhslink.com"), NOTE_ID),
            (TARGET.replace(NOTE_ID, "65a01234567890abcdef1234"), NOTE_ID),
            (f"https://www.xiaohongshu.com/user/profile/{NOTE_ID}", NOTE_ID),
        ):
            with self.subTest(url=url):
                self.assertIsNone(_validate_xiaohongshu_target(url, identity))

    def test_document_url_requires_same_host_identity_and_query(self):
        self.assertTrue(_xiaohongshu_document_url_matches(TARGET_SLASH, TARGET, NOTE_ID))
        for url in (
            TARGET.replace("www.xiaohongshu.com", "xiaohongshu.com"),
            TARGET.replace("b=2", "b=3"),
            TARGET.replace(NOTE_ID, "65a01234567890abcdef1234"),
        ):
            self.assertFalse(_xiaohongshu_document_url_matches(url, TARGET, NOTE_ID))

    def test_extracts_only_standard_target_head(self):
        expected = RenderedMetadataSupplement(
            author="Target",
            cover_url=COVER,
            author_source="page_metadata",
            cover_source="open_graph",
        )
        self.assertEqual(
            _extract_xiaohongshu_document_metadata(_document(), TARGET, NOTE_ID),
            expected,
        )
        self.assertIsNone(
            _extract_xiaohongshu_document_metadata(
                _document(og_url=TARGET.replace(NOTE_ID, "65a01234567890abcdef1234")),
                TARGET,
                NOTE_ID,
            )
        )
        self.assertIsNone(
            _extract_xiaohongshu_document_metadata(
                _document(title="请登录后查看"), TARGET, NOTE_ID
            )
        )
        body_only = b'<html><head></head><body><meta name="author" content="Wrong"></body></html>'
        self.assertEqual(
            _extract_xiaohongshu_document_metadata(body_only, TARGET, NOTE_ID),
            RenderedMetadataSupplement(
                author_source="page_metadata", cover_source="open_graph"
            ),
        )

    def test_cover_and_route_allowlists_are_exact(self):
        self.assertEqual(_safe_xiaohongshu_cover_candidate(COVER, TARGET), COVER)
        self.assertEqual(
            _safe_xiaohongshu_cover_candidate("/cover.jpg", TARGET),
            "https://www.xiaohongshu.com/cover.jpg",
        )
        for cover in (
            "http://sns-webpic-qc.xhscdn.com/a.jpg",
            "https://sns-webpic-qn.xhscdn.com/a.jpg",
            "https://sns-avatar-qc.xhscdn.com/a.jpg",
            "https://evil.sns-webpic-qc.xhscdn.com/a.jpg",
            "https://sns-webpic-qc.xhscdn.com/a.mp4",
            "/" + "a" * 2047,
        ):
            self.assertIsNone(_safe_xiaohongshu_cover_candidate(cover, TARGET))
        self.assertFalse(_xiaohongshu_should_block_request("document", TARGET, TARGET, NOTE_ID))
        for kind, url in (
            ("script", "https://fe-static.xhscdn.com/app.js"),
            ("xhr", "https://edith.xiaohongshu.com/api/note"),
            ("image", COVER),
            ("document", TARGET.replace("b=2", "b=3")),
        ):
            self.assertTrue(_xiaohongshu_should_block_request(kind, url, TARGET, NOTE_ID))

    def test_document_response_contract_rejects_status_mime_size_and_identity(self):
        read = PlaywrightRenderedMetadataProbe._read_document
        self.assertIsNotNone(read("xiaohongshu", _Response(), TARGET, TARGET, NOTE_ID))
        for response, page_url in (
            (_Response(status=403), TARGET),
            (_Response(content_type="application/json"), TARGET),
            (_Response(length=4 * 1024 * 1024 + 1), TARGET),
            (_Response(body=b"x" * (4 * 1024 * 1024 + 1)), TARGET),
            (_Response(url=TARGET.replace("b=2", "b=3")), TARGET),
            (_Response(), TARGET.replace("b=2", "b=3")),
        ):
            with self.subTest(response=response, page_url=page_url):
                self.assertIsNone(
                    read("xiaohongshu", response, page_url, TARGET, NOTE_ID)
                )


class XiaohongshuRenderedMetadataCaptureTests(unittest.TestCase):
    def test_fresh_batch_or_legacy_cache_without_status_does_not_probe(self):
        probe = _Probe(RenderedMetadataSupplement(author="Target", author_source="page_metadata"))
        _, fetcher, service = _service(_Adapter(), probe)
        first = service.preview(CollectionPreviewRequest(input_text=TARGET), allow_rendered_cover=False)
        second = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(second.metadata, first.metadata)
        self.assertEqual(probe.calls, [])
        self.assertEqual(fetcher.url_validator.calls, [])
        refreshed = service.preview(CollectionPreviewRequest(input_text=TARGET, refresh_metadata=True))
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(refreshed.metadata.author.value, "Target")

    def test_api_save_reopen_author_search_and_failed_bookmark(self):
        for supplement in (
            RenderedMetadataSupplement(author="SupplementCreator", cover_url=COVER,
                                       author_source="page_metadata", cover_source="open_graph"),
            None,
        ):
            with self.subTest(success=supplement is not None), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = SQLiteRepository(root / "test.sqlite3")
                probe = _Probe(supplement)
                output = BytesIO()
                Image.new("RGB", (2, 2), (22, 44, 66)).save(output, format="PNG")
                image_fetcher = _ImageFetcher(output.getvalue())
                http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                    200, text='<head><title>Public note</title></head>',
                    headers={"content-type": "text/html"},
                )))
                fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ["8.8.8.8"])
                app = create_app(
                    LocalFullPipeline(repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()),
                    capture_fetcher=fetcher, rendered_metadata_probe=probe, cover_fetcher=image_fetcher,
                    upload_root=root / "uploads", inspiration_temp_root=root / "audio",
                    cover_cache_root=root / "covers", user_cover_root=root / "user-covers",
                )
                with http, TestClient(app) as client:
                    response = client.post("/api/v1/collection-previews", json={"input_text": TARGET})
                    self.assertEqual(response.status_code, 201, response.text)
                    preview = response.json()
                    if supplement:
                        cover = client.get(f"/api/v1/collection-previews/{preview['preview_id']}/cover")
                        self.assertEqual(cover.status_code, 200, cover.text)
                        self.assertEqual(cover.content, output.getvalue())
                        self.assertEqual(cover.headers["cross-origin-resource-policy"], "same-origin")
                        self.assertEqual(image_fetcher.calls, [COVER])
                    saved = client.post("/api/v1/collection-items",
                        headers={"Idempotency-Key": "xhs-supplement"},
                        json={"preview_id": preview["preview_id"], "user_title": "My note"})
                    self.assertEqual(saved.status_code, 201, saved.text)
                    item = saved.json()
                    cached = client.post("/api/v1/collection-previews", json={"input_text": TARGET})
                    self.assertEqual(cached.json()["metadata"], preview["metadata"])
                    self.assertEqual(len(probe.calls), 1)
                    if supplement:
                        found = client.get("/api/v1/collection-items", params={"query": "SupplementCreator"})
                        self.assertEqual([row["id"] for row in found.json()["items"]], [item["id"]])
                    with repo._connect() as db:
                        for table in ("jobs", "videos", "transcript_segments", "extractions"):
                            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                # New repository and app against the on-disk synthetic database,
                # after the original clients and lifecycle have been closed.
                reopened_repo = SQLiteRepository(root / "test.sqlite3")
                reopened_app = create_app(
                    LocalFullPipeline(reopened_repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()),
                    capture_fetcher=_Fetcher(), rendered_metadata_probe=_Probe(None),
                    cover_fetcher=image_fetcher,
                    upload_root=root / "uploads", inspiration_temp_root=root / "audio",
                    cover_cache_root=root / "covers", user_cover_root=root / "user-covers",
                )
                with TestClient(reopened_app) as client:
                    reopened = client.get(f"/api/v1/collection-items/{item['id']}")
                    self.assertEqual(reopened.status_code, 200, reopened.text)
                    self.assertEqual(reopened.json(), item)
                    if supplement:
                        found = client.get("/api/v1/collection-items", params={"query": "SupplementCreator"})
                        self.assertEqual([row["id"] for row in found.json()["items"]], [item["id"]])
                        cover = client.get(f"/api/v1/collection-items/{item['id']}/cover")
                        self.assertEqual(cover.status_code, 200, cover.text)
                        self.assertEqual(cover.content, output.getvalue())

    def test_missing_fields_are_recovered_with_generic_provenance_and_cached(self):
        supplement = RenderedMetadataSupplement(
            author="Target",
            cover_url=COVER,
            author_source="page_metadata",
            cover_source="open_graph",
        )
        probe = _Probe(supplement)
        repository, fetcher, service = _service(_Adapter(), probe)

        first = service.preview(CollectionPreviewRequest(input_text=TARGET))
        second = service.preview(CollectionPreviewRequest(input_text=TARGET))

        self.assertEqual(first.canonical_url, TARGET)
        self.assertEqual(first.metadata_status, "generic")
        self.assertEqual(first.metadata.author.value, "Target")
        self.assertEqual(first.metadata.author.source, "page_metadata")
        self.assertEqual(first.metadata.cover_url.value, COVER)
        self.assertEqual(first.metadata.cover_url.source, "open_graph")
        self.assertEqual(first.metadata, second.metadata)
        self.assertEqual(probe.calls, [("xiaohongshu", TARGET, NOTE_ID)])
        self.assertEqual(fetcher.url_validator.calls, [COVER])
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "found")

    def test_partial_merge_never_overwrites_existing_values(self):
        probe = _Probe(RenderedMetadataSupplement(
            author="Wrong", cover_url=COVER,
            author_source="page_metadata", cover_source="open_graph",
        ))
        _, _, service = _service(_Adapter(author="Existing", cover=""), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "Existing")
        self.assertEqual(preview.metadata.cover_url.value, COVER)

        probe = _Probe(RenderedMetadataSupplement(
            author="Target", author_source="page_metadata", cover_source="open_graph"
        ))
        repository, _, service = _service(_Adapter(), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "Target")
        self.assertEqual(preview.metadata.cover_url.value, "")
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "partial")

    def test_complete_failed_and_batch_paths_do_not_probe(self):
        probe = _Probe(RenderedMetadataSupplement(author="Wrong", cover_url=COVER))
        _, _, service = _service(_Adapter(author="Existing", cover=COVER), probe)
        service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(probe.calls, [])

        _, _, service = _service(_Adapter(fail=True), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.canonical_url, "")
        self.assertEqual(probe.calls, [])

        _, _, service = _service(_Adapter(), probe)
        service.preview(
            CollectionPreviewRequest(input_text=TARGET), allow_rendered_cover=False
        )
        self.assertEqual(probe.calls, [])

    def test_unsafe_fields_fail_independently_and_remain_saveable(self):
        probe = _Probe(RenderedMetadataSupplement(
            author="https://www.xiaohongshu.com/user/profile/1",
            cover_url="https://sns-avatar-qc.xhscdn.com/avatar.jpg",
            author_source="page_metadata",
            cover_source="open_graph",
        ))
        repository, _, service = _service(_Adapter(), probe)
        preview = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(preview.metadata.author.value, "")
        self.assertEqual(preview.metadata.cover_url.value, "")
        self.assertIn(preview.preview_id, repository.previews)
        self.assertEqual(repository.cache[TARGET]["rendered_metadata_status"], "unavailable")
        self.assertTrue(any("作者/封面" in item for item in preview.metadata.warnings))
        cached = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(cached.metadata, preview.metadata)
        self.assertEqual(len(probe.calls), 1)

    def test_explicit_refresh_retries_after_cached_unavailable(self):
        probe = _Probe(RenderedMetadataSupplement(
            author_source="page_metadata", cover_source="open_graph"
        ))
        _, _, service = _service(_Adapter(), probe)
        service.preview(CollectionPreviewRequest(input_text=TARGET))
        service.preview(CollectionPreviewRequest(input_text=TARGET))
        service.preview(CollectionPreviewRequest(
            input_text=TARGET, refresh_metadata=True
        ))
        self.assertEqual(len(probe.calls), 2)


if __name__ == "__main__":
    unittest.main()
