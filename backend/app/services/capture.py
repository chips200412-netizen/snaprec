from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..adapters.douyin import SHARE_METADATA_WARNING
from ..adapters.source import SourceAdapterRegistry
from ..adapters.xiaohongshu import (
    CAPTURE_HOSTS,
    COVER_HOSTS as XIAOHONGSHU_COVER_HOSTS,
    XiaohongshuSourceAdapter,
)
from ..adapters.youtube import CAPTURE_HOSTS as YOUTUBE_HOSTS, YouTubeSourceAdapter
from ..domain.models import (
    CollectionPreview,
    CollectionPreviewRequest,
    OrganizationSuggestion,
    SourceMetadata,
)
from ..repositories.sqlite import normalize_identity_url
from .pipeline import PipelineError
from .organization import DeterministicOrganizationSuggestionService
from .public_metadata import (
    HeadMetadataParser,
    empty_metadata as _empty_metadata,
    metadata_field as _field,
    parse_public_metadata,
)
from .rendered_cover import (
    RenderedCoverProbe, _validate_douyin_target, validate_rendered_cover_candidate,
)
from .rendered_metadata import (
    RenderedMetadataProbe,
    _safe_author_candidate,
    _safe_bilibili_cover_candidate,
    _safe_xiaohongshu_cover_candidate,
    _validate_bilibili_target,
    _validate_xiaohongshu_target,
)
from .safe_http import SafeHttpError, SafePublicFetcher


_URL_CANDIDATE = re.compile(
    r'https?://[^\s<>"]+',
    re.IGNORECASE,
)
_WHOLE_URL = re.compile(r'https?://[^\s<>"]+', re.IGNORECASE)
_URL_SCHEME = re.compile(r'https?://', re.IGNORECASE)
_URL_WRAPPERS = {
    "(": ")", "[": "]", "{": "}", "'": "'",
    "（": "）", "【": "】", "《": "》", "「": "」", "『": "』",
}
_RENDERED_COVER_UNAVAILABLE_WARNING = "平台自动封面暂不可用，仍可保存普通书签。"
_RENDERED_METADATA_UNAVAILABLE_WARNING = "平台自动作者/封面暂不可用，仍可保存普通书签。"
_REJECTED_PAGE_COVER_WARNING = "页面封面链接未通过安全校验，已忽略。"
_PLATFORM_TITLE_SUFFIXES = {
    "bilibili": "_哔哩哔哩_bilibili",
    "xiaohongshu": " - 小红书",
}


class CaptureRepository(Protocol):
    def get_collection_metadata_cache(self, alias_url: str) -> dict | None: ...
    def upsert_collection_metadata_cache(self, payload: dict, aliases: list[str]) -> None: ...
    def create_collection_preview(self, payload: dict) -> None: ...


def extract_single_public_url(input_text: str) -> str:
    matches = list(_URL_CANDIDATE.finditer(input_text))
    if not matches:
        raise PipelineError("CAPTURE_LINK_REQUIRED", "请提供一个公开 HTTP/HTTPS 链接。")
    if len(matches) != 1:
        raise PipelineError("CAPTURE_LINK_AMBIGUOUS", "一次只能预览一个公开链接。")
    match = matches[0]
    candidate = match.group(0)
    whole_input = input_text.strip()
    if _WHOLE_URL.fullmatch(whole_input):
        # A complete URL is authoritative, including legal punctuation in its
        # path/query/fragment. Never guess that its last character is prose.
        candidate = whole_input
    else:
        # Only remove demonstrable outer wrappers in sharing text. Balanced
        # parentheses inside a URL and unwrapped query punctuation remain data.
        prefix = input_text[:match.start()]
        wrappers: list[str] = []
        while prefix and prefix[-1] in _URL_WRAPPERS:
            wrappers.append(prefix[-1])
            prefix = prefix[:-1]
        if (
            not wrappers
            and match.end() == len(input_text.rstrip())
            and candidate.endswith("。")
        ):
            # Preserve the inherited Chinese sentence-final full stop boundary
            # in unquoted sharing prose. Never split on punctuation inside a URL;
            # a standalone or explicitly wrapped URL is preserved exactly.
            candidate = candidate[:-1]
        for opening in reversed(wrappers):
            closing = _URL_WRAPPERS[opening]
            if not candidate.endswith(closing):
                break
            if opening != closing and candidate.count(closing) <= candidate.count(opening):
                break
            candidate = candidate[:-1]
    # A scheme inside the outer URL's query/fragment is data, not a second
    # submitted link. Separately delimited tokens, and joined path links such
    # as "https://a.example/a,https://b.example/b", remain ambiguous.
    data_start = min(
        (candidate.index(separator) for separator in ("?", "#") if separator in candidate),
        default=len(candidate),
    )
    if any(match.start() < data_start for match in list(_URL_SCHEME.finditer(candidate))[1:]):
        raise PipelineError("CAPTURE_LINK_AMBIGUOUS", "一次只能预览一个公开链接。")
    try:
        normalize_identity_url(candidate)
        return candidate
    except ValueError as exc:
        raise PipelineError("CAPTURE_LINK_INVALID", "公开链接格式无效。") from exc


def _classify(url: str) -> tuple[str, str]:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    if host in {"bilibili.com", "www.bilibili.com", "b23.tv"}:
        return "video", "bilibili"
    if host in {"douyin.com", "www.douyin.com", "v.douyin.com"}:
        return "video", "douyin"
    if host in YOUTUBE_HOSTS:
        return "video", "youtube"
    if host in CAPTURE_HOSTS:
        return "webpage", "xiaohongshu"
    return "webpage", "web"


class CaptureService:
    def __init__(
        self,
        repository: CaptureRepository,
        *,
        fetcher: SafePublicFetcher | None = None,
        adapter_registry: Any | None = None,
        source_adapter_registry: SourceAdapterRegistry | None = None,
        cache_ttl_seconds: int = 24 * 60 * 60,
        organization_suggestion_service: Any | None = None,
        rendered_cover_probe: RenderedCoverProbe | None = None,
        rendered_metadata_probe: RenderedMetadataProbe | None = None,
    ) -> None:
        if cache_ttl_seconds <= 0:
            raise ValueError("cache_ttl_seconds must be positive")
        self.repository = repository
        self.fetcher = fetcher or SafePublicFetcher()
        self.adapter_registry = adapter_registry
        self.source_adapter_registry = source_adapter_registry or SourceAdapterRegistry(
            (XiaohongshuSourceAdapter(self.fetcher), YouTubeSourceAdapter(self.fetcher))
        )
        self.cache_ttl_seconds = cache_ttl_seconds
        self.organization_suggestion_service = (
            organization_suggestion_service
            or DeterministicOrganizationSuggestionService()
        )
        self.rendered_cover_probe = rendered_cover_probe
        self.rendered_metadata_probe = rendered_metadata_probe

    def preview(
        self,
        request: CollectionPreviewRequest,
        *,
        preview_id: str | None = None,
        allow_rendered_cover: bool = True,
    ) -> CollectionPreview:
        """Create one immutable preview.

        ``preview_id`` is an internal recovery seam for the collection-import
        worker.  The public single-link API never supplies it and therefore
        retains its existing server-generated identity and response contract.
        ``allow_rendered_cover`` is an explicit orchestration boundary for all
        browser-assisted public metadata: the single-link route keeps it
        enabled while R2.6 batch preview disables it.
        """

        reserved_preview_id = preview_id or str(uuid.uuid4())
        if not reserved_preview_id or len(reserved_preview_id) > 200:
            raise ValueError("preview_id must be a bounded server identity")
        source_url = extract_single_public_url(request.input_text)
        lookup_url = normalize_identity_url(source_url)
        now = datetime.now(UTC)
        cached = self.repository.get_collection_metadata_cache(lookup_url)
        try:
            cache_is_fresh = (
                cached is not None
                and datetime.fromisoformat(cached["expires_at"]) > now
            )
        except (KeyError, TypeError, ValueError):
            cache_is_fresh = False
        if cache_is_fresh and not request.refresh_metadata:
            capture = cached
        else:
            capture = self._capture(source_url, now)
        rendered_cover_added = False
        rendered_metadata_added = False
        if allow_rendered_cover:
            # Legacy and batch-created snapshots are also immutable while fresh.
            if not cache_is_fresh or request.refresh_metadata:
                rendered_cover_added = self._with_rendered_cover(
                    capture, fetched_at=now.isoformat()
                )
            # XHS fresh cache reuse includes legacy and batch-created entries
            # without a probe status: no new external request until refresh/TTL.
            if capture["platform"] != "xiaohongshu" or not cache_is_fresh or request.refresh_metadata:
                rendered_metadata_added = self._with_rendered_metadata(
                    capture, fetched_at=now.isoformat()
                )
        if not cache_is_fresh or request.refresh_metadata:
            # Reconcile only the newly fetched snapshot after every fallback.
            # A rejected candidate is not the final outcome once another public
            # cover URL passed validation. Other warnings and fresh cache stay.
            metadata = capture["metadata"]
            title = metadata["title"]
            suffix = _PLATFORM_TITLE_SUFFIXES.get(capture["platform"], "")
            if (suffix and title["source"] in {"open_graph", "page_metadata", "platform_public"}
                    and title["value"].endswith(suffix)):
                # One precise site decoration, never hashtags or user/share text.
                clean_title = title["value"][:-len(suffix)]
                if clean_title.strip():
                    title["value"] = clean_title
            cover = metadata["cover_url"]
            if cover["value"] and cover["source"] in {"open_graph", "platform_public"}:
                metadata["warnings"] = [
                    warning for warning in metadata["warnings"]
                    if warning != _REJECTED_PAGE_COVER_WARNING
                ]
        if (
            not cache_is_fresh
            or request.refresh_metadata
            or rendered_cover_added
            or rendered_metadata_added
        ):
            canonical_alias = (
                [normalize_identity_url(capture["canonical_url"])]
                if capture["canonical_url"]
                else []
            )
            self.repository.upsert_collection_metadata_cache(
                capture,
                [
                    lookup_url,
                    *canonical_alias,
                    *capture.pop("aliases", []),
                ],
            )
        created_at = now.isoformat()
        # Metadata cache is shared by URL, but share text belongs to this caller.
        # Copy the public snapshot before overlaying any per-preview fallback.
        cached_metadata = SourceMetadata.model_validate(capture["metadata"]).model_dump(
            mode="json"
        )
        metadata = self._without_cached_share_metadata(cached_metadata)
        if capture["platform"] == "xiaohongshu" and not metadata["source_copy"]["value"]:
            share_text = request.input_text.replace(source_url, "", 1).strip(
                " \t\r\n，。；：！？、,.;:!?()（）[]【】《》「」『』"
            )
            metadata["source_copy"] = _field(share_text, "share_text", created_at)
        elif capture["platform"] in {"bilibili", "douyin"}:
            metadata = self._with_per_preview_share_metadata(
                source_url,
                capture["canonical_url"],
                request.input_text,
                created_at,
                metadata,
            )
        try:
            suggestion = self.organization_suggestion_service.suggest(
                SourceMetadata.model_validate(metadata),
                capture["source_kind"],
                capture["platform"],
            )
            suggestion = OrganizationSuggestion.model_validate(suggestion)
        except Exception:
            suggestion = OrganizationSuggestion(
                primary_category="",
                secondary_category="",
                tags=[],
                method="deterministic",
                status="failed",
            )
        preview = {
            "preview_id": reserved_preview_id,
            "original_input": request.input_text,
            "source_url": source_url,
            "canonical_url": capture["canonical_url"],
            "identity_url": capture["identity_url"],
            "source_kind": capture["source_kind"],
            "platform": capture["platform"],
            "metadata_status": capture["metadata_status"],
            "metadata": metadata,
            "organization_suggestion": suggestion.model_dump(mode="json"),
            "created_at": created_at,
            "expires_at": (now + timedelta(seconds=self.cache_ttl_seconds)).isoformat(),
        }
        validated = CollectionPreview.model_validate(preview)
        self.repository.create_collection_preview(validated.model_dump(mode="json"))
        return validated

    def _with_rendered_cover(
        self, capture: dict[str, Any], *, fetched_at: str
    ) -> bool:
        probe = self.rendered_cover_probe
        if probe is None or capture.get("platform") != "douyin":
            return False
        try:
            metadata = SourceMetadata.model_validate(capture.get("metadata", {})).model_dump(
                mode="json"
            )
        except Exception:
            return False
        metadata_probe = getattr(probe, "probe_metadata", None)
        structured = callable(metadata_probe)
        if structured:
            skip = (
                bool(metadata["author"]["value"] and metadata["cover_url"]["value"])
                or capture.get("rendered_metadata_status") in {"found", "partial", "unavailable"}
            )
        else:
            skip = (
                bool(metadata["cover_url"]["value"])
                or capture.get("rendered_cover_status") in {"found", "unavailable"}
            )
        if skip:
            return False
        canonical_url = str(capture.get("canonical_url") or "")
        parsed = urlsplit(canonical_url)
        identity_match = re.fullmatch(r"/video/([0-9]+)", parsed.path)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold().rstrip(".") != "www.douyin.com"
            or identity_match is None
            or _validate_douyin_target(canonical_url, identity_match.group(1)) is None
        ):
            return False
        author = None
        cover = None
        try:
            if structured:
                supplement = metadata_probe("douyin", canonical_url, identity_match.group(1))
                author = _safe_author_candidate(getattr(supplement, "author", ""))
                cover = validate_rendered_cover_candidate(getattr(supplement, "cover_url", ""))
            else:
                cover = validate_rendered_cover_candidate(
                    probe.probe("douyin", canonical_url, identity_match.group(1))
                )
        except Exception:
            cover = None
        if cover and not metadata["cover_url"]["value"]:
            try:
                self.fetcher.url_validator.resolve(cover)
            except Exception:
                cover = None
        accepted = False
        if author and not metadata["author"]["value"]:
            metadata["author"] = _field(author, "platform_public", fetched_at)
            accepted = True
        if cover and not metadata["cover_url"]["value"]:
            metadata["cover_url"] = _field(cover, "platform_public", fetched_at)
            accepted = True
        if accepted:
            capture["metadata_status"] = "recognized"
        has_cover = bool(metadata["cover_url"]["value"])
        capture["rendered_cover_status"] = "found" if has_cover else "unavailable"
        complete = has_cover and (bool(metadata["author"]["value"]) if structured else True)
        warning = _RENDERED_METADATA_UNAVAILABLE_WARNING if structured else _RENDERED_COVER_UNAVAILABLE_WARNING
        if structured:
            capture["rendered_metadata_status"] = (
                "found" if complete else "partial"
                if metadata["author"]["value"] or has_cover else "unavailable"
            )
        if complete:
            metadata["warnings"] = [item for item in metadata["warnings"] if item != warning]
        else:
            metadata["warnings"] = list(dict.fromkeys([*metadata["warnings"], warning]))
        capture["metadata"] = SourceMetadata.model_validate(metadata).model_dump(
            mode="json"
        )
        return True

    def _with_rendered_metadata(
        self, capture: dict[str, Any], *, fetched_at: str
    ) -> bool:
        probe = self.rendered_metadata_probe
        platform = str(capture.get("platform") or "")
        if probe is None or platform not in {"bilibili", "xiaohongshu"}:
            return False
        try:
            metadata = SourceMetadata.model_validate(capture.get("metadata", {})).model_dump(
                mode="json"
            )
        except Exception:
            return False
        if (
            (
                metadata["author"]["value"]
                and metadata["cover_url"]["value"]
            )
            or capture.get("rendered_metadata_status")
            in {"found", "partial", "unavailable"}
        ):
            return False

        target = ""
        expected_identity = ""
        if platform == "bilibili":
            candidates = [
                str(capture.get("canonical_url") or ""),
                str(capture.get("identity_url") or ""),
            ]
            for candidate in candidates:
                if not candidate:
                    continue
                match = re.fullmatch(
                    r"/video/(BV[0-9A-Za-z]+)", urlsplit(candidate).path
                )
                identity = match.group(1) if match else ""
                validated = _validate_bilibili_target(candidate, identity)
                if validated:
                    target, expected_identity = validated, identity
                    break
        else:
            candidate = str(capture.get("canonical_url") or "")
            match = re.fullmatch(
                r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?",
                urlsplit(candidate).path,
            )
            identity = match.group(1).casefold() if match else ""
            validated = _validate_xiaohongshu_target(candidate, identity)
            if validated:
                target, expected_identity = validated, identity
        if not target:
            return False

        accepted = False
        try:
            supplement = probe.probe(platform, target, expected_identity)
        except Exception:
            supplement = None
        author = _safe_author_candidate(getattr(supplement, "author", ""))
        author_source = str(getattr(supplement, "author_source", ""))
        cover_source = str(getattr(supplement, "cover_source", ""))
        if platform == "bilibili":
            cover = _safe_bilibili_cover_candidate(
                getattr(supplement, "cover_url", "")
            )
            author_source = cover_source = "platform_public"
        else:
            cover = _safe_xiaohongshu_cover_candidate(
                getattr(supplement, "cover_url", ""), target
            )
            if author_source not in {"open_graph", "page_metadata"}:
                author = None
            if cover_source != "open_graph":
                cover = None
        if cover:
            try:
                if platform == "xiaohongshu":
                    self.fetcher.url_validator.resolve(
                        cover, allowed_hosts=XIAOHONGSHU_COVER_HOSTS
                    )
                else:
                    self.fetcher.url_validator.resolve(cover)
            except Exception:
                cover = None
        if author and not metadata["author"]["value"]:
            metadata["author"] = _field(author, author_source, fetched_at)
            accepted = True
        if cover and not metadata["cover_url"]["value"]:
            metadata["cover_url"] = _field(cover, cover_source, fetched_at)
            accepted = True

        complete = bool(
            metadata["author"]["value"] and metadata["cover_url"]["value"]
        )
        if complete:
            metadata["warnings"] = [
                warning
                for warning in metadata["warnings"]
                if warning != _RENDERED_METADATA_UNAVAILABLE_WARNING
            ]
            capture["rendered_metadata_status"] = "found"
        else:
            metadata["warnings"] = list(dict.fromkeys([
                *metadata["warnings"],
                _RENDERED_METADATA_UNAVAILABLE_WARNING,
            ]))
            capture["rendered_metadata_status"] = (
                "partial" if accepted else "unavailable"
            )
        if accepted:
            capture["canonical_url"] = target
            capture["identity_url"] = normalize_identity_url(target)
            capture["source_kind"] = (
                "video" if platform == "bilibili" else "webpage"
            )
            capture["metadata_status"] = (
                "recognized" if platform == "bilibili" or self._has_provenance(metadata, {"platform_public"}) else "generic"
            )
        capture["metadata"] = SourceMetadata.model_validate(metadata).model_dump(
            mode="json"
        )
        return True

    def _capture(self, source_url: str, now: datetime) -> dict[str, Any]:
        fetched_at = now.isoformat()
        expires_at = (now + timedelta(seconds=self.cache_ttl_seconds)).isoformat()
        source_capture = self._capture_source_platform(source_url, fetched_at, expires_at)
        if source_capture is not None:
            return source_capture
        specialized = self._capture_known_platform(source_url, fetched_at, expires_at)
        if specialized is not None:
            return specialized
        try:
            response = self.fetcher.fetch(source_url)
            if any(
                self.source_adapter_registry.matching(target) is not None
                for target in (*response.redirects, response.final_url)
            ):
                # A generic external shortener is not an alternate entry into
                # platform HTML extraction. Keep its original bookmark instead.
                raise SafeHttpError("HOST_NOT_ALLOWED")
            canonical_url = normalize_identity_url(response.final_url)
            metadata = self._parse_metadata(response.text, canonical_url, fetched_at)
            status = "generic" if self._has_metadata(metadata) else "metadata_unavailable"
            aliases = [normalize_identity_url(alias) for alias in response.redirects]
        except (SafeHttpError, ValueError):
            canonical_url = ""
            metadata = _empty_metadata("公开元信息暂不可用，仍可保存普通书签。")
            status = "metadata_unavailable"
            aliases = []
        source_kind, platform = _classify(canonical_url or source_url)
        return {
            "identity_url": normalize_identity_url(canonical_url or source_url),
            "canonical_url": canonical_url,
            "source_kind": source_kind,
            "platform": platform,
            "metadata_status": status,
            "metadata": metadata,
            "fetched_at": fetched_at,
            "expires_at": expires_at,
            "aliases": aliases,
        }

    def _capture_source_platform(
        self, source_url: str, fetched_at: str, expires_at: str
    ) -> dict[str, Any] | None:
        adapter = self.source_adapter_registry.matching(source_url)
        if adapter is None:
            return None
        try:
            resource = adapter.resolve(source_url)
            metadata = SourceMetadata.model_validate(
                adapter.get_public_metadata(resource, fetched_at)
            ).model_dump(mode="json")
            canonical_url = normalize_identity_url(resource.canonical_url)
            status = "recognized" if self._has_provenance(metadata, {"platform_public"}) else "generic" if self._has_provenance(
                metadata, {"open_graph", "page_metadata", "page_description"}
            ) else "metadata_unavailable"
            return {
                "identity_url": canonical_url,
                "canonical_url": canonical_url,
                "source_kind": resource.source_kind,
                "platform": resource.platform,
                "metadata_status": status,
                "metadata": metadata,
                "fetched_at": fetched_at,
                "expires_at": expires_at,
                "aliases": list(resource.aliases),
            }
        except Exception:
            # Never retry a rejected platform route through unrestricted HTML
            # capture, or disclose URLs/transport details from exceptions.
            source_kind, platform = _classify(source_url)
            return {
                "identity_url": normalize_identity_url(source_url),
                "canonical_url": "",
                "source_kind": source_kind,
                "platform": platform,
                "metadata_status": "metadata_unavailable",
                "metadata": _empty_metadata("平台公开元信息暂不可用，仍可保存普通书签。"),
                "fetched_at": fetched_at,
                "expires_at": expires_at,
                "aliases": [],
            }

    def _capture_known_platform(
        self,
        source_url: str,
        fetched_at: str,
        expires_at: str,
    ) -> dict[str, Any] | None:
        if self.adapter_registry is None:
            return None
        adapter = self.adapter_registry.matching(source_url)
        if adapter is None:
            return None
        try:
            resolved = adapter.resolve(source_url)
            if resolved.platform not in {"bilibili", "douyin"}:
                return None
            public = adapter.get_metadata(resolved)
            cover = getattr(public, "cover_url", "")
            warning_values = [*getattr(public, "warnings", [])]
            warnings = list(dict.fromkeys(warning_values))
            if cover:
                try:
                    self.fetcher.url_validator.resolve(cover)
                except (SafeHttpError, ValueError):
                    cover = ""
                    warnings.append("页面封面链接未通过安全校验，已忽略。")
            metadata = SourceMetadata.model_validate({
                "title": _field(
                    getattr(public, "title", "") or "", "platform_public", fetched_at
                ),
                "author": _field(
                    getattr(public, "author", "") or "", "platform_public", fetched_at
                ),
                "cover_url": _field(cover or "", "platform_public", fetched_at),
                "source_copy": _field(
                    getattr(public, "description", "") or "",
                    "platform_description",
                    fetched_at,
                ),
                "platform_tags": [
                    {"value": value, "source": "platform_public"}
                    for value in (getattr(public, "tags", []) or []) if str(value).strip()
                ],
                "warnings": warnings,
            }).model_dump(mode="json")
            canonical_url = resolved.canonical_url or source_url
            has_platform_public = self._has_provenance(
                metadata, {"platform_public", "platform_description"}
            )
            return {
                "identity_url": normalize_identity_url(canonical_url),
                "canonical_url": canonical_url,
                "source_kind": "video",
                "platform": resolved.platform,
                "metadata_status": "recognized" if has_platform_public else "metadata_unavailable",
                "metadata": metadata,
                "fetched_at": fetched_at,
                "expires_at": expires_at,
                "aliases": [
                    normalize_identity_url(alias)
                    for alias in (source_url, *getattr(resolved, "aliases", ()))
                ],
            }
        except Exception:
            source_kind, platform = _classify(source_url)
            return {
                "identity_url": normalize_identity_url(source_url),
                "canonical_url": "",
                "source_kind": source_kind,
                "platform": platform,
                "metadata_status": "metadata_unavailable",
                "metadata": _empty_metadata(
                    "平台公开元信息暂不可用，仍可保存普通书签。"
                ),
                "fetched_at": fetched_at,
                "expires_at": expires_at,
            }

    @staticmethod
    def _without_cached_share_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        public = SourceMetadata.model_validate(metadata).model_dump(mode="json")
        for name in ("title", "author", "cover_url", "source_copy"):
            if public[name]["source"] == "share_text":
                public[name] = _field("", "none", "")
        public_tags = [
            tag for tag in public["platform_tags"] if tag["source"] != "share_text"
        ]
        public["platform_tags"] = public_tags
        # Historical cache rows can contain only the caller-owned warning even
        # when every value came from public metadata. Never carry that prior
        # caller signal into a new preview; current share use adds it back below.
        public["warnings"] = [
            warning
            for warning in public["warnings"]
            if warning != SHARE_METADATA_WARNING
        ]
        return public

    def _with_per_preview_share_metadata(
        self,
        source_url: str,
        canonical_url: str,
        input_text: str,
        fetched_at: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if self.adapter_registry is None:
            return metadata
        adapter = None
        for candidate in (canonical_url, source_url):
            if not candidate:
                continue
            try:
                adapter = self.adapter_registry.matching(candidate)
            except Exception:
                adapter = None
            if adapter is not None:
                break
        share_reader = getattr(adapter, "get_share_metadata", None)
        if not callable(share_reader):
            return metadata
        try:
            share = share_reader(input_text)
        except Exception:
            return metadata

        used_share = False
        for field_name, attribute in (
            ("title", "title"),
            ("author", "author"),
            ("source_copy", "description"),
        ):
            value = getattr(share, attribute, "") or ""
            if not metadata[field_name]["value"] and str(value).strip():
                metadata[field_name] = _field(str(value), "share_text", fetched_at)
                used_share = True
        if not metadata["platform_tags"]:
            metadata["platform_tags"] = [
                {"value": value, "source": "share_text"}
                for value in (getattr(share, "tags", []) or [])
                if str(value).strip()
            ]
            used_share = bool(metadata["platform_tags"]) or used_share
        if used_share:
            metadata["warnings"] = list(dict.fromkeys([
                *metadata["warnings"],
                *getattr(share, "warnings", []),
            ]))
        return SourceMetadata.model_validate(metadata).model_dump(mode="json")

    def _parse_metadata(
        self, html: str, page_url: str, fetched_at: str
    ) -> dict[str, Any]:
        parser = HeadMetadataParser(html)
        return parse_public_metadata(parser, page_url, fetched_at, self.fetcher)

    @staticmethod
    def _has_metadata(metadata: dict[str, Any]) -> bool:
        return any(
            (
                metadata["title"]["value"],
                metadata["author"]["value"],
                metadata["cover_url"]["value"],
                metadata["source_copy"]["value"],
                metadata["platform_tags"],
            )
        )

    @staticmethod
    def _has_provenance(metadata: dict[str, Any], sources: set[str]) -> bool:
        fields = (
            metadata["title"],
            metadata["author"],
            metadata["cover_url"],
            metadata["source_copy"],
            *metadata["platform_tags"],
        )
        return any(
            str(field.get("value", "")).strip()
            and field.get("source") in sources
            for field in fields
        )
