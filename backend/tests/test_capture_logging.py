from __future__ import annotations

import logging
import unittest
from concurrent.futures import ThreadPoolExecutor

import httpx

from backend.app.services.safe_http import SafeHttpError, SafePublicFetcher


_HTTP_LOGGERS = (
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)
_PRIVATE_MARKER = "synthetic-share-query-must-not-be-logged"


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class CaptureHttpLoggingTests(unittest.TestCase):
    def setUp(self):
        self.handler = _Records()
        self.saved = []
        for name in _HTTP_LOGGERS:
            logger = logging.getLogger(name)
            self.saved.append((logger, logger.level, logger.propagate))
            logger.addHandler(self.handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False

    def tearDown(self):
        for logger, level, propagate in self.saved:
            logger.removeHandler(self.handler)
            logger.setLevel(level)
            logger.propagate = propagate

    def test_success_suppresses_capture_transport_records_not_other_contexts(self):
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                logging.getLogger("httpcore.http11").debug("body %s", _PRIVATE_MARKER)
                yield b"<html><head><title>public</title></head></html>"

            def close(self):
                logging.getLogger("httpcore.connection").debug("close %s", _PRIVATE_MARKER)

        def handler(request):
            for name in _HTTP_LOGGERS:
                logging.getLogger(name).debug("capture %s", _PRIVATE_MARKER)
            # Other work on another thread must retain its original logging.
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(logging.getLogger("httpx").info, "unrelated-thread").result()
            return httpx.Response(200, headers={"content-type": "text/html"}, stream=Body())

        logging.getLogger("httpx").info("before-capture")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with SafePublicFetcher(client=client, dns_resolver=lambda _host: ["8.8.8.8"]) as fetcher:
                result = fetcher.fetch(f"https://www.xiaohongshu.com/explore/note?xsec_token={_PRIVATE_MARKER}")
                self.assertEqual(result.status_code, 200)
        logging.getLogger("httpx").info("after-capture")
        self.assertEqual(self.handler.messages, ["before-capture", "unrelated-thread", "after-capture"])

    def test_failure_resets_logging_scope_and_has_safe_public_message(self):
        def handler(request):
            logging.getLogger("httpcore.http11").debug("failure %s", _PRIVATE_MARKER)
            raise httpx.ReadTimeout(_PRIVATE_MARKER)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with SafePublicFetcher(client=client, dns_resolver=lambda _host: ["8.8.8.8"]) as fetcher:
                with self.assertRaises(SafeHttpError) as caught:
                    fetcher.fetch(f"https://www.xiaohongshu.com/explore/note?xsec_token={_PRIVATE_MARKER}")
        self.assertEqual(caught.exception.code, "FETCH_TIMEOUT")
        self.assertNotIn(_PRIVATE_MARKER, str(caught.exception))
        logging.getLogger("httpx").info("after-failure")
        self.assertEqual(self.handler.messages, ["after-failure"])

    def test_response_close_failures_remain_safe_and_restore_logging(self):
        for failure in ("mime", "status", "read", "close"):
            with self.subTest(failure=failure):
                self.handler.messages.clear()

                class Body(httpx.SyncByteStream):
                    def __iter__(self):
                        if failure == "read":
                            raise httpx.ReadError(_PRIVATE_MARKER)
                        yield b"<head><title>public</title></head>"

                    def close(self):
                        logging.getLogger("httpcore.connection").debug(
                            "close %s", _PRIVATE_MARKER,
                        )
                        raise httpx.CloseError(_PRIVATE_MARKER)

                def handler(request):
                    return httpx.Response(
                        503 if failure == "status" else 200,
                        headers={"content-type": (
                            "application/json" if failure == "mime" else "text/html"
                        )},
                        stream=Body(),
                    )

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    fetcher = SafePublicFetcher(
                        client=client, dns_resolver=lambda _host: ["8.8.8.8"],
                    )
                    with self.assertRaises(SafeHttpError) as caught:
                        fetcher.fetch(f"https://ordinary.example/page?token={_PRIVATE_MARKER}")
                self.assertNotIn(_PRIVATE_MARKER, str(caught.exception))
                logging.getLogger("httpx").info("after-close-failure")
                self.assertEqual(self.handler.messages, ["after-close-failure"])


if __name__ == "__main__":
    unittest.main()
