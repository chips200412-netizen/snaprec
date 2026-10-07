from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx


DnsResolver = Callable[[str], Iterable[str]]
MonotonicClock = Callable[[], float]

DEFAULT_MAX_REDIRECTS = 3
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_IMAGE_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TOTAL_TIMEOUT_SECONDS = 15.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0

_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_IMAGE_ACCEPT = "image/jpeg,image/png,image/webp"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_PUBLIC_FETCH_ACTIVE: ContextVar[bool] = ContextVar("public_metadata_fetch_active", default=False)
_TRANSPORT_LOGGERS = (
    "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
    "httpcore.proxy", "httpcore.socks",
)
_SAFE_MESSAGES = {
    "UNSAFE_URL": "公开页面地址未通过安全校验。",
    "HOST_NOT_ALLOWED": "公开页面主机不在允许列表中。",
    "DNS_RESOLUTION_FAILED": "公开页面主机解析失败。",
    "UNSAFE_ADDRESS": "公开页面目标不是公网地址。",
    "REDIRECT_MISSING_LOCATION": "公开页面重定向缺少目标地址。",
    "TOO_MANY_REDIRECTS": "公开页面重定向次数过多。",
    "UNSUPPORTED_CONTENT_TYPE": "公开页面返回了不支持的内容类型。",
    "RESPONSE_TOO_LARGE": "公开页面响应超过大小限制。",
    "HTTP_STATUS_ERROR": "公开页面返回了不可用状态。",
    "FETCH_TIMEOUT": "访问公开页面超时。",
    "FETCH_FAILED": "访问公开页面失败。",
}


class SafeHttpError(Exception):
    """A stable public failure that never includes a URL or transport detail."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_SAFE_MESSAGES.get(code, _SAFE_MESSAGES["FETCH_FAILED"]))


class _PublicFetchLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX INFO contains the whole URL; httpcore DEBUG can include request
        # details. Suppress only this fetch's context, never global logger levels
        # or unrelated requests running on another thread.
        return not _PUBLIC_FETCH_ACTIVE.get()


_PUBLIC_FETCH_LOG_FILTER = _PublicFetchLogFilter()


@dataclass(frozen=True, slots=True)
class ValidatedPublicUrl:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SafeFetchResult:
    original_url: str
    final_url: str
    status_code: int
    media_type: str
    body: bytes
    redirects: tuple[str, ...]
    content_type: str

    @property
    def url(self) -> str:
        return self.final_url

    @property
    def content(self) -> bytes:
        return self.body

    @property
    def text(self) -> str:
        # Let HTTPX apply its well-tested charset parsing and safe fallback without
        # exposing or retaining the rest of the response headers.
        return httpx.Response(
            self.status_code,
            headers={"content-type": self.content_type},
            content=self.body,
        ).text


@dataclass(frozen=True, slots=True)
class SafeImageFetchResult:
    final_url: str
    media_type: str
    body: bytes
    redirects: tuple[str, ...]
    content_type: str

    @property
    def url(self) -> str:
        return self.final_url

    @property
    def content(self) -> bytes:
        return self.body


class _PublicUrlValidator:
    """Normalize one URL and resolve every address before it may be requested."""

    def __init__(
        self,
        *,
        dns_resolver: DnsResolver | None = None,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        self.dns_resolver = dns_resolver or resolve_addresses
        self.allowed_hosts = _normalize_allowlist(allowed_hosts)

    def resolve(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> ValidatedPublicUrl:
        normalized, parsed, host, port = _normalize_url(url)
        effective_allowlist = (
            _normalize_allowlist(allowed_hosts)
            if allowed_hosts is not None
            else self.allowed_hosts
        )
        if effective_allowlist is not None and host not in effective_allowlist:
            raise SafeHttpError("HOST_NOT_ALLOWED")
        addresses = _resolve_and_validate(host, self.dns_resolver)
        return ValidatedPublicUrl(
            url=normalized,
            scheme=parsed.scheme,
            host=host,
            port=port,
            addresses=addresses,
        )


class PinnedNetworkBackend(httpcore.SyncBackend):
    """Connect to an IP selected only after all current DNS answers pass."""

    def __init__(
        self,
        resolver: DnsResolver,
        backend: httpcore.SyncBackend | Any | None = None,
    ) -> None:
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
        normalized_host = _normalize_transport_host(host)
        addresses = _resolve_and_validate(normalized_host, self.resolver)
        # httpcore keeps the original Origin for Host and TLS SNI. Only the TCP
        # destination is replaced, so a second OS DNS lookup cannot rebind it.
        return self.backend.connect_tcp(
            addresses[0], port, timeout, local_address, socket_options
        )

    def connect_unix_socket(self, path: str, timeout=None, socket_options=None):
        return self.backend.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        self.backend.sleep(seconds)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport whose TCP connections use only freshly validated IPs."""

    def __init__(self, resolver: DnsResolver) -> None:
        super().__init__(retries=0)
        self._pool = httpcore.ConnectionPool(
            network_backend=PinnedNetworkBackend(resolver), retries=0
        )


class SafePublicFetcher:
    """Fetch a bounded public HTML page through manually validated redirects."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        dns_resolver: DnsResolver | None = None,
        allowed_hosts: Iterable[str] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        if max_redirects < 0 or max_response_bytes < 1:
            raise ValueError("safe HTTP limits must be positive")
        if total_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("safe HTTP timeouts must be positive")

        resolver = dns_resolver or resolve_addresses
        self.url_validator = _PublicUrlValidator(
            dns_resolver=resolver, allowed_hosts=allowed_hosts
        )
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.total_timeout_seconds = total_timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self._clock = (
            monotonic_clock if monotonic_clock is not None else time.monotonic
        )
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                total_timeout_seconds, connect=connect_timeout_seconds
            ),
            follow_redirects=False,
            transport=PinnedHTTPTransport(resolver),
            trust_env=False,
        )

    def fetch(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> SafeFetchResult:
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).addFilter(_PUBLIC_FETCH_LOG_FILTER)
        token = _PUBLIC_FETCH_ACTIVE.set(True)
        try:
            return self._fetch(url, allowed_hosts=allowed_hosts)
        finally:
            _PUBLIC_FETCH_ACTIVE.reset(token)

    def _fetch(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None,
        youtube_oembed: bool = False,
    ) -> SafeFetchResult:
        original_url = url
        current = url
        redirects: list[str] = []
        deadline = self._clock() + (min(15.0, self.total_timeout_seconds) if youtube_oembed else self.total_timeout_seconds)
        byte_limit = min(65536, self.max_response_bytes) if youtube_oembed else self.max_response_bytes
        media_types = frozenset({"application/json"}) if youtube_oembed else _HTML_MEDIA_TYPES
        pending_target: ValidatedPublicUrl | None = None

        while True:
            target = pending_target or self.url_validator.resolve(
                current, allowed_hosts=allowed_hosts
            )
            pending_target = None
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SafeHttpError("FETCH_TIMEOUT")
            response = self._request(target.url, remaining, accept="application/json" if youtube_oembed else "text/html,application/xhtml+xml")
            result: SafeFetchResult | None = None
            try:
                if self._clock() >= deadline:
                    raise SafeHttpError("FETCH_TIMEOUT")
                declared_size = _declared_content_length(response)
                if declared_size is not None and declared_size > byte_limit:
                    raise SafeHttpError("RESPONSE_TOO_LARGE")

                if response.status_code in _REDIRECT_STATUSES:
                    if youtube_oembed:
                        # No redirect, even to the same provider: the fixed
                        # registered endpoint is the only permitted JSON target.
                        raise SafeHttpError("TOO_MANY_REDIRECTS")
                    # Redirect bodies are never used, but still consume at most the
                    # same bounded amount before the connection can be reused.
                    redirect_body = _read_limited(
                        response, self.max_response_bytes, deadline, self._clock
                    )
                    redirect_media_type = _media_type(
                        response.headers.get("content-type", "")
                    )
                    if redirect_media_type not in _HTML_MEDIA_TYPES:
                        raise SafeHttpError("UNSUPPORTED_CONTENT_TYPE")
                    if len(redirects) >= self.max_redirects:
                        raise SafeHttpError("TOO_MANY_REDIRECTS")
                    location = response.headers.get("location")
                    if not location:
                        raise SafeHttpError("REDIRECT_MISSING_LOCATION")
                    try:
                        current = urljoin(target.url, location)
                    except (TypeError, ValueError):
                        raise SafeHttpError("UNSAFE_URL") from None
                    # Record only after the next target has passed URL/DNS checks.
                    validated_redirect = self.url_validator.resolve(
                        current, allowed_hosts=allowed_hosts
                    )
                    current = validated_redirect.url
                    pending_target = validated_redirect
                    redirects.append(current)
                else:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SafeHttpError("HTTP_STATUS_ERROR")
                    content_type = response.headers.get("content-type", "")
                    media_type = _media_type(content_type)
                    if media_type not in media_types:
                        raise SafeHttpError("UNSUPPORTED_CONTENT_TYPE")
                    body = _read_limited(
                        response, byte_limit, deadline, self._clock
                    )
                    result = SafeFetchResult(
                        original_url=original_url,
                        final_url=target.url,
                        status_code=response.status_code,
                        media_type=media_type,
                        body=body,
                        redirects=tuple(redirects),
                        content_type=content_type,
                    )
            except SafeHttpError:
                # Preserve the already-classified primary failure. Cleanup is
                # still attempted, but a secondary close failure must not turn
                # a size/MIME/status/timeout rejection into a different code.
                try:
                    response.close()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    response.close()
                except Exception:
                    pass
                raise SafeHttpError("FETCH_FAILED") from None
            else:
                try:
                    response.close()
                except Exception:
                    # A close failure is primary only after otherwise successful
                    # response processing. Keep it content-free and stable.
                    raise SafeHttpError("FETCH_FAILED") from None
            # A successful read is not complete until EOF and response cleanup
            # have both finished inside the same end-to-end deadline.
            if self._clock() >= deadline:
                raise SafeHttpError("FETCH_TIMEOUT")
            if result is not None:
                return result

    def fetch_youtube_oembed(self, video_id: str) -> SafeFetchResult:
        """A fixed public JSON channel; callers cannot supply an endpoint URL."""
        if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise SafeHttpError("UNSAFE_URL")
        url = "https://www.youtube.com/oembed?" + urlencode({
            "url": f"https://www.youtube.com/watch?v={video_id}", "format": "json",
        })
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).addFilter(_PUBLIC_FETCH_LOG_FILTER)
        token = _PUBLIC_FETCH_ACTIVE.set(True)
        try:
            return self._fetch(url, allowed_hosts={"www.youtube.com"}, youtube_oembed=True)
        finally:
            _PUBLIC_FETCH_ACTIVE.reset(token)

    def _request(self, url: str, remaining: float, *, accept: str = "text/html,application/xhtml+xml") -> httpx.Response:
        timeout = httpx.Timeout(
            remaining,
            connect=min(self.connect_timeout_seconds, remaining),
        )
        # Construct the request independently instead of Client.build_request():
        # client-level cookies, Authorization, and other ambient credentials must
        # never be merged into a public metadata request.
        request = httpx.Request(
            "GET",
            url,
            headers={
                "Accept": accept,
                "Accept-Encoding": "identity",
                "User-Agent": "VideoKnowledgeCapture/0.1",
            },
            extensions={"timeout": timeout.as_dict()},
        )
        try:
            return self.client.send(
                request,
                stream=True,
                auth=None,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise SafeHttpError("FETCH_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise SafeHttpError("FETCH_FAILED") from None
        except SafeHttpError:
            raise
        except Exception:
            raise SafeHttpError("FETCH_FAILED") from None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SafePublicFetcher:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class SafePublicImageFetcher:
    """Fetch a bounded public JPEG, PNG, or WebP through validated redirects."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        dns_resolver: DnsResolver | None = None,
        allowed_hosts: Iterable[str] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_IMAGE_RESPONSE_BYTES,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        if max_redirects < 0 or max_response_bytes < 1:
            raise ValueError("safe HTTP limits must be positive")
        if total_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("safe HTTP timeouts must be positive")

        resolver = dns_resolver or resolve_addresses
        self.url_validator = _PublicUrlValidator(
            dns_resolver=resolver, allowed_hosts=allowed_hosts
        )
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.total_timeout_seconds = total_timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self._clock = (
            monotonic_clock if monotonic_clock is not None else time.monotonic
        )
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(
                total_timeout_seconds, connect=connect_timeout_seconds
            ),
            follow_redirects=False,
            transport=PinnedHTTPTransport(resolver),
        )

    def fetch(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> SafeImageFetchResult:
        for name in _TRANSPORT_LOGGERS:
            logging.getLogger(name).addFilter(_PUBLIC_FETCH_LOG_FILTER)
        token = _PUBLIC_FETCH_ACTIVE.set(True)
        try:
            return self._fetch(url, allowed_hosts=allowed_hosts)
        finally:
            _PUBLIC_FETCH_ACTIVE.reset(token)

    def _fetch(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> SafeImageFetchResult:
        current = url
        redirects: list[str] = []
        deadline = self._clock() + self.total_timeout_seconds
        pending_target: ValidatedPublicUrl | None = None

        while True:
            target = pending_target or self.url_validator.resolve(
                current, allowed_hosts=allowed_hosts
            )
            pending_target = None
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SafeHttpError("FETCH_TIMEOUT")
            response = self._request(target.url, remaining)
            result: SafeImageFetchResult | None = None
            try:
                if self._clock() >= deadline:
                    raise SafeHttpError("FETCH_TIMEOUT")
                declared_size = _declared_content_length(response)
                if declared_size is not None and declared_size > self.max_response_bytes:
                    raise SafeHttpError("RESPONSE_TOO_LARGE")

                if response.status_code in _REDIRECT_STATUSES:
                    # Redirect response bodies are not image assets and their MIME
                    # is not authoritative. Consume only a bounded body so cleanup
                    # and the shared end-to-end deadline remain deterministic.
                    _read_limited(
                        response, self.max_response_bytes, deadline, self._clock
                    )
                    if len(redirects) >= self.max_redirects:
                        raise SafeHttpError("TOO_MANY_REDIRECTS")
                    location = response.headers.get("location")
                    if not location:
                        raise SafeHttpError("REDIRECT_MISSING_LOCATION")
                    try:
                        current = urljoin(target.url, location)
                    except (TypeError, ValueError):
                        raise SafeHttpError("UNSAFE_URL") from None
                    validated_redirect = self.url_validator.resolve(
                        current, allowed_hosts=allowed_hosts
                    )
                    current = validated_redirect.url
                    pending_target = validated_redirect
                    redirects.append(current)
                else:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SafeHttpError("HTTP_STATUS_ERROR")
                    content_type = response.headers.get("content-type", "")
                    media_type = _media_type(content_type)
                    if media_type not in _IMAGE_MEDIA_TYPES:
                        raise SafeHttpError("UNSUPPORTED_CONTENT_TYPE")
                    body = _read_limited(
                        response, self.max_response_bytes, deadline, self._clock
                    )
                    result = SafeImageFetchResult(
                        final_url=target.url,
                        media_type=media_type,
                        body=body,
                        redirects=tuple(redirects),
                        content_type=content_type,
                    )
            except SafeHttpError:
                try:
                    response.close()
                except Exception:
                    pass
                raise
            except Exception:
                try:
                    response.close()
                except Exception:
                    pass
                raise SafeHttpError("FETCH_FAILED") from None
            else:
                try:
                    response.close()
                except Exception:
                    raise SafeHttpError("FETCH_FAILED") from None
            if self._clock() >= deadline:
                raise SafeHttpError("FETCH_TIMEOUT")
            if result is not None:
                return result

    def _request(self, url: str, remaining: float) -> httpx.Response:
        timeout = httpx.Timeout(
            remaining,
            connect=min(self.connect_timeout_seconds, remaining),
        )
        request = httpx.Request(
            "GET",
            url,
            headers={
                "Accept": _IMAGE_ACCEPT,
                "Accept-Encoding": "identity",
                "User-Agent": "VideoKnowledgeCapture/0.1",
            },
            extensions={"timeout": timeout.as_dict()},
        )
        try:
            return self.client.send(
                request,
                stream=True,
                auth=None,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise SafeHttpError("FETCH_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise SafeHttpError("FETCH_FAILED") from None
        except SafeHttpError:
            raise
        except Exception:
            raise SafeHttpError("FETCH_FAILED") from None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> SafePublicImageFetcher:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class SafeUrlResolver:
    """Expand a public URL through the shared safe fetch path.

    ``resolve`` is the architecture-level short-link operation. ``validate`` is
    exposed as a narrow seam for callers that need to validate a standalone URL
    (for example, a canonical or cover URL) without issuing a request.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        dns_resolver: DnsResolver | None = None,
        allowed_hosts: Iterable[str] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self.fetcher = SafePublicFetcher(
            client=client,
            dns_resolver=dns_resolver,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
            total_timeout_seconds=total_timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
            monotonic_clock=monotonic_clock,
        )

    def resolve(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> SafeFetchResult:
        return self.fetcher.fetch(url, allowed_hosts=allowed_hosts)

    def validate(
        self, url: str, *, allowed_hosts: Iterable[str] | None = None
    ) -> ValidatedPublicUrl:
        return self.fetcher.url_validator.resolve(url, allowed_hosts=allowed_hosts)

    def close(self) -> None:
        self.fetcher.close()

    def __enter__(self) -> SafeUrlResolver:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def resolve_addresses(host: str) -> tuple[str, ...]:
    """Resolve all stream addresses for a host without selecting one early."""

    return tuple(
        dict.fromkeys(
            item[4][0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        )
    )


def validate_public_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    try:
        candidates = tuple(dict.fromkeys(str(value).strip() for value in addresses))
    except (TypeError, ValueError):
        raise SafeHttpError("DNS_RESOLUTION_FAILED") from None
    if not candidates or any(not value for value in candidates):
        raise SafeHttpError("DNS_RESOLUTION_FAILED")
    normalized: list[str] = []
    for value in candidates:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise SafeHttpError("DNS_RESOLUTION_FAILED") from None
        if not address.is_global:
            raise SafeHttpError("UNSAFE_ADDRESS")
        normalized.append(address.compressed)
    return tuple(normalized)


def _resolve_and_validate(host: str, resolver: DnsResolver) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = resolver(host)
        except SafeHttpError:
            raise
        except Exception:
            raise SafeHttpError("DNS_RESOLUTION_FAILED") from None
        return validate_public_addresses(addresses)
    return validate_public_addresses((literal.compressed,))


def _normalize_url(url: str) -> tuple[str, SplitResult, str, int]:
    if not isinstance(url, str) or not url or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise SafeHttpError("UNSAFE_URL")
    if "\\" in url:
        raise SafeHttpError("UNSAFE_URL")
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        host_value = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        raise SafeHttpError("UNSAFE_URL") from None
    if (
        scheme not in {"http", "https"}
        or host_value is None
        or parsed.username is not None
        or parsed.password is not None
        or (
            port is not None
            and not (
                (scheme == "http" and port == 80)
                or (scheme == "https" and port == 443)
            )
        )
    ):
        raise SafeHttpError("UNSAFE_URL")
    host = _normalize_host(host_value)
    effective_port = port or (443 if scheme == "https" else 80)
    netloc_host = f"[{host}]" if ":" in host else host
    default_port = (scheme == "http" and effective_port == 80) or (
        scheme == "https" and effective_port == 443
    )
    netloc = netloc_host if default_port else f"{netloc_host}:{effective_port}"
    normalized = urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, "")
    )
    normalized_parsed = urlsplit(normalized)
    return normalized, normalized_parsed, host, effective_port


def _normalize_host(host: str) -> str:
    value = host.casefold().rstrip(".")
    if not value or "%" in value:
        raise SafeHttpError("UNSAFE_URL")
    try:
        literal = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii").casefold()
        except (UnicodeError, ValueError):
            raise SafeHttpError("UNSAFE_URL") from None
        if (
            not value
            or len(value) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(
                    not (character.isascii() and (character.isalnum() or character == "-"))
                    for character in label
                )
                for label in value.split(".")
            )
        ):
            raise SafeHttpError("UNSAFE_URL")
        return value
    return literal.compressed


def _normalize_transport_host(host: str | bytes) -> str:
    try:
        value = host.decode("ascii") if isinstance(host, bytes) else str(host)
    except (UnicodeError, ValueError):
        raise SafeHttpError("UNSAFE_URL") from None
    return _normalize_host(value)


def _normalize_allowlist(
    allowed_hosts: Iterable[str] | None,
) -> frozenset[str] | None:
    if allowed_hosts is None:
        return None
    if isinstance(allowed_hosts, str):
        allowed_hosts = (allowed_hosts,)
    try:
        return frozenset(_normalize_host(str(host)) for host in allowed_hosts)
    except TypeError:
        raise ValueError("allowed_hosts must be an iterable of exact host names") from None


def _declared_content_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None:
        return None
    try:
        size = int(value.strip())
    except (TypeError, ValueError):
        raise SafeHttpError("FETCH_FAILED") from None
    if size < 0:
        raise SafeHttpError("FETCH_FAILED")
    return size


def _read_limited(
    response: httpx.Response,
    limit: int,
    deadline: float,
    clock: MonotonicClock,
) -> bytes:
    content = bytearray()
    try:
        for chunk in response.iter_bytes():
            if clock() >= deadline:
                raise SafeHttpError("FETCH_TIMEOUT")
            content.extend(chunk)
            if len(content) > limit:
                raise SafeHttpError("RESPONSE_TOO_LARGE")
    except SafeHttpError:
        raise
    except httpx.TimeoutException:
        raise SafeHttpError("FETCH_TIMEOUT") from None
    except (httpx.HTTPError, OSError):
        raise SafeHttpError("FETCH_FAILED") from None
    except Exception:
        raise SafeHttpError("FETCH_FAILED") from None
    if clock() >= deadline:
        raise SafeHttpError("FETCH_TIMEOUT")
    return bytes(content)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_IMAGE_RESPONSE_BYTES",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TOTAL_TIMEOUT_SECONDS",
    "PinnedHTTPTransport",
    "PinnedNetworkBackend",
    "SafeFetchResult",
    "SafeImageFetchResult",
    "SafeHttpError",
    "SafePublicFetcher",
    "SafePublicImageFetcher",
    "SafeUrlResolver",
    "ValidatedPublicUrl",
    "resolve_addresses",
    "validate_public_addresses",
]
