from __future__ import annotations

import copy
from html import escape
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from backend.app.adapters.bilibili import BilibiliAdapter
from backend.app.domain.models import CollectionItemCreateRequest, CollectionPreviewRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.capture import CaptureService
from backend.app.services.collections import CollectionService
from backend.app.services.safe_http import SafePublicFetcher
from backend.tests.test_capture_quality import _Registry


URLS = {'bilibili': 'https://www.bilibili.com/video/BV1ZKub6PEpi',
        'xiaohongshu': 'https://www.xiaohongshu.com/explore/64a01234567890abcdef1234',
        'webpage': 'https://example.com/article'}
SUFFIXES = {'bilibili': '_哔哩哔哩_bilibili', 'xiaohongshu': ' - 小红书'}


class CaptureTitleSuffixTests(unittest.TestCase):
    def make_service(self, platform, title, repo=None):
        cache = {}
        def upsert(payload, aliases):
            for alias in aliases:
                cache[alias] = copy.deepcopy(payload)
        repo = repo or SimpleNamespace(get_collection_metadata_cache=lambda url: copy.deepcopy(cache.get(url)),
            upsert_collection_metadata_cache=upsert, create_collection_preview=Mock())
        requests = []
        def transport(request):
            requests.append(request)
            html = (f'<html><head><title>{escape(title)}</title>'
                    f'<meta property="og:title" content="{escape(title, quote=True)}">'
                    '<meta name="description" content="原始正文 #真实话题 - 小红书">'
                    '<meta name="keywords" content="真实话题,中文🌈"></head></html>')
            return httpx.Response(200, text=html, headers={'content-type': 'text/html'})
        http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(http.close)
        fetcher = SafePublicFetcher(http, dns_resolver=lambda _: ['8.8.8.8'])
        registry = _Registry(BilibiliAdapter(http, dns_resolver=lambda _: ['8.8.8.8']))
        return CaptureService(repo, fetcher=fetcher, adapter_registry=registry), cache, requests

    def test_exact_platform_suffix_only_preserves_source_copy_tags_and_unicode(self):
        for platform, suffix in SUFFIXES.items():
            with self.subTest(platform=platform):
                title = '#真实话题 我的标题🌈'
                service, cache, requests = self.make_service(platform, title + suffix)
                result = service.preview(CollectionPreviewRequest(input_text=URLS[platform]))
                self.assertEqual(result.metadata.title.value, title)
                self.assertEqual(result.metadata.title.source, 'platform_public' if platform == 'bilibili' else 'open_graph')
                self.assertTrue(result.metadata.title.fetched_at)
                self.assertEqual(result.metadata.source_copy.value, '原始正文 #真实话题 - 小红书')
                self.assertIn('真实话题', [tag.value for tag in result.metadata.platform_tags])
                self.assertEqual(cache[URLS[platform]]['metadata']['title']['value'], title)
                self.assertEqual(len(requests), 1)

    def test_non_suffix_empty_body_and_other_platform_are_untouched(self):
        cases = [('bilibili', '_哔哩哔哩_bilibili'), ('bilibili', '标题_哔哩哔哩_bilibili访谈'),
                 ('bilibili', '标题_bilibili'), ('bilibili', '标题 - 小红书'),
                 ('xiaohongshu', '#健身 #自律 正文标题'), ('xiaohongshu', '研究 - 小红书的故事'),
                 ('webpage', '文章 - 小红书'), ('webpage', '文章_哔哩哔哩_bilibili')]
        for platform, title in cases:
            with self.subTest(platform=platform, title=title):
                service, _, _ = self.make_service(platform, title)
                result = service.preview(CollectionPreviewRequest(input_text=URLS[platform]))
                self.assertEqual(result.metadata.title.value, title)

    def test_suffix_is_removed_once_not_recursively(self):
        service, _, _ = self.make_service('bilibili', '原题' + SUFFIXES['bilibili'] * 2)
        result = service.preview(CollectionPreviewRequest(input_text=URLS['bilibili']))
        self.assertEqual(result.metadata.title.value, '原题' + SUFFIXES['bilibili'])

    def test_suffix_cleanup_preserves_body_whitespace_and_rejects_blank_body(self):
        for body in ('正文 ', '正文\t', '   ', '\t'):
            with self.subTest(body=repr(body)):
                service, _, _ = self.make_service('bilibili', 'initial')
                raw = body + SUFFIXES['bilibili']
                # Use the capture seam so upstream HTML whitespace policy cannot
                # hide extra normalization in the suffix reconciliation itself.
                original = service._capture
                def decorated(*args, **kwargs):
                    result = original(*args, **kwargs)
                    result['metadata']['title']['value'] = raw
                    return result
                with patch.object(service, '_capture', side_effect=decorated):
                    result = service.preview(CollectionPreviewRequest(input_text=URLS['bilibili']))
                self.assertEqual(result.metadata.title.value, body if body.strip() else raw)

    def test_legacy_cache_unchanged_until_refresh_and_no_added_network(self):
        for platform, suffix in SUFFIXES.items():
            service, cache, requests = self.make_service(platform, '原题' + suffix)
            request = CollectionPreviewRequest(input_text=URLS[platform])
            service.preview(request)
            cache[URLS[platform]]['metadata']['title']['value'] = '旧题' + suffix
            before = copy.deepcopy(cache)
            legacy = service.preview(request)
            self.assertEqual(legacy.metadata.title.value, '旧题' + suffix)
            self.assertEqual(cache, before)
            self.assertEqual(len(requests), 1)
            fresh = service.preview(CollectionPreviewRequest(input_text=URLS[platform], refresh_metadata=True))
            self.assertEqual(fresh.metadata.title.value, '原题')
            self.assertEqual(legacy.metadata.title.value, '旧题' + suffix)
            self.assertEqual(len(requests), 2)

    def test_xhs_gate_title_is_rejected_before_cleanup(self):
        service, _, _ = self.make_service('xiaohongshu', '登录 - 小红书')
        result = service.preview(CollectionPreviewRequest(input_text=URLS['xiaohongshu']))
        self.assertEqual(result.metadata.title.value, '')
        self.assertTrue(result.metadata.warnings)

    def test_saved_user_title_and_old_item_survive_refresh_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'synthetic.sqlite3'
            repo = SQLiteRepository(path)
            service, _, _ = self.make_service('bilibili', '来源题' + SUFFIXES['bilibili'], repo)
            request = CollectionPreviewRequest(input_text=URLS['bilibili'])
            preview = service.preview(request)
            items = CollectionService(repo)
            saved = items.create(CollectionItemCreateRequest(preview_id=preview.preview_id,
                user_title='我的标题 - 小红书', user_author='我的作者', personal_tags=['个人标签']), 'suffix-save')
            self.assertEqual(saved.metadata.title.value, '来源题')
            service.preview(CollectionPreviewRequest(input_text=URLS['bilibili'], refresh_metadata=True))
            reopened = SQLiteRepository(path)
            self.assertEqual(CollectionService(reopened).get(saved.id), saved)
            self.assertEqual(saved.display_title, '我的标题 - 小红书')
            self.assertEqual(saved.user_author, '我的作者')
            with reopened._connect() as db:
                for table in ('jobs', 'videos', 'transcript_segments', 'extractions'):
                    self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
