from __future__ import annotations

import unittest

import httpx

from backend.app.services.safe_http import (
    PinnedNetworkBackend,
    SafeHttpError,
    SafePublicFetcher,
    SafeUrlResolver,
)


PUBLIC_V4 = "8.8.8.8"
PUBLIC_V6 = "2001:4860:4860::8888"


def _client(handler, **kwargs) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)


class _ScriptedClock:
    def __init__(self, *values: float):
        self.values = list(values)

    def __call__(self) -> float:
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class _MutableClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class SafeUrlResolverTests(unittest.TestCase):
    def test_normalizes_public_http_url_and_all_dns_results(self):
        calls: list[str] = []

        def dns(host: str):
            calls.append(host)
            return [PUBLIC_V4, PUBLIC_V6, PUBLIC_V4]

        target = SafeUrlResolver(dns_resolver=dns).validate(
            "HTTPS://Example.COM.:443/path?q=1#fragment"
        )

        self.assertEqual(target.url, "https://example.com/path?q=1")
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.addresses, (PUBLIC_V4, PUBLIC_V6))
        self.assertEqual(calls, ["example.com"])

    def test_rejects_private_and_mixed_dns_answers(self):
        for answers in (["127.0.0.1"], [PUBLIC_V4, "10.0.0.1"]):
            with self.subTest(answers=answers):
                with self.assertRaises(SafeHttpError) as caught:
                    SafeUrlResolver(dns_resolver=lambda _host, a=answers: a).validate(
                        "https://example.com/"
                    )
                self.assertEqual(caught.exception.code, "UNSAFE_ADDRESS")

    def test_rejects_userinfo_unsupported_scheme_and_abnormal_port(self):
        resolver = SafeUrlResolver(dns_resolver=lambda _host: [PUBLIC_V4])
        for url in (
            "https://user:password@example.com/",
            "ftp://example.com/file",
            "https://example.com:444/",
            "https://example.com:80/",
            "http://example.com:443/",
            "https://example.com:not-a-port/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SafeHttpError) as caught:
                    resolver.validate(url)
                self.assertEqual(caught.exception.code, "UNSAFE_URL")
                self.assertNotIn("password", str(caught.exception))

    def test_exact_allowlist_does_not_accept_subdomains(self):
        resolver = SafeUrlResolver(
            dns_resolver=lambda _host: [PUBLIC_V4],
            allowed_hosts={"example.com"},
        )
        resolver.validate("https://example.com/")
        with self.assertRaises(SafeHttpError) as caught:
            resolver.validate("https://www.example.com/")
        self.assertEqual(caught.exception.code, "HOST_NOT_ALLOWED")

    def test_resolve_expands_with_injected_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/short":
                return httpx.Response(
                    302,
                    headers={"location": "/final", "content-type": "text/html"},
                )
            return httpx.Response(
                200, text="ok", headers={"content-type": "text/html"}
            )

        result = SafeUrlResolver(
            client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
        ).resolve("https://example.com/short")

        self.assertEqual(result.final_url, "https://example.com/final")


class SafePublicFetcherTests(unittest.TestCase):
    def test_fetches_public_html_without_cookie_or_authorization(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                text="<title>public</title>",
                headers={"content-type": "text/html; charset=utf-8"},
            )

        client = _client(
            handler,
            headers={"Authorization": "Bearer must-not-leak"},
            cookies={"session": "must-not-leak"},
        )
        result = SafePublicFetcher(
            client=client, dns_resolver=lambda _host: [PUBLIC_V4]
        ).fetch("https://example.com/page")

        self.assertEqual(result.final_url, "https://example.com/page")
        self.assertEqual(result.media_type, "text/html")
        self.assertEqual(result.text, "<title>public</title>")
        self.assertEqual(result.redirects, ())
        self.assertEqual(seen[0].headers["accept-encoding"], "identity")
        self.assertNotIn("authorization", seen[0].headers)
        self.assertNotIn("cookie", seen[0].headers)
        self.assertLessEqual(seen[0].extensions["timeout"]["connect"], 5)
        self.assertLessEqual(seen[0].extensions["timeout"]["read"], 15)

    def test_follows_relative_and_cross_domain_redirects_manually(self):
        calls: list[str] = []
        dns_calls: list[str] = []

        # Keep the relative hop distinct from the initial URL.
        def ordered_handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"location": "/next", "content-type": "text/html"},
                )
            if request.url.host == "short.example":
                return httpx.Response(
                    301,
                    headers={
                        "location": "https://other.example/final",
                        "content-type": "text/html",
                    },
                )
            return httpx.Response(
                200, text="ok", headers={"content-type": "application/xhtml+xml"}
            )

        def dns(host: str):
            dns_calls.append(host)
            return [PUBLIC_V4]

        result = SafePublicFetcher(
            client=_client(ordered_handler), dns_resolver=dns
        ).fetch("https://short.example/start")

        self.assertEqual(
            calls,
            [
                "https://short.example/start",
                "https://short.example/next",
                "https://other.example/final",
            ],
        )
        self.assertEqual(dns_calls, ["short.example", "short.example", "other.example"])
        self.assertEqual(
            result.redirects,
            ("https://short.example/next", "https://other.example/final"),
        )
        self.assertEqual(result.final_url, "https://other.example/final")

    def test_total_timeout_stops_before_the_next_redirect_request(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "/next", "content-type": "text/html"},
            )

        fetcher = SafePublicFetcher(
            client=_client(handler),
            dns_resolver=lambda _host: [PUBLIC_V4],
            total_timeout_seconds=10,
            monotonic_clock=_ScriptedClock(0, 0, 0, 0, 0, 11),
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/start")

        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")
        self.assertEqual(calls, ["https://example.com/start"])

    def test_total_timeout_is_enforced_while_streaming_the_body(self):
        calls: list[str] = []

        class OneChunk(httpx.SyncByteStream):
            def __iter__(self):
                yield b"body"

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                200,
                stream=OneChunk(),
                headers={"content-type": "text/html"},
            )

        fetcher = SafePublicFetcher(
            client=_client(handler),
            dns_resolver=lambda _host: [PUBLIC_V4],
            total_timeout_seconds=10,
            monotonic_clock=_ScriptedClock(0, 0, 0, 11),
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/page")

        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")
        self.assertEqual(calls, ["https://example.com/page"])

    def test_total_timeout_is_enforced_after_empty_body_eof(self):
        clock = _MutableClock()

        class EmptyLate(httpx.SyncByteStream):
            def __iter__(self):
                clock.value = 11
                return
                yield b""

        fetcher = SafePublicFetcher(
            client=_client(lambda _request: httpx.Response(
                200,
                stream=EmptyLate(),
                headers={"content-type": "text/html"},
            )),
            dns_resolver=lambda _host: [PUBLIC_V4],
            total_timeout_seconds=10,
            monotonic_clock=clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/empty")

        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")

    def test_total_timeout_is_enforced_after_last_chunk_eof(self):
        clock = _MutableClock()

        class LastChunkThenLate(httpx.SyncByteStream):
            def __iter__(self):
                yield b"body"
                clock.value = 11

        fetcher = SafePublicFetcher(
            client=_client(lambda _request: httpx.Response(
                200,
                stream=LastChunkThenLate(),
                headers={"content-type": "text/html"},
            )),
            dns_resolver=lambda _host: [PUBLIC_V4],
            total_timeout_seconds=10,
            monotonic_clock=clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/last-chunk")

        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")

    def test_total_timeout_is_enforced_after_response_close(self):
        clock = _MutableClock()

        class CloseLate(httpx.SyncByteStream):
            def __iter__(self):
                yield b"body"

            def close(self):
                clock.value = 11

        fetcher = SafePublicFetcher(
            client=_client(lambda _request: httpx.Response(
                200,
                stream=CloseLate(),
                headers={"content-type": "text/html"},
            )),
            dns_resolver=lambda _host: [PUBLIC_V4],
            total_timeout_seconds=10,
            monotonic_clock=clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/close")

        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")

    def test_late_close_does_not_mask_an_existing_size_error(self):
        clock = _MutableClock()

        class OversizedAndLateClose(httpx.SyncByteStream):
            def __iter__(self):
                yield b"too large"

            def close(self):
                clock.value = 11

        fetcher = SafePublicFetcher(
            client=_client(lambda _request: httpx.Response(
                200,
                stream=OversizedAndLateClose(),
                headers={"content-type": "text/html"},
            )),
            dns_resolver=lambda _host: [PUBLIC_V4],
            max_response_bytes=1,
            total_timeout_seconds=10,
            monotonic_clock=clock,
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/large")

        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

    def test_response_close_failure_remains_a_stable_fetch_error(self):
        class CloseFailure(httpx.SyncByteStream):
            def __iter__(self):
                yield b"body"

            def close(self):
                raise OSError("private close detail")

        fetcher = SafePublicFetcher(
            client=_client(lambda _request: httpx.Response(
                200,
                stream=CloseFailure(),
                headers={"content-type": "text/html"},
            )),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/close-failure")

        self.assertEqual(caught.exception.code, "FETCH_FAILED")
        self.assertNotIn("private", str(caught.exception))

    def test_close_failure_does_not_mask_an_existing_safe_error(self):
        class CloseFailure(httpx.SyncByteStream):
            def __init__(self, body=b"body", *, late_clock=None):
                self.body = body
                self.late_clock = late_clock

            def __iter__(self):
                if self.late_clock is not None:
                    self.late_clock.value = 11
                yield self.body

            def close(self):
                raise OSError("private secondary close detail")

        clock = _MutableClock()
        cases = (
            (
                "size",
                httpx.Response(
                    200,
                    stream=CloseFailure(b"too large"),
                    headers={"content-type": "text/html"},
                ),
                "RESPONSE_TOO_LARGE",
                {"max_response_bytes": 1},
            ),
            (
                "mime",
                httpx.Response(
                    200,
                    stream=CloseFailure(),
                    headers={"content-type": "application/json"},
                ),
                "UNSUPPORTED_CONTENT_TYPE",
                {},
            ),
            (
                "status",
                httpx.Response(
                    500,
                    stream=CloseFailure(),
                    headers={"content-type": "text/html"},
                ),
                "HTTP_STATUS_ERROR",
                {},
            ),
            (
                "timeout",
                httpx.Response(
                    200,
                    stream=CloseFailure(late_clock=clock),
                    headers={"content-type": "text/html"},
                ),
                "FETCH_TIMEOUT",
                {
                    "total_timeout_seconds": 10,
                    "monotonic_clock": clock,
                },
            ),
        )
        for name, response, expected, options in cases:
            with self.subTest(name=name):
                fetcher = SafePublicFetcher(
                    client=_client(lambda _request, value=response: value),
                    dns_resolver=lambda _host: [PUBLIC_V4],
                    **options,
                )
                with self.assertRaises(SafeHttpError) as caught:
                    fetcher.fetch(f"https://example.com/{name}")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("private", str(caught.exception))

    def test_revalidates_allowlist_and_dns_before_each_redirect_hop(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={
                    "location": "https://blocked.example/final",
                    "content-type": "text/html",
                },
            )

        fetcher = SafePublicFetcher(
            client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://allowed.example/start", allowed_hosts={"allowed.example"})
        self.assertEqual(caught.exception.code, "HOST_NOT_ALLOWED")
        self.assertEqual(calls, ["https://allowed.example/start"])

        calls.clear()
        answers = {
            "allowed.example": [PUBLIC_V4],
            "blocked.example": ["192.168.1.20"],
        }
        fetcher = SafePublicFetcher(
            client=_client(handler), dns_resolver=lambda host: answers[host]
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://allowed.example/start")
        self.assertEqual(caught.exception.code, "UNSAFE_ADDRESS")
        self.assertEqual(calls, ["https://allowed.example/start"])

    def test_rejects_more_than_three_redirects(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            index = int(request.url.path.removeprefix("/"))
            return httpx.Response(
                302,
                headers={
                    "location": f"/{index + 1}",
                    "content-type": "text/html",
                },
            )

        fetcher = SafePublicFetcher(
            client=_client(handler), dns_resolver=lambda _host: [PUBLIC_V4]
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/0")
        self.assertEqual(caught.exception.code, "TOO_MANY_REDIRECTS")
        self.assertEqual(len(calls), 4)

    def test_rejects_missing_or_non_html_content_type(self):
        for headers in ({}, {"content-type": "application/json"}):
            with self.subTest(headers=headers):
                fetcher = SafePublicFetcher(
                    client=_client(lambda _request, h=headers: httpx.Response(200, headers=h)),
                    dns_resolver=lambda _host: [PUBLIC_V4],
                )
                with self.assertRaises(SafeHttpError) as caught:
                    fetcher.fetch("https://example.com/")
                self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_TYPE")

        redirect = SafePublicFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    302,
                    content=b'{}',
                    headers={
                        "content-type": "application/json",
                        "location": "/final",
                    },
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            redirect.fetch("https://example.com/")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_TYPE")

        empty_redirect = SafePublicFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    302, headers={"location": "/final"}
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            empty_redirect.fetch("https://example.com/")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_TYPE")

    def test_rejects_declared_and_streamed_bodies_over_four_mib(self):
        limit = 4 * 1024 * 1024
        declared = SafePublicFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    content=b"x",
                    headers={
                        "content-type": "text/html",
                        "content-length": str(limit + 1),
                    },
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            declared.fetch("https://example.com/")
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

        class ChunkedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * limit
                yield b"x"

        streamed = SafePublicFetcher(
            client=_client(
                lambda _request: httpx.Response(
                    200,
                    stream=ChunkedStream(),
                    headers={"content-type": "text/html"},
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            streamed.fetch("https://example.com/")
        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

    def test_network_failure_is_stable_and_does_not_expose_detail(self):
        fetcher = SafePublicFetcher(
            client=_client(
                lambda _request: (_ for _ in ()).throw(
                    httpx.ConnectError("secret internal endpoint")
                )
            ),
            dns_resolver=lambda _host: [PUBLIC_V4],
        )
        with self.assertRaises(SafeHttpError) as caught:
            fetcher.fetch("https://example.com/")
        self.assertEqual(caught.exception.code, "FETCH_FAILED")
        self.assertNotIn("secret", str(caught.exception))


class PinnedNetworkBackendTests(unittest.TestCase):
    def test_connects_to_validated_ip_while_preserving_original_host_upstream(self):
        class RecordingBackend:
            def __init__(self):
                self.calls: list[tuple[str, int]] = []

            def connect_tcp(
                self, host, port, timeout=None, local_address=None, socket_options=None
            ):
                self.calls.append((host, port))
                return object()

        transport = RecordingBackend()
        backend = PinnedNetworkBackend(
            lambda host: [PUBLIC_V4, PUBLIC_V6], backend=transport
        )

        backend.connect_tcp("origin.example", 443)

        self.assertEqual(transport.calls, [(PUBLIC_V4, 443)])

    def test_rebinding_to_private_address_never_reaches_tcp_backend(self):
        class RecordingBackend:
            def __init__(self):
                self.called = False

            def connect_tcp(self, *args, **kwargs):
                self.called = True
                return object()

        transport = RecordingBackend()
        backend = PinnedNetworkBackend(
            lambda _host: [PUBLIC_V4, "127.0.0.1"], backend=transport
        )

        with self.assertRaises(SafeHttpError) as caught:
            backend.connect_tcp("origin.example", 443)
        self.assertEqual(caught.exception.code, "UNSAFE_ADDRESS")
        self.assertFalse(transport.called)


if __name__ == "__main__":
    unittest.main()
