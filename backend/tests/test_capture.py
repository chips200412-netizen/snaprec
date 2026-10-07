from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.adapters.base import ResolvedVideo, VideoMetadata
from backend.app.adapters.douyin import DouyinAdapter, SHARE_METADATA_WARNING
from backend.app.api.main import create_app
from backend.app.domain.models import CollectionPreviewRequest, CollectionItemCreateRequest
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.capture import CaptureService
from backend.app.services.collections import CollectionService
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import DeterministicFullExtractor, UnconfiguredAsrProvider
from backend.app.services.safe_http import SafeFetchResult, SafeHttpError


class _Validator:
    def resolve(self, url):
        return object()


class FakeFetcher:
    def __init__(
        self,
        *,
        html: str = "",
        failure: str | None = None,
        redirects: tuple[str, ...] = (),
    ):
        self.html = html
        self.failure = failure
        self.redirects = redirects
        self.calls: list[str] = []
        self.url_validator = _Validator()

    def fetch(self, url):
        self.calls.append(url)
        if self.failure:
            raise SafeHttpError(self.failure)
        return SafeFetchResult(
            original_url=url,
            final_url="https://example.com/final?a=1#ignored",
            status_code=200,
            media_type="text/html",
            body=self.html.encode("utf-8"),
            redirects=self.redirects,
            content_type="text/html; charset=utf-8",
        )


class CaptureApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "capture.sqlite3")
        self.fetcher = FakeFetcher(html="""
            <html><head><title>页面标题</title>
            <meta name="author" content="页面作者">
            <meta name="description" content="页面说明">
            <meta name="keywords" content="设计, 灵感">
            </head></html>
        """)
        pipeline = LocalFullPipeline(
            self.repo, UnconfiguredAsrProvider(), DeterministicFullExtractor()
        )
        self.client = TestClient(create_app(
            pipeline, capture_fetcher=self.fetcher, upload_root=self.root / "uploads"
        ))

    def tearDown(self):
        self.temp.cleanup()

    def test_single_link_gate_and_generic_page_metadata_preserve_source(self):
        self.assertEqual(self.client.post(
            "/api/v1/collection-previews", json={"input_text": "没有链接"}
        ).json()["error"]["code"], "CAPTURE_LINK_REQUIRED")
        ambiguous = self.client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://a.example/x https://b.example/y"},
        )
        self.assertEqual(ambiguous.status_code, 400)
        self.assertEqual(ambiguous.json()["error"]["code"], "CAPTURE_LINK_AMBIGUOUS")
        for joined in (
            "https://a.example/x,https://b.example/y",
            "https://a.example/x，https://b.example/y",
            "看 https://a.example/x。另见https://b.example/y",
        ):
            with self.subTest(joined=joined):
                response = self.client.post(
                    "/api/v1/collection-previews", json={"input_text": joined}
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "CAPTURE_LINK_AMBIGUOUS"
                )

        raw = "https://Example.com:443/watch?a=1#Part"
        response = self.client.post(
            "/api/v1/collection-previews", json={"input_text": f"分享 {raw}。"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        preview = response.json()
        self.assertEqual(preview["source_url"], raw)
        self.assertEqual(preview["identity_url"], "https://example.com/final?a=1")
        self.assertEqual(preview["metadata_status"], "generic")
        self.assertEqual(preview["metadata"]["title"]["source"], "page_metadata")
        self.assertEqual(preview["metadata"]["author"]["source"], "page_metadata")
        self.assertEqual(preview["metadata"]["platform_tags"][0]["source"], "page_metadata")

    def test_fresh_alias_cache_zero_fetch_and_refresh_refetches(self):
        self.fetcher.redirects = ("https://short.example/middle",)
        body = {"input_text": "https://Example.com:443/watch?a=1#first"}
        first = self.client.post("/api/v1/collection-previews", json=body)
        second = self.client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://example.com/watch?a=1#second"},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(len(self.fetcher.calls), 1)
        middle = self.client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://short.example/middle"},
        )
        self.assertEqual(middle.status_code, 201, middle.text)
        self.assertEqual(len(self.fetcher.calls), 1)
        refreshed = self.client.post(
            "/api/v1/collection-previews",
            json={**body, "refresh_metadata": True},
        )
        self.assertEqual(refreshed.status_code, 201)
        self.assertEqual(len(self.fetcher.calls), 2)
        self.assertNotEqual(first.json()["preview_id"], second.json()["preview_id"])

    def test_fetch_failure_falls_back_and_preview_snapshot_controls_save(self):
        self.fetcher.failure = "UNSAFE_ADDRESS"
        preview = self.client.post(
            "/api/v1/collection-previews",
            json={"input_text": "https://public.example/item"},
        ).json()
        self.assertEqual(preview["metadata_status"], "metadata_unavailable")
        self.assertEqual(preview["canonical_url"], "")
        self.assertTrue(preview["metadata"]["warnings"])
        forged = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "forged"},
            json={"preview_id": preview["preview_id"], "user_title": "保留", "source_url": "https://evil.example"},
        )
        self.assertEqual(forged.status_code, 422)
        saved = self.client.post(
            "/api/v1/collection-items",
            headers={"Idempotency-Key": "saved"},
            json={"preview_id": preview["preview_id"], "user_title": "保留"},
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["source_url"], "https://public.example/item")
        self.assertEqual(saved.json()["canonical_url"], "")


class _KnownAdapter:
    def __init__(self):
        self.metadata_calls = 0
        self.subtitle_calls = 0
        self.media_calls = 0

    def resolve(self, url):
        return ResolvedVideo(
            platform="bilibili", source_url=url,
            canonical_url="https://www.bilibili.com/video/BV1known",
            video_id="BV1known", aliases=(url,),
        )

    def get_metadata(self, video):
        self.metadata_calls += 1
        return VideoMetadata(title="平台标题", author="作者", tags=["公开标签"])

    def get_share_metadata(self, input_text):
        return VideoMetadata(description="分享说明")

    def get_subtitles(self, video):
        self.subtitle_calls += 1
        raise AssertionError("capture must not read subtitles")

    def get_media(self, video):
        self.media_calls += 1
        raise AssertionError("capture must not read media")


class _FailedKnownAdapter(_KnownAdapter):
    def resolve(self, url):
        raise RuntimeError("platform allowlist rejected target")


class _ShareOnlyAdapter(_KnownAdapter):
    def get_metadata(self, video):
        self.metadata_calls += 1
        return VideoMetadata()

    def get_share_metadata(self, input_text):
        return VideoMetadata(description="仅分享文案")


class _WhitespacePublicAdapter(_ShareOnlyAdapter):
    def get_metadata(self, video):
        self.metadata_calls += 1
        return VideoMetadata(title="   ", tags=["  "])


class _UnsafeCoverOnlyAdapter(_ShareOnlyAdapter):
    def get_metadata(self, video):
        self.metadata_calls += 1
        return VideoMetadata(cover_url="javascript:alert(1)")


class _DouyinShareAdapter(_ShareOnlyAdapter):
    def resolve(self, url):
        return ResolvedVideo(
            platform="douyin",
            source_url=url,
            canonical_url="https://www.douyin.com/video/7351234567890123456",
            video_id="7351234567890123456",
            aliases=(url,),
        )

    def get_share_metadata(self, input_text):
        return DouyinAdapter.get_share_metadata(self, input_text)


class _DouyinPublicShareAdapter(_DouyinShareAdapter):
    def get_metadata(self, video):
        self.metadata_calls += 1
        return VideoMetadata(
            title="公开标题",
            author="公开作者",
            description="公开描述",
            tags=["公开标签"],
            warnings=["公共元信息警告。"],
        )


class _Registry:
    def __init__(self, adapter): self.adapter = adapter
    def matching(self, url): return self.adapter


class CaptureServiceTests(unittest.TestCase):
    def test_dy_fix_share_description_survives_save_and_public_author_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "dy-fix-save.sqlite3")
            capture = CaptureService(repo, fetcher=FakeFetcher(), adapter_registry=_Registry(_DouyinShareAdapter()))
            raw = "9.76 NWm:/ :6pm W@Z.mD 11/11 网站部署上线来喽 @抖音科技 #网站上线 https://v.douyin.com/Hr4YRKf_zOg/ 复制此链接，打开Dou音搜索，直接观看视频！"
            preview = capture.preview(CollectionPreviewRequest(input_text=raw))
            self.assertEqual(preview.metadata.author.value, "")
            self.assertEqual(preview.metadata.title.value, "网站部署上线来喽")
            self.assertEqual(preview.metadata.source_copy.value, "网站部署上线来喽 @抖音科技 #网站上线")
            saved = CollectionService(repo).create(CollectionItemCreateRequest(
                preview_id=preview.preview_id,
                user_title="用户自己的标题",
                personal_tags=["用户标签"],
            ), "dy-fix-save")
            reloaded = CollectionService(repo).get(saved.id)
            self.assertEqual(reloaded.metadata.source_copy, preview.metadata.source_copy)
            self.assertEqual(reloaded.metadata.author.source, "none")
            self.assertEqual(reloaded.original_input, raw)
            self.assertEqual(reloaded.display_title, "用户自己的标题")
            self.assertEqual(reloaded.personal_tags, ["用户标签"])
            public_capture = CaptureService(repo, fetcher=FakeFetcher(), adapter_registry=_Registry(_DouyinPublicShareAdapter()))
            refreshed = public_capture.preview(CollectionPreviewRequest(input_text=raw, refresh_metadata=True))
            self.assertEqual(refreshed.metadata.author.value, "公开作者")
            self.assertEqual(refreshed.metadata.source_copy.value, "公开描述")
            self.assertEqual(refreshed.metadata.source_copy.source, "platform_description")
            # Refreshing a preview must never silently repair or overwrite a saved snapshot.
            unchanged = CollectionService(repo).get(saved.id)
            self.assertEqual(unchanged, reloaded)

    def test_known_platform_is_recognized_without_deep_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "known.sqlite3")
            adapter = _KnownAdapter()
            service = CaptureService(
                repo, fetcher=FakeFetcher(), adapter_registry=_Registry(adapter)
            )
            preview = service.preview(CollectionPreviewRequest(
                input_text="看看 https://www.bilibili.com/video/BV1known"
            ))
        self.assertEqual(preview.metadata_status, "recognized")
        self.assertEqual(preview.platform, "bilibili")
        self.assertEqual(preview.metadata.title.source, "platform_public")
        self.assertEqual(preview.metadata.source_copy.source, "share_text")
        self.assertEqual(adapter.metadata_calls, 1)
        self.assertEqual(adapter.subtitle_calls, 0)
        self.assertEqual(adapter.media_calls, 0)

    def test_failed_known_adapter_does_not_fall_through_to_unrestricted_fetch(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "failed-known.sqlite3")
            adapter = _FailedKnownAdapter()
            fetcher = FakeFetcher(html="<title>不应读取</title>")
            service = CaptureService(
                repo, fetcher=fetcher, adapter_registry=_Registry(adapter)
            )
            preview = service.preview(CollectionPreviewRequest(
                input_text="https://b23.tv/unsafe"
            ))
        self.assertEqual(preview.metadata_status, "metadata_unavailable")
        self.assertEqual(preview.canonical_url, "")
        self.assertEqual(fetcher.calls, [])

    def test_share_text_alone_does_not_claim_platform_recognition(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "share-only.sqlite3")
            service = CaptureService(
                repo,
                fetcher=FakeFetcher(),
                adapter_registry=_Registry(_ShareOnlyAdapter()),
            )
            preview = service.preview(CollectionPreviewRequest(
                input_text="看看 https://www.bilibili.com/video/BV1share"
            ))
        self.assertEqual(preview.metadata_status, "metadata_unavailable")
        self.assertEqual(preview.metadata.source_copy.source, "share_text")

    def test_whitespace_platform_fields_do_not_claim_recognition(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "whitespace-public.sqlite3")
            service = CaptureService(
                repo,
                fetcher=FakeFetcher(),
                adapter_registry=_Registry(_WhitespacePublicAdapter()),
            )
            preview = service.preview(CollectionPreviewRequest(
                input_text="看看 https://www.bilibili.com/video/BV1blank"
            ))
        self.assertEqual(preview.metadata_status, "metadata_unavailable")

    def test_rejected_cover_does_not_claim_recognition_or_block_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "unsafe-cover.sqlite3")

            class RejectingValidator:
                def resolve(self, url):
                    raise SafeHttpError("UNSAFE_URL")

            fetcher = FakeFetcher()
            fetcher.url_validator = RejectingValidator()
            service = CaptureService(
                repo,
                fetcher=fetcher,
                adapter_registry=_Registry(_UnsafeCoverOnlyAdapter()),
            )
            preview = service.preview(CollectionPreviewRequest(
                input_text="https://www.bilibili.com/video/BV1cover"
            ))
        self.assertEqual(preview.metadata_status, "metadata_unavailable")
        self.assertEqual(preview.metadata.cover_url.value, "")
        self.assertTrue(preview.metadata.warnings)

    def test_douyin_share_metadata_is_per_preview_and_never_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "douyin-share-cache.sqlite3")
            adapter = _DouyinShareAdapter()
            service = CaptureService(
                repo,
                fetcher=FakeFetcher(),
                adapter_registry=_Registry(adapter),
            )
            url = "https://v.douyin.com/share-alias/"
            canonical = "https://www.douyin.com/video/7351234567890123456"
            intermediate = "https://www.douyin.com/share/middle"
            first = service.preview(CollectionPreviewRequest(
                input_text=f"9.76 NWm:/ 标题甲 #标签甲 @作者甲 {url}"
            ))
            clean_cache = repo.get_collection_metadata_cache(url)
            legacy_cache = {**clean_cache, "metadata": {
                **clean_cache["metadata"],
                "title": {
                    "value": "标题甲", "source": "share_text",
                    "fetched_at": clean_cache["fetched_at"],
                },
                "author": {
                    "value": "作者甲", "source": "share_text",
                    "fetched_at": clean_cache["fetched_at"],
                },
                "platform_tags": [{"value": "标签甲", "source": "share_text"}],
                "warnings": [
                    "公共元信息警告。",
                    "部分元信息来自你粘贴的抖音分享文案，仅用于整理，不作为视频内容证据。",
                ],
            }}
            repo.upsert_collection_metadata_cache(
                legacy_cache, [url, canonical, intermediate]
            )
            with patch.object(
                repo,
                "upsert_collection_metadata_cache",
                wraps=repo.upsert_collection_metadata_cache,
            ) as cache_writer:
                second = service.preview(CollectionPreviewRequest(
                    input_text=f"8.21 ABC:/ 标题乙 #标签乙 @作者乙 {canonical}"
                ))
                plain = service.preview(CollectionPreviewRequest(
                    input_text=intermediate
                ))
                cache_writer.assert_not_called()
            cached = repo.get_collection_metadata_cache(canonical)
            first_snapshot = repo.get_collection_preview(first.preview_id)

        self.assertEqual(adapter.metadata_calls, 1)
        self.assertEqual(clean_cache["metadata"]["title"]["source"], "none")
        self.assertEqual(clean_cache["metadata"]["author"]["source"], "none")
        self.assertEqual(clean_cache["metadata"]["platform_tags"], [])
        self.assertEqual(first.metadata.title.value, "标题甲")
        self.assertEqual(first.metadata.author.value, "")
        self.assertEqual(first.metadata.source_copy.value, "标题甲 #标签甲 @作者甲")
        self.assertEqual(first.metadata.source_copy.source, "share_text")
        self.assertEqual(clean_cache["metadata"]["source_copy"]["source"], "none")
        self.assertEqual(
            [tag.value for tag in first.metadata.platform_tags], ["标签甲"]
        )
        self.assertEqual(second.metadata.title.value, "标题乙")
        self.assertEqual(second.metadata.author.value, "")
        self.assertEqual(second.metadata.source_copy.value, "标题乙 #标签乙 @作者乙")
        self.assertEqual(second.metadata.source_copy.source, "share_text")
        self.assertEqual(
            [tag.value for tag in second.metadata.platform_tags], ["标签乙"]
        )
        self.assertEqual(plain.metadata.title.value, "")
        self.assertEqual(plain.metadata.author.value, "")
        self.assertEqual(plain.metadata.source_copy.value, "")
        self.assertEqual(plain.metadata.platform_tags, [])
        self.assertIsNotNone(cached)
        self.assertEqual(cached["metadata"]["title"]["source"], "share_text")
        self.assertEqual(cached["metadata"]["author"]["source"], "share_text")
        self.assertEqual(cached["metadata"]["platform_tags"][0]["source"], "share_text")
        self.assertIn("公共元信息警告。", cached["metadata"]["warnings"])
        self.assertEqual(first_snapshot["metadata"]["title"]["value"], "标题甲")
        self.assertEqual(first_snapshot["metadata"]["source_copy"]["value"], "标题甲 #标签甲 @作者甲")
        self.assertEqual(first.organization_suggestion.status, "insufficient_metadata")
        self.assertEqual(second.organization_suggestion.status, "insufficient_metadata")

    def test_stale_legacy_reader_cannot_overwrite_a_concurrent_refresh(self):
        class RefreshRaceRepository(SQLiteRepository):
            def __init__(self, path):
                super().__init__(path)
                self.replacement = None
                self.replacement_aliases = []
                self.service_writes = 0

            def arm_refresh(self, replacement, aliases):
                self.replacement = replacement
                self.replacement_aliases = aliases

            def get_collection_metadata_cache(self, alias_url):
                stale_snapshot = super().get_collection_metadata_cache(alias_url)
                if self.replacement is not None:
                    replacement = self.replacement
                    aliases = self.replacement_aliases
                    self.replacement = None
                    super().upsert_collection_metadata_cache(replacement, aliases)
                return stale_snapshot

            def upsert_collection_metadata_cache(self, payload, aliases):
                self.service_writes += 1
                return super().upsert_collection_metadata_cache(payload, aliases)

        with tempfile.TemporaryDirectory() as temp:
            repo = RefreshRaceRepository(Path(temp) / "douyin-refresh-race.sqlite3")
            adapter = _DouyinShareAdapter()
            service = CaptureService(
                repo,
                fetcher=FakeFetcher(),
                adapter_registry=_Registry(adapter),
            )
            alias = "https://v.douyin.com/race-alias/"
            canonical = "https://www.douyin.com/video/7351234567890123456"
            intermediate = "https://www.douyin.com/share/race-middle"
            service.preview(CollectionPreviewRequest(
                input_text=f"9.76 NWm:/ 旧标题 #旧标签 @旧作者 {alias}"
            ))
            clean = repo.get_collection_metadata_cache(canonical)
            legacy = json.loads(json.dumps(clean, ensure_ascii=False))
            legacy["metadata"]["title"] = {
                "value": "旧标题",
                "source": "share_text",
                "fetched_at": clean["fetched_at"],
            }
            repo.upsert_collection_metadata_cache(
                legacy, [alias, canonical, intermediate]
            )
            refreshed = json.loads(json.dumps(clean, ensure_ascii=False))
            refreshed["metadata"]["title"] = {
                "value": "并发刷新公开标题",
                "source": "platform_public",
                "fetched_at": clean["fetched_at"],
            }
            writes_before_reader = repo.service_writes
            repo.arm_refresh(refreshed, [alias, canonical, intermediate])

            preview = service.preview(CollectionPreviewRequest(
                input_text=f"8.21 ABC:/ 当前标题 #当前标签 @当前作者 {intermediate}"
            ))
            stored = repo.get_collection_metadata_cache(alias)

        self.assertEqual(adapter.metadata_calls, 1)
        self.assertEqual(repo.service_writes, writes_before_reader)
        self.assertEqual(preview.metadata.title.value, "当前标题")
        self.assertEqual(preview.metadata.title.source, "share_text")
        self.assertEqual(stored["metadata"]["title"]["value"], "并发刷新公开标题")
        self.assertEqual(stored["metadata"]["title"]["source"], "platform_public")

    def test_warning_only_legacy_cache_is_scrubbed_per_preview_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = SQLiteRepository(Path(temp) / "douyin-warning-only.sqlite3")
            adapter = _DouyinPublicShareAdapter()
            service = CaptureService(
                repo,
                fetcher=FakeFetcher(),
                adapter_registry=_Registry(adapter),
            )
            alias = "https://v.douyin.com/warning-alias/"
            canonical = "https://www.douyin.com/video/7351234567890123456"
            intermediate = "https://www.douyin.com/share/warning-middle"
            original = service.preview(CollectionPreviewRequest(input_text=alias))
            clean = repo.get_collection_metadata_cache(canonical)
            legacy = json.loads(json.dumps(clean, ensure_ascii=False))
            legacy["metadata"]["warnings"] = [
                "公共元信息警告。",
                SHARE_METADATA_WARNING,
            ]
            repo.upsert_collection_metadata_cache(
                legacy, [alias, canonical, intermediate]
            )
            with patch.object(
                repo,
                "upsert_collection_metadata_cache",
                wraps=repo.upsert_collection_metadata_cache,
            ) as cache_writer:
                previews = [
                    service.preview(CollectionPreviewRequest(input_text=value))
                    for value in (alias, canonical, intermediate)
                ]
                cache_writer.assert_not_called()
            stored = repo.get_collection_metadata_cache(intermediate)
            original_snapshot = repo.get_collection_preview(original.preview_id)

        self.assertEqual(adapter.metadata_calls, 1)
        for preview in previews:
            self.assertEqual(preview.metadata.title.value, "公开标题")
            self.assertEqual(preview.metadata.title.source, "platform_public")
            self.assertEqual(preview.metadata.warnings, ["公共元信息警告。"])
        self.assertEqual(
            stored["metadata"]["warnings"],
            ["公共元信息警告。", SHARE_METADATA_WARNING],
        )
        self.assertEqual(
            original_snapshot["metadata"]["warnings"], ["公共元信息警告。"]
        )

    def test_v11_to_v12_failure_rolls_back_and_retry_is_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "migration.sqlite3"
            SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute("DROP TABLE collection_previews")
                db.execute("DROP TABLE collection_metadata_cache_aliases")
                db.execute("DROP TABLE collection_metadata_cache")
                db.execute("PRAGMA user_version=11")
                db.commit()

            def fail(db):
                db.execute("CREATE TABLE collection_previews(id TEXT PRIMARY KEY)")
                raise RuntimeError("injected v12 failure")

            with patch.object(SQLiteRepository, "_migrate_v11_to_v12", staticmethod(fail)):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 11)
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_previews'"
                ).fetchone())
            SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
