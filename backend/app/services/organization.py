from __future__ import annotations

from collections.abc import Iterable
import re

from ..domain.models import OrganizationSuggestion, SourceMetadata


_PUBLIC_SOURCES = {
    "platform_public",
    "platform_description",
    "open_graph",
    "page_metadata",
    "page_description",
}

# Ordered and deliberately small. A rule is explainable and stable; it never
# attempts to infer the unseen contents of a source.
_CATEGORY_RULES = (
    (
        "技术与工具",
        (
            ("AI 与自动化", ("ai", "人工智能", "大模型", "chatgpt", "codex", "自动化")),
            ("编程开发", ("编程", "代码", "开发", "python", "javascript", "github")),
            ("效率工具", ("工具", "软件", "应用", "效率", "workflow")),
        ),
    ),
    (
        "知识与学习",
        (
            ("教程方法", ("教程", "教学", "入门", "学习", "课程", "方法", "指南")),
            ("阅读研究", ("研究", "论文", "书籍", "阅读", "知识")),
        ),
    ),
    (
        "商业与营销",
        (
            ("增长营销", ("营销", "增长", "获客", "品牌", "运营", "流量")),
            ("商业管理", ("商业", "创业", "管理", "销售", "产品", "定价")),
        ),
    ),
    (
        "创意与设计",
        (
            ("视觉设计", ("设计", "视觉", "ui", "ux", "字体", "配色")),
            ("内容创作", ("创作", "写作", "摄影", "视频", "剪辑", "文案")),
        ),
    ),
    (
        "生活与健康",
        (
            ("健康运动", ("健康", "运动", "健身", "营养", "睡眠")),
            ("生活方式", ("生活", "旅行", "美食", "家居", "穿搭")),
        ),
    ),
    (
        "文化与娱乐",
        (
            ("文化艺术", ("文化", "艺术", "历史", "电影", "音乐", "展览")),
            ("休闲娱乐", ("娱乐", "游戏", "动漫", "综艺")),
        ),
    ),
)


def _trusted_values(metadata: SourceMetadata) -> tuple[list[str], list[str]]:
    text_values: list[str] = []
    tag_values: list[str] = []
    for field in (
        metadata.title,
        metadata.author,
        metadata.source_copy,
    ):
        if field.source in _PUBLIC_SOURCES and field.value.strip():
            text_values.append(field.value.strip())
    for tag in metadata.platform_tags:
        if tag.source in _PUBLIC_SOURCES and tag.value.strip():
            value = tag.value.strip()
            text_values.append(value)
            tag_values.append(value)
    return text_values, tag_values


def _stable_tags(values: Iterable[str]) -> list[str]:
    tags: dict[str, str] = {}
    for value in values:
        display = " ".join(value.strip().split())
        if display:
            tags.setdefault(display.casefold(), display)
    return list(tags.values())[:12]


def _marker_matches(haystack: str, marker: str) -> bool:
    normalized = marker.casefold()
    if normalized.isascii() and normalized.replace(" ", "").isalnum():
        return re.search(
            rf"(?<![\w]){re.escape(normalized)}(?![\w])", haystack
        ) is not None
    return normalized in haystack


class DeterministicOrganizationSuggestionService:
    method = "deterministic"

    def suggest(
        self,
        metadata: SourceMetadata,
        source_kind: str,
        platform: str,
    ) -> OrganizationSuggestion:
        del source_kind, platform  # Classification remains evidence-driven.
        values, public_tags = _trusted_values(metadata)
        if not values:
            return OrganizationSuggestion(
                primary_category="",
                secondary_category="",
                tags=[],
                method=self.method,
                status="insufficient_metadata",
            )

        haystack = "\n".join(values).casefold()
        primary = ""
        secondary = ""
        matched_topics: list[str] = []
        for candidate_primary, secondary_rules in _CATEGORY_RULES:
            for candidate_secondary, markers in secondary_rules:
                if any(_marker_matches(haystack, marker) for marker in markers):
                    matched_topics.append(candidate_secondary)
                    if not primary:
                        primary = candidate_primary
                        secondary = candidate_secondary
                    break

        tags = _stable_tags([*public_tags, *matched_topics])
        if not primary and not tags:
            return OrganizationSuggestion(
                primary_category="",
                secondary_category="",
                tags=[],
                method=self.method,
                status="insufficient_metadata",
            )
        return OrganizationSuggestion(
            primary_category=primary,
            secondary_category=secondary,
            tags=tags,
            method=self.method,
            status="generated",
        )
