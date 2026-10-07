from __future__ import annotations

import re
import unicodedata
from http import HTTPStatus
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..repositories.sqlite import normalize_identity_url
from ..services.public_metadata import (
    HeadMetadataParser,
    empty_metadata,
    metadata_field,
    parse_public_metadata,
)
from ..services.safe_http import SafeHttpError, SafePublicFetcher
from ..services.xiaohongshu_embedded import extract_xhs_embedded_metadata
from .source import ResolvedSourcePage


PAGE_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})
CAPTURE_HOSTS = PAGE_HOSTS | {"xhslink.com", "xhslink.cn"}
COVER_HOSTS = PAGE_HOSTS | {"sns-webpic-qc.xhscdn.com"}
_NOTE_PATH = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?")
_SHORT_PATH = re.compile(r"/(?:[amo]/)?[A-Za-z0-9_-]{1,128}")
_UNAVAILABLE = "小红书公开元信息暂不可用，仍可保存普通书签。"
_SHELL_TITLES = {
    "小红书", "小红书你的生活兴趣社区", "小红书你的生活指南", "小红书发现生活",
}
_GATE_TITLES = {
    "404", "404notfound", "notfound", "pagenotfound", "error", "访问受限",
    "访问异常", "访问验证", "安全验证", "滑块验证", "验证码", "请登录", "登录",
    "请登录后查看", "内容不存在", "笔记不存在", "笔记已删除", "页面不存在",
    "该笔记已删除", "页面暂时无法访问", "当前内容无法展示", "页面走丢了",
    "内容审核中", "笔记审核中", "注册", "登录注册", "注册登录",
    "login", "signin", "signup", "loginsignup", "signinsignup", "accessdenied",
    "forbidden", "unauthorized", "internalservererror", "badgateway",
    "serviceunavailable", "gatewaytimeout",
}


def _title_key(title: str) -> str:
    return re.sub(r"[\s_\-—–|·/]+", "", unicodedata.normalize("NFKC", title)).casefold()


_HTTP_ERROR_TITLES = {
    _title_key(value)
    for status in HTTPStatus if status.value >= 400
    for value in (str(status.value), f"{status.value}{status.phrase}")
}


def _note_id(url: str) -> str | None:
    parsed = urlsplit(url)
    if (parsed.hostname or "").casefold().rstrip(".") not in PAGE_HOSTS:
        return None
    match = _NOTE_PATH.fullmatch(parsed.path)
    return match.group(1).casefold() if match else None


def _allowed_route(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return bool(
        _note_id(url)
        or (host == "xhslink.com" and _SHORT_PATH.fullmatch(parsed.path))
        or (host == "xhslink.cn" and parsed.path.startswith("/o/") and _SHORT_PATH.fullmatch(parsed.path))
    )


def _is_gate_title(title: str) -> bool:
    value = _title_key(title)
    if value in _SHELL_TITLES:
        return True
    value = value.removeprefix("小红书").removesuffix("小红书")
    return value in _GATE_TITLES or value in _HTTP_ERROR_TITLES


class XiaohongshuSourceAdapter:
    """Best-effort public note metadata without an authenticated/private API."""

    def __init__(self, fetcher: SafePublicFetcher) -> None:
        self.fetcher = fetcher

    def match(self, url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        except ValueError:
            return False
        # Match the whole known platform so invalid routes fail to a bookmark,
        # not to an unrestricted generic fetch of a login/profile/site page.
        return host in CAPTURE_HOSTS

    def resolve(self, url: str) -> ResolvedSourcePage:
        original = normalize_identity_url(url)
        if not _allowed_route(original):
            raise ValueError(_UNAVAILABLE)
        page = self.fetcher.fetch(url, allowed_hosts=CAPTURE_HOSTS)
        canonical = normalize_identity_url(page.final_url)
        chain = tuple(dict.fromkeys(
            normalize_identity_url(value)
            for value in (original, *page.redirects, canonical)
        ))
        final_id = _note_id(canonical)
        if not final_id or any(not _allowed_route(value) for value in chain):
            raise ValueError(_UNAVAILABLE)
        if any(_note_id(value) not in {None, final_id} for value in chain):
            raise ValueError(_UNAVAILABLE)
        return ResolvedSourcePage(
            platform="xiaohongshu", source_kind="webpage", canonical_url=canonical,
            aliases=chain, page=page,
        )

    def get_public_metadata(
        self, resource: ResolvedSourcePage, fetched_at: str
    ) -> dict[str, Any]:
        parser = HeadMetadataParser(resource.page.text)
        titles = [*parser.titles, parser.meta.get("og:title", "")]
        if any(_is_gate_title(title) for title in titles):
            return empty_metadata(_UNAVAILABLE)
        og_url = parser.meta.get("og:url", "")
        if og_url:
            try:
                declared = normalize_identity_url(urljoin(resource.canonical_url, og_url))
                if _note_id(declared) != _note_id(resource.canonical_url):
                    return empty_metadata(_UNAVAILABLE)
            except ValueError:
                return empty_metadata(_UNAVAILABLE)
        metadata = parse_public_metadata(
            parser, resource.canonical_url, fetched_at, self.fetcher,
            cover_hosts=COVER_HOSTS, include_article_tags=True,
        )
        if not metadata["author"]["value"] or not metadata["cover_url"]["value"]:
            try:
                supplement = extract_xhs_embedded_metadata(resource.page.text, _note_id(resource.canonical_url))
            except Exception:
                # Optional supplementation must never discard standard metadata.
                supplement = None
            if supplement is not None:
                if supplement.author and not metadata["author"]["value"]:
                    metadata["author"] = metadata_field(supplement.author, "platform_public", fetched_at)
                if supplement.cover_url and not metadata["cover_url"]["value"]:
                    try:
                        self.fetcher.url_validator.resolve(supplement.cover_url, allowed_hosts=COVER_HOSTS)
                    except (SafeHttpError, ValueError):
                        pass
                    else:
                        metadata["cover_url"] = metadata_field(supplement.cover_url, "platform_public", fetched_at)
        has_fields = any(
            metadata[name]["value"] for name in ("title", "author", "cover_url", "source_copy")
        )
        if not has_fields and not metadata["platform_tags"]:
            metadata["warnings"].append(_UNAVAILABLE)
        return metadata
