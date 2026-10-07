from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..repositories.sqlite import normalize_identity_url
from ..services.public_metadata import empty_metadata, is_author_reference, metadata_field
from ..services.safe_http import SafePublicFetcher
from .source import ResolvedSourcePage


CAPTURE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_UNAVAILABLE = "YouTube 公开元信息暂不可用，仍可保存普通书签。"


def _video_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in CAPTURE_HOSTS
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}):
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=32)
    # Tracking/time parameters may coexist with a video; alternative identity
    # or playlist keys, duplicate keys, and case/encoding tricks fail closed.
    if any(key not in {"v", "t", "start", "si", "feature", "pp", "app", "ab_channel", "utm_source", "utm_medium", "utm_campaign"} for key, _ in query):
        return None
    if len({key for key, _ in query}) != len(query):
        return None
    values = dict(query)
    if parsed.hostname == "youtu.be":
        candidate = parsed.path.removeprefix("/")
        if "v" in values:
            return None
    elif parsed.path == "/watch":
        candidate = values.get("v", "")
    else:
        match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})/?", parsed.path)
        if not match or "v" in values:
            return None
        candidate = match.group(1)
    return candidate if _VIDEO_ID.fullmatch(candidate) else None


class YouTubeSourceAdapter:
    """Collection-only public oEmbed. No HTML rendering or media/deep methods."""

    def __init__(self, fetcher: SafePublicFetcher) -> None:
        self.fetcher = fetcher

    def match(self, url: str) -> bool:
        try:
            return (urlsplit(url).hostname or "").casefold().rstrip(".") in CAPTURE_HOSTS
        except ValueError:
            return False

    def resolve(self, url: str) -> ResolvedSourcePage:
        original = normalize_identity_url(url)
        video_id = _video_id(original)
        if video_id is None:
            raise ValueError(_UNAVAILABLE)
        canonical = f"https://www.youtube.com/watch?v={video_id}"
        page = self.fetcher.fetch_youtube_oembed(video_id)
        return ResolvedSourcePage(
            platform="youtube", source_kind="video", canonical_url=canonical,
            aliases=tuple(dict.fromkeys((original, canonical))), page=page,
        )

    def get_public_metadata(self, resource: ResolvedSourcePage, fetched_at: str) -> dict[str, Any]:
        payload = json.loads(resource.page.body)
        if not isinstance(payload, dict) or payload.get("type") != "video" or payload.get("version") not in {"1.0", 1.0}:
            raise ValueError(_UNAVAILABLE)
        metadata = empty_metadata()
        for key, source_key in (("title", "title"), ("author", "author_name")):
            value = payload.get(source_key, "")
            if not isinstance(value, str):
                raise ValueError(_UNAVAILABLE)
            if key == "author" and is_author_reference(value):
                value = ""
            metadata[key] = metadata_field(value, "platform_public", fetched_at)
        if not metadata["title"]["value"] and not metadata["author"]["value"]:
            metadata["warnings"].append(_UNAVAILABLE)
        return metadata
