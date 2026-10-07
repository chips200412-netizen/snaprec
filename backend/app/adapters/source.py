from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from ..services.safe_http import SafeFetchResult


@dataclass(frozen=True, slots=True)
class ResolvedSourcePage:
    platform: str
    source_kind: str
    canonical_url: str
    aliases: tuple[str, ...]
    page: SafeFetchResult  # Ephemeral; never placed in cache or preview payloads.


class SourceAdapter(Protocol):
    def match(self, url: str) -> bool: ...
    def resolve(self, url: str) -> ResolvedSourcePage: ...
    def get_public_metadata(
        self, resource: ResolvedSourcePage, fetched_at: str
    ) -> dict[str, Any]: ...


class SourceAdapterRegistry:
    """Collection-only adapters: no video resolution, subtitles, or media seam."""

    def __init__(self, adapters: Iterable[SourceAdapter] = ()) -> None:
        self.adapters = tuple(adapters)

    def matching(self, url: str) -> SourceAdapter | None:
        return next((adapter for adapter in self.adapters if adapter.match(url)), None)
