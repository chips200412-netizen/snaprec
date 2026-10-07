from __future__ import annotations

import multiprocessing
import re
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..adapters.bilibili import _extract_marked_json
from ..adapters.xiaohongshu import (
    COVER_HOSTS as _XIAOHONGSHU_COVER_HOSTS,
    PAGE_HOSTS as _XIAOHONGSHU_PAGE_HOSTS,
    _is_gate_title as _xiaohongshu_is_gate_title,
    _note_id as _xiaohongshu_note_id,
)
from .public_metadata import HeadMetadataParser, safe_author_name as _safe_author_candidate
from .rendered_cover import (
    PinnedConnectProxy,
    _document_response_is_download,
    _enter_worker_containment,
    _start_playwright,
    _terminate_process_tree,
)


_BILIBILI_PAGE_HOST = "www.bilibili.com"
_BILIBILI_COVER_HOSTS = frozenset(
    {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
)
_BVID = re.compile(r"BV[0-9A-Za-z]+")
_MEDIA_PATH_SUFFIXES = (".flv", ".m3u8", ".mov", ".mp4", ".mpd", ".webm")
_MAX_CANDIDATE_URL_LENGTH = 2048
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_BROWSER_REQUESTS = 256
_XIAOHONGSHU_NOTE_ID = re.compile(r"[0-9a-f]{24}")


@dataclass(frozen=True)
class RenderedMetadataSupplement:
    author: str = ""
    cover_url: str = ""
    author_source: str = "platform_public"
    cover_source: str = "platform_public"


class RenderedMetadataProbe(Protocol):
    def probe(
        self, platform: str, canonical_url: str, expected_identity: str
    ) -> RenderedMetadataSupplement | None: ...


def _validate_bilibili_target(url: str, expected_identity: str) -> str | None:
    if not isinstance(expected_identity, str) or _BVID.fullmatch(expected_identity) is None:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != _BILIBILI_PAGE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/video/{expected_identity}"
    ):
        return None
    return f"https://{_BILIBILI_PAGE_HOST}/video/{expected_identity}"


def _safe_bilibili_cover_candidate(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_CANDIDATE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".") not in _BILIBILI_COVER_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path.casefold().endswith(_MEDIA_PATH_SUFFIXES)
    ):
        return None
    return value


def _normalize_bilibili_document_cover(value: object) -> str | None:
    """Upgrade only Bilibili's exact legacy image hosts to the HTTPS contract."""
    if not isinstance(value, str) or len(value) > _MAX_CANDIDATE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "http"
        or host not in _BILIBILI_COVER_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80}
        or parsed.fragment
    ):
        return _safe_bilibili_cover_candidate(value)
    upgraded = urlunsplit(("https", host, parsed.path, parsed.query, ""))
    return _safe_bilibili_cover_candidate(upgraded)


def _extract_bilibili_document_metadata(
    body: bytes, expected_identity: str
) -> RenderedMetadataSupplement | None:
    if not isinstance(body, bytes) or len(body) > _MAX_DOCUMENT_BYTES:
        return None
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    state = _extract_marked_json(document, "__INITIAL_STATE__") or {}
    video_data = state.get("videoData")
    if not isinstance(video_data, dict) or video_data.get("bvid") != expected_identity:
        return None
    owner = video_data.get("owner")
    author = _safe_author_candidate(owner.get("name")) if isinstance(owner, dict) else None
    cover = _normalize_bilibili_document_cover(video_data.get("pic"))
    return RenderedMetadataSupplement(author=author or "", cover_url=cover or "")


def _bilibili_should_block_request(
    resource_type: str, url: str, expected_identity: str | None = None
) -> bool:
    return (
        str(resource_type).casefold() != "document"
        or expected_identity is None
        or _validate_bilibili_target(url, expected_identity) is None
    )


def _bilibili_proxy_host_allowed(host: str) -> bool:
    return host.casefold().rstrip(".") == _BILIBILI_PAGE_HOST


def _bilibili_document_url_matches(url: str, expected_identity: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == _BILIBILI_PAGE_HOST
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/") == f"/video/{expected_identity}"
    )


def _validate_xiaohongshu_target(
    url: str, expected_identity: str
) -> str | None:
    if (
        not isinstance(expected_identity, str)
        or _XIAOHONGSHU_NOTE_ID.fullmatch(expected_identity) is None
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".")
        not in _XIAOHONGSHU_PAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or _xiaohongshu_note_id(url) != expected_identity
    ):
        return None
    return url


def _xiaohongshu_document_url_matches(
    url: str, target: str, expected_identity: str
) -> bool:
    if _validate_xiaohongshu_target(url, expected_identity) is None:
        return False
    try:
        parsed = urlsplit(url)
        expected = urlsplit(target)
    except (TypeError, ValueError):
        return False
    return (
        (parsed.hostname or "").casefold().rstrip(".")
        == (expected.hostname or "").casefold().rstrip(".")
        and parsed.query == expected.query
        and parsed.path.rstrip("/") == expected.path.rstrip("/")
    )


def _safe_xiaohongshu_cover_candidate(
    value: object, page_url: str
) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_CANDIDATE_URL_LENGTH:
        return None
    try:
        candidate = urljoin(page_url, value)
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        len(candidate) > _MAX_CANDIDATE_URL_LENGTH
        or parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".")
        not in _XIAOHONGSHU_COVER_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path
        or parsed.fragment
        or parsed.path.casefold().endswith(_MEDIA_PATH_SUFFIXES)
    ):
        return None
    return candidate


def _extract_xiaohongshu_document_metadata(
    body: bytes, target: str, expected_identity: str
) -> RenderedMetadataSupplement | None:
    if not isinstance(body, bytes) or len(body) > _MAX_DOCUMENT_BYTES:
        return None
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parser = HeadMetadataParser(document)
    titles = [*parser.titles, parser.meta.get("og:title", "")]
    if any(_xiaohongshu_is_gate_title(title) for title in titles):
        return None
    declared_url = parser.meta.get("og:url", "")
    if declared_url:
        try:
            if _xiaohongshu_note_id(urljoin(target, declared_url)) != expected_identity:
                return None
        except (TypeError, ValueError):
            return None
    author, author_source = parser.author(target, value_validator=_safe_author_candidate)
    safe_author = _safe_author_candidate(author)
    cover = _safe_xiaohongshu_cover_candidate(
        parser.meta.get("og:image", ""), target
    )
    return RenderedMetadataSupplement(
        author=safe_author or "",
        cover_url=cover or "",
        author_source=(author_source if safe_author else "page_metadata"),
        cover_source="open_graph",
    )


def _xiaohongshu_should_block_request(
    resource_type: str,
    url: str,
    target: str,
    expected_identity: str | None = None,
) -> bool:
    return (
        str(resource_type).casefold() != "document"
        or expected_identity is None
        or not _xiaohongshu_document_url_matches(
            url, target, expected_identity
        )
    )


def _isolated_metadata_worker(
    sender: Any,
    browser_channel: str,
    timeout_seconds: float,
    platform: str,
    target: str,
    expected_identity: str,
) -> None:
    containment = _enter_worker_containment()
    if containment is None:
        try:
            sender.send((None, "containment-unavailable"))
        finally:
            sender.close()
        return
    diagnostics: list[str] = []
    try:
        probe = PlaywrightRenderedMetadataProbe(
            browser_channel=browser_channel,
            timeout_seconds=timeout_seconds,
            diagnostic_sink=diagnostics.append,
            process_isolation=False,
        )
        result = probe._probe_acquired(platform, target, expected_identity)
        sender.send((result, diagnostics[-1] if diagnostics else None))
    except Exception:
        try:
            sender.send((None, "isolated-worker-failed"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


class PlaywrightRenderedMetadataProbe:
    def __init__(
        self,
        *,
        browser_channel: str = "msedge",
        timeout_seconds: float = 10,
        playwright_starter: Callable[[], Any] = _start_playwright,
        proxy_factory: Callable[..., AbstractContextManager[PinnedConnectProxy]] = PinnedConnectProxy,
        diagnostic_sink: Callable[[str], None] | None = None,
        process_isolation: bool | None = None,
        isolated_worker_target: Callable[..., None] | None = None,
    ) -> None:
        if browser_channel not in {"msedge", "chrome", "chromium"}:
            raise ValueError("unsupported browser channel")
        if timeout_seconds <= 0 or timeout_seconds > 10:
            raise ValueError("timeout_seconds out of range")
        self.browser_channel = browser_channel
        self.timeout_seconds = timeout_seconds
        self.playwright_starter = playwright_starter
        self.proxy_factory = proxy_factory
        self.diagnostic_sink = diagnostic_sink
        self.process_isolation = (
            playwright_starter is _start_playwright and proxy_factory is PinnedConnectProxy
            if process_isolation is None
            else process_isolation
        )
        self.isolated_worker_target = isolated_worker_target or _isolated_metadata_worker
        self._slot = threading.Semaphore(1)
        self._disabled = threading.Event()

    def probe(
        self, platform: str, canonical_url: str, expected_identity: str
    ) -> RenderedMetadataSupplement | None:
        target = (
            _validate_bilibili_target(canonical_url, expected_identity)
            if platform == "bilibili"
            else _validate_xiaohongshu_target(canonical_url, expected_identity)
            if platform == "xiaohongshu"
            else None
        )
        if target is None or self._disabled.is_set() or not self._slot.acquire(False):
            return None
        if self.process_isolation:
            try:
                return self._probe_isolated(platform, target, expected_identity)
            finally:
                self._slot.release()

        completed = threading.Event()
        cancelled = threading.Event()
        outcome: list[RenderedMetadataSupplement | None] = []

        def run() -> None:
            try:
                outcome.append(
                    None
                    if cancelled.is_set()
                    else self._probe_acquired(platform, target, expected_identity)
                )
            except Exception:
                outcome.append(None)
            finally:
                self._slot.release()
                completed.set()

        threading.Thread(target=run, name="rendered-metadata-probe", daemon=True).start()
        if not completed.wait(self.timeout_seconds):
            cancelled.set()
            self._note("watchdog-timeout")
            return None
        return outcome[0] if outcome else None

    def _probe_isolated(
        self, platform: str, target: str, expected_identity: str
    ) -> RenderedMetadataSupplement | None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=self.isolated_worker_target,
            args=(
                sender,
                self.browser_channel,
                self.timeout_seconds,
                platform,
                target,
                expected_identity,
            ),
            name="rendered-metadata-isolate",
            daemon=False,
        )
        deadline = time.monotonic() + self.timeout_seconds
        result: RenderedMetadataSupplement | None = None
        try:
            process.start()
            sender.close()
            if receiver.poll(max(0.0, deadline - time.monotonic())):
                try:
                    candidate, diagnostic = receiver.recv()
                except (EOFError, OSError, ValueError):
                    candidate, diagnostic = None, "isolated-worker-failed"
                if diagnostic:
                    self._note(str(diagnostic))
                if diagnostic == "containment-unavailable":
                    self._disabled.set()
                result = candidate if isinstance(candidate, RenderedMetadataSupplement) else None
            else:
                self._note("watchdog-timeout")
        except Exception:
            self._note("isolated-worker-failed")
        finally:
            try:
                sender.close()
            except OSError:
                pass
            try:
                receiver.close()
            except OSError:
                pass
            if process.pid is not None:
                process.join(timeout=max(0.0, deadline - time.monotonic()))
                if process.is_alive():
                    _terminate_process_tree(process)
                    process.join(timeout=2)
                if process.is_alive():
                    self._disabled.set()
                    result = None
                    self._note("isolate-cleanup-failed")
        if time.monotonic() > deadline or process.exitcode not in {0, None}:
            return None
        return result

    def _probe_acquired(
        self, platform: str, target: str, expected_identity: str
    ) -> RenderedMetadataSupplement | None:
        if platform not in {"bilibili", "xiaohongshu"}:
            return None
        driver = browser = context = page = cdp = None
        deadline = time.monotonic() + self.timeout_seconds
        request_limit = threading.Event()
        popup_seen = threading.Event()
        download_seen = threading.Event()
        cleanup_failed = False
        result: RenderedMetadataSupplement | None = None
        stage = "proxy"

        def remaining_ms() -> int:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError("rendered metadata deadline exceeded")
            return remaining

        try:
            target_host = (urlsplit(target).hostname or "").casefold().rstrip(".")
            with self.proxy_factory(
                host_allowed=(
                    _bilibili_proxy_host_allowed
                    if platform == "bilibili"
                    else lambda host: host.casefold().rstrip(".") == target_host
                ),
                connect_timeout_seconds=min(3, self.timeout_seconds),
                tunnel_timeout_seconds=self.timeout_seconds,
            ) as proxy:
                try:
                    stage = "driver"
                    driver = self.playwright_starter()
                    stage = "browser"
                    browser = driver.chromium.launch(
                        channel=self.browser_channel,
                        headless=True,
                        timeout=remaining_ms(),
                        args=["--disable-quic", "--disable-background-networking"],
                    )
                    stage = "context"
                    context = browser.new_context(
                        accept_downloads=False,
                        service_workers="block",
                        java_script_enabled=False,
                        storage_state={"cookies": [], "origins": []},
                        proxy={"server": proxy.server_url},
                        viewport={"width": 1280, "height": 720},
                    )
                    context.set_default_timeout(remaining_ms())
                    context.route(
                        "**/*",
                        self._route_handler(
                            request_limit.set,
                            platform,
                            target,
                            expected_identity,
                            main_frame_getter=lambda: page.main_frame if page is not None else None,
                        ),
                    )
                    stage = "page"
                    page = context.new_page()
                    cdp = context.new_cdp_session(page)

                    def guard_document_response(event: Mapping[str, Any]) -> None:
                        request = event.get("request")
                        if event.get("resourceType") != "Document" or not isinstance(request, Mapping):
                            return
                        request_id = event.get("requestId")
                        if not isinstance(request_id, str):
                            download_seen.set()
                            return
                        try:
                            if _document_response_is_download(event):
                                download_seen.set()
                                cdp.send(
                                    "Fetch.failRequest",
                                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                                )
                            else:
                                cdp.send("Fetch.continueResponse", {"requestId": request_id})
                        except Exception:
                            download_seen.set()

                    cdp.on("Fetch.requestPaused", guard_document_response)
                    cdp.send(
                        "Fetch.enable",
                        {"patterns": [{
                            "urlPattern": "*",
                            "resourceType": "Document",
                            "requestStage": "Response",
                        }]},
                    )

                    def reject_download(download: Any) -> None:
                        download_seen.set()
                        try:
                            download.cancel()
                        except Exception:
                            pass

                    def reject_popup(popup: Any) -> None:
                        if popup is page:
                            return
                        popup_seen.set()
                        try:
                            popup.close()
                        except Exception:
                            pass

                    page.on("download", reject_download)
                    context.on("page", reject_popup)
                    stage = "goto"
                    response = page.goto(
                        target, wait_until="domcontentloaded", timeout=remaining_ms()
                    )
                    stage = "document"
                    result = self._read_document(
                        platform, response, page.url, target, expected_identity
                    )
                    if request_limit.is_set() or popup_seen.is_set() or download_seen.is_set():
                        result = None
                        self._note(
                            "request-limit" if request_limit.is_set() else
                            "popup-rejected" if popup_seen.is_set() else
                            "download-rejected"
                        )
                finally:
                    if cdp is not None:
                        try:
                            cdp.send("Fetch.disable")
                            cdp.detach()
                        except Exception:
                            cleanup_failed = True
                    for resource in (page, context, browser, driver):
                        if resource is None:
                            continue
                        try:
                            resource.stop() if resource is driver else resource.close()
                        except Exception:
                            cleanup_failed = True
            if time.monotonic() >= deadline:
                cleanup_failed = True
            if request_limit.is_set() or popup_seen.is_set() or download_seen.is_set():
                result = None
            if cleanup_failed:
                self._note("failed-cleanup")
                return None
            return result
        except Exception:
            self._note(f"failed-{stage}")
            return None

    @staticmethod
    def _read_document(
        platform: str,
        response: Any,
        page_url: str,
        target: str,
        expected_identity: str,
    ) -> RenderedMetadataSupplement | None:
        try:
            headers = response.all_headers()
            content_type = str(headers.get("content-type", "")).partition(";")[0].strip().casefold()
            declared = str(headers.get("content-length", "")).strip()
            if (
                response.status != 200
                or (
                    platform == "bilibili"
                    and (
                        not _bilibili_document_url_matches(
                            response.url, expected_identity
                        )
                        or not _bilibili_document_url_matches(
                            page_url, expected_identity
                        )
                    )
                )
                or (
                    platform == "xiaohongshu"
                    and (
                        not _xiaohongshu_document_url_matches(
                            response.url, target, expected_identity
                        )
                        or not _xiaohongshu_document_url_matches(
                            page_url, target, expected_identity
                        )
                    )
                )
                or content_type not in {"text/html", "application/xhtml+xml"}
                or (declared and (not declared.isdigit() or int(declared) > _MAX_DOCUMENT_BYTES))
            ):
                return None
            body = response.body()
        except Exception:
            return None
        if platform == "bilibili":
            return _extract_bilibili_document_metadata(body, expected_identity)
        if platform == "xiaohongshu":
            return _extract_xiaohongshu_document_metadata(
                body, target, expected_identity
            )
        return None

    @staticmethod
    def _route_handler(
        on_request_limit: Callable[[], None] | None = None,
        platform: str = "bilibili",
        target: str = "",
        expected_identity: str | None = None,
        *,
        main_frame_getter: Callable[[], Any] | None = None,
    ) -> Callable[[Any], None]:
        request_count = 0

        def handle(route: Any) -> None:
            nonlocal request_count
            request_count += 1
            request = route.request
            if request_count > _MAX_BROWSER_REQUESTS and on_request_limit is not None:
                on_request_limit()
            should_block = (
                _bilibili_should_block_request(
                    request.resource_type, request.url, expected_identity
                )
                if platform == "bilibili"
                else _xiaohongshu_should_block_request(
                    request.resource_type,
                    request.url,
                    target,
                    expected_identity,
                )
                if platform == "xiaohongshu"
                else True
            )
            try:
                main_frame = main_frame_getter() if main_frame_getter else None
                is_main_navigation = (
                    main_frame is not None
                    and request.frame is main_frame
                    and request.is_navigation_request()
                )
            except Exception:
                is_main_navigation = False
            if request_count > _MAX_BROWSER_REQUESTS or should_block or not is_main_navigation:
                route.abort("blockedbyclient")
                return
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.casefold() not in {
                    "authorization", "cookie", "proxy-authorization", "referer"
                }
            }
            route.continue_(headers=headers)

        return handle

    def _note(self, value: str) -> None:
        if self.diagnostic_sink is not None:
            self.diagnostic_sink(value)
