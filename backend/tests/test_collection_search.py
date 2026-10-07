from __future__ import annotations

import sqlite3
import tempfile
import unittest
import asyncio
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app.api.main import QueryStringAccessLogRedactionMiddleware, create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.pipeline import LocalFullPipeline
from backend.app.services.providers import (
    DeterministicFullExtractor,
    UnconfiguredAsrProvider,
)


def collection_graph(
    *,
    slug: str,
    title: str,
    platform: str,
    author: str,
    source_copy: str,
    primary: str,
    secondary: str,
    platform_tags: list[str],
    organization_tags: list[str],
    personal_tags: list[str],
    inspiration: str,
) -> dict:
    source_url = f"https://example.com/{slug}"
    return {
        "source_kind": "video" if platform != "web" else "article",
        "platform": platform,
        "original_input": f"分享文本 {source_url}",
        "source_url": source_url,
        "canonical_url": source_url,
        "metadata_status": "generic",
        "user_title": title,
        "untitled_confirmed": False,
        "metadata": {
            "title": {
                "value": f"来源标题 {slug}",
                "source": "open_graph",
                "fetched_at": "2026-08-20T00:00:00Z",
            },
            "author": {
                "value": author,
                "source": "open_graph",
                "fetched_at": "2026-08-20T00:00:00Z",
            },
            "cover_url": {"value": "", "source": "none", "fetched_at": ""},
            "source_copy": {
                "value": source_copy,
                "source": "page_description",
                "fetched_at": "2026-08-20T00:00:00Z",
            },
            "platform_tags": [
                {"value": value, "source": "page_metadata"}
                for value in platform_tags
            ],
            "warnings": [],
        },
        "organization_suggestion": {
            "primary_category": "",
            "secondary_category": "",
            "tags": [],
            "basis": "public_metadata",
            "method": "deterministic",
            "status": "insufficient_metadata",
        },
        "organization_confirmation": {
            "primary_category": primary,
            "secondary_category": secondary,
            "organization_tags": organization_tags,
        },
        "personal_tags": personal_tags,
        "inspiration": {
            "content": inspiration,
            "input_mode": "text",
            "transcription_status": "not_applicable",
        }
        if inspiration
        else None,
    }


class CollectionSearchApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "collections.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        pipeline = LocalFullPipeline(
            self.repo,
            UnconfiguredAsrProvider(),
            DeterministicFullExtractor(),
        )
        self.client = TestClient(create_app(pipeline, upload_root=root / "uploads"))

    def tearDown(self):
        self.temp.cleanup()

    def _save(self, payload: dict, position: int) -> dict:
        item = self.repo.create_collection_item(
            payload,
            f"search-key-{position}",
            f"search-hash-{position}",
        )
        timestamp = f"2026-08-{20 + position:02d}T00:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE library_items SET created_at=?, updated_at=? WHERE id=?",
                (timestamp, timestamp, item["id"]),
            )
            db.commit()
        return item

    def _seed_three(self) -> list[dict]:
        return [
            self._save(
                collection_graph(
                    slug="motion",
                    title="自然光转场",
                    platform="youtube",
                    author="林岚",
                    source_copy="用窗帘控制光影节奏",
                    primary="设计",
                    secondary="动效",
                    platform_tags=["教程"],
                    organization_tags=["节奏"],
                    personal_tags=["项目参考"],
                    inspiration="下次片头试试缓慢推近",
                ),
                1,
            ),
            self._save(
                collection_graph(
                    slug="travel",
                    title="海边散步",
                    platform="xiaohongshu",
                    author="阿岑",
                    source_copy="沿海步道公开攻略",
                    primary="旅行",
                    secondary="路线",
                    platform_tags=["攻略"],
                    organization_tags=["周末"],
                    personal_tags=["夏天"],
                    inspiration="带父母走短线",
                ),
                2,
            ),
            self._save(
                collection_graph(
                    slug="book",
                    title="品牌书摘",
                    platform="web",
                    author="编辑部",
                    source_copy="关于零售空间的文章",
                    primary="书籍",
                    secondary="品牌",
                    platform_tags=["专栏"],
                    organization_tags=["案例"],
                    personal_tags=["项目参考"],
                    inspiration="给提案补一个反例",
                ),
                3,
            ),
        ]

    def test_authoritative_list_is_stably_sorted_cursor_paginated_and_has_facets(self):
        seeded = self._seed_three()
        first = self.client.get("/api/v1/collection-items?limit=2")
        self.assertEqual(first.status_code, 200, first.text)
        page = first.json()
        self.assertEqual(page["total"], 3)
        self.assertEqual([item["id"] for item in page["items"]], [seeded[2]["id"], seeded[1]["id"]])
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(
            {facet["primary_category"] for facet in page["facets"]["categories"]},
            {"设计", "旅行", "书籍"},
        )
        self.assertIn(
            {"name": "项目参考", "source": "personal", "count": 2},
            page["facets"]["tags"],
        )

        second = self.client.get(
            "/api/v1/collection-items",
            params={"limit": 2, "cursor": page["next_cursor"]},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([item["id"] for item in second.json()["items"]], [seeded[0]["id"]])
        self.assertIsNone(second.json()["next_cursor"])

    def test_keyword_and_platform_category_and_sourced_tag_filters_combine(self):
        seeded = self._seed_three()
        response = self.client.get(
            "/api/v1/collection-items",
            params={
                "query": "缓慢推近",
                "platform": "youtube",
                "primary_category": "设计",
                "secondary_category": "动效",
                "tag": "项目参考",
                "tag_source": "personal",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()["items"]], [seeded[0]["id"]])

        for query, expected_total in (
            ("林岚", 1),
            ("窗帘", 1),
            ("教程", 1),
            ("节奏", 1),
            ("项目参考", 2),
            ("motion", 1),
        ):
            matched = self.client.get("/api/v1/collection-items", params={"query": query})
            self.assertEqual(matched.status_code, 200, matched.text)
            self.assertEqual(matched.json()["total"], expected_total, query)
            self.assertIn(seeded[0]["id"], [item["id"] for item in matched.json()["items"]], query)

        for query in ("自然光转场 林岚", "youtube"):
            excluded = self.client.get(
                "/api/v1/collection-items", params={"query": query}
            )
            self.assertEqual(excluded.status_code, 200, excluded.text)
            self.assertEqual(excluded.json()["total"], 0, query)

    def test_equal_update_times_use_id_tiebreak_without_page_duplicates(self):
        seeded = self._seed_three()
        tied_ids = sorted([seeded[0]["id"], seeded[1]["id"]])
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE library_items SET updated_at=? WHERE id IN (?, ?)",
                ("2026-08-24T00:00:00+00:00", *tied_ids),
            )
            db.commit()

        first = self.client.get("/api/v1/collection-items", params={"limit": 1})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual([item["id"] for item in first.json()["items"]], tied_ids[:1])

        second = self.client.get(
            "/api/v1/collection-items",
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual([item["id"] for item in second.json()["items"]], tied_ids[1:])

    def test_search_excludes_deep_analysis_content_and_rejects_cross_query_cursor(self):
        seeded = self._seed_three()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute(
                "UPDATE library_items SET deep_analysis_resource_key=? WHERE id=?",
                ("secret-transcript-evidence", seeded[0]["id"]),
            )
            db.commit()
        excluded = self.client.get(
            "/api/v1/collection-items", params={"query": "secret-transcript-evidence"}
        )
        self.assertEqual(excluded.status_code, 200, excluded.text)
        self.assertEqual(excluded.json()["total"], 0)

        first = self.client.get("/api/v1/collection-items", params={"limit": 1})
        invalid = self.client.get(
            "/api/v1/collection-items",
            params={"limit": 1, "query": "不同查询", "cursor": first.json()["next_cursor"]},
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertEqual(invalid.json()["error"]["code"], "COLLECTION_CURSOR_INVALID")

        malformed = self.client.get(
            "/api/v1/collection-items", params={"limit": 1, "cursor": "%%%"}
        )
        self.assertEqual(malformed.status_code, 400, malformed.text)
        self.assertEqual(
            malformed.json()["error"]["code"], "COLLECTION_CURSOR_INVALID"
        )

    def test_normalization_literal_search_self_excluding_facets_and_deep_boundary(self):
        self._seed_three()
        with patch.object(
            SQLiteRepository,
            "search_videos",
            side_effect=AssertionError("new collection search entered legacy deep search"),
        ):
            normalized = self.client.get(
                "/api/v1/collection-items", params={"query": "  ＭＯＴＩＯＮ  "}
            )
        self.assertEqual(normalized.status_code, 200, normalized.text)
        self.assertEqual(normalized.json()["total"], 1)

        for literal in ("%", "_", "\\"):
            response = self.client.get(
                "/api/v1/collection-items", params={"query": literal}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 0, literal)

        filtered = self.client.get(
            "/api/v1/collection-items",
            params={
                "platform": "youtube",
                "primary_category": "设计",
                "tag": "项目参考",
                "tag_source": "personal",
            },
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(
            {entry["platform"] for entry in filtered.json()["facets"]["platforms"]},
            {"youtube"},
        )
        self.assertEqual(
            {entry["primary_category"] for entry in filtered.json()["facets"]["categories"]},
            {"设计"},
        )
        self.assertIn(
            {"name": "教程", "source": "platform", "count": 1},
            filtered.json()["facets"]["tags"],
        )

        missing_source = self.client.get(
            "/api/v1/collection-items", params={"tag": "项目参考"}
        )
        self.assertEqual(missing_source.status_code, 400, missing_source.text)
        self.assertEqual(
            missing_source.json()["error"]["code"], "COLLECTION_FILTER_INVALID"
        )

    def test_access_log_scope_is_redacted_while_app_receives_search_query(self):
        received_query = None

        async def downstream(scope, receive, send):
            nonlocal received_query
            received_query = scope["query_string"]

        middleware = QueryStringAccessLogRedactionMiddleware(downstream)
        server_scope = {
            "type": "http",
            "query_string": "query=个人灵感".encode("utf-8"),
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        asyncio.run(middleware(server_scope, receive, send))
        self.assertEqual(received_query, "query=个人灵感".encode("utf-8"))
        self.assertEqual(server_scope["query_string"], b"")


if __name__ == "__main__":
    unittest.main()
