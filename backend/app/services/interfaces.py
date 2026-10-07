from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.models import (
    AutomaticTag,
    Evidence,
    FocusedAnswer,
    FullExtraction,
    Segment,
)


class AsrProvider(Protocol):
    def transcribe(self, media_path: Path) -> list[Segment]: ...


class SubtitleProvider(Protocol):
    def get_subtitles(self, media_path: Path) -> list[Segment] | None: ...


class FullExtractor(Protocol):
    def extract(
        self, raw_transcript: str, clean_transcript: str, segments: list[Segment]
    ) -> tuple[str, FullExtraction, list[Evidence]]: ...


class AutoTagger(Protocol):
    generator_id: str
    generator_version: str

    def generate(
        self,
        raw_transcript: str,
        clean_transcript: str,
        segments: list[Segment],
        extraction: FullExtraction,
    ) -> list[AutomaticTag]: ...


class FocusedExtractor(Protocol):
    def extract(
        self,
        focus_query: str,
        raw_transcript: str,
        clean_transcript: str,
        segments: list[Segment],
        *,
        transcript_incomplete: bool = False,
    ) -> FocusedAnswer: ...
