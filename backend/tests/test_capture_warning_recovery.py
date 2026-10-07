from __future__ import annotations

import copy
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.domain.models import CollectionPreviewRequest
from backend.app.services.capture import CaptureService
from backend.app.services.rendered_metadata import RenderedMetadataSupplement
from backend.app.services.safe_http import SafePublicFetcher
from backend.app.services.safe_http import SafeImageFetchResult
from backend.app.repositories.sqlite import SQLiteRepository
from backend.tests.test_xiaohongshu_embedded_metadata import NOTE_ID, HTTPS_COVER, _app, _html, _state


REJECTED = "页面封面链接未通过安全校验，已忽略。"
TARGET = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


class CoverWarningRecoveryTests(unittest.TestCase):
    def test_recovered_warning_stays_correct_through_save_reopen_and_image(self):
        service, _, _, probe = self.make_service(_state())
        image_data = BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(image_data, 'PNG')
        images = SimpleNamespace(fetch=Mock(return_value=SafeImageFetchResult(
            HTTPS_COVER, 'image/png', image_data.getvalue(), (), 'image/png')))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = SQLiteRepository(root / 'synthetic.sqlite3')
            with TestClient(_app(root, repository, service.fetcher, probe, images)) as client:
                response = client.post('/api/v1/collection-previews', json={'input_text': TARGET})
                self.assertEqual(response.status_code, 201)
                preview = response.json()
                self.assertNotIn(REJECTED, preview['metadata']['warnings'])
                saved = client.post('/api/v1/collection-items', headers={'Idempotency-Key': 'warning-recovery'},
                    json={'preview_id': preview['preview_id'], 'user_author': '我的作者备注'})
                self.assertEqual(saved.status_code, 201)
                item = saved.json()
            reopened = SQLiteRepository(root / 'synthetic.sqlite3')
            with TestClient(_app(root, reopened, service.fetcher, probe, images)) as client:
                self.assertEqual(client.get(f"/api/v1/collection-items/{item['id']}").json(), item)
                self.assertEqual(item['user_author'], '我的作者备注')
                self.assertNotIn(REJECTED, item['metadata']['warnings'])
                image = client.get(f"/api/v1/collection-items/{item['id']}/cover")
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.content, image_data.getvalue())
            with reopened._connect() as db:
                for table in ('jobs', 'videos', 'transcript_segments', 'extractions'):
                    self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    def make_service(self, state, supplement=None):
        cache = {}
        def upsert(payload, aliases):
            for alias in aliases:
                cache[alias] = copy.deepcopy(payload)
        repo = SimpleNamespace(
            get_collection_metadata_cache=lambda url: copy.deepcopy(cache.get(url)),
            upsert_collection_metadata_cache=upsert, create_collection_preview=Mock())
        body = _html(state).replace('</head>',
            '<meta property="og:image" content="https://untrusted.example/logo.png"></head>')
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(http.close)
        fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ["8.8.8.8"])
        probe = SimpleNamespace(probe=Mock(return_value=supplement))
        service = CaptureService(repo, fetcher=fetcher, rendered_metadata_probe=probe)
        return service, cache, requests, probe

    def test_embedded_cover_recovery_removes_only_obsolete_candidate_warning(self):
        service, cache, requests, probe = self.make_service(_state())
        original = service._capture
        def capture(*args):
            result = original(*args)
            result['metadata']['warnings'].append('其他字段仍需核对。')
            return result
        service._capture = capture
        result = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(result.metadata.cover_url.value, HTTPS_COVER)
        self.assertNotIn(REJECTED, result.metadata.warnings)
        self.assertEqual(result.metadata.warnings, ['其他字段仍需核对。'])
        self.assertNotIn(REJECTED, cache[TARGET]['metadata']['warnings'])
        self.assertEqual(len(requests), 1)
        probe.probe.assert_not_called()

    def test_rendered_recovery_removes_warning(self):
        supplement = RenderedMetadataSupplement(cover_url=HTTPS_COVER, cover_source='open_graph')
        service, _, _, probe = self.make_service(_state(cover=''), supplement)
        result = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertEqual(result.metadata.cover_url.value, HTTPS_COVER)
        self.assertNotIn(REJECTED, result.metadata.warnings)
        probe.probe.assert_called_once()

    def test_missing_or_rejected_replacement_keeps_safety_warning(self):
        for cover in ('', 'https://untrusted.example/cover.png'):
            with self.subTest(cover=cover):
                service, _, _, _ = self.make_service(_state(cover=cover))
                result = service.preview(CollectionPreviewRequest(input_text=TARGET))
                self.assertEqual(result.metadata.cover_url.value, '')
                self.assertIn(REJECTED, result.metadata.warnings)

    def test_fresh_legacy_cache_is_immutable_until_explicit_refresh(self):
        service, cache, requests, probe = self.make_service(_state())
        service.preview(CollectionPreviewRequest(input_text=TARGET))
        cache[TARGET]['metadata']['warnings'] = [REJECTED, '其他字段仍需核对。']
        snapshot = copy.deepcopy(cache)
        fresh = service.preview(CollectionPreviewRequest(input_text=TARGET))
        self.assertIn(REJECTED, fresh.metadata.warnings)
        self.assertEqual(cache, snapshot)
        self.assertEqual(len(requests), 1)
        probe.probe.assert_not_called()
        refreshed = service.preview(CollectionPreviewRequest(input_text=TARGET, refresh_metadata=True))
        self.assertNotIn(REJECTED, refreshed.metadata.warnings)
        self.assertEqual(len(requests), 2)
        self.assertIn(REJECTED, fresh.metadata.warnings)

    def test_batch_uses_same_cleanup_without_adding_probe(self):
        service, _, _, probe = self.make_service(_state())
        result = service.preview(CollectionPreviewRequest(input_text=TARGET), allow_rendered_cover=False)
        self.assertNotIn(REJECTED, result.metadata.warnings)
        probe.probe.assert_not_called()
