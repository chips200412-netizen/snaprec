"""Behavior-only R2 search regressions against the pre-index search contract.

The oracle intentionally imports no production search/normalization helpers. It
also remains usable by a synthetic benchmark: pass full collection graphs in
``updated_at DESC, id ASC`` order and ordinary repository search parameters.
"""

from __future__ import annotations

import json
import tempfile
import unicodedata
import unittest
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app.api.main import create_app
from backend.app.repositories.sqlite import SQLiteRepository
from backend.app.services.collections import CollectionService
from backend.app.services.pipeline import LocalFullPipeline, PipelineError
from backend.app.services.providers import (
    DeterministicAutoTagger,
    DeterministicFocusedExtractor,
    DeterministicFullExtractor,
    SidecarSubtitleProvider,
    UnconfiguredAsrProvider,
)
from backend.tests.test_collection_search import collection_graph


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _tag_values(graph: dict) -> dict[str, list[str]]:
    return {
        "platform": [entry["value"] for entry in graph["metadata"]["platform_tags"]],
        "organization": graph["organization_confirmation"]["organization_tags"],
        "personal": graph["personal_tags"],
    }


def legacy_search_oracle(
    graphs: list[dict],
    *,
    query: str = "",
    platform: str | None = None,
    primary_category: str = "",
    secondary_category: str = "",
    tag: str = "",
    tag_source: str | None = None,
    limit: int = 24,
    after: tuple[str, str] | None = None,
) -> dict:
    """Pure field-level literal search; no SQL, FTS, or production helpers.

    Inputs are already sorted full graphs (e.g. create/get responses). Facets
    retain the existing display-value category buckets and raw tag-occurrence
    counts; normalizing these buckets or deduplicating platform/organization
    tags would change the established response, even if matching stayed equal.
    """
    needle = _normalized(query)
    primary = _normalized(primary_category)
    secondary = _normalized(secondary_category)
    tag_key = _normalized(tag)
    keyword_rows: list[tuple[dict, dict[str, bool]]] = []
    for graph in graphs:
        metadata = graph["metadata"]
        confirmed = graph["organization_confirmation"]
        tags = _tag_values(graph)
        fields = [
            graph["display_title"],
            graph.get("user_author") or "",
            metadata["title"]["value"],
            metadata["author"]["value"],
            metadata["source_copy"]["value"],
            graph["source_url"],
            graph["canonical_url"],
            confirmed["primary_category"],
            confirmed["secondary_category"],
            *(value for values in tags.values() for value in values),
            (graph["inspiration"] or {}).get("content", ""),
        ]
        if needle and not any(needle in _normalized(field) for field in fields):
            continue
        conditions = {
            "platform": not platform or graph["platform"] == platform,
            "category": (
                (not primary or _normalized(confirmed["primary_category"]) == primary)
                and (
                    not secondary
                    or _normalized(confirmed["secondary_category"]) == secondary
                )
            ),
            "tag": not tag_key
            or any(_normalized(value) == tag_key for value in tags.get(tag_source, [])),
        }
        keyword_rows.append((graph, conditions))

    matches = [graph for graph, checks in keyword_rows if all(checks.values())]
    total = len(matches)
    if after is not None:
        matches = [
            graph
            for graph in matches
            if graph["updated_at"] < after[0]
            or (graph["updated_at"] == after[0] and graph["id"] > after[1])
        ]

    platform_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    child_counts: dict[str, Counter[str]] = defaultdict(Counter)
    tag_counts: Counter[tuple[str, str]] = Counter()
    tag_displays: dict[tuple[str, str], str] = {}
    for graph, checks in keyword_rows:
        if checks["category"] and checks["tag"]:
            platform_counts[graph["platform"]] += 1
        if checks["platform"] and checks["tag"]:
            confirmed = graph["organization_confirmation"]
            parent_name = confirmed["primary_category"].strip()
            child_name = confirmed["secondary_category"].strip()
            if parent_name:
                category_counts[parent_name] += 1
                if child_name:
                    child_counts[parent_name][child_name] += 1
        if checks["platform"] and checks["category"]:
            for source, values in _tag_values(graph).items():
                for value in values:
                    key = (source, _normalized(value))
                    tag_counts[key] += 1
                    tag_displays.setdefault(key, value)

    def sorted_categories(counts: Counter[str]) -> list[tuple[str, int]]:
        # Stable sorting is observable when distinct displays normalize equally.
        return sorted(counts.items(), key=lambda pair: (-pair[1], _normalized(pair[0])))

    return {
        "items": [
            {
                "id": graph["id"],
                "display_title": graph["display_title"],
                "platform": graph["platform"],
                "primary_category": graph["organization_confirmation"]["primary_category"],
                "cover_url": graph["metadata"]["cover_url"]["value"],
                "source_author": graph["metadata"]["author"]["value"],
                "user_author": graph.get("user_author"),
                "has_user_cover": bool(graph.get("user_cover_asset_id")),
                "created_at": graph["created_at"],
                "updated_at": graph["updated_at"],
            }
            for graph in matches[:limit]
        ],
        "total": total,
        "has_more": len(matches) > limit,
        "facets": {
            "platforms": [
                {"platform": name, "count": count}
                for name, count in sorted(
                    platform_counts.items(), key=lambda pair: (-pair[1], pair[0])
                )
            ],
            "categories": [
                {
                    "primary_category": name,
                    "count": count,
                    "children": [
                        {"secondary_category": child, "count": child_count}
                        for child, child_count in sorted_categories(child_counts[name])
                    ],
                }
                for name, count in sorted_categories(category_counts)
            ],
            "tags": [
                {"name": tag_displays[key], "source": key[0], "count": count}
                for key, count in sorted(
                    tag_counts.items(),
                    key=lambda pair: (-pair[1], pair[0][0], pair[0][1]),
                )
            ],
        },
    }


def _payload(slug: str, **overrides) -> dict:
    values = {
        "title": f"收藏 {slug}",
        "platform": "web",
        "author": "默认作者",
        "source_copy": "公开来源说明",
        "primary": "阅读",
        "secondary": "文章",
        "platform_tags": ["来源标签"],
        "organization_tags": ["整理标签"],
        "personal_tags": ["个人标签"],
        "inspiration": "",
    }
    values.update(overrides)
    return collection_graph(slug=slug, **values)


class CollectionSearchEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="search-equivalence-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "synthetic.sqlite3"
        self.repo = SQLiteRepository(self.db_path)
        self.service = CollectionService(self.repo)
        self.graphs: list[dict] = []
        self.external_calls = []
        for target in (
            "socket.create_connection",
            "socket.getaddrinfo",
            "httpx.HTTPTransport.handle_request",
            "httpx.AsyncHTTPTransport.handle_async_request",
            "subprocess.Popen",
        ):
            guard = patch(target, side_effect=AssertionError(f"external call: {target}"))
            self.external_calls.append(guard.start())
            self.addCleanup(guard.stop)

    def tearDown(self):
        for call in self.external_calls:
            call.assert_not_called()

    def _save(self, payload: dict, *, timestamp: datetime | None = None) -> dict:
        position = len(self.graphs)
        # Control only fixture time, so facet display ordering never depends on
        # Windows clock resolution; the save/search seams remain unmodified.
        with patch("backend.app.repositories.sqlite.datetime", wraps=datetime) as clock:
            clock.now.return_value = timestamp or (
                datetime(2026, 8, 26, tzinfo=UTC) + timedelta(seconds=position)
            )
            graph = self.repo.create_collection_item(
                payload, f"equivalence-key-{position}", f"equivalence-hash-{position}"
            )
        self.graphs.append(graph)
        self.graphs.sort(key=lambda item: item["id"])
        self.graphs.sort(key=lambda item: item["updated_at"], reverse=True)
        return graph

    def _seed_rich_library(self):
        fixtures = [
            _payload("chinese", title="星河计划", source_copy="远山夜色与中国设计"),
            _payload(
                "unicode", title="  ＭＯＴＩＯＮ\tＣａｆｅ\u0301\nStraße  ",
                author="ΟΔΥΣΣΕΎΣ", platform="youtube", primary="Design",
                secondary="Motion", source_copy="ﬃ　Ⓐ K ① 中文",
                platform_tags=["SHARED", "ｓｈａｒｅｄ"],
                organization_tags=["sHaReD", " Shared ", "镜头"],
                personal_tags=[" Shared ", "ＳＨＡＲＥＤ", "研究"],
            ),
            _payload(
                "unicode-peer", title="motion café STRASSE", platform="bilibili",
                primary="design", secondary="motion", platform_tags=["shared"],
                organization_tags=["SHARED"], personal_tags=["shared"],
            ),
            _payload(
                "punctuation", title='literal "quoted" 100% under_score path\\to\\item',
                source_copy="star*mark question?mark [bracket] (paren) :colon -minus",
                inspiration="AND OR NOT NEAR(foo bar)",
            ),
            _payload(
                "punctuation-decoy", title="literal unquoted 100X underXscore path/to/item",
                source_copy="starXmark questionXmark xbracket paren colon minus",
            ),
            _payload("nul", source_copy="before-null\x00needle-after-null 漢字後綴"),
            _payload(
                "emoji", title="🚀设计灵感🌙👩‍💻", author="设计师",
                source_copy="彩色☕️ 与 👨‍👩‍👧‍👦 组合",
            ),
            _payload("combining", source_copy="Cafe\u0301 a\u0308 naïve I\u0307 ß ς ＡＢＣ"),
            _payload("whitespace", source_copy="银河\t \n计划\u00a0第二\u3000阶段"),
            _payload(
                "cross", title="alpha", author="gamma", source_copy="delta",
                primary="epsilon", secondary="zeta", platform_tags=["theta", "iota"],
                organization_tags=["kappa", "lambda"], personal_tags=["mu", "nu"],
                inspiration="omicron",
            ),
        ]
        fixtures[-1]["metadata"]["title"]["value"] = "beta"
        for position in range(12):
            fixtures.append(_payload(
                f"background-{position:02d}", title=f"普通样本 {position}",
                platform=("web", "douyin", "youtube", "xiaohongshu")[position % 4],
                primary=("Design", "design", "设计", "阅读")[position % 4],
                secondary=("Motion", "motion", "动效")[position % 3],
                personal_tags=["shared" if position % 2 else "备用", f"分组{position % 3}"],
            ))
        for payload in fixtures:
            self._save(payload)

    def _assert_equivalent(self, *, after=None, **params) -> dict:
        expected = legacy_search_oracle(
            self.graphs, after=after,
            **{name: value for name, value in params.items() if name != "cursor"},
        )
        actual = self.service.search(**params).model_dump(mode="json")
        self.assertEqual(actual["items"], expected["items"], params)
        self.assertEqual(actual["total"], expected["total"], params)
        self.assertEqual(actual["facets"], expected["facets"], params)
        self.assertEqual(actual["next_cursor"] is not None, expected["has_more"], params)
        self.assertEqual(actual["limit"], params.get("limit", 24))
        return actual

    def test_literal_unicode_and_filter_matrix_matches_independent_oracle(self):
        self._seed_rich_library()
        queries = (
            "", " \t\n ", "example.com", "星", "星河", "星河计", "中国设计", "不存在",
            "🚀", "🌙", "👩‍💻", "☕️", "👨‍👩‍👧‍👦", "组合",
            "  motion  ", "ＭＯＴＩＯＮ　ＣＡＦÉ", "cafe\u0301", "CAFÉ", "STRASSE",
            "ffi", "①", "ä", "I\u0307", "σ", "ＡＢＣ", "银河  计划\n第二",
            '"', '"quoted"', "%", "_", "\\", "*", "?", "[", "]", "(paren)",
            "AND", "OR NOT", "NEAR(foo bar)", "before-null", "needle-after-null",
            "\x00", "null\x00nee", "漢字後綴", "alpha beta", "beta gamma",
            "theta iota", "kappa lambda", "mu nu", "epsilonzeta", "alpha gamma",
        )
        for query in queries:
            with self.subTest(query=query):
                self._assert_equivalent(query=query, limit=7)
        for query in ("", "example.com", "motion", "shared", "never-matches"):
            for source in ("platform", "organization", "personal"):
                for platform in (None, "web", "youtube"):
                    with self.subTest(query=query, source=source, platform=platform):
                        self._assert_equivalent(
                            query=query, platform=platform, primary_category=" ＤＥＳＩＧＮ ",
                            secondary_category="ＭＯＴＩＯＮ", tag=" ＳＨＡＲＥＤ ",
                            tag_source=source, limit=1,
                        )

    def test_every_searchable_field_matches_without_indexing_unconfirmed_content(self):
        routes = (
            ("user_title",), ("user_author",), ("metadata", "title", "value"),
            ("metadata", "author", "value"), ("metadata", "source_copy", "value"),
            ("organization_confirmation", "primary_category"),
            ("organization_confirmation", "secondary_category"),
            ("metadata", "platform_tags"),
            ("organization_confirmation", "organization_tags"),
            ("personal_tags",), ("inspiration",), ("source_url",), ("canonical_url",),
        )
        saved = []
        for position, route in enumerate(routes):
            payload = _payload(f"field-{position:02d}")
            marker = f"needlefield{position:02d}"
            value: object = marker
            if route[-1] == "platform_tags":
                value = [{"value": marker, "source": "page_metadata"}]
            elif route[-1] in {"organization_tags", "personal_tags"}:
                value = [marker]
            elif route[-1] == "inspiration":
                value = {
                    "content": marker, "input_mode": "voice",
                    "transcription_status": "completed",
                }
            elif route[-1] in {"source_url", "canonical_url"}:
                value = f"https://example.com/{marker}"
            target = payload
            for component in route[:-1]:
                target = target[component]
            target[route[-1]] = value
            saved.append(self._save(payload))

        for position, graph in enumerate(saved):
            with self.subTest(field=routes[position]):
                page = self._assert_equivalent(query=f"NEEDLEFIELD{position:02d}")
                self.assertEqual([item["id"] for item in page["items"]], [graph["id"]])
        self.assertEqual(self._assert_equivalent(query="needlefield")["total"], len(routes))

        excluded = _payload("excluded-content")
        excluded["original_input"] = "original-private-marker"
        excluded["organization_suggestion"].update({
            "primary_category": "unconfirmed-primary-marker",
            "secondary_category": "unconfirmed-secondary-marker",
            "tags": ["unconfirmed-tag-marker"], "status": "generated",
        })
        excluded["metadata"]["warnings"] = ["warning-only-marker"]
        excluded["metadata"]["cover_url"] = {
            "value": "https://example.com/cover-only-marker.jpg",
            "source": "open_graph", "fetched_at": "2026-08-20T00:00:00Z",
        }
        self._save(excluded)
        for marker in (
            "original-private-marker", "unconfirmed-primary-marker",
            "unconfirmed-secondary-marker", "unconfirmed-tag-marker",
            "warning-only-marker", "cover-only-marker",
        ):
            with self.subTest(excluded=marker):
                self.assertEqual(self._assert_equivalent(query=marker)["total"], 0)

    def test_literal_symbols_and_field_boundaries_have_explicit_expected_matches(self):
        self._seed_rich_library()
        punctuation = next(
            graph for graph in self.graphs if graph["source_url"].endswith("/punctuation")
        )
        for symbol in ('"quoted"', "%", "_", "\\", "*", "?", "[", "]"):
            with self.subTest(symbol=symbol):
                page = self._assert_equivalent(query=symbol)
                self.assertEqual([item["id"] for item in page["items"]], [punctuation["id"]])
        for query in (
            "alpha beta", "beta gamma", "gamma delta", "theta iota",
            "kappa lambda", "mu nu", "epsilon zeta", "alpha gamma",
            "alphabeta", "thetaiota", "mulambda", "underX_score",
        ):
            with self.subTest(boundary=query):
                self.assertEqual(self._assert_equivalent(query=query)["total"], 0)
        nul_graph = next(
            graph for graph in self.graphs if graph["source_url"].endswith("/nul")
        )
        for query in ("\x00", "null\x00nee", "needle-after-null", "漢字後綴"):
            with self.subTest(nul_query=query):
                page = self._assert_equivalent(query=query)
                self.assertEqual([item["id"] for item in page["items"]], [nul_graph["id"]])

    def test_tag_sources_occurrences_and_category_display_buckets_remain_distinct(self):
        first = self._save(_payload(
            "facet-a", platform="youtube", primary="Design", secondary="Motion",
            platform_tags=["Shared", "shared"], organization_tags=["SHARED", "ｓｈａｒｅｄ"],
            personal_tags=[" Shared ", "shared"],
        ))
        second = self._save(_payload(
            "facet-b", primary="design", secondary="motion", platform_tags=["sHaReD"],
            organization_tags=["Shared"], personal_tags=["SHARED"],
        ))
        third = self._save(_payload(
            "facet-c", platform="youtube", primary="Design", secondary="motion",
            platform_tags=["SHARED"], organization_tags=[], personal_tags=["other"],
        ))
        fourth = self._save(_payload(
            "facet-d", primary="Design", secondary="Still", platform_tags=["Other"],
            organization_tags=["Other"], personal_tags=["Shared"],
        ))
        page = self._assert_equivalent(primary_category=" ＤＥＳＩＧＮ ")
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["facets"]["categories"], [
            {
                "primary_category": "Design", "count": 3,
                "children": [
                    {"secondary_category": "motion", "count": 1},
                    {"secondary_category": "Motion", "count": 1},
                    {"secondary_category": "Still", "count": 1},
                ],
            },
            {
                "primary_category": "design", "count": 1,
                "children": [{"secondary_category": "motion", "count": 1}],
            },
        ])
        self.assertEqual(page["facets"]["tags"], [
            {"name": "SHARED", "source": "platform", "count": 4},
            {"name": "Shared", "source": "organization", "count": 3},
            {"name": "Shared", "source": "personal", "count": 3},
            {"name": "Other", "source": "organization", "count": 1},
            {"name": "other", "source": "personal", "count": 1},
            {"name": "Other", "source": "platform", "count": 1},
        ])
        expected_ids = {
            "platform": {first["id"], second["id"], third["id"]},
            "organization": {first["id"], second["id"]},
            "personal": {first["id"], second["id"], fourth["id"]},
        }
        for source, ids in expected_ids.items():
            with self.subTest(source=source):
                page = self._assert_equivalent(tag=" ＳＨＡＲＥＤ ", tag_source=source)
                self.assertEqual({item["id"] for item in page["items"]}, ids)

    def test_facets_exclude_only_their_own_group_and_precede_pagination(self):
        variants = (
            ("youtube", "Design", "Motion", "target"),
            ("web", "Design", "Motion", "target"),
            ("youtube", "Travel", "Route", "target"),
            ("youtube", "Design", "Motion", "other"),
            ("youtube", "Design", "Still", "target"),
        )
        for position, (platform, primary, secondary, tag) in enumerate(variants):
            self._save(_payload(
                f"cohort-{position}", title="cohort", platform=platform,
                primary=primary, secondary=secondary, personal_tags=[tag],
            ))
        self._save(_payload(
            "distractor", platform="bilibili", primary="Design", secondary="Motion",
            personal_tags=["target"],
        ))
        params = {
            "query": "cohort", "platform": "youtube", "primary_category": "design",
            "secondary_category": "motion", "tag": "target", "tag_source": "personal",
        }
        first = self._assert_equivalent(**params, limit=1)
        self.assertEqual(first["total"], 1)
        self.assertEqual(first["facets"]["platforms"], [
            {"platform": "web", "count": 1}, {"platform": "youtube", "count": 1},
        ])
        self.assertEqual(first["facets"]["categories"], [
            {
                "primary_category": "Design", "count": 2,
                "children": [
                    {"secondary_category": "Motion", "count": 1},
                    {"secondary_category": "Still", "count": 1},
                ],
            },
            {
                "primary_category": "Travel", "count": 1,
                "children": [{"secondary_category": "Route", "count": 1}],
            },
        ])
        self.assertEqual(
            [facet for facet in first["facets"]["tags"] if facet["source"] == "personal"],
            [
                {"name": "other", "source": "personal", "count": 1},
                {"name": "target", "source": "personal", "count": 1},
            ],
        )
        self.assertEqual(first["facets"], self._assert_equivalent(**params, limit=100)["facets"])
        broad_first = self._assert_equivalent(query="cohort", limit=1)
        broad_all = self._assert_equivalent(query="cohort", limit=100)
        self.assertEqual(broad_first["facets"], broad_all["facets"])
        self.assertEqual(broad_first["total"], 5)
        self.assertIsNotNone(broad_first["next_cursor"])

    def test_cursor_pages_equal_the_oracle_for_full_and_selective_queries(self):
        self._seed_rich_library()
        for params in (
            {"limit": 3}, {"query": "example.com", "limit": 7},
            {"query": "motion", "limit": 1},
            {"tag": "shared", "tag_source": "personal", "limit": 2},
        ):
            with self.subTest(params=params):
                cursor = ""
                after = None
                seen = []
                expected = legacy_search_oracle(self.graphs, **{**params, "limit": 100})
                for _ in range(len(self.graphs) + 1):
                    page = self._assert_equivalent(**params, cursor=cursor, after=after)
                    seen.extend(item["id"] for item in page["items"])
                    self.assertEqual(page["facets"], expected["facets"])
                    if page["next_cursor"] is None:
                        break
                    self.assertTrue(page["items"])
                    last = page["items"][-1]
                    after = (last["updated_at"], last["id"])
                    cursor = page["next_cursor"]
                else:
                    self.fail("cursor failed to reach the terminal page")
                self.assertEqual(seen, [item["id"] for item in expected["items"]])
                self.assertEqual(len(seen), len(set(seen)))

    def test_cursor_accepts_normalized_equivalence_but_rejects_other_searches(self):
        self._seed_rich_library()
        first = self._assert_equivalent(query="  ＭＯＴＩＯＮ ", limit=1)
        cursor = first["next_cursor"]
        self.assertIsNotNone(cursor)
        last = first["items"][-1]
        self._assert_equivalent(
            query="motion", limit=1, cursor=cursor, after=(last["updated_at"], last["id"]),
        )
        for changes in (
            {"query": "different-private-query"}, {"platform": "web"},
            {"primary_category": "design"}, {"secondary_category": "motion"},
            {"tag": "shared", "tag_source": "personal"}, {"limit": 2},
            {"cursor": "%%%"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(PipelineError) as raised:
                    self.service.search(**{
                        "query": "motion", "limit": 1, "cursor": cursor, **changes,
                    })
                self.assertEqual(raised.exception.code, "COLLECTION_CURSOR_INVALID")
                self.assertNotIn("different-private-query", str(raised.exception))
                self.assertNotIn(cursor, str(raised.exception))
        for params in ({"tag": "shared"}, {"tag_source": "personal"}):
            with self.subTest(incomplete_tag_filter=params):
                with self.assertRaises(PipelineError) as raised:
                    self.service.search(**params)
                self.assertEqual(raised.exception.code, "COLLECTION_FILTER_INVALID")

    def test_equal_timestamps_use_id_order_across_page_boundaries(self):
        tied_time = datetime(2026, 8, 25, tzinfo=UTC)
        tied = [
            self._save(_payload(f"tied-{position}", title="tied result"), timestamp=tied_time)
            for position in range(4)
        ]
        seen = []
        cursor = ""
        after = None
        for _ in range(4):
            page = self._assert_equivalent(query="tied result", limit=1, cursor=cursor, after=after)
            self.assertEqual(page["total"], 4)
            self.assertEqual(len(page["items"]), 1)
            item = page["items"][0]
            seen.append(item["id"])
            after = (item["updated_at"], item["id"])
            cursor = page["next_cursor"]
        self.assertEqual(seen, sorted(graph["id"] for graph in tied))
        self.assertIsNone(cursor)

    def test_api_save_visibility_reopen_compact_response_and_zero_deep_side_effects(self):
        providers = [
            Mock(spec=UnconfiguredAsrProvider), Mock(spec=DeterministicFullExtractor),
            Mock(spec=SidecarSubtitleProvider), Mock(spec=DeterministicFocusedExtractor),
            Mock(spec=DeterministicAutoTagger),
        ]
        for provider in providers:
            for method in ("transcribe", "extract", "get_subtitles", "generate"):
                if hasattr(provider, method):
                    getattr(provider, method).side_effect = AssertionError("provider invoked")
        pipeline = LocalFullPipeline(
            self.repo, providers[0], providers[1], temp_root=self.root / "media-temp",
            subtitle_provider=providers[2], focused_extractor=providers[3],
            auto_tagger=providers[4],
        )
        app = create_app(
            pipeline, upload_root=self.root / "uploads",
            inspiration_temp_root=self.root / "inspiration-temp",
        )
        guards = (
            "backend.app.services.capture.CaptureService.preview",
            "backend.app.services.safe_http.SafePublicFetcher.fetch",
            "backend.app.services.deep_analysis.CollectionDeepAnalysisService.get",
            "backend.app.services.deep_analysis.CollectionDeepAnalysisService.start",
            "backend.app.services.deep_analysis.CollectionDeepAnalysisService.claim_retry",
            "backend.app.services.pipeline.LocalFullPipeline.process",
            "backend.app.services.pipeline.LocalFullPipeline.resume",
            "backend.app.services.resolution.ResolutionService.preview",
            "backend.app.services.resolution.ResolutionService.process",
            "backend.app.services.resolution.ResolutionService.resume",
            "backend.app.services.questions.QuestionService.ask",
            "backend.app.services.inspiration.InspirationTranscriptionService.transcribe",
        )
        repository_guards = (
            "search_videos", "get_result", "load_result", "get_video_detail",
            "get_deep_result_summary", "get_collection_deep_job",
            "get_or_create_collection_deep_job", "save_job", "save_result",
            "save_job_artifact", "save_video_question",
        )
        with ExitStack() as stack:
            calls = [
                stack.enter_context(patch(target, side_effect=AssertionError(target)))
                for target in guards
            ]
            calls.extend(
                stack.enter_context(patch.object(
                    SQLiteRepository, name, side_effect=AssertionError(name),
                ))
                for name in repository_guards
            )
            client = stack.enter_context(TestClient(app))
            payload = _payload(
                "persisted", title="公开卡片标题", platform="youtube",
                source_copy="source-copy-sensitive-marker", primary="设计", secondary="动效",
                personal_tags=["私人标签"], inspiration="inspiration-sensitive-marker",
            )
            payload["original_input"] = "original-input-sensitive-marker"
            saved = self._save(payload)
            immediate = client.get(
                "/api/v1/collection-items", params={"query": "inspiration-sensitive-marker"},
            )
            self.assertEqual(immediate.status_code, 200, immediate.text)
            self.assertEqual(immediate.json()["total"], 1)
            self.assertEqual(immediate.json()["items"][0]["id"], saved["id"])
            self.assertEqual(set(immediate.json()["items"][0]), {
                "id", "display_title", "platform", "primary_category", "cover_url",
                "source_author", "user_author", "has_user_cover",
                "created_at", "updated_at",
            })
            for marker in (
                "source-copy-sensitive-marker", "original-input-sensitive-marker",
                "inspiration-sensitive-marker",
            ):
                self.assertNotIn(marker, json.dumps(immediate.json(), ensure_ascii=False))
            detail = client.get(f"/api/v1/collection-items/{saved['id']}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json(), saved)

            # Re-instantiation opens the same synthetic on-disk library, not a
            # process-local search cache or a user's persistent database.
            reopened = SQLiteRepository(self.db_path)
            restored = CollectionService(reopened)
            self.assertEqual(restored.get(saved["id"]).model_dump(mode="json"), saved)
            restored_page = restored.search(query="inspiration-sensitive-marker")
            self.assertEqual(restored_page.model_dump(mode="json"), immediate.json())
            repeated = client.get(
                "/api/v1/collection-items", params={"query": "inspiration-sensitive-marker"},
            )
            self.assertEqual(repeated.json(), immediate.json())
            for call in calls:
                call.assert_not_called()
        for provider in providers:
            self.assertEqual(provider.mock_calls, [])

    def test_ordinary_bookmark_remains_searchable_and_restores_without_metadata(self):
        payload = _payload("metadata-unavailable", title="", author="", source_copy="")
        payload["user_title"] = None
        payload["untitled_confirmed"] = True
        payload["metadata_status"] = "metadata_unavailable"
        payload["metadata"]["platform_tags"] = []
        for name in ("title", "author", "cover_url", "source_copy"):
            payload["metadata"][name] = {"value": "", "source": "none", "fetched_at": ""}
        saved = self._save(payload)
        self.assertEqual(saved["display_title"], "未命名收藏")
        for query in ("未命名", "metadata-unavailable"):
            with self.subTest(query=query):
                page = self._assert_equivalent(query=query)
                self.assertEqual([item["id"] for item in page["items"]], [saved["id"]])
        restored = CollectionService(SQLiteRepository(self.db_path))
        self.assertEqual(restored.get(saved["id"]).model_dump(mode="json"), saved)


if __name__ == "__main__":
    unittest.main()
