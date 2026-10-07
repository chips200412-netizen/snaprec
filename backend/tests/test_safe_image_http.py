from __future__ import annotations

import gzip
import logging
import unittest

import httpx

from backend.app.services.safe_http import (
    DEFAULT_MAX_IMAGE_RESPONSE_BYTES,
    SafeHttpError,
    SafePublicImageFetcher,
)


PUBLIC_V4 = "8.8.8.8"


def _client(handler, **kwargs) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)


class _MutableClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _MessageHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class SafePublicImageFetcherTests(unittest.TestCase):
    def test_fetches_declared_image_with_minimal_isolated_headers(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                content=b"image body",
                headers={"content-type": "image/png; profile=test"},
            )

        client = _client(
            handler,
            headers={
                "Authorization": "Bearer must-not-leak",
                "Referer": "https://private.example/page",
                "X-Ambient": "must-not-leak",
            },
            cookies={"session": "must-not-leak"},
        )
        result = SafePublicImageFetcher(
            client=client, dns_resolver=lambda _host: [PUBLIC_V4]
        ).fetch("https://images.example/cover.png")

        self.assertEqual(result.final_url, "https://images.example/cover.png")
        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.content_type, "image/png; profile=test")
        self.assertEqual(result.body, b"image body")
        self.assertEqual(result.redirects, ())
        self.assertEqual(result.url, result.final_url)
        self.assertEqual(result.content, result.body)
        self.assertEqual(
            seen[0].headers["accept"], "image/jpeg,image/png,image/webp"
        )
        self.assertEqual(seen[0].headers["accept-encoding"], "identity")
        for name in ("authorization", "cookie", "referer", "x-ambient"):
            self.assertNotIn(name, seen[0].headers)
        self.assertLessEqual(seen[0].extensions["timeout"]["connect"], 5)
        self.assertLessEqual(seen[0].extensions["timeout"]["read"], 15)

    def test_rejects_initial_private_or_mixed_dns_before_request(self):
        called = False

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(
                200, content=b"image", headers={"content-type": "image/jpeg"}
            )

        for answers in (["127.0.0.1"], [PUBLIC_V4, "10.0.0.1"]):
            with self.subTest(answers=answers):
                fetcher = SafePublicImageFetcher(
                    client=_client(handler), dns_resolver=lambda _host, a=answers: a
                )
                with self.assertRaises(SafeHttpError) as caught:
                    fetcher.fetch("https://images.example/private.jpg")
                self.assertEqual(caught.exception.code, "UNSAFE_ADDRESS")
                self.assertNotIn("images.example", str(caught.exception))
        self.assertFalse(called)

    def test_redirects_are_bounded_and_each_target_is_revalidated(self):
        calls: list[str] = []
        dns_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.host == "first.example":
                return httpx.Response(
                    302,
                    headers={"location": "https://second.example/final.webp"},
                )
            return httpx.Response(
                200, content=b"webp", headers={"content-type": "image/webp"}
            )

        def dns(host: str):
            dns_calls.append(host)
            return [PUBLIC_V4]

        result = SafePublicImageFetcher(
            client=_client(handler), dns_resolver=dns
        ).fetch("https://first.example/start")

        self.assertEqual(
            calls,
            [
                "https://first.example/start",
                "https://second.example/final.webp",
            ],
        )
        self.assertEqual(dns_calls, ["first.example", "second.example"])
        self.assertEqual(
            result.redirects, ("https://second.example/final.webp",)
        )
        self.assertEqual(result.final_url, "https://second.example/final.webp")

        calls.clear()
        answers = {
            "first.example": [PUBLIC_V4],
            "second.example": ["192.168.1.10"],
        }
        blocked = SafePublicImageFetcher(
            client=_client(handler), dns_resolver=lambda host: answers[host]
        )
        with self.assertRaises(SafeHttpError) as caught:
            blocked.fetch("https://first.example/start")
        self.assertEqual(caught.exception.code, "UNSAFE_ADDRESS")
        self.assertEqual(calls, ["https://first.example/start"])

    def test_redirect_allowlist_is_enforced_at_every_hop(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://blocked.example/cover.jpg"},
            )

        fetcher = SafePublicImageFetcher(
            client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch(
                "https://allowed.example/start",
                allowed_hosts={"allowed.example"},
            )
        self.assertEqual(caught.exception.code, "HOST_NOT_ALLOWED")
        self.assertEqual(calls, ["https://allowed.example/start"])

    def test_rejects_more_than_three_redirects(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            index = int(request.url.path.removeprefix("/"))
            return httpx.Response(302, headers={"location": f"/{index + 1}"})

        fetcher = SafePublicImageFetcher(
            client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://images.example/0")
        self.assertEqual(caught.exception.code, "TOO_MANY_REDIRECTS")
        self.assertEqual(len(calls), 4)

    def test_accepts_only_jpeg_png_and_webp_declared_media_types(self):
        for declared in (
            "",
            "image/gif",
            "image/svg+xml",
            "image/avif",
            "application/octet-stream",
            "text/html",
        ):
            with self.subTest(declared=declared):
                fetcher = SafePublicImageFetcher(
                    client=_client(
                        lambda _request, value=declared: httpx.Response(
                            200,
                            content=b"not inspected here",
                            headers={"content-type": value} if value else {},
                        )
                    ),
                    dns_resolver=lambda _host: [PUBLIC_V4],
                )
                with self.assertRaises(SafeHttpError) as caught:
                    fetcher.fetch("https://images.example/cover")
                self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_TYPE")

        for declared in ("image/jpeg", "image/png", "image/webp"):
            with self.subTest(declared=declared):
                result = SafePublicImageFetcher(
                    client=_client(
                        lambda _request, value=declared: httpx.Response(
                            200,
                            content=b"validated later",
                            headers={"content-type": value},
                        )
                    ),
                    dns_resolver=lambda _host: [PUBLIC_V4],
                ).fetch("https://images.example/cover")
                self.assertEqual(result.media_type, declared)

    def test_rejects_declared_streamed_and_decoded_bodies_over_five_mib(self):
        limit = DEFAULT_MAX_IMAGE_RESPONSE_BYTES
        declared = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    content=b"x",
                    headers={
                        "content-type": "image/jpeg",
                        "content-length": str(limit + 1),
                    },
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            declared.fetch("https://images.example/declared.jpg")
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

        class ChunkedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * limit
                yield b"x"

        streamed = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=ChunkedStream(),
                    headers={"content-type": "image/png"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            streamed.fetch("https://images.example/streamed.png")
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

        encoded = gzip.compress(b"x" * (limit + 1))
        decoded = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    content=encoded,
                    headers={
                        "content-type": "image/webp",
                        "content-encoding": "gzip",
                        "content-length": str(len(encoded)),
                    },
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            decoded.fetch("https://images.example/decoded.webp")
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

    def test_total_deadline_covers_stream_eof_and_response_close(self):
        stream_clock = _MutableClock()

        class LastChunkThenLate(httpx.SyncByteStream):
            def __iter__(self):
                yield b"image"
                stream_clock.value = 16

        streamed = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=LastChunkThenLate(),
                    headers={"content-type": "image/jpeg"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
            monotonic_clock=stream_clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            streamed.fetch("https://images.example/late-eof.jpg")
        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")

        close_clock = _MutableClock()

        class CloseLate(httpx.SyncByteStream):
            def __iter__(self):
                yield b"image"

            def close(self):
                close_clock.value = 16

        closing = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=CloseLate(),
                    headers={"content-type": "image/png"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
            monotonic_clock=close_clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            closing.fetch("https://images.example/late-close.png")
        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")

    def test_close_failure_is_stable_and_does_not_mask_primary_rejection(self):
        class CloseFailure(httpx.SyncByteStream):
            def __iter__(self):
                yield b"image"

            def close(self):
                raise OSError("private close detail")

        successful_body = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=CloseFailure(),
                    headers={"content-type": "image/jpeg"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            successful_body.fetch("https://images.example/close.jpg")
        self.assertEqual(caught.exception.code, "FETCH_FAILED")
        self.assertNotIn("private", str(caught.exception))

        rejected_mime = SafePublicImageFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=CloseFailure(),
                    headers={"content-type": "text/html"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            rejected_mime.fetch("https://images.example/not-image")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_TYPE")
        self.assertNotIn("private", str(caught.exception))

    def test_transport_logs_are_suppressed_only_inside_image_fetch_context(self):
        transport_logger = logging.getLogger("httpx")
        unrelated_logger = logging.getLogger("safe_image_http_test.unrelated")
        transport_handler = _MessageHandler()
        unrelated_handler = _MessageHandler()
        old_transport_level = transport_logger.level
        old_unrelated_level = unrelated_logger.level
        old_unrelated_propagate = unrelated_logger.propagate
        transport_logger.setLevel(logging.INFO)
        unrelated_logger.setLevel(logging.INFO)
        unrelated_logger.propagate = False
        transport_logger.addHandler(transport_handler)
        unrelated_logger.addHandler(unrelated_handler)
        try:
            def handler(_request: httpx.Request) -> httpx.Response:
                transport_logger.info(
                    "must-not-log https://images.example/private-token"
                )
                unrelated_logger.info("unrelated-kept")
                return httpx.Response(
                    200,
                    content=b"image",
                    headers={"content-type": "image/jpeg"},
                )

            SafePublicImageFetcher(
                client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
            ).fetch("https://images.example/private-token")
            transport_logger.info("outside-context-kept")
        finally:
            transport_logger.removeHandler(transport_handler)
            unrelated_logger.removeHandler(unrelated_handler)
            transport_logger.setLevel(old_transport_level)
            unrelated_logger.setLevel(old_unrelated_level)
            unrelated_logger.propagate = old_unrelated_propagate

        self.assertNotIn(
            "must-not-log https://images.example/private-token",
            transport_handler.messages,
        )
        self.assertIn("outside-context-kept", transport_handler.messages)
        self.assertIn("unrelated-kept", unrelated_handler.messages)


if __name__ == "__main__":
    unittest.main()
