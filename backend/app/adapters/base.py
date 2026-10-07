from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..domain.models import Segment


@dataclass(frozen=True)
class ResolvedVideo:
    platform: str
    source_url: str
    canonical_url: str
    video_id: str
    page_html: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoMetadata:
    author: str = ""
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    duration: float = 0
    cover_url: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SubtitlePayload:
    segments: list[Segment] = field(default_factory=list)
    source: str = "none"
    warnings: list[str] = field(default_factory=list)


class VideoAdapter(Protocol):
    def match(self, url: str) -> bool: ...

    def resolve(self, url: str) -> ResolvedVideo: ...

    def get_metadata(self, video: ResolvedVideo) -> VideoMetadata: ...

    def get_subtitles(self, video: ResolvedVideo) -> SubtitlePayload: ...

    def get_media(self, video: ResolvedVideo): ...
