from __future__ import annotations

import copy
import json
import unittest

import httpx

from backend.app.adapters import AdapterRegistry, BilibiliAdapter
from backend.app.adapters.douyin import DouyinAdapter
from backend.app.domain.models import CollectionPreviewRequest
from backend.app.services.capture import CaptureService
from backend.app.services.safe_http import SafePublicFetcher


class MemoryCaptureRepository:
    def __init__(self):
        self.cache = {}
        self.previews = {}

    def get_collection_metadata_cache(self, url):
        return copy.deepcopy(self.cache.get(url))

    def upsert_collection_metadata_cache(self, payload, aliases):
        for alias in aliases:
            self.cache[alias] = copy.deepcopy(payload)

    def create_collection_preview(self, payload):
        self.previews[payload['preview_id']] = copy.deepcopy(payload)


def ld(value):
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


class AuthorMetadataTests(unittest.TestCase):
    def preview(self, url, page, *, redirect=None):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            if redirect and len(calls) == 1:
                return httpx.Response(302, headers={'location': redirect, 'content-type': 'text/html'})
            return httpx.Response(200, text=page, headers={'content-type': 'text/html'})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            fetcher = SafePublicFetcher(client, dns_resolver=lambda _: ['8.8.8.8'])
            registry = AdapterRegistry([
                BilibiliAdapter(client, dns_resolver=lambda _: ['8.8.8.8']),
                DouyinAdapter(client, dns_resolver=lambda _: ['8.8.8.8']),
            ])
            repo = MemoryCaptureRepository()
            service = CaptureService(repo, fetcher=fetcher, adapter_registry=registry)
            result = service.preview(CollectionPreviewRequest(input_text=url))
            cached = service.preview(CollectionPreviewRequest(input_text=url))
            self.assertEqual(result.metadata, cached.metadata)
            return result, repo, calls

    def test_real_head_author_and_itemprop_for_web_and_xhs(self):
        for url in ['https://example.com/article', 'https://www.xiaohongshu.com/explore/' + 'a' * 24]:
            with self.subTest(url=url):
                result, _, _ = self.preview(url, '<head><meta itemprop="author" content="Target"></head><body><meta name="author" content="Body">')
                self.assertEqual(result.metadata.author.value, 'Target')
                self.assertEqual(result.metadata.author.source, 'page_metadata')

    def test_body_and_inert_authors_never_escape(self):
        for markup in [
            '<body><meta name="author" content="Wrong">',
            '<p>Body</p><head><meta name="author" content="Wrong"></head>',
            '<head><template><meta name="author" content="Wrong"></template></head>',
            '<head><noscript><meta name="author" content="Wrong"></noscript></head>',
        ]:
            with self.subTest(markup=markup):
                result, _, _ = self.preview('https://example.com/article', markup)
                self.assertEqual(result.metadata.author.value, '')

    def test_target_jsonld_reference_and_recommendations(self):
        url = 'https://example.com/article'
        payload = {'@context': 'https://schema.org', '@graph': [
            {'@type': 'Article', '@id': url + '#article', 'url': url, 'author': {'@id': '#writer'}},
            {'@type': 'Person', '@id': '#writer', 'name': 'Target'},
            {'@type': 'Article', 'url': 'https://example.com/other', 'author': {'name': 'Other'}},
        ]}
        result, _, _ = self.preview(url, ld(payload))
        self.assertEqual(result.metadata.author.value, 'Target')
        self.assertEqual(result.metadata.author.source, 'page_metadata')

    def test_jsonld_ambiguity_and_external_reference_are_empty(self):
        url = 'https://example.com/article'
        for data in [
            [{'@type': 'Article', 'author': {'name': 'One'}}, {'@type': 'Article', 'author': {'name': 'Two'}}],
            {'@type': 'Article', 'url': 'https://example.com/other', 'author': {'name': 'Other'}},
            {'@type': 'Article', 'url': url, '@id': 'https://example.com/other', 'author': {'name': 'Other'}},
            {'@type': 'Article', 'url': url, 'author': {'@id': 'https://elsewhere.com/person'}},
            {'@type': 'WebPage', 'review': {'@type': 'Article', 'author': {'name': 'Reviewer'}}},
        ]:
            with self.subTest(data=data):
                result, _, _ = self.preview(url, ld(data))
                self.assertEqual(result.metadata.author.value, '')
        result, _, _ = self.preview(url, '<meta name="author" content="One"><meta itemprop="author" content="Two">')
        self.assertEqual(result.metadata.author.value, '')

    def test_author_profile_urls_are_not_display_names_or_fetch_targets(self):
        for markup, expected in [
            ('<meta property="article:author" content="https://example.com/profile">', ''),
            ('<meta property="article:author" content="https://example.com/profile"><meta name="author" content="Name">', 'Name'),
            ('<meta itemprop="author" content="//example.com/profile">', ''),
        ]:
            result, _, calls = self.preview('https://example.com/article', markup)
            self.assertEqual(result.metadata.author.value, expected)
            self.assertEqual(calls, ['https://example.com/article'])

    def test_itemprop_only_expands_author_and_jsonld_rejects_redefinitions(self):
        result, _, _ = self.preview('https://example.com/article', '<meta itemprop="description" content="Not in contract">')
        self.assertEqual(result.metadata.source_copy.value, '')
        for script in [
            '{"@context":{"author":"https://schema.org/publisher"},"@type":"Article","author":{"name":"Publisher"}}',
            '{"@type":"Article","author":{"name":"One"},"author":{"name":"Two"}}',
        ]:
            result, _, _ = self.preview('https://example.com/article', '<script type="application/ld+json">' + script + '</script>')
            self.assertEqual(result.metadata.author.value, '')

    def test_bilibili_owner_wins_and_missing_owner_stays_empty(self):
        url = 'https://www.bilibili.com/video/BV1target'
        for owner, expected in [({'name': 'Target'}, 'Target'), ({}, '')]:
            state = {'videoData': {'bvid': 'BV1target', 'owner': owner, 'title': 'Video'}, 'upData': {'name': 'Wrong'}}
            page = '<meta name="author" content="Wrong"><script>window.__INITIAL_STATE__=' + json.dumps(state) + '</script>'
            result, _, _ = self.preview(url, page)
            self.assertEqual(result.metadata.author.value, expected)

    def test_bilibili_mismatched_work_and_redirect_cannot_create_alias(self):
        url = 'https://www.bilibili.com/video/BV1target'
        other = 'https://www.bilibili.com/video/BV1other'
        page = '<script>window.__INITIAL_STATE__=' + json.dumps({'videoData': {'bvid': 'BV1other', 'owner': {'name': 'Wrong'}}}) + '</script>'
        for redirect in [None, other]:
            result, repo, _ = self.preview(url, page, redirect=redirect)
            self.assertEqual(result.metadata_status, 'metadata_unavailable')
            self.assertEqual(result.canonical_url, '')
            self.assertNotIn(other, repo.cache)

    def test_bilibili_av_mapping_requires_aid(self):
        url = 'https://www.bilibili.com/video/av123'
        for aid, canonical in [(None, url), (123, 'https://www.bilibili.com/video/BV1target')]:
            data = {'bvid': 'BV1target', 'owner': {'name': 'Target'}}
            if aid is not None:
                data['aid'] = aid
            result, _, _ = self.preview(url, '<script>window.__INITIAL_STATE__=' + json.dumps({'videoData': data}) + '</script>')
            self.assertEqual(result.canonical_url, canonical)
            self.assertEqual(result.metadata.author.value, 'Target' if aid else '')

    def test_douyin_head_fallback_does_not_change_identity_guard(self):
        url = 'https://www.douyin.com/video/7351234567890123456'
        for markup, expected in [('<meta itemprop="author" content="Target">', 'Target'), ('<body><meta name="author" content="Wrong">', '')]:
            result, _, _ = self.preview(url, markup)
            self.assertEqual(result.metadata.author.value, expected)
        payload = {'aweme_id': '7351234567890123456', 'desc': 'Target title'}
        result, _, _ = self.preview(url, '<meta name="author" content="Wrong"><script type="application/json">' + json.dumps(payload) + '</script>')
        self.assertEqual(result.metadata.author.value, '')
