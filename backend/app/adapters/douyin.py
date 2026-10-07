from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from ..domain.models import Segment
from ..services.pipeline import PipelineError
from ..services.public_metadata import HeadMetadataParser, safe_author_name
from .base import ResolvedVideo, SubtitlePayload, VideoMetadata
from .bilibili import (
    PinnedHTTPTransport,
    _content_type_allowed,
    _json_content_type,
    _reject_private_addresses,
    _resolve_addresses,
)

_PAGE_HOSTS = ("www.douyin.com", "v.douyin.com", "www.iesdouyin.com")
_SUBTITLE_HOSTS = (
    "douyin.com",
    "douyinvod.com",
    "bytecdn.cn",
    "byteimg.com",
)
_VIDEO_RE = re.compile(r"^/(?:video|share/video)/(?P<id>\d+)(?:[/?#]|$)")
_MAX_REDIRECTS = 3
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
_SHARE_URL_RE = re.compile(r"https?://[^\s<>'\"，。！？；：、）】》」』]+", re.IGNORECASE)
_SHARE_TAG_RE = re.compile(r"#\s*([^\s#@，。！？；：、]+)")
_SHARE_AUTHOR_RE = re.compile(r"(?<![\w.])@([^\s#@，。！？；：、]+)")
_SHARE_ENVELOPE_RE = re.compile(r"[A-Za-z0-9]{1,12}:/")
_SHARE_METRIC_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,2}")
_SHARE_TIME_RE = re.compile(r":\d{1,2}(?:am|pm)", re.IGNORECASE)
_SHARE_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}")
_SHARE_CODE_RE = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+")
_SHARE_EXPLICIT_AUTHOR_RE = re.compile(r"^【([^【】\r\n]{1,80})的作品】\s*")
_SHARE_INVITATION_RE = re.compile(
    r"^(?:[0-9]{1,3}\.[0-9]{1,2}\s+)?复制打开抖音，看看"
    r"(?P<author>【([^【】\r\n]{1,80})的作品】)(?!】)"
)
SHARE_METADATA_WARNING = (
    "部分元信息来自你粘贴的抖音分享文案，仅用于整理，不作为视频内容证据。"
)


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.json_scripts: list[tuple[str, str]] = []
        self._script_id = ""
        self._script_type = ""
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        item = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            key = item.get("property") or item.get("name")
            content = item.get("content", "").strip()
            if key and content and key.casefold() not in self.values:
                self.values[key.casefold()] = html.unescape(content)
        elif tag.casefold() == "script":
            self._script_id = item.get("id", "")
            self._script_type = item.get("type", "")
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._script_id or "json" in self._script_type.casefold():
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._script_parts:
            self.json_scripts.append(
                (self._script_id, "".join(self._script_parts).strip())
            )
        if tag.casefold() == "script":
            self._script_id = ""
            self._script_type = ""
            self._script_parts = []


class DouyinAdapter:
    """Reads only metadata and subtitles exposed in public Douyin pages."""

    platform = "douyin"

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
        if parsed.scheme.casefold() not in {"http", "https"}:
            return False
        if host == "v.douyin.com":
            return bool(parsed.path.strip("/"))
        return host == "www.douyin.com" and _video_id_from_url(url) is not None

    def identify(self, url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        if (parsed.hostname or "").casefold().rstrip(".") != "www.douyin.com":
            return None
        video_id = _video_id_from_url(url)
        if video_id is None:
            return None
        return video_id, f"https://www.douyin.com/video/{video_id}"

    def resolve(self, url: str) -> ResolvedVideo:
        current = _normalize_page_url(url)
        expected_id = _video_id_from_url(current)
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._get_limited(current, _MAX_PAGE_BYTES, _PAGE_HOSTS)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError("CONTENT_UNAVAILABLE", "平台短链重定向缺少目标地址。")
                current = _normalize_page_url(urljoin(current, location))
                redirect_id = _video_id_from_url(current)
                if expected_id and redirect_id and expected_id != redirect_id:
                    raise PipelineError("CONTENT_UNAVAILABLE", "页面跳转的视频身份与原链接不一致。")
                expected_id = expected_id or redirect_id
                continue
            if response.status_code in {401, 403}:
                raise PipelineError("AUTHORIZATION_REQUIRED", "该内容需要授权，建议上传本地媒体。")
            if response.status_code == 404:
                raise PipelineError("CONTENT_UNAVAILABLE", "视频不存在或已不可公开访问。")
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise PipelineError("CONTENT_UNAVAILABLE", "无法访问抖音公开视频页面。") from exc
            if not _content_type_allowed(
                response.headers.get("content-type"),
                {"text/html", "application/xhtml+xml"},
            ):
                raise PipelineError("CONTENT_UNAVAILABLE", "公开视频页面返回了非 HTML 内容。")

            response_url = str(response.url)
            path_video_id = _video_id_from_url(response_url)
            if path_video_id is None:
                path_video_id = _video_id_from_url(current)
            parser, payloads = _page_payloads(response.text)
            public_video_id = _public_video_id(payloads)
            video_id = path_video_id or public_video_id
            if expected_id and video_id != expected_id:
                raise PipelineError("CONTENT_UNAVAILABLE", "公开页面的视频身份与原链接不一致。")
            if video_id is None:
                raise PipelineError(
                    "CONTENT_UNAVAILABLE", "短链未指向受支持的抖音视频页面。"
                )
            _assert_public_target(payloads, video_id)
            canonical = f"https://www.douyin.com/video/{video_id}"
            aliases = tuple(
                dict.fromkeys((url, current, response_url, canonical))
            )
            return ResolvedVideo(
                platform=self.platform,
                source_url=url,
                canonical_url=canonical,
                video_id=video_id,
                page_html=response.text,
                aliases=aliases,
            )
        raise PipelineError("CONTENT_UNAVAILABLE", "平台短链重定向次数过多。")

    def get_metadata(self, video: ResolvedVideo) -> VideoMetadata:
        parser, payloads = _page_payloads(video.page_html)
        identities = _assert_public_target(payloads, video.video_id)
        # Once the page exposes video identities, generic page meta cannot be
        # attributed to a particular item. Missing target fields stay missing.
        head = HeadMetadataParser(video.page_html) if not identities else None
        page_meta = head.meta if head is not None else {}
        item = _video_item(payloads, video.video_id)
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        video_data = item.get("video") if isinstance(item.get("video"), dict) else {}
        description = _first_text(
            item.get("desc"),
            page_meta.get("og:description"),
            page_meta.get("description"),
        )
        title = _first_text(item.get("desc"), page_meta.get("og:title"))
        author_name = safe_author_name(author.get("nickname")) or (
            safe_author_name(head.author(video.canonical_url, include_jsonld=False)[0])
            if head is not None else None
        ) or ""
        cover = _first_text(
            _first_url(video_data.get("cover")),
            _first_url(video_data.get("dynamic_cover")),
            page_meta.get("og:image"),
        )
        tags = _tags(None, item) or _tags(page_meta.get("keywords"), {})
        duration = _duration(video_data.get("duration"))
        warnings = []
        if not title:
            warnings.append("抖音公开页面未提供标题。")
        return VideoMetadata(
            author=author_name,
            title=title,
            description=description,
            tags=tags,
            duration=duration,
            cover_url=cover,
            warnings=warnings,
        )

    def get_share_metadata(self, input_text: str) -> VideoMetadata:
        """Read conservative metadata hints from user-pasted Douyin share copy."""
        text = _SHARE_URL_RE.sub(" ", _strip_share_suffix_fields(input_text))
        text = re.sub(
            r"复制(?:此|这条|这)?链接[^。！!]*[。！!]?\s*$",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = text.strip()
        text = _strip_share_prefix_fields(text)
        invitation = _SHARE_INVITATION_RE.match(text)
        if invitation and safe_author_name(invitation.group(2).strip().lstrip("@")):
            text = text[invitation.start("author"):]
        explicit_author = _SHARE_EXPLICIT_AUTHOR_RE.match(text)
        author = explicit_author.group(1).strip().lstrip("@") if explicit_author else ""
        if explicit_author:
            text = text[explicit_author.end():]
        description = " ".join(text.split()).strip()
        tags = list(dict.fromkeys(_SHARE_TAG_RE.findall(description)))
        title_source = description

        title = ""
        if title_source:
            title_source = _SHARE_TAG_RE.sub(" ", title_source)
            title_source = _SHARE_AUTHOR_RE.sub(" ", title_source)
            title = _share_title(" ".join(title_source.split()).strip(" -—|，。；："))

        warnings = []
        if title or author or tags or description:
            warnings.append(SHARE_METADATA_WARNING)
        return VideoMetadata(
            author=author,
            title=title,
            description=description,
            tags=tags,
            warnings=warnings,
        )

    def get_subtitles(self, video: ResolvedVideo) -> SubtitlePayload:
        _, payloads = _page_payloads(video.page_html)
        item = _video_item(payloads, video.video_id)
        if not item:
            return SubtitlePayload(
                source="none",
                warnings=[
                    "公开页面未提供与目标视频 ID 匹配的字幕结构；结果仅包含页面元信息，不代表视频全文。"
                ],
            )
        for inline in _inline_subtitle_collections([item]):
            segments = _segments_from_payload(inline)
            if segments:
                return SubtitlePayload(segments=segments, source="public_page")

        for candidate in dict.fromkeys(_subtitle_urls([item])):
            try:
                url = candidate if not candidate.startswith("//") else f"https:{candidate}"
                response = self._get_following_redirects(
                    url, _MAX_SUBTITLE_BYTES, _SUBTITLE_HOSTS
                )
                if not _json_content_type(response.headers.get("content-type")):
                    continue
                segments = _segments_from_payload(response.json())
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
            warnings=[
                "未从抖音公开页面获得完整字幕；结果仅包含元信息，不代表视频全文。"
            ],
        )

    def get_media(self, video: ResolvedVideo):
        # S4 intentionally does not discover or download platform media streams.
        return None

    def _get_following_redirects(
        self, url: str, max_bytes: int, allowed_hosts: tuple[str, ...]
    ) -> httpx.Response:
        current = _normalize_subtitle_url(url, allowed_hosts)
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._get_limited(current, max_bytes, allowed_hosts)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError("SUBTITLE_UNAVAILABLE", "字幕重定向缺少目标地址。")
                current = _normalize_subtitle_url(
                    urljoin(current, location), allowed_hosts
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
                    "Accept-Encoding": "identity",
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
                return httpx.Response(
                    streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(content),
                    request=streamed.request,
                )
        except httpx.HTTPError as exc:
            raise PipelineError("CONTENT_UNAVAILABLE", "访问公开页面失败。") from exc


def _normalize_page_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PipelineError("CONTENT_UNAVAILABLE", "外部请求端口无效。") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or host not in _PAGE_HOSTS
    ):
        raise PipelineError("CONTENT_UNAVAILABLE", "外部请求地址未通过安全校验。")
    if parsed.path.startswith("/user/"):
        # Only canonicalize after validating the original origin and only for
        # one selected video; profile meta and account feeds are not inputs.
        identity = _video_id_from_url(url) if host == "www.douyin.com" else None
        if identity is None:
            raise PipelineError("CONTENT_UNAVAILABLE", "链接未指定唯一有效的抖音视频。")
        return f"https://www.douyin.com/video/{identity}"
    return parsed._replace(fragment="").geturl()


def _normalize_subtitle_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PipelineError("SUBTITLE_UNAVAILABLE", "字幕地址端口无效。") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _host_allowed(host, allowed_hosts)
    ):
        raise PipelineError("SUBTITLE_UNAVAILABLE", "字幕地址未通过安全校验。")
    return parsed._replace(fragment="").geturl()


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed)


def _video_id_from_path(path: str) -> str | None:
    match = _VIDEO_RE.match(path)
    return match.group("id") if match else None


def _video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    if re.fullmatch(r"/user/[^/]+/?", parsed.path):
        values = parse_qs(parsed.query, keep_blank_values=True).get("modal_id", [])
        if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
            return None
        return values[0]
    return _video_id_from_path(parsed.path)


def _strip_share_suffix_fields(text: str) -> str:
    """Only remove a complete envelope after an exact Douyin URL boundary."""
    urls = list(_SHARE_URL_RE.finditer(text))
    if not urls:
        return text
    url = urls[-1]
    try:
        parsed = urlparse(url.group())
        if ((parsed.hostname or "").casefold() not in _PAGE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443 if parsed.scheme.casefold() == "https" else 80}):
            return text
    except ValueError:
        return text
    suffix = text[url.end():]
    fields = suffix.split()
    patterns = (_SHARE_ENVELOPE_RE, _SHARE_CODE_RE, _SHARE_DATE_RE, _SHARE_TIME_RE)
    if (suffix[:1].isspace() and len(fields) == len(patterns)
            and all(sum(bool(pattern.fullmatch(field)) for field in fields) == 1 for pattern in patterns)):
        return text[:url.end()]
    return text


def _strip_share_prefix_fields(text: str) -> str:
    """Recognize bounded wrapper fields without depending on their order."""
    parts = text.split()
    patterns = {
        "envelope": _SHARE_ENVELOPE_RE,
        "metric": _SHARE_METRIC_RE,
        "time": _SHARE_TIME_RE,
        "date": _SHARE_DATE_RE,
        "code": _SHARE_CODE_RE,
    }
    fields: list[str] = []
    for index, token in enumerate(parts[:len(patterns)]):
        field = next((name for name, pattern in patterns.items() if pattern.fullmatch(token)), None)
        attached_body = None
        if field is None:
            marker = _SHARE_ENVELOPE_RE.match(token)
            if marker:
                field = "envelope"
                attached_body = token[marker.end():]
        if field is None or field in fields:
            break
        fields.append(field)
        if attached_body:
            # Historically the marker can touch the title without whitespace.
            # Keep that boundary, but never search for a marker inside prose.
            parts[index:index + 1] = [token[:-len(attached_body)], attached_body]
            break
    if "envelope" not in fields:
        return text
    if "time" in fields and ("date" in fields or "code" in fields):
        return " ".join(parts[len(fields):])
    # Preserve the older minimal marker form, but don't consume a trailing
    # date or code that may actually be the title without corroboration.
    marker = fields.index("envelope")
    if all(field in {"metric", "code"} for field in fields[:marker]):
        return " ".join(parts[marker + 1:])
    return text


def _share_title(text: str) -> str:
    """Keep short copy intact; excerpt long copy without generating a summary."""
    if len(text) <= 80:
        return text
    sentence = re.search(r"[。！？!?]+[\"'”’」』】）)]*|\.(?=\s|$)", text)
    excerpt = text[:sentence.end()] if sentence else text
    return excerpt if len(excerpt) <= 80 else excerpt[:79] + "…"


def _page_payloads(source: str) -> tuple[_MetaParser, list[Any]]:
    parser = _MetaParser()
    try:
        parser.feed(source)
    except Exception:
        pass
    payloads: list[Any] = []
    for script_id, value in parser.json_scripts:
        if not value:
            continue
        candidates = [value]
        if script_id in {"RENDER_DATA", "__UNIVERSAL_DATA_FOR_REHYDRATION__"}:
            candidates.insert(0, unquote(value))
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            payloads.append(payload)
            break
    return parser, payloads


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _public_video_ids(payloads: list[Any]) -> set[str]:
    identities: list[str] = []
    for item in _walk(payloads):
        if not any(key in item for key in ("desc", "video", "author")):
            continue
        for key in ("aweme_id", "awemeId", "itemId"):
            value = item.get(key)
            if isinstance(value, (str, int)) and re.fullmatch(r"[0-9]+", str(value)):
                identities.append(str(value))
                break
    return set(identities)


def _public_video_id(payloads: list[Any]) -> str | None:
    identities = _public_video_ids(payloads)
    return next(iter(identities)) if len(identities) == 1 else None


def _assert_public_target(payloads: list[Any], video_id: str) -> set[str]:
    identities = _public_video_ids(payloads)
    if identities and video_id not in identities:
        raise PipelineError("CONTENT_UNAVAILABLE", "公开页面未包含与原链接匹配的视频身份。")
    return identities


def _video_item(payloads: list[Any], video_id: str) -> dict[str, Any]:
    for item in _walk(payloads):
        identity = _first_text(
            item.get("aweme_id"), item.get("awemeId"), item.get("itemId")
        )
        looks_like_video = any(key in item for key in ("desc", "video", "author"))
        if identity == video_id and looks_like_video:
            return item
    return {}


def _first_text(*values: Any) -> str:
    return next((str(value).strip() for value in values if str(value or "").strip()), "")


def _first_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        candidates = value.get("url_list") or value.get("urlList")
        if isinstance(candidates, list):
            return _first_text(*candidates)
    return ""


def _duration(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0
    if result < 0:
        return 0
    # Douyin public page video.duration is expressed in milliseconds.
    return result / 1000


def _tags(keywords: str | None, item: dict[str, Any]) -> list[str]:
    result = [part.strip() for part in (keywords or "").split(",") if part.strip()]
    text_extra = item.get("text_extra") or item.get("textExtra")
    if isinstance(text_extra, list):
        for extra in text_extra:
            if not isinstance(extra, dict):
                continue
            tag = _first_text(extra.get("hashtag_name"), extra.get("hashtagName"))
            if tag:
                result.append(tag)
    return list(dict.fromkeys(result))


def _subtitle_urls(payloads: list[Any]) -> list[str]:
    result: list[str] = []
    for item in _walk(payloads):
        collections = item.get("subtitleInfos") or item.get("subtitle_infos")
        if not isinstance(collections, list):
            continue
        for subtitle in collections:
            if not isinstance(subtitle, dict):
                continue
            value = _first_text(
                subtitle.get("url"),
                subtitle.get("Url"),
                subtitle.get("subtitle_url"),
                subtitle.get("subtitleUrl"),
            )
            if value:
                result.append(value)
    return result


def _inline_subtitle_collections(payloads: list[Any]) -> Iterator[Any]:
    for item in _walk(payloads):
        for key in (
            "captions",
            "utterances",
            "subtitle_segments",
            "subtitleSegments",
        ):
            value = item.get(key)
            if isinstance(value, list):
                yield value


def _segments_from_payload(payload: Any) -> list[Segment]:
    values = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        for key in ("body", "captions", "utterances", "segments"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
        if values is None and isinstance(payload.get("data"), dict):
            return _segments_from_payload(payload["data"])
    if not isinstance(values, list):
        return []
    segments = []
    for item in values:
        if not isinstance(item, dict):
            continue
        text = _first_text(
            item.get("text"), item.get("content"), item.get("words")
        )
        if not text:
            continue
        start = _subtitle_time_from_item(item, "start")
        end = _subtitle_time_from_item(item, "end")
        if start is not None and end is not None and end < start:
            continue
        segments.append(
            Segment(
                id=f"seg-{len(segments) + 1}",
                start=start,
                end=end,
                text=text,
            )
        )
    return segments


def _subtitle_time_from_item(item: dict[str, Any], base: str) -> float | None:
    # Douyin camel/snake "*Time" fields are milliseconds. A plain start/end
    # field is treated as seconds so common public JSON caption formats remain
    # usable without guessing from the numeric magnitude.
    for key in (f"{base}_time", f"{base}Time"):
        if key in item:
            return _subtitle_time(item.get(key), milliseconds=True)
    return _subtitle_time(item.get(base), milliseconds=False)


def _subtitle_time(value: Any, *, milliseconds: bool) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0:
        return None
    return result / 1000 if milliseconds else result
