from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import httpcore

from ..domain.models import Segment
from ..services.pipeline import PipelineError
from ..services.public_metadata import HeadMetadataParser
from .base import ResolvedVideo, SubtitlePayload, VideoMetadata

_PAGE_HOSTS = ("bilibili.com", "b23.tv")
_SUBTITLE_HOSTS = ("bilibili.com", "hdslb.com", "bilivideo.com")
_VIDEO_RE = re.compile(r"^/(?:video/)?(?P<id>BV[0-9A-Za-z]+|av\d+)(?:[/?#]|$)", re.I)
_MAX_REDIRECTS = 3
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024


class BilibiliAdapter:
    """Reads only information embedded in publicly delivered Bilibili pages."""

    platform = "bilibili"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        dns_resolver: Callable[[str], list[str]] | None = None,
    ):
        self.dns_resolver = dns_resolver or _resolve_addresses
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            transport=PinnedHTTPTransport(self.dns_resolver),
        )

    def match(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not _host_allowed(host, _PAGE_HOSTS):
            return False
        if host == "b23.tv" or host.endswith(".b23.tv"):
            return bool(parsed.path.strip("/"))
        return _video_id_from_path(parsed.path) is not None

    def identify(self, url: str) -> tuple[str, str] | None:
        """Return a stable identity without network access for canonical/direct URLs."""
        video_id = _video_id_from_path(urlparse(url).path)
        if video_id is None:
            return None
        return video_id, f"https://www.bilibili.com/video/{video_id}"

    def resolve(self, url: str) -> ResolvedVideo:
        current = _normalize_public_url(url, _PAGE_HOSTS)
        chain = [current]
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._get_limited(current, _MAX_PAGE_BYTES, _PAGE_HOSTS)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError("CONTENT_UNAVAILABLE", "平台短链重定向缺少目标地址。")
                current = _normalize_public_url(urljoin(current, location), _PAGE_HOSTS)
                chain.append(current)
                identities = {_video_id_from_path(urlparse(value).path) for value in chain}
                bv_ids = {value for value in identities if value and value.startswith("BV")}
                av_ids = {value for value in identities if value and value.startswith("av")}
                if len(bv_ids) > 1 or len(av_ids) > 1:
                    raise PipelineError("CONTENT_UNAVAILABLE", "页面跳转的作品身份与原链接不一致。")
                continue
            if response.status_code in {401, 403}:
                raise PipelineError("AUTHORIZATION_REQUIRED", "该内容需要授权，建议上传本地媒体。")
            if response.status_code == 404:
                raise PipelineError("CONTENT_UNAVAILABLE", "视频不存在或已不可公开访问。")
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise PipelineError("CONTENT_UNAVAILABLE", "无法访问公开视频页面。") from exc
            if not _content_type_allowed(
                response.headers.get("content-type"), {"text/html", "application/xhtml+xml"}
            ):
                raise PipelineError("CONTENT_UNAVAILABLE", "公开视频页面返回了非 HTML 内容。")
            path_video_id = _video_id_from_path(urlparse(str(response.url)).path)
            video_id = path_video_id
            if video_id is None:
                path_video_id = _video_id_from_path(urlparse(current).path)
                video_id = path_video_id
            if video_id is None:
                raise PipelineError("CONTENT_UNAVAILABLE", "短链未指向受支持的 B站视频页面。")
            initial_state = _extract_marked_json(response.text, "__INITIAL_STATE__") or {}
            video_data = initial_state.get("videoData")
            video_data = video_data if isinstance(video_data, dict) else {}
            public_bvid = video_data.get("bvid")
            if not isinstance(public_bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]+", public_bvid):
                public_bvid = None
            public_aid = _public_aid(video_data)
            identities = {_video_id_from_path(urlparse(value).path) for value in (*chain, str(response.url))}
            identities.discard(None)
            bv_ids = {value for value in identities if value.startswith("BV")}
            av_ids = {value for value in identities if value.startswith("av")}
            if (len(bv_ids) > 1 or len(av_ids) > 1
                or (public_bvid and bv_ids and bv_ids != {public_bvid})
                or (public_aid and av_ids and av_ids != {public_aid})
                or (bv_ids and av_ids and (bv_ids != {public_bvid} or av_ids != {public_aid}))):
                raise PipelineError("CONTENT_UNAVAILABLE", "公开页面的作品身份与原链接不一致。")
            if public_bvid and (video_id == public_bvid or video_id == public_aid):
                video_id = public_bvid
            canonical = f"https://www.bilibili.com/video/{video_id}"
            path_alias = (
                f"https://www.bilibili.com/video/{path_video_id}"
                if path_video_id
                else canonical
            )
            aliases = tuple(
                dict.fromkeys((url, *chain, str(response.url), path_alias, canonical))
            )
            return ResolvedVideo(
                platform="bilibili",
                source_url=url,
                canonical_url=canonical,
                video_id=video_id,
                page_html=response.text,
                aliases=aliases,
            )
        raise PipelineError("CONTENT_UNAVAILABLE", "平台短链重定向次数过多。")

    def get_metadata(self, video: ResolvedVideo) -> VideoMetadata:
        head = HeadMetadataParser(video.page_html)
        values = head.meta
        state = _extract_marked_json(video.page_html, "__INITIAL_STATE__") or {}
        video_data = state.get("videoData") if isinstance(state, dict) else {}
        if not isinstance(video_data, dict):
            video_data = {}
        has_work = isinstance(state.get("videoData"), dict)
        confirmed = video.video_id in (video_data.get("bvid"), _public_aid(video_data))
        if has_work and not confirmed:
            return VideoMetadata(warnings=["公开页面未提供可确认归属的作品元信息。"])
        owner = video_data.get("owner")
        owner = owner if isinstance(owner, dict) else {}
        title = _first_text(
            values.get("og:title"), video_data.get("title"), state.get("title")
        )
        description = _first_text(
            values.get("og:description"),
            values.get("description"),
            video_data.get("desc"),
        )
        author = _first_text(owner.get("name")) if has_work else head.author(video.canonical_url, include_jsonld=False)[0]
        cover = _first_text(values.get("og:image"), video_data.get("pic"))
        tags = _tags(values.get("keywords"), state, video_data)
        duration = _duration(
            values.get("video:duration"),
            video_data.get("duration"),
            _nested(state, "epInfo", "duration"),
        )
        warnings = []
        if not title:
            warnings.append("公开视频页面未提供标题。")
        return VideoMetadata(
            author=author,
            title=title,
            description=description,
            tags=tags,
            duration=duration,
            cover_url=cover,
            warnings=warnings,
        )

    def get_subtitles(self, video: ResolvedVideo) -> SubtitlePayload:
        payloads = [
            _extract_marked_json(video.page_html, "__playinfo__"),
            _extract_marked_json(video.page_html, "__INITIAL_STATE__"),
        ]
        candidates: list[str] = []
        for payload in payloads:
            candidates.extend(_subtitle_urls(payload))
        for candidate in dict.fromkeys(candidates):
            try:
                url = candidate if not candidate.startswith("//") else f"https:{candidate}"
                response = self._get_following_redirects(
                    url, _MAX_SUBTITLE_BYTES, _SUBTITLE_HOSTS
                )
                if not _json_content_type(response.headers.get("content-type")):
                    continue
                body = response.json().get("body", [])
                segments = []
                for index, item in enumerate(body, 1):
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("content", "")).strip()
                    if not text:
                        continue
                    segments.append(
                        Segment(
                            id=f"seg-{index}",
                            start=_optional_float(item.get("from")),
                            end=_optional_float(item.get("to")),
                            text=text,
                        )
                    )
                if segments:
                    return SubtitlePayload(segments=segments, source="public_page")
            except (
                PipelineError,
                ValueError,
                TypeError,
                json.JSONDecodeError,
                httpx.HTTPError,
            ):
                continue
        return SubtitlePayload(
            source="none",
            warnings=["未从公开视频页面获得完整字幕；结果仅包含元信息，不代表视频全文。"],
        )

    def get_media(self, video: ResolvedVideo):
        # S3 intentionally does not discover or download platform media streams.
        return None

    def _get_following_redirects(
        self, url: str, max_bytes: int, allowed_hosts: tuple[str, ...]
    ) -> httpx.Response:
        current = _normalize_public_url(url, allowed_hosts, https_only=True)
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._get_limited(current, max_bytes, allowed_hosts)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError("SUBTITLE_UNAVAILABLE", "字幕重定向缺少目标地址。")
                current = _normalize_public_url(
                    urljoin(current, location), allowed_hosts, https_only=True
                )
                continue
            response.raise_for_status()
            return response
        raise PipelineError("SUBTITLE_UNAVAILABLE", "字幕重定向次数过多。")

    def _get_limited(
        self, url: str, max_bytes: int, allowed_hosts: tuple[str, ...]
    ) -> httpx.Response:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if not _host_allowed(host, allowed_hosts):
            raise PipelineError("CONTENT_UNAVAILABLE", "外部请求目标不在允许列表中。")
        _reject_private_addresses(host, self.dns_resolver)
        try:
            with self.client.stream(
                "GET",
                url,
                headers={
                    "Accept": "text/html,application/json",
                    "User-Agent": "VideoKnowledgePrototype/0.1",
                },
                follow_redirects=False,
            ) as streamed:
                declared = streamed.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise PipelineError("CONTENT_UNAVAILABLE", "平台响应超过大小限制。")
                content = bytearray()
                for chunk in streamed.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise PipelineError("CONTENT_UNAVAILABLE", "平台响应超过大小限制。")
                decoded_headers = httpx.Headers(
                    (name, value)
                    for name, value in streamed.headers.multi_items()
                    if name.casefold() not in {
                        "content-encoding",
                        "content-length",
                        "transfer-encoding",
                    }
                )
                response = httpx.Response(
                    streamed.status_code,
                    headers=decoded_headers,
                    content=bytes(content),
                    request=streamed.request,
                )
        except httpx.HTTPError as exc:
            raise PipelineError("CONTENT_UNAVAILABLE", "访问公开页面失败。") from exc
        return response


def _public_aid(video_data: dict[str, Any]) -> str | None:
    aid = video_data.get("aid")
    if isinstance(aid, bool) or not isinstance(aid, (int, str)) or not re.fullmatch(r"[1-9][0-9]*", str(aid)):
        return None
    return f"av{aid}"


def _normalize_public_url(
    url: str, allowed_hosts: tuple[str, ...], *, https_only: bool = False
) -> str:
    parsed = urlparse(url)
    valid_schemes = {"https"} if https_only else {"http", "https"}
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PipelineError("CONTENT_UNAVAILABLE", "外部请求端口无效。") from exc
    if (
        parsed.scheme.casefold() not in valid_schemes
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or not _host_allowed(host, allowed_hosts)
    ):
        raise PipelineError("CONTENT_UNAVAILABLE", "外部请求地址未通过安全校验。")
    return parsed._replace(fragment="").geturl()


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed)


def _content_type_allowed(value: str | None, allowed: set[str]) -> bool:
    media_type = (value or "").split(";", 1)[0].strip().casefold()
    return media_type in allowed


def _json_content_type(value: str | None) -> bool:
    media_type = (value or "").split(";", 1)[0].strip().casefold()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


class _PinnedNetworkBackend(httpcore.SyncBackend):
    """Resolve, validate, and connect to the exact validated address."""

    def __init__(
        self,
        resolver: Callable[[str], list[str]],
        backend: httpcore.SyncBackend | None = None,
    ):
        self.resolver = resolver
        self.backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        addresses = self.resolver(host)
        _validate_public_addresses(addresses)
        return self.backend.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )

    def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        return self.backend.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self, resolver: Callable[[str], list[str]]):
        super().__init__(retries=0)
        self._pool = httpcore.ConnectionPool(
            network_backend=_PinnedNetworkBackend(resolver), retries=0
        )


def _reject_private_addresses(
    host: str, resolver: Callable[[str], list[str]]
) -> None:
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise PipelineError("CONTENT_UNAVAILABLE", "平台域名解析失败。") from exc
    _validate_public_addresses(addresses)


def _validate_public_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise PipelineError("CONTENT_UNAVAILABLE", "平台域名没有可用地址。")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise PipelineError("CONTENT_UNAVAILABLE", "外部请求目标不是公网地址。")


def _resolve_addresses(host: str) -> list[str]:
    return list(
        dict.fromkeys(
            item[4][0]
            for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        )
    )


def _video_id_from_path(path: str) -> str | None:
    match = _VIDEO_RE.match(path)
    if not match:
        return None
    value = match.group("id")
    return f"av{value[2:]}" if value.casefold().startswith("av") else value


def _extract_marked_json(source: str, marker: str) -> dict[str, Any] | None:
    position = source.find(marker)
    if position < 0:
        return None
    start = source.find("{", position + len(marker))
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(source[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _subtitle_urls(payload: Any) -> list[str]:
    urls = []
    for item in _walk(payload):
        value = item.get("subtitle_url") or item.get("subtitleUrl")
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())
    return urls


def _nested(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _first_text(*values: Any) -> str:
    return next((str(value).strip() for value in values if str(value or "").strip()), "")


def _tags(keywords: str | None, state: dict[str, Any], video_data: dict[str, Any]) -> list[str]:
    result = [item.strip() for item in (keywords or "").split(",") if item.strip()]
    for source in (state.get("tags"), video_data.get("tags")):
        if isinstance(source, list):
            for item in source:
                value = item.get("tag_name") if isinstance(item, dict) else item
                if str(value or "").strip():
                    result.append(str(value).strip())
    return list(dict.fromkeys(result))


def _duration(*values: Any) -> float:
    for value in values:
        try:
            result = float(value)
        except (TypeError, ValueError):
            continue
        if result >= 0:
            return result / 1000 if result > 86400 else result
    return 0


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
