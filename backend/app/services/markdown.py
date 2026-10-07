from __future__ import annotations

from datetime import UTC, datetime

from typing import Literal

from ..domain.models import (
    Evidence,
    ExtractionItem,
    FocusedHistoryItem,
    PersonalNotes,
    VideoDetail,
    VideoResult,
)


_SECTIONS = [
    ("核心观点", "key_points"),
    ("重要数据", "important_data"),
    ("案例和论据", "cases_and_arguments"),
    ("方法或操作步骤", "steps"),
    ("注意事项和风险", "risks"),
    ("可复用金句", "quotes"),
]


def render_markdown(result: VideoResult) -> str:
    evidence = {item.id: item for item in result.evidence}
    lines = [
        f"# {result.title}",
        "",
        f"- 来源：{result.source_url}",
        f"- 平台：{result.platform}",
        f"- 作者：{result.author or '未知'}",
        f"- 字幕来源：{result.subtitle_source}",
        f"- 模式：{result.extraction_mode}",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
        "",
        "## 摘要",
        "",
        result.summary,
    ]
    for heading, attribute in _SECTIONS:
        lines.extend(["", f"## {heading}", ""])
        items: list[ExtractionItem] = getattr(result.full_extraction, attribute)
        if not items:
            lines.append("- 未从字幕中提取到可靠内容。")
        for item in items:
            lines.append(f"- {item.text}（{item.claim_type}，置信度 {item.confidence:.2f}）")
            for ref in item.evidence_refs:
                ev = evidence[ref]
                stamp = (
                    "时间未知" if ev.start_time is None
                    else f"{ev.start_time:g}s–{ev.end_time:g}s"
                )
                lines.append(f"  - 证据 [{stamp}]：{ev.evidence}")
    lines.extend(["", "## 警告", ""])
    lines.extend([f"- {warning}" for warning in result.warnings] or ["- 无"])
    lines.extend(["", "## 完整字幕", "", result.raw_transcript, ""])
    return "\n".join(lines)


def _metadata_lines(result: VideoResult) -> list[str]:
    return [
        f"# {result.title}",
        "",
        f"- 来源：{result.source_url}",
        f"- 平台：{result.platform}",
        f"- 作者：{result.author or '未知'}",
        f"- 字幕来源：{result.subtitle_source}",
        f"- 模式：{result.extraction_mode}",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
    ]


def _item_lines(
    items: list[ExtractionItem], evidence: dict[str, Evidence]
) -> list[str]:
    if not items:
        return ["- 未从字幕中提取到可靠内容。"]
    lines: list[str] = []
    for item in items:
        lines.append(
            f"- {item.text}（{item.claim_type}，置信度 {item.confidence:.2f}）"
        )
        for ref in item.evidence_refs:
            item_evidence = evidence[ref]
            stamp = (
                "时间未知"
                if item_evidence.start_time is None
                else f"{item_evidence.start_time:g}s–{item_evidence.end_time:g}s"
            )
            lines.append(f"  - 证据 [{stamp}]：{item_evidence.evidence}")
    return lines


def _warning_lines(warnings: list[str]) -> list[str]:
    return ["", "## 警告", "", *([f"- {item}" for item in warnings] or ["- 无"])]


def _personal_lines(notes: PersonalNotes) -> list[str]:
    if notes.spark is None and not notes.annotations:
        return []
    lines = ["", "## 个人内容", ""]
    if notes.spark is not None:
        spark = notes.spark
        lines.extend(
            [
                "### 闪念",
                "",
                f"- {spark.content}",
                f"  - 作者：{spark.author}",
                f"  - 创建时间：{spark.created_at}",
                f"  - 更新时间：{spark.updated_at}",
            ]
        )
    if notes.annotations:
        lines.extend(["", "### 个人备注", ""])
        for annotation in notes.annotations:
            lines.extend(
                [
                    f"- `{annotation.target_key}`：{annotation.content}",
                    f"  - 作者：{annotation.author}",
                    f"  - 类型：{annotation.target_type}",
                    f"  - 创建时间：{annotation.created_at}",
                    f"  - 更新时间：{annotation.updated_at}",
                ]
            )
    return lines


def render_detail_markdown(
    detail: VideoDetail,
    *,
    view: Literal["all", "overview", "steps", "transcript"] = "all",
    include_personal: bool = True,
    focused: FocusedHistoryItem | None = None,
) -> str:
    """Render one saved view without recomputing extraction or evidence."""
    result = detail.result
    lines = _metadata_lines(result)
    if focused is not None:
        lines[-2] = "- 模式：focused"
        answer = focused.focused_answer
        evidence = {item.id: item for item in answer.supporting_segments}
        if view in {"all", "overview"}:
            lines.extend(
                [
                    "",
                    "## 用户关注的问题",
                    "",
                    focused.focus_query,
                    "",
                    "## 信息完整性",
                    "",
                    answer.mention_status,
                    "",
                    "## 直接结论",
                    "",
                    answer.direct_answer,
                    "",
                    "## 相关内容整理",
                    "",
                    *_item_lines(
                        [*answer.key_points, *answer.supplementary_context], evidence
                    ),
                ]
            )
            if answer.missing_information:
                lines.extend(
                    [
                        "",
                        "## 信息缺口",
                        "",
                        *[f"- {item}" for item in answer.missing_information],
                    ]
                )
            lines.extend(["", "## 字幕依据", ""])
            lines.extend(
                [
                    f"- {item.evidence}"
                    + (
                        ""
                        if item.start_time is None
                        else f"（{item.start_time:g}s–{item.end_time:g}s）"
                    )
                    for item in answer.supporting_segments
                ]
                or ["- 当前没有可用的相关字幕依据。"]
            )
        elif view == "steps":
            step_items = [
                item
                for item in [*answer.key_points, *answer.supplementary_context]
                if item.item_type in {"step", "risk"}
            ]
            lines.extend(
                ["", "## 方法或操作步骤", "", *_item_lines(step_items, evidence)]
            )
        else:
            pass
    elif view == "all":
        content = render_markdown(result)
        if not include_personal:
            return content
        marker = "\n## 完整字幕\n"
        personal = _personal_lines(detail.personal_notes)
        if not personal:
            return content
        before, transcript = content.split(marker, maxsplit=1)
        return "\n".join(
            [before.rstrip(), *personal, "", "## 完整字幕", transcript.rstrip(), ""]
        )
    elif view == "overview":
        evidence = {item.id: item for item in result.evidence}
        lines.extend(["", "## 摘要", "", result.summary])
        for heading, attribute in _SECTIONS:
            lines.extend(
                [
                    "",
                    f"## {heading}",
                    "",
                    *_item_lines(getattr(result.full_extraction, attribute), evidence),
                ]
            )
    elif view == "steps":
        evidence = {item.id: item for item in result.evidence}
        for heading, attribute in (
            ("方法或操作步骤", "steps"),
            ("注意事项和风险", "risks"),
        ):
            lines.extend(
                [
                    "",
                    f"## {heading}",
                    "",
                    *_item_lines(getattr(result.full_extraction, attribute), evidence),
                ]
            )
    else:
        pass

    lines.extend(_warning_lines(result.warnings))
    if include_personal:
        lines.extend(_personal_lines(detail.personal_notes))
    if view == "transcript":
        lines.extend(
            ["", "## 完整字幕", "", result.raw_transcript or "当前没有可用字幕。"]
        )
    return "\n".join([*lines, ""])
