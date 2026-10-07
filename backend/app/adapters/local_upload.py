from __future__ import annotations

import re
from urllib.parse import urlparse

from .base import ResolvedVideo, SubtitlePayload, VideoMetadata

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalUploadAdapter:
    """Adapter for opaque identities produced by the upload pipeline.

    This adapter never accepts filesystem paths or remote URLs. Media ingestion
    remains owned by ``LocalFullPipeline``; the adapter exposes the resulting
    cached resource through the same read interface as platform adapters.
    """

    platform = "local_upload"

    def __init__(self, repository=None):
        self.repository = repository

    def match(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.scheme == "local"
            and parsed.netloc == "sha256"
            and _SHA256_RE.fullmatch(parsed.path.removeprefix("/")) is not None
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )

    def resolve(self, url: str) -> ResolvedVideo:
        if not self.match(url):
            raise ValueError("invalid controlled local upload identity")
        video_id = urlparse(url).path.removeprefix("/")
        canonical = f"local://sha256/{video_id}"
        return ResolvedVideo(
            platform=self.platform,
            source_url=canonical,
            canonical_url=canonical,
            video_id=video_id,
            aliases=(canonical,),
        )

    def get_metadata(self, video: ResolvedVideo) -> VideoMetadata:
        result = self._load(video)
        if result is None:
            return VideoMetadata(warnings=["本地上传记录不存在或已清理。"])
        return VideoMetadata(
            author=result.author,
            title=result.title,
            description=result.description,
            tags=result.tags,
            duration=result.duration,
            cover_url=result.cover_url,
            warnings=list(result.warnings),
        )

    def get_subtitles(self, video: ResolvedVideo) -> SubtitlePayload:
        result = self._load(video)
        if result is None:
            return SubtitlePayload(
                source="none",
                warnings=["本地上传字幕记录不存在或已清理。"],
            )
        return SubtitlePayload(
            segments=list(result.segments),
            source=result.subtitle_source,
            warnings=list(result.warnings),
        )

    def get_media(self, video: ResolvedVideo):
        # Temporary uploaded media is intentionally deleted after processing.
        return None

    def _load(self, video: ResolvedVideo):
        if self.repository is None or video.platform != self.platform:
            return None
        return self.repository.load_result(
            video.video_id,
            platform=self.platform,
        )
