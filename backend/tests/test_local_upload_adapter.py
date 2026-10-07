from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.app.adapters import (
    AdapterRegistry,
    BilibiliAdapter,
    DouyinAdapter,
    LocalUploadAdapter,
)
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    SidecarSubtitleProvider,
    UnconfiguredAsrProvider,
)


class LocalUploadAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "notes.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_registry_selects_exactly_three_platform_adapters(self):
        local = LocalUploadAdapter(self.repo)
        registry = AdapterRegistry(
            [BilibiliAdapter(), DouyinAdapter(), local]
        )
        local_url = f"local://sha256/{'a' * 64}"
        cases = {
            "https://www.bilibili.com/video/BV1abc123": BilibiliAdapter,
            "https://www.douyin.com/video/1234567890123456789": DouyinAdapter,
            local_url: LocalUploadAdapter,
        }
        for url, expected in cases.items():
            self.assertIsInstance(registry.matching(url), expected)
        self.assertIsNone(registry.matching("https://example.com/video/1"))
        self.assertIsNone(registry.matching("file:///private/video.mp4"))
        self.assertIsNone(registry.matching("local://filename.mp4"))

    def test_all_adapters_expose_unified_contract(self):
        for adapter in (
            BilibiliAdapter(),
            DouyinAdapter(),
            LocalUploadAdapter(self.repo),
        ):
            for method in (
                "match", "resolve", "get_metadata",
                "get_subtitles", "get_media",
            ):
                self.assertTrue(callable(getattr(adapter, method, None)))

    def test_upload_pipeline_result_is_readable_through_local_adapter(self):
        media = self.root / "lesson.mp4"
        media.write_bytes(b"safe fixture")
        Path(f"{media}.txt").write_text(
            "0|2|第一步准备100元。\n2|4|没有授权不要继续。",
            encoding="utf-8",
        )
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
            temp_root=self.root / "tmp",
            subtitle_provider=SidecarSubtitleProvider(),
        )
        _, result, _ = pipeline.process(media)
        adapter = LocalUploadAdapter(self.repo)
        resolved = adapter.resolve(result.canonical_url)
        metadata = adapter.get_metadata(resolved)
        subtitles = adapter.get_subtitles(resolved)
        self.assertEqual(resolved.video_id, result.video_id)
        self.assertEqual(metadata.title, "lesson")
        self.assertEqual(subtitles.source, "user_upload")
        self.assertEqual(
            [item.text for item in subtitles.segments],
            [item.text for item in result.segments],
        )
        self.assertIsNone(adapter.get_media(resolved))


if __name__ == "__main__":
    unittest.main()
