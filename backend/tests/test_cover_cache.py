from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.cover_cache import (
    CoverCacheService,
    CoverUnavailable,
    validate_image,
)
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)
from backend.app.services.safe_http import (
    SafeFetchResult,
    SafeHttpError,
    SafeImageFetchResult,
)


COVER_URL = "https://cdn.example/cover.png"
CACHE_MARKER = ".collection-cover-cache-v1"


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + checksum.to_bytes(4, "big")
    )


def _png(
    width: int = 1,
    height: int = 1,
    *,
    color: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """Build a small, decoder-valid 8-bit RGB PNG without fixture files."""
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes((8, 2, 0, 0, 0))
    )
    row = b"\x00" + bytes(color) * width
    pixels = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(pixels))
        + _png_chunk(b"IEND", b"")
    )


def _jpeg(width: int = 1, height: int = 1) -> bytes:
    """Generate a small, fully decoder-valid baseline JPEG in memory."""
    output = BytesIO()
    Image.new("RGB", (width, height), (17, 34, 51)).save(
        output,
        format="JPEG",
        quality=75,
        progressive=False,
        optimize=False,
    )
    return output.getvalue()


def _webp(width: int = 1, height: int = 1) -> bytes:
    """Generate a small, fully decoder-valid lossless WebP in memory."""
    output = BytesIO()
    Image.new("RGB", (width, height), (68, 85, 102)).save(
        output,
        format="WEBP",
        lossless=True,
        method=0,
    )
    return output.getvalue()


def _animated_png() -> bytes:
    output = BytesIO()
    first = Image.new("RGB", (2, 2), (255, 0, 0))
    second = Image.new("RGB", (2, 2), (0, 0, 255))
    first.save(
        output,
        format="PNG",
        save_all=True,
        append_images=[second],
        duration=10,
        loop=0,
    )
    return output.getvalue()


def _png_with_corrupt_idat() -> bytes:
    body = bytearray(_png(2, 2, color=(5, 6, 7)))
    position = 8
    while position + 12 <= len(body):
        chunk_size = int.from_bytes(body[position:position + 4], "big")
        chunk_type = bytes(body[position + 4:position + 8])
        data_start = position + 8
        data_end = data_start + chunk_size
        crc_end = data_end + 4
        if chunk_type == b"IDAT" and chunk_size:
            body[data_start] ^= 0xFF
            checksum = binascii.crc32(chunk_type + body[data_start:data_end]) & 0xFFFFFFFF
            body[data_end:crc_end] = checksum.to_bytes(4, "big")
            return bytes(body)
        position = crc_end
    raise AssertionError("generated PNG did not contain IDAT")


def _jpeg_with_corrupt_entropy() -> bytes:
    body = _jpeg(2, 2)
    marker = body.find(b"\xff\xda")
    if marker < 0 or marker + 4 > len(body):
        raise AssertionError("generated JPEG did not contain SOS")
    segment_length = int.from_bytes(body[marker + 2:marker + 4], "big")
    scan_start = marker + 2 + segment_length
    # An unescaped SOF marker is not legal entropy-coded scan data. The shallow
    # header parser can still establish dimensions, while full decode must fail.
    return body[:scan_start] + b"\xff\xc0\x00\x02\xff\xd9"


class _MutableClock:
    def __init__(self, value: float = 10_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _colliding_cover_urls() -> tuple[str, str]:
    first_url = "https://cdn.example/stripe-0.png"
    prefix = hashlib.sha256(first_url.encode("utf-8")).hexdigest()[:3]
    for index in range(1, 100_000):
        candidate = f"https://cdn.example/stripe-{index}.png"
        if hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:3] == prefix:
            return first_url, candidate
    raise AssertionError("failed to construct a deterministic stripe collision")


class _SequenceImageFetcher:
    def __init__(
        self,
        *steps: tuple[bytes, str] | BaseException,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.steps = list(steps)
        self.calls: list[str] = []
        self.started = started
        self.release = release
        self._lock = threading.Lock()

    def fetch(self, url: str) -> SafeImageFetchResult:
        with self._lock:
            index = len(self.calls)
            self.calls.append(url)
            if index >= len(self.steps):
                raise AssertionError("unexpected cover fetch")
            step = self.steps[index]
        if self.started is not None:
            self.started.set()
        if self.release is not None and not self.release.wait(timeout=5):
            raise SafeHttpError("FETCH_TIMEOUT")
        if isinstance(step, BaseException):
            raise step
        body, media_type = step
        return SafeImageFetchResult(
            final_url=url,
            media_type=media_type,
            body=body,
            redirects=(),
            content_type=media_type,
        )


class _AllowingValidator:
    def resolve(self, _url: str, *, allowed_hosts=None):
        return object()


class _MetadataFetcher:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []
        self.url_validator = _AllowingValidator()

    def fetch(self, url: str) -> SafeFetchResult:
        self.calls.append(url)
        return SafeFetchResult(
            original_url=url,
            final_url=url,
            status_code=200,
            media_type="text/html",
            body=self.html.encode("utf-8"),
            redirects=(),
            content_type="text/html; charset=utf-8",
        )


def _preview_payload(
    preview_id: str,
    *,
    cover_url: str = COVER_URL,
    source_url: str = "https://public.example/article",
    expires_at: str = "2099-08-29T00:00:00+00:00",
) -> dict:
    cover_source = "open_graph" if cover_url else "none"
    cover_fetched_at = "2026-08-29T00:00:00+00:00" if cover_url else ""
    return {
        "preview_id": preview_id,
        "original_input": source_url,
        "source_url": source_url,
        "canonical_url": source_url,
        "identity_url": source_url,
        "source_kind": "webpage",
        "platform": "web",
        "metadata_status": "generic",
        "metadata": {
            "title": {
                "value": "公开文章标题",
                "source": "open_graph",
                "fetched_at": "2026-08-29T00:00:00+00:00",
            },
            "author": {"value": "", "source": "none", "fetched_at": ""},
            "cover_url": {
                "value": cover_url,
                "source": cover_source,
                "fetched_at": cover_fetched_at,
            },
            "source_copy": {
                "value": "公开页面说明",
                "source": "page_description",
                "fetched_at": "2026-08-29T00:00:00+00:00",
            },
            "platform_tags": [],
            "warnings": [],
        },
        "organization_suggestion": {
            "primary_category": "阅读",
            "secondary_category": "文章",
            "tags": ["待整理"],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "generated",
        },
        "created_at": "2026-08-29T00:00:00+00:00",
        "expires_at": expires_at,
    }


class CoverImageValidationTests(unittest.TestCase):
    def test_accepts_png_jpeg_and_webp_and_reports_dimensions(self):
        cases = (
            (_png(2, 3), "image/png", (2, 3)),
            (_jpeg(13, 7), "image/jpeg", (13, 7)),
            (_webp(5, 4), "image/webp", (5, 4)),
        )
        for body, media_type, expected in cases:
            with self.subTest(media_type=media_type):
                self.assertEqual(
                    validate_image(
                        body,
                        media_type,
                        max_bytes=1024 * 1024,
                        max_dimension=1024,
                        max_pixels=1024 * 1024,
                    ),
                    expected,
                )

    def test_rejects_mime_magic_mismatch_truncation_decoder_corruption_and_animation(self):
        cases = (
            (_png(), "image/jpeg"),
            (_jpeg(), "image/png"),
            (_webp(), "image/gif"),
            (_png()[:-1], "image/png"),
            (_jpeg()[:-2], "image/jpeg"),
            (_webp()[:-1], "image/webp"),
            (_png_with_corrupt_idat(), "image/png"),
            (_jpeg_with_corrupt_entropy(), "image/jpeg"),
            (_animated_png(), "image/png"),
        )
        for body, media_type in cases:
            with self.subTest(media_type=media_type, size=len(body)):
                with self.assertRaises(CoverUnavailable):
                    validate_image(
                        body,
                        media_type,
                        max_bytes=1024 * 1024,
                        max_dimension=1024,
                        max_pixels=1024 * 1024,
                    )

    def test_enforces_encoded_size_dimension_and_pixel_limits(self):
        body = _png(4, 3)
        with self.assertRaises(CoverUnavailable):
            validate_image(
                body,
                "image/png",
                max_bytes=len(body) - 1,
                max_dimension=100,
                max_pixels=10_000,
            )
        with self.assertRaises(CoverUnavailable):
            validate_image(
                _png(101, 1),
                "image/png",
                max_bytes=1024 * 1024,
                max_dimension=100,
                max_pixels=10_000,
            )
        with self.assertRaises(CoverUnavailable):
            validate_image(
                _png(20, 20),
                "image/png",
                max_bytes=1024 * 1024,
                max_dimension=100,
                max_pixels=399,
            )


class CoverCacheServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fresh_asset_is_reused_after_service_restart_without_fetch(self):
        root = self.base / "restart-cache"
        clock = _MutableClock()
        body = _png(2, 2, color=(1, 2, 3))
        first_fetcher = _SequenceImageFetcher((body, "image/png"))
        first_service = CoverCacheService(
            root, fetcher=first_fetcher, fresh_seconds=60, clock=clock
        )

        first = first_service.get(COVER_URL)
        self.assertEqual(first.cache_status, "MISS")
        self.assertEqual(first.body, body)
        self.assertEqual(first_fetcher.calls, [COVER_URL])

        second_fetcher = _SequenceImageFetcher(SafeHttpError("FETCH_FAILED"))
        reopened = CoverCacheService(
            root, fetcher=second_fetcher, fresh_seconds=60, clock=clock
        )
        second = reopened.get(COVER_URL)

        self.assertEqual(second.cache_status, "HIT")
        self.assertEqual(second.body, body)
        self.assertEqual(second.content_sha256, first.content_sha256)
        self.assertEqual(second_fetcher.calls, [])

    def test_cached_only_read_never_fetches_cold_or_stale_entries(self):
        root = self.base / "cached-only"
        clock = _MutableClock()
        body = _png(color=(14, 28, 42))
        refreshed_body = _png(color=(84, 56, 28))
        fetcher = _SequenceImageFetcher(
            (body, "image/png"),
            (refreshed_body, "image/png"),
        )
        service = CoverCacheService(
            root, fetcher=fetcher, fresh_seconds=10, clock=clock
        )

        with self.assertRaises(CoverUnavailable):
            service.get_cached(COVER_URL)
        self.assertEqual(fetcher.calls, [])

        seeded = service.get(COVER_URL)
        self.assertEqual(seeded.cache_status, "MISS")
        self.assertEqual(fetcher.calls, [COVER_URL])
        clock.advance(11)

        stale = service.get_cached(COVER_URL)
        self.assertEqual(stale.cache_status, "STALE")
        self.assertEqual(stale.body, body)
        self.assertEqual(fetcher.calls, [COVER_URL])

    def test_mark_stale_forces_one_refresh_then_returns_fresh_hit(self):
        root = self.base / "mark-stale-cache"
        first_body = _png(color=(1, 2, 3))
        refreshed_body = _png(color=(9, 8, 7))
        fetcher = _SequenceImageFetcher(
            (first_body, "image/png"),
            (refreshed_body, "image/png"),
        )
        service = CoverCacheService(root, fetcher=fetcher)

        self.assertEqual(service.get(COVER_URL).cache_status, "MISS")
        service.mark_stale(COVER_URL)
        refreshed = service.get(COVER_URL)
        hit = service.get(COVER_URL)

        self.assertEqual(refreshed.cache_status, "REFRESH")
        self.assertEqual(refreshed.body, refreshed_body)
        self.assertEqual(hit.cache_status, "HIT")
        self.assertEqual(hit.body, refreshed_body)
        self.assertEqual(fetcher.calls, [COVER_URL, COVER_URL])
        key = service.cache_key(COVER_URL)
        stale_marker = json.loads(
            (root / f"{key}.stale").read_text(encoding="ascii")
        )
        manifest = json.loads((root / f"{key}.json").read_text(encoding="ascii"))
        self.assertEqual(manifest["stale_token"], stale_marker["token"])

    def test_newer_stale_generation_during_refresh_is_not_lost(self):
        root = self.base / "stale-generation-race-cache"
        first_body = _png(color=(1, 1, 2))
        second_body = _png(color=(2, 2, 3))
        third_body = _png(color=(3, 3, 4))
        seed_service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher((first_body, "image/png")),
        )
        seed_service.get(COVER_URL)
        seed_service.mark_stale(COVER_URL)
        key = seed_service.cache_key(COVER_URL)
        first_token = json.loads(
            (root / f"{key}.stale").read_text(encoding="ascii")
        )["token"]

        started = threading.Event()
        release = threading.Event()
        refresh_fetcher = _SequenceImageFetcher(
            (second_body, "image/png"),
            (third_body, "image/png"),
            started=started,
            release=release,
        )
        refresh_service = CoverCacheService(root, fetcher=refresh_fetcher)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(refresh_service.get, COVER_URL)
            self.assertTrue(started.wait(timeout=2), "refresh did not start")
            seed_service.mark_stale(COVER_URL)
            second_token = json.loads(
                (root / f"{key}.stale").read_text(encoding="ascii")
            )["token"]
            self.assertNotEqual(second_token, first_token)
            release.set()
            first_refresh = future.result(timeout=5)

        manifest_after_first = json.loads(
            (root / f"{key}.json").read_text(encoding="ascii")
        )
        self.assertEqual(first_refresh.cache_status, "REFRESH")
        self.assertEqual(first_refresh.body, second_body)
        self.assertEqual(manifest_after_first["stale_token"], first_token)
        self.assertEqual(
            json.loads((root / f"{key}.stale").read_text(encoding="ascii"))["token"],
            second_token,
        )

        second_refresh = refresh_service.get(COVER_URL)
        hit = refresh_service.get(COVER_URL)

        self.assertEqual(second_refresh.cache_status, "REFRESH")
        self.assertEqual(second_refresh.body, third_body)
        self.assertEqual(hit.cache_status, "HIT")
        self.assertEqual(hit.body, third_body)
        self.assertEqual(refresh_fetcher.calls, [COVER_URL, COVER_URL])
        final_manifest = json.loads(
            (root / f"{key}.json").read_text(encoding="ascii")
        )
        self.assertEqual(final_manifest["stale_token"], second_token)

    def test_expired_refresh_failure_serves_last_known_good(self):
        root = self.base / "stale-if-error-cache"
        clock = _MutableClock()
        body = _png(color=(4, 5, 6))
        fetcher = _SequenceImageFetcher(
            (body, "image/png"), SafeHttpError("FETCH_FAILED")
        )
        service = CoverCacheService(
            root,
            fetcher=fetcher,
            fresh_seconds=10,
            clock=clock,
        )
        service.get(COVER_URL)
        clock.advance(11)

        stale = service.get(COVER_URL)

        self.assertEqual(stale.cache_status, "STALE")
        self.assertEqual(stale.body, body)
        self.assertEqual(fetcher.calls, [COVER_URL, COVER_URL])

    def test_cold_fetch_or_validation_failure_never_publishes_an_asset(self):
        cases = (
            (SafeHttpError("FETCH_FAILED"), "fetch"),
            ((b"not a png", "image/png"), "invalid-image"),
            ((_png(), "image/svg+xml"), "unsupported-media"),
        )
        for step, name in cases:
            with self.subTest(name=name):
                root = self.base / name
                fetcher = _SequenceImageFetcher(step)
                service = CoverCacheService(root, fetcher=fetcher)
                with self.assertRaises(CoverUnavailable) as caught:
                    service.get(COVER_URL)
                self.assertEqual(str(caught.exception), "")
                self.assertEqual(fetcher.calls, [COVER_URL])
                self.assertEqual(list(root.glob("*.json")), [])
                self.assertEqual(list(root.glob("*.img")), [])

    def test_cold_failure_is_backed_off_then_retried_after_the_bounded_window(self):
        root = self.base / "failure-backoff-cache"
        clock = _MutableClock()
        body = _png(color=(6, 5, 4))
        fetcher = _SequenceImageFetcher(
            SafeHttpError("FETCH_FAILED"), (body, "image/png")
        )
        service = CoverCacheService(
            root,
            fetcher=fetcher,
            failure_retry_seconds=60,
            clock=clock,
        )

        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        clock.advance(59)
        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        self.assertEqual(fetcher.calls, [COVER_URL])

        clock.advance(2)
        recovered = service.get(COVER_URL)

        self.assertEqual(recovered.cache_status, "MISS")
        self.assertEqual(recovered.body, body)
        self.assertEqual(fetcher.calls, [COVER_URL, COVER_URL])

    def test_retention_never_short_circuits_an_active_failure_retry(self):
        root = self.base / "failure-retention-cache"
        clock = _MutableClock(time.time())
        body = _png(color=(9, 7, 5))
        fetcher = _SequenceImageFetcher(
            SafeHttpError("FETCH_FAILED"), (body, "image/png")
        )
        service = CoverCacheService(
            root,
            fetcher=fetcher,
            retention_seconds=5,
            failure_retry_seconds=60,
            max_cache_bytes=1,
            lease_seconds=1,
            clock=clock,
        )

        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        failure = root / f"{service.cache_key(COVER_URL)}.failure"
        self.assertTrue(failure.is_file())

        clock.advance(6)
        service.cleanup()
        self.assertTrue(failure.is_file())
        self.assertFalse(service._cleanup_scan_active)
        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        self.assertEqual(fetcher.calls, [COVER_URL])

        clock.advance(55)
        recovered = service.get(COVER_URL)
        self.assertEqual(recovered.body, body)
        self.assertEqual(fetcher.calls, [COVER_URL, COVER_URL])

    def test_mark_stale_clears_cold_failure_backoff_for_explicit_retry(self):
        root = self.base / "explicit-retry-cache"
        clock = _MutableClock()
        body = _png(color=(3, 2, 1))
        fetcher = _SequenceImageFetcher(
            SafeHttpError("FETCH_FAILED"), (body, "image/png")
        )
        service = CoverCacheService(
            root,
            fetcher=fetcher,
            failure_retry_seconds=60,
            clock=clock,
        )
        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        self.assertEqual(fetcher.calls, [COVER_URL])

        service.mark_stale(COVER_URL)
        recovered = service.get(COVER_URL)

        self.assertEqual(recovered.cache_status, "MISS")
        self.assertEqual(recovered.body, body)
        self.assertEqual(fetcher.calls, [COVER_URL, COVER_URL])

    def test_same_process_concurrency_across_instances_performs_one_fetch(self):
        root = self.base / "singleflight-cache"
        body = _png(3, 2, color=(7, 8, 9))
        started = threading.Event()
        release = threading.Event()
        fetcher = _SequenceImageFetcher(
            (body, "image/png"), started=started, release=release
        )
        services = (
            CoverCacheService(root, fetcher=fetcher),
            CoverCacheService(root, fetcher=fetcher),
        )

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(services[index % len(services)].get, COVER_URL)
                for index in range(8)
            ]
            self.assertTrue(started.wait(timeout=2), "fetch did not start")
            release.set()
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(fetcher.calls, [COVER_URL])
        self.assertEqual(
            [result.cache_status for result in results].count("MISS"), 1
        )
        self.assertTrue(
            all(result.cache_status in {"MISS", "HIT"} for result in results)
        )
        self.assertTrue(all(result.body == body for result in results))

    def test_overlapping_cold_waiter_rechecks_failure_backoff_after_lock(self):
        root = self.base / "cold-failure-waiter-cache"
        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_entered_lock_wait = threading.Event()
        owner_fetcher = _SequenceImageFetcher(
            SafeHttpError("FETCH_FAILED"),
            started=owner_started,
            release=release_owner,
        )
        waiter_fetcher = _SequenceImageFetcher((_png(), "image/png"))

        class _SignalingWaiterService(CoverCacheService):
            def _acquire_process_lock(self, key: str, *, wait_seconds: float):
                if wait_seconds > 0:
                    waiter_entered_lock_wait.set()
                return super()._acquire_process_lock(
                    key, wait_seconds=wait_seconds
                )

        owner = CoverCacheService(
            root,
            fetcher=owner_fetcher,
            wait_seconds=0.1,
            lease_seconds=1,
        )
        waiter = _SignalingWaiterService(
            root,
            fetcher=waiter_fetcher,
            wait_seconds=0.1,
            lease_seconds=1,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            owner_future = pool.submit(owner.get, COVER_URL)
            self.assertTrue(owner_started.wait(timeout=2), "owner fetch did not start")
            waiter_future = pool.submit(waiter.get, COVER_URL)
            self.assertTrue(
                waiter_entered_lock_wait.wait(timeout=2),
                "cold waiter did not enter the cross-instance lock wait",
            )
            release_owner.set()

            with self.assertRaises(CoverUnavailable):
                owner_future.result(timeout=5)
            with self.assertRaises(CoverUnavailable):
                waiter_future.result(timeout=5)

        self.assertEqual(owner_fetcher.calls, [COVER_URL])
        self.assertEqual(waiter_fetcher.calls, [])
        self.assertEqual(
            len(owner_fetcher.calls) + len(waiter_fetcher.calls),
            1,
        )
        failure_path = root / f"{owner.cache_key(COVER_URL)}.failure"
        self.assertTrue(failure_path.is_file())
        self.assertEqual(
            set(json.loads(failure_path.read_text(encoding="ascii"))),
            {"version", "retry_after", "stale_token"},
        )

    def test_different_cold_keys_sharing_a_lock_stripe_both_complete(self):
        root = self.base / "stripe-collision-cache"
        first_url, second_url = _colliding_cover_urls()

        started = threading.Event()
        release = threading.Event()
        waiter_entered_lock_wait = threading.Event()
        owner_entered_commit = threading.Event()
        release_commit = threading.Event()

        class _GatedCommitService(CoverCacheService):
            def _commit(self, key: str, *args, **kwargs):
                owner_entered_commit.set()
                if not release_commit.wait(timeout=5):
                    raise AssertionError("stripe owner commit was not released")
                return super()._commit(key, *args, **kwargs)

        class _SignalingWaiterService(CoverCacheService):
            def _acquire_process_lock(self, key: str, *, wait_seconds: float):
                if wait_seconds > 0:
                    waiter_entered_lock_wait.set()
                return super()._acquire_process_lock(key, wait_seconds=wait_seconds)

        first_body = _png(color=(1, 3, 5))
        second_body = _png(color=(2, 4, 6))
        first_fetcher = _SequenceImageFetcher(
            (first_body, "image/png"), started=started, release=release
        )
        first_service = _GatedCommitService(root, fetcher=first_fetcher)
        second_fetcher = _SequenceImageFetcher((second_body, "image/png"))
        # The success case uses the production bounded lease window. Fetch
        # release alone cannot promise that validation, fsync, commit and
        # cleanup have finished within the old 0.5-second fixture window.
        second_service = _SignalingWaiterService(root, fetcher=second_fetcher)

        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first_future = pool.submit(first_service.get, first_url)
                self.assertTrue(started.wait(timeout=5), "first stripe owner did not start")
                second_future = pool.submit(second_service.get, second_url)
                self.assertTrue(
                    waiter_entered_lock_wait.wait(timeout=5),
                    "colliding cold key did not enter the real lock wait",
                )
                self.assertFalse(second_future.done())
                release.set()
                self.assertTrue(
                    owner_entered_commit.wait(timeout=5),
                    "first stripe owner did not enter commit",
                )
                self.assertFalse(first_future.done())
                self.assertFalse(second_future.done())
                self.assertEqual(second_fetcher.calls, [])
                release_commit.set()
                first = first_future.result(timeout=10)
                second = second_future.result(timeout=10)
            finally:
                release.set()
                release_commit.set()

        self.assertEqual(first.body, first_body)
        self.assertEqual(second.body, second_body)
        self.assertEqual(first_fetcher.calls, [first_url])
        self.assertEqual(second_fetcher.calls, [second_url])
        self.assertEqual(list(root.glob("*.part")), [])

    def test_colliding_cold_key_wait_exhaustion_is_safe_and_can_recover(self):
        root = self.base / "stripe-wait-exhaustion-cache"
        first_url, second_url = _colliding_cover_urls()
        owner_entered_commit = threading.Event()
        release_commit = threading.Event()
        waiter_entered_lock_wait = threading.Event()

        class _GatedCommitService(CoverCacheService):
            def _commit(self, key: str, *args, **kwargs):
                owner_entered_commit.set()
                if not release_commit.wait(timeout=5):
                    raise AssertionError("stripe owner commit was not released")
                return super()._commit(key, *args, **kwargs)

        class _SignalingWaiterService(CoverCacheService):
            def _acquire_process_lock(self, key: str, *, wait_seconds: float):
                if wait_seconds > 0:
                    waiter_entered_lock_wait.set()
                return super()._acquire_process_lock(key, wait_seconds=wait_seconds)

        first_body = _png(color=(1, 3, 5))
        second_body = _png(color=(2, 4, 6))
        first_fetcher = _SequenceImageFetcher((first_body, "image/png"))
        first_service = _GatedCommitService(root, fetcher=first_fetcher)
        second_fetcher = _SequenceImageFetcher((second_body, "image/png"))
        second_service = _SignalingWaiterService(
            root,
            fetcher=second_fetcher,
            wait_seconds=0.05,
            lease_seconds=0.1,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first_future = pool.submit(first_service.get, first_url)
                self.assertTrue(owner_entered_commit.wait(timeout=5))
                second_future = pool.submit(second_service.get, second_url)
                self.assertTrue(waiter_entered_lock_wait.wait(timeout=5))
                # Keep the actual owner lock held until the bounded waiter
                # returns. No sleep guesses at the exhaustion boundary.
                with self.assertRaises(CoverUnavailable) as caught:
                    second_future.result(timeout=5)
                self.assertEqual(str(caught.exception), "")
                self.assertFalse(first_future.done())
                self.assertEqual(second_fetcher.calls, [])
                second_key = second_service.cache_key(second_url)
                self.assertFalse((root / f"{second_key}.json").exists())
                self.assertEqual(list(root.glob(f"{second_key}.*.img")), [])
                self.assertFalse((root / f"{second_key}.failure").exists())
            finally:
                release_commit.set()
            first = first_future.result(timeout=10)

        recovered = second_service.get(second_url)
        self.assertEqual(first.body, first_body)
        self.assertEqual(recovered.body, second_body)
        self.assertEqual(recovered.cache_status, "MISS")
        self.assertEqual(first_fetcher.calls, [first_url])
        self.assertEqual(second_fetcher.calls, [second_url])
        self.assertEqual(list(root.glob("*.part")), [])

    def test_concurrent_first_initialization_is_create_or_wait_safe(self):
        root = self.base / "concurrent-initialization-cache"
        start = threading.Barrier(8)

        def construct() -> Path:
            start.wait(timeout=5)
            return CoverCacheService(
                root, fetcher=_SequenceImageFetcher()
            ).root

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _index: construct(), range(8)))

        self.assertEqual(results, [root.resolve()] * 8)
        self.assertEqual((root / CACHE_MARKER).read_bytes(), b"collection-cover-cache-v1\n")
        self.assertEqual(
            (root / ".collection-cover-cache-locks-v1").stat().st_size,
            4096,
        )

    def test_non_finite_limits_and_persisted_times_fail_closed(self):
        for name, kwargs in (
            ("fresh-nan", {"fresh_seconds": float("nan")}),
            ("retention-inf", {"retention_seconds": float("inf")}),
            ("wait-nan", {"wait_seconds": float("nan")}),
            ("wait-huge", {"wait_seconds": 1e308}),
            ("lease-huge", {"lease_seconds": 1e308}),
            ("retry-inf", {"failure_retry_seconds": float("inf")}),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    CoverCacheService(
                        self.base / name,
                        fetcher=_SequenceImageFetcher(),
                        **kwargs,
                    )

        root = self.base / "non-finite-manifest-cache"
        first_body = _png(color=(1, 2, 1))
        second_body = _png(color=(2, 1, 2))
        seeded = CoverCacheService(
            root, fetcher=_SequenceImageFetcher((first_body, "image/png"))
        )
        seeded.get(COVER_URL)
        key = seeded.cache_key(COVER_URL)
        manifest_path = root / f"{key}.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        manifest["fresh_until"] = float("nan")
        manifest_path.write_text(json.dumps(manifest), encoding="ascii")

        recovery_fetcher = _SequenceImageFetcher((second_body, "image/png"))
        recovered = CoverCacheService(root, fetcher=recovery_fetcher).get(COVER_URL)
        self.assertEqual(recovered.body, second_body)
        self.assertEqual(recovery_fetcher.calls, [COVER_URL])

        overflow_root = self.base / "finite-addition-overflow-cache"
        overflow_service = CoverCacheService(
            overflow_root,
            fetcher=_SequenceImageFetcher((_png(), "image/png")),
            fresh_seconds=1e308,
            failure_retry_seconds=1e308,
            clock=_MutableClock(1e308),
        )
        with self.assertRaises(CoverUnavailable):
            overflow_service.get(COVER_URL)
        self.assertEqual(list(overflow_root.glob("*.json")), [])
        self.assertEqual(list(overflow_root.glob("*.failure")), [])

    def test_cleanup_reclaims_expired_failure_and_orphan_stale_markers(self):
        root = self.base / "auxiliary-retention-cache"
        clock = _MutableClock(time.time())
        failed_url = "https://cdn.example/failed.png"
        stale_url = "https://cdn.example/orphan-stale.png"
        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(SafeHttpError("FETCH_FAILED")),
            retention_seconds=5,
            lease_seconds=1,
            clock=clock,
        )
        with self.assertRaises(CoverUnavailable):
            service.get(failed_url)
        service.mark_stale(stale_url)
        failure = root / f"{service.cache_key(failed_url)}.failure"
        stale = root / f"{service.cache_key(stale_url)}.stale"
        self.assertTrue(failure.is_file())
        self.assertTrue(stale.is_file())
        old = clock.value - 10
        os.utime(failure, (old, old))
        os.utime(stale, (old, old))
        clock.advance(70)

        # Long-running cold-failure/mark-only traffic must trigger bounded
        # maintenance without a caller invoking cleanup() directly.
        for index in range(128):
            service.mark_stale(f"https://cdn.example/maintenance-{index}.png")

        self.assertFalse(failure.exists())
        self.assertFalse(stale.exists())

    def test_replaced_lock_table_identity_fails_closed_without_fetch(self):
        root = self.base / "lock-identity-cache"
        fetcher = _SequenceImageFetcher((_png(), "image/png"))
        service = CoverCacheService(root, fetcher=fetcher)
        replacement = root / "replacement-lock-table"
        replacement.write_bytes(b"\0" * 4096)
        os.replace(replacement, root / ".collection-cover-cache-locks-v1")

        with self.assertRaises(CoverUnavailable):
            service.get(COVER_URL)
        self.assertEqual(fetcher.calls, [])

    def test_corrupt_manifest_or_content_is_never_returned(self):
        body = _png(2, 1, color=(10, 20, 30))
        for corruption in ("manifest", "content"):
            with self.subTest(corruption=corruption):
                root = self.base / f"corrupt-{corruption}"
                seed_fetcher = _SequenceImageFetcher((body, "image/png"))
                service = CoverCacheService(root, fetcher=seed_fetcher)
                service.get(COVER_URL)
                key = service.cache_key(COVER_URL)
                manifest_path = root / f"{key}.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                if corruption == "manifest":
                    manifest_path.write_text("{}", encoding="ascii")
                else:
                    content_path = root / manifest["content_name"]
                    content_path.write_bytes(b"X" * len(body))

                failing = _SequenceImageFetcher(SafeHttpError("FETCH_FAILED"))
                reopened = CoverCacheService(root, fetcher=failing)
                with self.assertRaises(CoverUnavailable):
                    reopened.get(COVER_URL)
                self.assertEqual(failing.calls, [COVER_URL])

    def test_cache_root_requires_service_marker_before_touching_nonempty_directory(self):
        root = self.base / "controlled-cache"
        service = CoverCacheService(root, fetcher=_SequenceImageFetcher())
        marker = root / CACHE_MARKER
        self.assertEqual(marker.read_bytes(), b"collection-cover-cache-v1\n")
        self.assertEqual(service.root, root.resolve())
        reopened = CoverCacheService(root, fetcher=_SequenceImageFetcher())
        self.assertEqual(reopened.root, root.resolve())

        unmarked = self.base / "unmarked"
        unmarked.mkdir()
        unknown = unmarked / "user-file.txt"
        unknown.write_text("preserve me", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing its marker"):
            CoverCacheService(unmarked, fetcher=_SequenceImageFetcher())
        self.assertEqual(unknown.read_text(encoding="utf-8"), "preserve me")

        invalid_marker = self.base / "invalid-marker"
        invalid_marker.mkdir()
        (invalid_marker / CACHE_MARKER).mkdir()
        with self.assertRaisesRegex(ValueError, "marker is invalid"):
            CoverCacheService(invalid_marker, fetcher=_SequenceImageFetcher())

        file_root = self.base / "not-a-directory"
        file_root.write_text("not a cache", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be a directory"):
            CoverCacheService(file_root, fetcher=_SequenceImageFetcher())

    def test_cleanup_applies_retention_without_touching_unknown_files(self):
        root = self.base / "retention-cache"
        clock = _MutableClock(time.time())
        body = _png(color=(2, 4, 6))
        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher((body, "image/png")),
            retention_seconds=5,
            lease_seconds=1,
            clock=clock,
        )
        service.get(COVER_URL)
        manifest_path = next(root.glob("*.json"))
        content_path = next(root.glob("*.img"))
        unknown = root / "do-not-touch.txt"
        unknown.write_bytes(b"user-owned")
        clock.advance(20)
        old = clock.value - 10
        os.utime(content_path, (old, old))
        os.utime(unknown, (old, old))

        service.cleanup()

        self.assertFalse(content_path.exists())
        self.assertFalse(manifest_path.exists())
        self.assertEqual(unknown.read_bytes(), b"user-owned")
        self.assertTrue((root / CACHE_MARKER).is_file())

    def test_cleanup_enforces_content_budget_oldest_first_and_ignores_unknown_size(self):
        root = self.base / "budget-cache"
        clock = _MutableClock(time.time())
        first_url = "https://cdn.example/first.png"
        second_url = "https://cdn.example/second.png"
        first_body = _png(color=(1, 1, 1))
        second_body = _png(color=(2, 2, 2))
        budget = max(len(first_body), len(second_body)) + 1
        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(
                (first_body, "image/png"), (second_body, "image/png")
            ),
            max_cache_bytes=budget,
            retention_seconds=10_000,
            lease_seconds=1,
            clock=clock,
        )
        service.get(first_url)
        first_key = service.cache_key(first_url)
        first_manifest = root / f"{first_key}.json"
        first_content_name = json.loads(
            first_manifest.read_text(encoding="ascii")
        )["content_name"]
        first_content = root / first_content_name
        old = clock.value - 100
        os.utime(first_content, (old, old))
        unknown = root / "large-unknown.bin"
        unknown.write_bytes(b"U" * (budget * 4))
        clock.advance(1)

        second = service.get(second_url)

        self.assertEqual(second.body, second_body)
        self.assertFalse(first_content.exists())
        self.assertFalse(first_manifest.exists())
        self.assertTrue((root / f"{service.cache_key(second_url)}.json").is_file())
        self.assertEqual(unknown.read_bytes(), b"U" * (budget * 4))
        self.assertLessEqual(
            sum(path.stat().st_size for path in root.glob("*.img")), budget
        )

    def test_budget_reclaims_content_and_manifest_as_one_accounted_unit(self):
        root = self.base / "budget-logical-unit-cache"
        clock = _MutableClock(time.time())
        first_url = "https://cdn.example/logical-first.png"
        second_url = "https://cdn.example/logical-second.png"
        seed = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(
                (_png(color=(4, 4, 4)), "image/png"),
                (_png(color=(5, 5, 5)), "image/png"),
            ),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        seed.get(first_url)
        clock.advance(1)
        seed.get(second_url)

        first_key = seed.cache_key(first_url)
        second_key = seed.cache_key(second_url)
        first_manifest = root / f"{first_key}.json"
        second_manifest = root / f"{second_key}.json"
        first_content = root / json.loads(
            first_manifest.read_text(encoding="ascii")
        )["content_name"]
        second_content = root / json.loads(
            second_manifest.read_text(encoding="ascii")
        )["content_name"]
        os.utime(first_content, (clock.value - 100, clock.value - 100))
        retained_budget = (
            second_content.stat().st_size + second_manifest.stat().st_size
        )
        seed.close()

        CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=retained_budget,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        ).close()

        self.assertFalse(first_content.exists())
        self.assertFalse(first_manifest.exists())
        self.assertTrue(second_content.is_file())
        self.assertTrue(second_manifest.is_file())

    def test_retention_reclaim_accounts_for_manifest_seen_before_content(self):
        root = self.base / "retention-scan-order-cache"
        clock = _MutableClock(time.time())
        old_url = "https://cdn.example/retention-old.png"
        retained_url = "https://cdn.example/retention-retained.png"
        seed = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(
                (_png(color=(6, 6, 6)), "image/png"),
                (_png(color=(7, 7, 7)), "image/png"),
            ),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        seed.get(old_url)
        clock.advance(1)
        seed.get(retained_url)

        old_key = seed.cache_key(old_url)
        retained_key = seed.cache_key(retained_url)
        old_manifest = root / f"{old_key}.json"
        retained_manifest = root / f"{retained_key}.json"
        old_content = root / json.loads(
            old_manifest.read_text(encoding="ascii")
        )["content_name"]
        retained_content = root / json.loads(
            retained_manifest.read_text(encoding="ascii")
        )["content_name"]
        retained_budget = (
            retained_content.stat().st_size + retained_manifest.stat().st_size
        )
        seed.close()

        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        old = clock.value - 100
        os.utime(old_content, (old, old))
        service.max_cache_bytes = retained_budget
        service.retention_seconds = 5
        # ``os.scandir`` ordering is unspecified. Force the valid order that
        # previously left the already-deleted old manifest counted, causing
        # the retained logical unit to be evicted as if the cache were large.
        service._cleanup_iterator = (
            SimpleNamespace(name=name)
            for name in (
                old_manifest.name,
                old_content.name,
                retained_content.name,
                retained_manifest.name,
            )
        )

        service.cleanup()
        service.close()

        self.assertFalse(old_content.exists())
        self.assertFalse(old_manifest.exists())
        self.assertTrue(retained_content.is_file())
        self.assertTrue(retained_manifest.is_file())

    def test_retention_reclaim_does_not_precount_unseen_manifest_bytes(self):
        root = self.base / "retention-content-first-cache"
        clock = _MutableClock(time.time())
        old_url = "https://cdn.example/content-first-old.png"
        candidate_url = "https://cdn.example/content-first-candidate.png"
        seed = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(
                (_png(color=(8, 8, 8)), "image/png"),
                (_png(color=(9, 9, 9)), "image/png"),
            ),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        seed.get(old_url)
        clock.advance(1)
        seed.get(candidate_url)

        old_key = seed.cache_key(old_url)
        candidate_key = seed.cache_key(candidate_url)
        old_manifest = root / f"{old_key}.json"
        candidate_manifest = root / f"{candidate_key}.json"
        old_content = root / json.loads(
            old_manifest.read_text(encoding="ascii")
        )["content_name"]
        candidate_content = root / json.loads(
            candidate_manifest.read_text(encoding="ascii")
        )["content_name"]
        candidate_size = (
            candidate_content.stat().st_size + candidate_manifest.stat().st_size
        )
        seed.close()

        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        old = clock.value - 100
        os.utime(old_content, (old, old))
        service.max_cache_bytes = candidate_size - 1
        service.retention_seconds = 5
        # Count the retained content first, then evict an expired logical unit
        # before either manifest is encountered. Deleting the expired manifest
        # must not subtract it from the running total before it was counted.
        service._cleanup_iterator = (
            SimpleNamespace(name=name)
            for name in (
                candidate_content.name,
                old_content.name,
                old_manifest.name,
                candidate_manifest.name,
            )
        )

        service.cleanup()
        service.close()

        self.assertFalse(old_content.exists())
        self.assertFalse(old_manifest.exists())
        self.assertFalse(candidate_content.exists())
        self.assertFalse(candidate_manifest.exists())

    def test_deferred_retention_candidates_converge_across_bounded_cycles(self):
        root = self.base / "bounded-retention-cache"
        clock = _MutableClock(time.time())
        urls = [f"https://cdn.example/retention-{index}.png" for index in range(3)]
        seed = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(
                *((_png(color=(index, index, index)), "image/png") for index in range(3))
            ),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        for url in urls:
            seed.get(url)
            clock.advance(1)
        pairs: list[tuple[Path, Path]] = []
        for index, url in enumerate(urls):
            manifest = root / f"{seed.cache_key(url)}.json"
            content = root / json.loads(
                manifest.read_text(encoding="ascii")
            )["content_name"]
            old = clock.value - 100 - index
            os.utime(content, (old, old))
            pairs.append((content, manifest))
        seed.close()

        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        service.retention_seconds = 5
        with patch(
            "backend.app.services.cover_cache._CLEANUP_CANDIDATE_LIMIT", 2
        ):
            service.cleanup()
            self.assertEqual(
                sum(content.is_file() for content, _manifest in pairs), 1
            )
            self.assertTrue(service._cleanup_scan_active)
            service.cleanup()
        service.close()

        self.assertTrue(
            all(
                not content.exists() and not manifest.exists()
                for content, manifest in pairs
            )
        )
        self.assertFalse(service._cleanup_scan_active)

    def test_cleanup_budget_converges_across_more_than_ten_thousand_managed_files(self):
        root = self.base / "large-managed-cache"
        budget = 256
        clock = _MutableClock()
        service = CoverCacheService(
            root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=budget,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=clock,
        )
        failure_payload = json.dumps(
            {
                "version": 1,
                "retry_after": clock.value - 60,
                "stale_token": "",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        # Every failure record is valid, outside its retry window, and larger
        # than the entire variable-file budget. Therefore no unseen tail file
        # can remain after a correct full-root convergence pass.
        failure_payload = failure_payload.ljust(budget + 64, b" ")
        managed_count = 10_032
        last_managed = root / f"{managed_count - 1:064x}.failure"
        for index in range(managed_count):
            failure = root / f"{index:064x}.failure"
            failure.write_bytes(failure_payload)
            os.utime(failure, (clock.value - 120, clock.value - 120))

        unknown = root / "user-owned.bin"
        unknown_body = b"preserve unknown bytes" * 32
        unknown.write_bytes(unknown_body)
        reparse = root / f"{'f' * 64}.failure"
        reparse_target = self.base / "outside-cache-target.bin"
        reparse_target.write_bytes(b"preserve reparse target")
        reparse_created = False
        try:
            reparse.symlink_to(reparse_target)
            reparse_created = True
        except (NotImplementedError, OSError):
            pass

        managed_name = re.compile(
            r"^(?:"
            r"[0-9a-f]{64}\.[0-9a-f]{64}\.img|"
            r"[0-9a-f]{64}\.(?:json|stale|failure)|"
            r"[0-9a-f]{64}\.[0-9a-f]{32}\."
            r"(?:content|manifest|stale|failure)\.part"
            r")$"
        )

        def variable_managed_bytes() -> int:
            return sum(
                path.stat(follow_symlinks=False).st_size
                for path in root.iterdir()
                if managed_name.fullmatch(path.name)
                and not path.is_symlink()
                and path.is_file()
            )

        self.assertGreater(managed_count, 10_000)
        self.assertGreater(variable_managed_bytes(), budget)
        self.assertGreater(
            (root / CACHE_MARKER).stat().st_size
            + (root / ".collection-cover-cache-locks-v1").stat().st_size,
            budget,
        )

        # Cleanup is intentionally quantum-bounded. A finite number of
        # maintenance calls must nevertheless advance the retained directory
        # scan through the complete root and converge on the global budget.
        for _ in range(8):
            service.cleanup()

        self.assertLessEqual(variable_managed_bytes(), budget)
        self.assertFalse(last_managed.exists())
        self.assertEqual(unknown.read_bytes(), unknown_body)
        if reparse_created:
            self.assertTrue(reparse.is_symlink())
            self.assertEqual(reparse_target.read_bytes(), b"preserve reparse target")

        # A current stale generation is coupled to its last-known-good asset.
        # Capacity cleanup must first evict content + manifest as one logical
        # unit, then reclaim the orphan stale marker on a later cycle.
        coupled_root = self.base / "capacity-stale-coupling-cache"
        coupled_clock = _MutableClock()
        budget_service = CoverCacheService(
            coupled_root,
            fetcher=_SequenceImageFetcher(),
            max_cache_bytes=1,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=coupled_clock,
        )
        seed = CoverCacheService(
            coupled_root,
            fetcher=_SequenceImageFetcher((_png(color=(8, 6, 4)), "image/png")),
            max_cache_bytes=1024 * 1024,
            retention_seconds=1_000_000,
            lease_seconds=1,
            clock=coupled_clock,
        )
        seed.get(COVER_URL)
        seed.mark_stale(COVER_URL)
        key = seed.cache_key(COVER_URL)
        manifest = coupled_root / f"{key}.json"
        content_name = json.loads(manifest.read_text(encoding="ascii"))["content_name"]
        content = coupled_root / content_name
        stale = coupled_root / f"{key}.stale"

        # Model slow initialization/writes explicitly, then align cleanup's
        # business clock with those controlled file mtimes. A clock captured
        # before seed construction plus real filesystem mtimes is not a
        # reliable protection-period fixture on a busy runner.
        written_at = coupled_clock.value + 5
        for path in (content, manifest, stale):
            os.utime(path, (written_at, written_at))
        coupled_clock.value = written_at
        budget_service.cleanup()

        self.assertFalse(content.exists())
        self.assertFalse(manifest.exists())
        self.assertTrue(
            stale.is_file(),
            "capacity cleanup deleted a live stale generation in the asset eviction cycle",
        )

        budget_service.cleanup()
        self.assertTrue(stale.is_file(), "young orphan stale marker lost its protection")
        coupled_clock.advance(budget_service.lease_seconds * 2)
        budget_service.cleanup()
        self.assertTrue(stale.is_file(), "stale protection must include its exact boundary")

        coupled_clock.advance(1)
        budget_service.cleanup()

        self.assertFalse(stale.exists())


class CoverApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = SQLiteRepository(self.base / "collections.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_app(
        self,
        cover_fetcher: _SequenceImageFetcher,
        *,
        capture_fetcher: _MetadataFetcher | None = None,
    ):
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        return create_app(
            pipeline,
            upload_root=self.base / "uploads",
            inspiration_temp_root=self.base / "inspiration-recordings",
            capture_fetcher=capture_fetcher,
            cover_fetcher=cover_fetcher,
            cover_cache_root=self.base / "cover-cache",
            cover_cache_fresh_seconds=60,
            cover_cache_retention_seconds=600,
            cover_cache_max_bytes=1024 * 1024,
            cover_max_response_bytes=1024 * 1024,
            cover_cache_lease_seconds=2,
            cover_cache_wait_seconds=0.2,
        )

    @staticmethod
    def _assert_unavailable(response) -> None:
        if response.status_code != 404:
            raise AssertionError(response.text)
        if response.content != b"":
            raise AssertionError("cover failure response disclosed a body")
        if response.headers.get("cache-control") != "no-store":
            raise AssertionError("cover failure response must not be cached")
        if response.headers.get("cross-origin-resource-policy") != "same-origin":
            raise AssertionError("cover failure response must remain same-origin")
        if response.headers.get("x-content-type-options") != "nosniff":
            raise AssertionError("cover failure response must disable sniffing")

    def test_unknown_expired_and_no_cover_identities_return_fixed_404_without_fetch(self):
        self.repo.create_collection_preview(
            _preview_payload(
                "expired-preview",
                source_url="https://public.example/expired",
                expires_at="2000-01-01T00:00:00+00:00",
            )
        )
        self.repo.create_collection_preview(
            _preview_payload(
                "no-cover-preview",
                source_url="https://public.example/no-cover",
                cover_url="",
            )
        )
        fetcher = _SequenceImageFetcher()
        with TestClient(self._create_app(fetcher)) as client:
            responses = (
                client.get(
                    "/api/v1/collection-previews/missing/cover",
                    params={"url": "https://attacker.example/proxy.png"},
                ),
                client.get("/api/v1/collection-previews/expired-preview/cover"),
                client.get("/api/v1/collection-previews/no-cover-preview/cover"),
                client.get("/api/v1/collection-items/missing/cover"),
            )
            for response in responses:
                self._assert_unavailable(response)

            saved = client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": "no-cover-save"},
                json={"preview_id": "no-cover-preview", "user_title": "无封面"},
            )
            self.assertEqual(saved.status_code, 201, saved.text)
            self._assert_unavailable(
                client.get(f"/api/v1/collection-items/{saved.json()['id']}/cover")
            )
        self.assertEqual(fetcher.calls, [])

    def test_preview_and_item_share_cache_with_etag_headers_and_unchanged_json_contract(self):
        preview = _preview_payload("shared-preview")
        self.repo.create_collection_preview(preview)
        body = _png(2, 2, color=(22, 44, 66))
        fetcher = _SequenceImageFetcher((body, "image/png"))
        with TestClient(self._create_app(fetcher)) as client:
            forged = client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": "forged-cover"},
                json={
                    "preview_id": "shared-preview",
                    "user_title": "不得伪造封面",
                    "cover_url": "https://attacker.example/forged.png",
                },
            )
            self.assertEqual(forged.status_code, 422, forged.text)

            saved_response = client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": "shared-cover-save"},
                json={"preview_id": "shared-preview", "user_title": "保留来源事实"},
            )
            self.assertEqual(saved_response.status_code, 201, saved_response.text)
            saved = saved_response.json()
            self.assertEqual(
                set(saved),
                {
                    "id",
                    "source_kind",
                    "platform",
                    "original_input",
                    "source_url",
                    "canonical_url",
                    "identity_url",
                    "metadata_status",
                    "user_title",
                    "user_author",
                    "user_cover_asset_id",
                    "display_title",
                    "metadata",
                    "organization_suggestion",
                    "organization_confirmation",
                    "personal_tags",
                    "selected_source_topic_indices",
                    "inspiration",
                    "deep_analysis_resource_key",
                    "created_at",
                    "revision",
                    "updated_at",
                },
            )
            self.assertIsNone(saved["selected_source_topic_indices"])
            self.assertEqual(saved["metadata"]["cover_url"], preview["metadata"]["cover_url"])

            preview_cover = client.get(
                "/api/v1/collection-previews/shared-preview/cover"
            )
            self.assertEqual(preview_cover.status_code, 200, preview_cover.text)
            self.assertEqual(preview_cover.content, body)
            self.assertEqual(preview_cover.headers["content-type"], "image/png")
            self.assertEqual(preview_cover.headers["cache-control"], "private, no-cache")
            self.assertEqual(
                preview_cover.headers["cross-origin-resource-policy"], "same-origin"
            )
            self.assertEqual(preview_cover.headers["x-content-type-options"], "nosniff")
            self.assertEqual(preview_cover.headers["x-cover-cache"], "MISS")
            etag = preview_cover.headers["etag"]
            self.assertRegex(etag, r'^"sha256-[0-9a-f]{64}"$')

            item_cover = client.get(f"/api/v1/collection-items/{saved['id']}/cover")
            self.assertEqual(item_cover.status_code, 200, item_cover.text)
            self.assertEqual(item_cover.content, body)
            self.assertEqual(item_cover.headers["etag"], etag)
            self.assertEqual(item_cover.headers["x-cover-cache"], "HIT")

            conditional = client.get(
                f"/api/v1/collection-items/{saved['id']}/cover",
                headers={"If-None-Match": f'"other", {etag}'},
            )
            self.assertEqual(conditional.status_code, 304, conditional.text)
            self.assertEqual(conditional.content, b"")
            self.assertEqual(conditional.headers["etag"], etag)
            self.assertEqual(conditional.headers["cache-control"], "private, no-cache")
            self.assertEqual(conditional.headers["x-cover-cache"], "HIT")

            weak_conditional = client.get(
                f"/api/v1/collection-items/{saved['id']}/cover",
                headers={"If-None-Match": f"W/{etag}"},
            )
            self.assertEqual(weak_conditional.status_code, 304, weak_conditional.text)
            self.assertEqual(weak_conditional.content, b"")

            restored = client.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(restored.json(), saved)
            search = client.get("/api/v1/collection-items")
            self.assertEqual(search.status_code, 200, search.text)
            self.assertEqual(
                set(search.json()["items"][0]),
                {
                    "id",
                    "display_title",
                    "platform",
                    "primary_category",
                    "cover_url",
                    "source_author",
                    "user_author",
                    "has_user_cover",
                    "created_at",
                    "updated_at",
                },
            )
            self.assertEqual(search.json()["items"][0]["cover_url"], COVER_URL)
        self.assertEqual(fetcher.calls, [COVER_URL])

    def test_item_cached_only_endpoint_never_fetches_cold_or_marked_stale_cover(self):
        self.repo.create_collection_preview(_preview_payload("cached-only-preview"))
        first_body = _png(color=(10, 20, 30))
        refreshed_body = _png(color=(30, 20, 10))
        fetcher = _SequenceImageFetcher(
            (first_body, "image/png"),
            (refreshed_body, "image/png"),
        )
        app = self._create_app(fetcher)
        with TestClient(app) as client:
            saved_response = client.post(
                "/api/v1/collection-items",
                headers={"Idempotency-Key": "cached-only-save"},
                json={"preview_id": "cached-only-preview", "user_title": "只读封面"},
            )
            self.assertEqual(saved_response.status_code, 201, saved_response.text)
            item_id = saved_response.json()["id"]
            cached_url = f"/api/v1/collection-items/{item_id}/cover?cache=only"

            self._assert_unavailable(client.get(cached_url))
            self.assertEqual(fetcher.calls, [])
            self._assert_unavailable(
                client.get(
                    f"/api/v1/collection-items/{item_id}/cover",
                    params={"cache": "only", "url": "https://attacker.example/cover.png"},
                )
            )
            self.assertEqual(fetcher.calls, [])

            seeded = client.get(f"/api/v1/collection-items/{item_id}/cover")
            self.assertEqual(seeded.status_code, 200, seeded.text)
            self.assertEqual(seeded.content, first_body)
            self.assertEqual(fetcher.calls, [COVER_URL])
            app.state.cover_cache_service.mark_stale(COVER_URL)

            stale = client.get(cached_url)
            self.assertEqual(stale.status_code, 200, stale.text)
            self.assertEqual(stale.content, first_body)
            self.assertEqual(stale.headers["x-cover-cache"], "STALE")
            self.assertEqual(fetcher.calls, [COVER_URL])

    def test_refresh_metadata_invalidates_matching_cover_without_changing_preview_schema(self):
        html = f"""
            <html><head>
              <meta property="og:title" content="刷新测试文章">
              <meta property="og:image" content="{COVER_URL}">
              <meta property="og:description" content="公开页面说明">
            </head></html>
        """
        metadata_fetcher = _MetadataFetcher(html)
        first_body = _png(color=(11, 22, 33))
        refreshed_body = _png(color=(44, 55, 66))
        image_fetcher = _SequenceImageFetcher(
            (first_body, "image/png"),
            (refreshed_body, "image/png"),
        )
        with TestClient(
            self._create_app(image_fetcher, capture_fetcher=metadata_fetcher)
        ) as client:
            first = client.post(
                "/api/v1/collection-previews",
                json={"input_text": "https://public.example/refresh-article"},
            )
            self.assertEqual(first.status_code, 201, first.text)
            first_json = first.json()
            self.assertEqual(
                set(first_json),
                {
                    "preview_id",
                    "original_input",
                    "source_url",
                    "canonical_url",
                    "identity_url",
                    "source_kind",
                    "platform",
                    "metadata_status",
                    "metadata",
                    "organization_suggestion",
                    "created_at",
                    "expires_at",
                },
            )
            self.assertEqual(first_json["metadata"]["cover_url"]["value"], COVER_URL)
            first_cover = client.get(
                f"/api/v1/collection-previews/{first_json['preview_id']}/cover"
            )
            self.assertEqual(first_cover.status_code, 200, first_cover.text)
            self.assertEqual(first_cover.content, first_body)
            self.assertEqual(first_cover.headers["x-cover-cache"], "MISS")

            refreshed = client.post(
                "/api/v1/collection-previews",
                json={
                    "input_text": "https://public.example/refresh-article",
                    "refresh_metadata": True,
                },
            )
            self.assertEqual(refreshed.status_code, 201, refreshed.text)
            refreshed_json = refreshed.json()
            self.assertEqual(set(refreshed_json), set(first_json))
            self.assertEqual(
                refreshed_json["metadata"]["cover_url"],
                first_json["metadata"]["cover_url"] | {
                    "fetched_at": refreshed_json["metadata"]["cover_url"]["fetched_at"]
                },
            )
            refreshed_cover = client.get(
                f"/api/v1/collection-previews/{refreshed_json['preview_id']}/cover"
            )
            self.assertEqual(refreshed_cover.status_code, 200, refreshed_cover.text)
            self.assertEqual(refreshed_cover.content, refreshed_body)
            self.assertEqual(refreshed_cover.headers["x-cover-cache"], "REFRESH")

        self.assertEqual(
            metadata_fetcher.calls,
            [
                "https://public.example/refresh-article",
                "https://public.example/refresh-article",
            ],
        )
        self.assertEqual(image_fetcher.calls, [COVER_URL, COVER_URL])


if __name__ == "__main__":
    unittest.main()
