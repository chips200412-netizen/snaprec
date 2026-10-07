from __future__ import annotations

import io
import json
import logging
import unittest
from urllib.parse import parse_qs

import httpx

from backend.app.domain.models import CollectionPreviewRequest
from backend.app.services.capture import CaptureService
from backend.app.services.safe_http import SafeHttpError, SafePublicFetcher
from backend.tests.test_author_metadata import MemoryCaptureRepository


VIDEO = 'Abc_123-xyz'
URL = 'https://www.youtube.com/watch?v=' + VIDEO
PAYLOAD = {'type': 'video', 'version': '1.0', 'title': 'Public title', 'author_name': 'Target channel',
           'author_url': 'https://www.youtube.com/@channel', 'html': '<iframe src="https://evil.invalid/media"></iframe>'}


class YouTubeCaptureTests(unittest.TestCase):
    def capture(self, url=URL, *, payload=None, status=200, mime='application/json', headers=None, dns=None, clock=None, on_request=None):
        calls = []
        def handler(request):
            calls.append(request)
            if on_request:
                on_request(request)
            return httpx.Response(status, content=json.dumps(PAYLOAD if payload is None else payload).encode(),
                                  headers={'content-type': mime, **(headers or {})})
        with httpx.Client(transport=httpx.MockTransport(handler), cookies={'secret': 'cookie'},
                          headers={'Authorization': 'Bearer secret'}, auth=('name', 'secret')) as client:
            fetcher = SafePublicFetcher(client, dns_resolver=dns or (lambda _: ['8.8.8.8']), monotonic_clock=clock)
            repo = MemoryCaptureRepository()
            service = CaptureService(repo, fetcher=fetcher)
            result = service.preview(CollectionPreviewRequest(input_text=url))
            cached = service.preview(CollectionPreviewRequest(input_text=url))
            self.assertEqual(result.metadata, cached.metadata)
            return result, calls, repo

    def test_default_capture_reads_only_fixed_public_oembed(self):
        for url in [URL, 'https://youtu.be/' + VIDEO, 'https://www.youtube.com/shorts/' + VIDEO,
                    'https://m.youtube.com/embed/' + VIDEO, 'https://youtube.com/live/' + VIDEO]:
            with self.subTest(url=url):
                result, calls, _ = self.capture(url)
                self.assertEqual(result.metadata.author.value, 'Target channel')
                self.assertEqual(result.metadata.author.source, 'platform_public')
                self.assertEqual(result.metadata.title.value, 'Public title')
                self.assertEqual(result.metadata_status, 'recognized')
                self.assertEqual(result.canonical_url, URL)
                self.assertEqual(len(calls), 1)
                request = calls[0]
                self.assertEqual((request.url.scheme, request.url.host, request.url.path), ('https', 'www.youtube.com', '/oembed'))
                self.assertEqual(parse_qs(request.url.query.decode()), {'url': [URL], 'format': ['json']})
                self.assertEqual(request.headers['Accept'], 'application/json')
                for name in ('Authorization', 'Cookie', 'Referer'):
                    self.assertNotIn(name, request.headers)
                self.assertNotIn('iframe', result.model_dump_json())

    def test_invalid_platform_routes_do_not_use_generic_fetch(self):
        for url in ['https://www.youtube.com/', 'https://www.youtube.com/playlist?list=abc',
                    URL + '&v=Different00', URL + '&list=abc', URL + '&V=Different00',
                    'https://youtu.be/' + VIDEO + '?v=Different00',
                    'https://www.youtube.com/watch?v=short', 'https://www.youtube.com/shorts/' + VIDEO + '/extra']:
            with self.subTest(url=url):
                result, calls, _ = self.capture(url)
                self.assertEqual(result.metadata_status, 'metadata_unavailable')
                self.assertEqual(calls, [])

    def test_failed_oembed_remains_bookmark_and_does_not_redirect(self):
        cases = [dict(status=404), dict(mime='text/html'), dict(status=302, headers={'location': 'https://evil.invalid/'}),
                 dict(status=302, headers={'location': URL}), dict(payload={'type': 'link', 'version': '1.0', 'author_name': 'Wrong'}),
                 dict(payload={**PAYLOAD, 'version': '2.0'}), dict(payload={**PAYLOAD, 'title': 'x' * 65536}),
                 dict(headers={'content-length': '65537'}), dict(dns=lambda _: ['127.0.0.1'])]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                result, calls, _ = self.capture(**kwargs)
                self.assertEqual(result.metadata_status, 'metadata_unavailable')
                self.assertEqual(result.canonical_url, '')
                self.assertEqual(result.metadata.author.value, '')
                self.assertLessEqual(len(calls), 1)

    def test_oembed_deadline_and_transport_logs_are_bounded(self):
        now = [0.0]
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger('httpx')
        old = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            result, calls, _ = self.capture(clock=lambda: now[0], on_request=lambda _: now.__setitem__(0, 16.0))
            self.assertEqual(result.metadata_status, 'metadata_unavailable')
            self.assertEqual(output.getvalue(), '')
            self.assertLessEqual(calls[0].extensions['timeout']['read'], 15)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)

    def test_html_fetch_does_not_accept_json(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=PAYLOAD))) as client:
            with self.assertRaises(SafeHttpError):
                SafePublicFetcher(client, dns_resolver=lambda _: ['8.8.8.8']).fetch('https://example.com/')

    def test_author_name_url_is_not_a_channel_name(self):
        result, calls, _ = self.capture(payload={**PAYLOAD, 'author_name': 'https://www.youtube.com/@channel'})
        self.assertEqual(result.metadata.author.value, '')
        self.assertEqual(result.metadata.title.value, 'Public title')
        self.assertEqual(len(calls), 1)
