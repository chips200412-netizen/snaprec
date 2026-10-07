from __future__ import annotations

import re
from pathlib import Path

import httpx

from ..domain.models import (
    AutomaticTag,
    Evidence,
    ExtractionItem,
    FocusedAnswer,
    FullExtraction,
    Segment,
)

SUBTITLE_SUFFIXES = (".srt", ".vtt", ".txt")
_CUE_TIME_RE = re.compile(
    r"^(?:(\d{1,3}):)?(\d{2}):(\d{2})[,.](\d{3})$"
)


def find_sidecar_subtitle(media_path: Path) -> Path | None:
    for suffix in SUBTITLE_SUFFIXES:
        candidate = Path(f"{media_path}{suffix}")
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


class SidecarSubtitleProvider:
    """Read controlled SRT, WebVTT or plain-text user sidecars."""

    def get_subtitles(self, media_path: Path) -> list[Segment] | None:
        sidecar = find_sidecar_subtitle(media_path)
        if sidecar is None:
            return None
        content = sidecar.read_text(encoding="utf-8-sig")
        if sidecar.suffix.casefold() == ".txt":
            return self._plain_text_segments(content)
        return self._timed_segments(content)

    @staticmethod
    def _plain_text_segments(content: str) -> list[Segment] | None:
        segments: list[Segment] = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            segments.append(
                Segment(
                    id=f"seg-{len(segments) + 1}",
                    start=None,
                    end=None,
                    text=line,
                )
            )
        return segments or None

    @staticmethod
    def _timed_segments(content: str) -> list[Segment] | None:
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n[ \t]*\n", normalized.strip())
        segments: list[Segment] = []
        for block in blocks:
            lines = [line.strip() for line in block.split("\n")]
            if not lines or lines[0].upper().startswith("WEBVTT"):
                continue
            if lines[0].upper().startswith(("NOTE", "STYLE", "REGION")):
                continue
            timing_index = next(
                (index for index, line in enumerate(lines) if "-->" in line),
                None,
            )
            if timing_index is None:
                continue
            timing = lines[timing_index].split("-->", 1)
            start = _parse_cue_time(timing[0].strip())
            end_token = timing[1].strip().split(maxsplit=1)[0]
            end = _parse_cue_time(end_token)
            text = "\n".join(
                line for line in lines[timing_index + 1 :] if line
            ).strip()
            if start is None or end is None or end < start or not text:
                continue
            segments.append(
                Segment(
                    id=f"seg-{len(segments) + 1}",
                    start=start,
                    end=end,
                    text=text,
                )
            )
        return segments or None


def _parse_cue_time(value: str) -> float | None:
    match = _CUE_TIME_RE.fullmatch(value)
    if match is None:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


class UnconfiguredAsrProvider:
    def transcribe(self, media_path: Path) -> list[Segment]:
        raise RuntimeError("ASR provider is not configured")


class HttpAsrProvider:
    """Adapter for a user-authorized multipart ASR endpoint.

    The endpoint must return either ``{"segments": [...]}`` or ``{"text": "..."}``.
    No endpoint is contacted unless the caller explicitly configures one.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        model: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.endpoint = endpoint
        self.token = token
        self.model = model
        self.client = client or httpx.Client(timeout=httpx.Timeout(300, connect=10))

    def transcribe(self, media_path: Path) -> list[Segment]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        data = {"model": self.model} if self.model else {}
        with media_path.open("rb") as stream:
            response = self.client.post(
                self.endpoint,
                headers=headers,
                data=data,
                files={"file": (media_path.name, stream, "application/octet-stream")},
            )
        response.raise_for_status()
        payload = response.json()
        raw_segments = payload.get("segments")
        if isinstance(raw_segments, list):
            segments = [
                Segment(
                    id=f"seg-{index}",
                    start=item.get("start"),
                    end=item.get("end"),
                    text=str(item.get("text", "")).strip(),
                )
                for index, item in enumerate(raw_segments, 1)
                if str(item.get("text", "")).strip()
            ]
        else:
            text = str(payload.get("text", "")).strip()
            segments = [Segment(id="seg-1", text=text)] if text else []
        if not segments:
            raise RuntimeError("ASR endpoint returned no transcript")
        return segments


# Backward-compatible alias for early S1 callers.
SidecarAsrProvider = UnconfiguredAsrProvider


class DeterministicFullExtractor:
    """Minimal offline extractor. Production LLM implementations use the same protocol."""

    def extract(
        self, raw_transcript: str, clean_transcript: str, segments: list[Segment]
    ) -> tuple[str, FullExtraction, list[Evidence]]:
        summary = clean_transcript[:100]
        full = FullExtraction()
        evidence: list[Evidence] = []
        for index, segment in enumerate(segments):
            text = segment.text.strip()
            if not text:
                continue
            evidence_id = f"ev-{index + 1}"
            ev = Evidence(
                id=evidence_id,
                claim=text,
                claim_type="creator_opinion",
                evidence=text,
                segment_ids=[segment.id],
                start_time=segment.start,
                end_time=segment.end,
                confidence=1.0,
            )
            evidence.append(ev)
            item_type = "key_point"
            target = full.key_points
            if re.search(r"\d|元|%|％", text):
                item_type, target = "important_data", full.important_data
            if re.search(r"第.+步|首先|然后|最后", text):
                item_type, target = "step", full.steps
            if re.search(r"风险|注意|不要|不能|否则", text):
                item_type, target = "risk", full.risks
            target.append(
                ExtractionItem(
                    text=text,
                    item_type=item_type,
                    claim_type="creator_opinion",
                    evidence_refs=[evidence_id],
                    confidence=1.0,
                )
            )
        return summary, full, evidence


class DeterministicAutoTagger:
    """Conservative offline tagger using transcript-derived content only."""

    generator_id = "deterministic-auto-tagger"
    generator_version = "1"

    _CONCEPTS = (
        ("AI", ("ai", "人工智能", "大模型", "chatgpt", "codex")),
        ("编程", ("编程", "代码", "开发", "github")),
        ("获客", ("获客", "引流", "客户", "客源")),
        ("收费", ("收费", "价格", "费用", "定价", "金额")),
        ("入门", ("入门", "新手", "起步")),
    )

    def generate(
        self,
        raw_transcript: str,
        clean_transcript: str,
        segments: list[Segment],
        extraction: FullExtraction,
    ) -> list[AutomaticTag]:
        # Deliberately no metadata arguments: title, description and platform
        # tags cannot become inputs by accident.
        haystack = f"{raw_transcript}\n{clean_transcript}".casefold()
        names: list[tuple[str, float]] = []
        for name, markers in self._CONCEPTS:
            if any(marker.casefold() in haystack for marker in markers):
                names.append((name, 0.9))
        if extraction.steps:
            names.append(("操作步骤", 0.85))
        if extraction.risks:
            names.append(("风险提示", 0.85))
        if extraction.important_data:
            names.append(("重要数据", 0.85))
        unique: dict[str, tuple[str, float]] = {}
        for name, confidence in names:
            unique.setdefault(name.casefold(), (name, confidence))
        return [
            AutomaticTag(
                name=name,
                confidence=confidence,
                generation_method="deterministic",
            )
            for name, confidence in list(unique.values())[:20]
        ]


class HttpFullExtractor:
    """Adapter for a user-authorized structured extraction endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.endpoint = endpoint
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(300, connect=10))

    def extract(
        self, raw_transcript: str, clean_transcript: str, segments: list[Segment]
    ) -> tuple[str, FullExtraction, list[Evidence]]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.client.post(
            self.endpoint,
            headers=headers,
            json={
                "mode": "full",
                "raw_transcript": raw_transcript,
                "clean_transcript": clean_transcript,
                "segments": [segment.model_dump(mode="json") for segment in segments],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return (
            str(payload["summary"]),
            FullExtraction.model_validate(payload["full_extraction"]),
            [Evidence.model_validate(item) for item in payload["evidence"]],
        )


_FOCUS_STOP_WORDS = {
    "帮我", "找出", "只看", "只整理", "提取", "视频", "里面", "有关",
    "关于", "部分", "内容", "是否", "有没有", "这个", "问题", "如何",
}
_CONCEPT_GROUPS = (
    frozenset({"收费", "价格", "费用", "金额", "价钱", "定价"}),
    frozenset({"获客", "客户", "引流", "客源", "用户增长"}),
    frozenset({"入门", "新手", "开始", "起步"}),
    frozenset({"步骤", "流程", "操作", "方法"}),
)


def _normalized_focus_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _focus_terms(query: str) -> list[str]:
    normalized = _normalized_focus_text(query)
    for word in _FOCUS_STOP_WORDS:
        normalized = normalized.replace(word, " ")
    terms = [term for term in normalized.split() if term]
    if terms:
        return terms
    return [_normalized_focus_text(query)] if _normalized_focus_text(query) else []


class DeterministicFocusedExtractor:
    """Conservative offline retrieval that never supplements transcript content."""

    def extract(
        self,
        focus_query: str,
        raw_transcript: str,
        clean_transcript: str,
        segments: list[Segment],
        *,
        transcript_incomplete: bool = False,
    ) -> FocusedAnswer:
        query = _normalized_focus_text(focus_query)
        terms = _focus_terms(focus_query)
        explicit: list[Segment] = []
        inferred: list[Segment] = []
        for segment in segments:
            text = _normalized_focus_text(segment.text)
            if query and query in text:
                explicit.append(segment)
                continue
            if terms and any(term in text for term in terms):
                explicit.append(segment)
                continue
            query_groups = [
                group for group in _CONCEPT_GROUPS if any(term in query for term in group)
            ]
            if any(any(alias in text for alias in group) for group in query_groups):
                inferred.append(segment)

        matched = explicit or inferred
        if not matched:
            if transcript_incomplete:
                return FocusedAnswer(
                    mention_status="unknown_incomplete_transcript",
                    direct_answer="字幕残缺，无法判断视频是否讨论这个问题。",
                    missing_information=["现有字幕不足以支持肯定或否定结论。"],
                )
            return FocusedAnswer(
                mention_status="not_mentioned",
                direct_answer="该视频没有明确讨论这个问题。",
                missing_information=["视频字幕中未找到与关注问题直接相关的内容。"],
            )

        status = "explicit" if explicit else "inferred"
        evidence: list[Evidence] = []
        items: list[ExtractionItem] = []
        for index, segment in enumerate(matched):
            evidence_id = f"focus-ev-{index + 1}"
            if status == "inferred":
                claim = f"根据上下文归纳：{segment.text}"
            else:
                claim = segment.text
            claim_type = "model_inference" if status == "inferred" else "creator_opinion"
            evidence.append(
                Evidence(
                    id=evidence_id,
                    claim=claim,
                    claim_type=claim_type,
                    evidence=segment.text,
                    segment_ids=[segment.id],
                    start_time=segment.start,
                    end_time=segment.end,
                    confidence=0.75 if status == "inferred" else 1.0,
                )
            )
            items.append(
                ExtractionItem(
                    text=claim,
                    item_type="key_point",
                    claim_type=claim_type,
                    evidence_refs=[evidence_id],
                    confidence=0.75 if status == "inferred" else 1.0,
                )
            )
        prefix = "视频明确提到：" if status == "explicit" else "根据上下文可以合理归纳："
        return FocusedAnswer(
            mention_status=status,
            direct_answer=prefix + "；".join(segment.text for segment in matched),
            key_points=items,
            supporting_segments=evidence,
        )


class HttpFocusedExtractor:
    """Adapter for a user-authorized structured focused-extraction endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.endpoint = endpoint
        self.token = token
        self.client = client or httpx.Client(timeout=httpx.Timeout(300, connect=10))

    def extract(
        self,
        focus_query: str,
        raw_transcript: str,
        clean_transcript: str,
        segments: list[Segment],
        *,
        transcript_incomplete: bool = False,
    ) -> FocusedAnswer:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = self.client.post(
            self.endpoint,
            headers=headers,
            json={
                "mode": "focused",
                "focus_query": focus_query,
                "raw_transcript": raw_transcript,
                "clean_transcript": clean_transcript,
                "segments": [segment.model_dump(mode="json") for segment in segments],
                "transcript_incomplete": transcript_incomplete,
            },
        )
        response.raise_for_status()
        return FocusedAnswer.model_validate(response.json())
