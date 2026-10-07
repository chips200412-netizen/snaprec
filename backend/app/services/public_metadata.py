from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from justhtml import JustHTML

from ..domain.models import SourceMetadata, validate_public_url_shape
from .safe_http import SafeHttpError, SafePublicFetcher


class HeadMetadataParser:
    """Read only direct, real HTML head fields from a bounded HTML5 tree."""

    def __init__(self, html: str) -> None:
        # The fetcher caps bytes; also bound tree-building work. This deliberately
        # counts '<' inside text too: an over-complex page can remain a bookmark.
        if html.count("<") > 10_000:
            raise ValueError("Public metadata markup limit exceeded")
        self.meta: dict[str, str] = {}
        self.meta_values: dict[str, list[str]] = {}
        self.raw_meta_values: dict[str, list[str]] = {}
        self.titles: list[str] = []
        self.jsonld: list[str] = []
        # scripting_enabled only selects the HTML noscript tokenizer semantics;
        # JustHTML executes no scripts or requests. Sanitizing first could move
        # or remove source fields. Never serialize/render this untrusted tree.
        document = JustHTML(html, sanitize=False, scripting_enabled=True, fragment=False)
        root = next((
            node for node in document.root.children
            if node.name == "html" and node.namespace == "html"
        ), None)
        head = next((
            node for node in (root.children if root is not None else [])
            if node.name == "head" and node.namespace == "html"
        ), None)
        for node in head.children if head is not None else []:
            if node.namespace != "html":
                continue
            if node.name == "title":
                self.titles.append(node.to_text(separator="", strip=False))
            elif node.name == "meta":
                attrs = node.attrs or {}
                item_author = "author" if (attrs.get("itemprop") or "").casefold() == "author" else ""
                name = (attrs.get("property") or attrs.get("name") or item_author).casefold()
                raw_content = attrs.get("content") or ""
                content = raw_content.strip()
                if name:
                    self.raw_meta_values.setdefault(name, []).append(raw_content)
                if name and content:
                    self.meta.setdefault(name, content)
                    self.meta_values.setdefault(name, []).append(content)
            elif node.name == "script" and (node.attrs or {}).get("type", "").strip().casefold() == "application/ld+json":
                self.jsonld.append(node.to_text(separator="", strip=False))

    @property
    def page_title(self) -> str:
        return self.titles[0] if self.titles else ""

    def author(
        self, page_url: str, *, include_jsonld: bool = True,
        value_validator: Callable[[str], str | None] | None = None,
    ) -> tuple[str, str]:
        # Optional strict consumers must see decoded source characters before
        # strip/split can erase controls. Preserve existing generic semantics.
        values = self.raw_meta_values if value_validator else self.meta_values
        candidates = []
        for key in ("article:author", "og:author", "author"):
            for value in values.get(key, []):
                if not value or is_author_reference(value):
                    continue
                if value_validator is not None:
                    value = value_validator(value)
                    if value is None:
                        return "", "none"
                if value.strip():
                    candidates.append((
                        " ".join(value.split()),
                        "open_graph" if key != "author" else "page_metadata",
                    ))
        if include_jsonld:
            name, ambiguous = _jsonld_author(self.jsonld, page_url, value_validator=value_validator)
            if ambiguous:
                return "", "none"
            if name:
                candidates.append((name, "page_metadata"))
        if len({name for name, _ in candidates}) != 1:
            return "", "none"
        return candidates[0]


_WORK_TYPES = frozenset({"Article", "NewsArticle", "BlogPosting", "VideoObject", "ScholarlyArticle", "TechArticle", "Report"})


def is_author_reference(value: str) -> bool:
    value = value.strip().casefold()
    return value.startswith(("http:", "https:", "//", "/", "#", "mailto:", "www."))


def safe_author_name(value: object) -> str | None:
    """Validate source characters before normalization can erase controls."""
    if not isinstance(value, str):
        return None
    if "\ufffd" in value or any(unicodedata.category(char).startswith("C") for char in value):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if (not normalized or len(normalized) > 200 or is_author_reference(normalized)
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized)):
        return None
    return normalized


def _document_url(value: str, base: str) -> str:
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid document identity")
    host = parsed.hostname.casefold()
    port = parsed.port
    netloc = host if port is None or (parsed.scheme, port) in {("https", 443), ("http", 80)} else f"{host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def _jsonld_author(
    scripts: list[str], page_url: str, *,
    value_validator: Callable[[str], str | None] | None = None,
) -> tuple[str, bool]:
    """Resolve only top-level/graph works and local author references, without I/O.

    Bound total data, object count and depth before interpreting the graph. We do
    not recursively discover works in comments, recommendations or publisher.
    """
    if not scripts:
        return "", False
    if len(scripts) > 16 or sum(len(s.encode("utf-8")) for s in scripts) > 65536:
        return "", True
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Ambiguous JSON-LD field")
                result[key] = value
            return result
        roots = [json.loads(script, object_pairs_hook=unique_object) for script in scripts]
        pending = [(root, 0) for root in roots]
        count = 0
        while pending:
            value, depth = pending.pop()
            count += 1
            if count > 2048 or depth > 16:
                return "", True
            if isinstance(value, dict) and "@context" in value:
                context = value["@context"]
                contexts = context if isinstance(context, list) else [context]
                if not contexts or any(item not in ("https://schema.org", "http://schema.org", "https://schema.org/", "http://schema.org/") for item in contexts):
                    return "", True
            children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
            pending.extend((child, depth + 1) for child in children)
        nodes: list[tuple[dict, bool]] = []
        for root in roots:
            for node in root if isinstance(root, list) else [root]:
                if not isinstance(node, dict):
                    continue
                nodes.append((node, True))
                graph = node.get("@graph", [])
                if isinstance(graph, list):
                    nodes.extend((entry, False) for entry in graph if isinstance(entry, dict))
        def types(node):
            value = node.get("@type", [])
            return {item.removeprefix("https://schema.org/").removeprefix("http://schema.org/")
                    for item in (value if isinstance(value, list) else [value]) if isinstance(item, str)}
        works = [(node, top) for node, top in nodes if types(node) & _WORK_TYPES]
        current = _document_url(page_url, page_url)
        candidates = []
        for node, top in works:
            identities = []
            for key in ("url", "@id", "mainEntityOfPage"):
                value = node.get(key)
                if isinstance(value, dict):
                    value = value.get("@id") or value.get("url")
                if value is not None:
                    if not isinstance(value, str) or not value.strip():
                        return "", True
                    identities.append(_document_url(value, page_url))
            if identities:
                if current in identities and any(identity != current for identity in identities):
                    return "", True
                if all(identity == current for identity in identities):
                    candidates.append(node)
            elif top and len(works) == 1:
                candidates.append(node)
        if len(candidates) != 1:
            return "", bool(candidates) or bool(works)
        refs: dict[str, dict] = {}
        for node, _ in nodes:
            identity = node.get("@id")
            if isinstance(identity, str) and _document_url(identity, page_url) == current:
                key = urljoin(page_url, identity)
                if key in refs and refs[key] != node:
                    return "", True
                refs[key] = node
        author = candidates[0].get("author")
        authors = author if isinstance(author, list) else [author]
        names = []
        for person in authors:
            if isinstance(person, str):
                # A URL is a reference, not a display name.
                if is_author_reference(person):
                    return "", True
                name = person
            elif isinstance(person, dict):
                reference = person.get("@id")
                if reference is not None:
                    if not isinstance(reference, str) or _document_url(reference, page_url) != current:
                        return "", True
                    resolved = refs.get(urljoin(page_url, reference))
                    if resolved is None:
                        return "", True
                    if person.get("name") and person.get("name") != resolved.get("name"):
                        return "", True
                    person = resolved
                if types(person) and not types(person) & {"Person", "Organization"}:
                    return "", True
                name = person.get("name", "")
            elif person is None:
                continue
            else:
                return "", True
            if not isinstance(name, str):
                return "", True
            if value_validator is not None and name:
                name = value_validator(name)
                if name is None:
                    return "", True
            name = " ".join(name.split())
            if name and not is_author_reference(name):
                names.append(name)
        if len(set(names)) > 1:
            return "", True
        return (names[0] if names else ""), False
    except (ValueError, TypeError, RecursionError):
        return "", True


def metadata_field(value: str, source: str, fetched_at: str) -> dict[str, str]:
    text = " ".join(value.strip().split())
    return {
        "value": text,
        "source": source if text else "none",
        "fetched_at": fetched_at if text else "",
    }


def empty_metadata(warning: str = "") -> dict[str, Any]:
    return {
        "title": metadata_field("", "none", ""),
        "author": metadata_field("", "none", ""),
        "cover_url": metadata_field("", "none", ""),
        "source_copy": metadata_field("", "none", ""),
        "platform_tags": [],
        "warnings": [warning] if warning else [],
    }


def parse_public_metadata(
    parser: HeadMetadataParser,
    page_url: str,
    fetched_at: str,
    fetcher: SafePublicFetcher,
    *,
    cover_hosts: Iterable[str] | None = None,
    include_article_tags: bool = False,
) -> dict[str, Any]:
    meta = parser.meta
    og_title = meta.get("og:title", "")
    page_title = parser.page_title
    author, author_source = parser.author(page_url)
    cover = meta.get("og:image", "")
    warnings: list[str] = []
    if cover:
        try:
            cover = urljoin(page_url, cover)
            validate_public_url_shape(cover, label="封面链接")
            if cover_hosts is None:
                fetcher.url_validator.resolve(cover)
            else:
                fetcher.url_validator.resolve(cover, allowed_hosts=cover_hosts)
        except (SafeHttpError, ValueError):
            cover = ""
            warnings.append("页面封面链接未通过安全校验，已忽略。")
    tags = [value.strip() for value in meta.get("keywords", "").split(",") if value.strip()]
    if include_article_tags:
        distinct: dict[str, str] = {}
        for value in [*tags, *parser.meta_values.get("article:tag", [])]:
            value = " ".join(value.split())
            if value:
                distinct.setdefault(value.casefold(), value)
        tags = list(distinct.values())
    return SourceMetadata.model_validate({
        "title": metadata_field(
            og_title or page_title, "open_graph" if og_title else "page_metadata", fetched_at
        ),
        "author": metadata_field(author, author_source, fetched_at),
        "cover_url": metadata_field(cover, "open_graph", fetched_at),
        "source_copy": metadata_field(
            meta.get("og:description") or meta.get("description", ""),
            "page_description", fetched_at,
        ),
        "platform_tags": [{"value": value, "source": "page_metadata"} for value in tags],
        "warnings": warnings,
    }).model_dump(mode="json")
