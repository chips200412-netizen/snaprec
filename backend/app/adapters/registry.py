from __future__ import annotations

from collections.abc import Iterable

from .base import VideoAdapter


class AdapterRegistry:
    def __init__(self, adapters: Iterable[VideoAdapter] = ()):
        self._adapters = list(adapters)

    def register(self, adapter: VideoAdapter) -> None:
        self._adapters.append(adapter)

    def matching(self, url: str) -> VideoAdapter | None:
        return next((adapter for adapter in self._adapters if adapter.match(url)), None)

    def supports(self, url: str) -> bool:
        return self.matching(url) is not None

