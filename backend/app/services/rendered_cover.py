from __future__ import annotations

import json
import multiprocessing
import os
import selectors
import signal
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from .public_metadata import safe_author_name
from .safe_http import _PublicUrlValidator, resolve_addresses


_DOUYIN_PAGE_HOST = "www.douyin.com"
_DOUYIN_RESOURCE_SUFFIXES = (
    "douyin.com",
    "douyinstatic.com",
    "douyinpic.com",
    "byteimg.com",
)
_DOUYIN_RESOURCE_HOSTS = frozenset({
    "lf-c-flwb.bytetos.com",
    "lf-cdn-tos.bytescm.com",
    "lf-headquarters-speed.yhgfb-cn-static.com",
    "lf-security-backup.bytegoofy.com",
    "lf-security.bytegoofy.com",
})
_DOUYIN_IMAGE_SUFFIX = "douyinpic.com"
_IMAGE_RESOURCE_SUFFIXES = ("douyinpic.com", "byteimg.com")
_IMAGE_PATH_SUFFIXES = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
_DOWNLOAD_PATH_SUFFIXES = (".crx", ".dmg", ".exe", ".msi", ".pkg", ".zip")
_MEDIA_HOST_MARKERS = ("douyinvod", "bytevcloud", "-vod-", ".vod.")
_MEDIA_PATH_SUFFIXES = (".mp4", ".m3u8", ".flv", ".mov", ".webm", ".mpd")
_DETAIL_RESPONSE_PATH = "/aweme/v1/web/aweme/detail/"
_MAX_BROWSER_REQUESTS = 256
_MAX_CANDIDATE_URL_LENGTH = 2048
_MAX_CONNECT_LINE = 4096
_MAX_CONNECT_HEADERS = 16 * 1024
_MAX_DETAIL_RESPONSE_BYTES = 1_000_000


@dataclass(frozen=True)
class DouyinMetadataSupplement:
    author: str = ""
    cover_url: str = ""


class RenderedCoverProbe(Protocol):
    def probe(
        self, platform: str, canonical_url: str, expected_identity: str
    ) -> str | None: ...


def _host_matches(host: str, suffixes: Iterable[str]) -> bool:
    folded = host.casefold().rstrip(".")
    return any(folded == suffix or folded.endswith(f".{suffix}") for suffix in suffixes)


def _douyin_resource_host_allowed(host: str) -> bool:
    folded = host.casefold().rstrip(".")
    return folded in _DOUYIN_RESOURCE_HOSTS or _host_matches(
        folded, _DOUYIN_RESOURCE_SUFFIXES
    )


def _validate_douyin_target(url: str, expected_identity: str) -> str | None:
    if not isinstance(expected_identity, str) or not expected_identity.isdigit():
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != _DOUYIN_PAGE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/video/{expected_identity}"
    ):
        return None
    return f"https://{_DOUYIN_PAGE_HOST}/video/{expected_identity}"


def _safe_image_candidate(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_CANDIDATE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not _host_matches(host, (_DOUYIN_IMAGE_SUFFIX,))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path.casefold().endswith(_MEDIA_PATH_SUFFIXES)
    ):
        return None
    return value


def validate_rendered_cover_candidate(value: object) -> str | None:
    """Validate the narrow URL contract shared by the probe and its caller."""
    return _safe_image_candidate(value)


def _validated_metadata_supplement(value: object) -> DouyinMetadataSupplement | None:
    """Revalidate the two bounded fields after crossing the worker boundary."""
    if isinstance(value, str):
        # Retain the old worker/testing seam without inventing an author.
        author, cover = "", _safe_image_candidate(value) or ""
    elif isinstance(value, DouyinMetadataSupplement):
        author = safe_author_name(value.author) or ""
        cover = _safe_image_candidate(value.cover_url) or ""
    else:
        return None
    return DouyinMetadataSupplement(author, cover) if author or cover else None


def _detail_response_matches(url: str, expected_identity: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        identities = parse_qs(parsed.query, keep_blank_values=True).get("aweme_id", [])
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == _DOUYIN_PAGE_HOST
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path == _DETAIL_RESPONSE_PATH
        and identities == [expected_identity]
    )


def _first_detail_image(value: object) -> str | None:
    candidates: list[object] = []
    if isinstance(value, str):
        candidates.append(value)
    elif isinstance(value, Mapping):
        url_list = value.get("url_list")
        if isinstance(url_list, Sequence) and not isinstance(url_list, (str, bytes)):
            candidates.extend(url_list[:4])
        candidates.extend((value.get("url"), value.get("uri")))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidates.extend(value[:4])
    for candidate in candidates:
        safe = _safe_image_candidate(candidate)
        if safe is not None:
            return safe
    return None


def _extract_douyin_detail_cover(payload: object, expected_identity: str) -> str | None:
    result = _extract_douyin_detail_metadata(payload, expected_identity)
    return result.cover_url or None if result is not None else None


def _extract_douyin_detail_metadata(
    payload: object, expected_identity: str
) -> DouyinMetadataSupplement | None:
    if not isinstance(payload, Mapping):
        return None
    item = payload.get("aweme_detail")
    if not isinstance(item, Mapping) or str(item.get("aweme_id")) != expected_identity:
        return None
    author_data = item.get("author")
    author = safe_author_name(author_data.get("nickname")) if isinstance(author_data, Mapping) else None
    video = item.get("video")
    cover = None
    if isinstance(video, Mapping):
        for key in ("cover", "dynamic_cover", "origin_cover"):
            cover = _first_detail_image(video.get(key))
            if cover is not None:
                break
    return DouyinMetadataSupplement(author or "", cover or "") if author or cover else None


def _read_detail_response(response: Any) -> object:
    try:
        if response.status != 200:
            return None
        headers = response.all_headers()
        content_type = str(headers.get("content-type", "")).partition(";")[0].strip().casefold()
        declared = str(headers.get("content-length", ""))
        if content_type != "application/json":
            return None
        if declared and (
            not declared.isdigit() or int(declared) > _MAX_DETAIL_RESPONSE_BYTES
        ):
            return None
        body = response.body()
        if len(body) > _MAX_DETAIL_RESPONSE_BYTES:
            return None
        return json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _cover_from_detail_response(response: Any, expected_identity: str) -> str | None:
    return _extract_douyin_detail_cover(_read_detail_response(response), expected_identity)


def _metadata_from_detail_response(
    response: Any, expected_identity: str
) -> DouyinMetadataSupplement | None:
    if not _detail_response_matches(getattr(response, "url", ""), expected_identity):
        return None
    return _extract_douyin_detail_metadata(_read_detail_response(response), expected_identity)


def _document_response_is_download(event: Mapping[str, Any]) -> bool:
    status = event.get("responseStatusCode")
    if not isinstance(status, int) or 300 <= status < 400:
        return False
    headers: dict[str, str] = {}
    raw_headers = event.get("responseHeaders")
    if isinstance(raw_headers, Sequence) and not isinstance(raw_headers, (str, bytes)):
        for item in raw_headers:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name", "")).strip().casefold()
            if name:
                headers[name] = str(item.get("value", "")).strip()
    disposition = headers.get("content-disposition", "").casefold()
    content_type = headers.get("content-type", "").partition(";")[0].strip().casefold()
    return "attachment" in disposition or content_type not in {
        "text/html",
        "application/xhtml+xml",
    }


def _should_block_request(
    resource_type: str, url: str, expected_identity: str | None = None
) -> bool:
    kind = str(resource_type).casefold()
    if kind in {"image", "media", "font"}:
        return True
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return True
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _douyin_resource_host_allowed(host)
        or (
            kind == "document"
            and (
                expected_identity is None
                or _validate_douyin_target(url, expected_identity) is None
            )
        )
        or _host_matches(host, _IMAGE_RESOURCE_SUFFIXES)
        or path.endswith(_IMAGE_PATH_SUFFIXES)
        or path.endswith(_DOWNLOAD_PATH_SUFFIXES)
        or any(marker in host for marker in _MEDIA_HOST_MARKERS)
        or path.endswith(_MEDIA_PATH_SUFFIXES)
        or (
            kind in {"xhr", "fetch"}
            and (
                expected_identity is None
                or not _detail_response_matches(url, expected_identity)
            )
        )
    ):
        return True
    return False


def _parse_connect_target(
    line: bytes,
    host_allowed: Callable[[str], bool] = _douyin_resource_host_allowed,
) -> str | None:
    try:
        method, authority, version = line.decode("ascii").strip().split(" ")
        host, port = authority.rsplit(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    host = host.casefold().rstrip(".")
    if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"} or port != "443":
        return None
    if not host or len(host) > 253 or not host_allowed(host):
        return None
    return host


class _PinnedConnectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, owner: "PinnedConnectProxy") -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _PinnedConnectHandler, bind_and_activate=True)


class _PinnedConnectHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        owner = self.server.owner  # type: ignore[attr-defined]
        self.connection.settimeout(owner.connect_timeout_seconds)
        line = self.rfile.readline(_MAX_CONNECT_LINE + 1)
        if not line or len(line) > _MAX_CONNECT_LINE:
            return
        host = _parse_connect_target(line, owner.host_allowed)
        header_bytes = 0
        while True:
            header = self.rfile.readline(_MAX_CONNECT_LINE + 1)
            header_bytes += len(header)
            if not header or header in {b"\r\n", b"\n"}:
                break
            if len(header) > _MAX_CONNECT_LINE or header_bytes > _MAX_CONNECT_HEADERS:
                host = None
                break
        if host is None:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        upstream: socket.socket | None = None
        try:
            target = owner.validator.resolve(
                f"https://{host}/", allowed_hosts={host}
            )
            upstream = owner.connector(
                (target.addresses[0], 443), owner.connect_timeout_seconds
            )
            upstream.settimeout(owner.tunnel_timeout_seconds)
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            owner.relay(self.connection, upstream)
        except Exception:
            return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class PinnedConnectProxy(AbstractContextManager["PinnedConnectProxy"]):
    def __init__(
        self,
        *,
        dns_resolver: Callable[[str], Iterable[str]] = resolve_addresses,
        connector: Callable[[tuple[str, int], float], socket.socket] | None = None,
        host_allowed: Callable[[str], bool] = _douyin_resource_host_allowed,
        connect_timeout_seconds: float = 3,
        tunnel_timeout_seconds: float = 12,
    ) -> None:
        self.validator = _PublicUrlValidator(dns_resolver=dns_resolver)
        self.connector = connector or self._connect
        self.host_allowed = host_allowed
        self.connect_timeout_seconds = connect_timeout_seconds
        self.tunnel_timeout_seconds = tunnel_timeout_seconds
        self._server: _PinnedConnectServer | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _connect(address: tuple[str, int], timeout: float) -> socket.socket:
        return socket.create_connection(address, timeout=timeout)

    @property
    def server_url(self) -> str:
        if self._server is None:
            raise RuntimeError("proxy is not running")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "PinnedConnectProxy":
        if self._server is not None:
            raise RuntimeError("proxy already started")
        self._server = _PinnedConnectServer(self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="rendered-cover-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def relay(self, client: socket.socket, upstream: socket.socket) -> None:
        deadline = time.monotonic() + self.tunnel_timeout_seconds
        selector = selectors.DefaultSelector()
        try:
            client.setblocking(False)
            upstream.setblocking(False)
            selector.register(client, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, client)
            while time.monotonic() < deadline:
                events = selector.select(timeout=0.25)
                if not events:
                    continue
                for key, _ in events:
                    source = key.fileobj
                    destination = key.data
                    try:
                        chunk = source.recv(64 * 1024)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        return
                    view = memoryview(chunk)
                    while view and time.monotonic() < deadline:
                        try:
                            sent = destination.send(view)
                        except (BlockingIOError, InterruptedError):
                            time.sleep(0.005)
                            continue
                        if sent <= 0:
                            return
                        view = view[sent:]
        finally:
            selector.close()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)


def _start_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _enter_worker_containment() -> object | None:
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            return None
        return True

    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, kernel32.GetCurrentProcess()
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return job


def _isolated_probe_worker(
    sender: Any,
    browser_channel: str,
    timeout_seconds: float,
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
        probe = PlaywrightRenderedCoverProbe(
            browser_channel=browser_channel,
            timeout_seconds=timeout_seconds,
            diagnostic_sink=diagnostics.append,
            process_isolation=False,
        )
        result = probe._probe_acquired(target, expected_identity)
        sender.send((result, diagnostics[-1] if diagnostics else None))
    except Exception:
        try:
            sender.send((None, "isolated-worker-failed"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _terminate_process_tree(process: Any) -> None:
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        try:
            process.kill()
        except (AttributeError, OSError):
            pass


class PlaywrightRenderedCoverProbe:
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
            playwright_starter is _start_playwright
            and proxy_factory is PinnedConnectProxy
            if process_isolation is None
            else process_isolation
        )
        self.isolated_worker_target = (
            isolated_worker_target or _isolated_probe_worker
        )
        self._slot = threading.Semaphore(1)
        self._disabled = threading.Event()

    def probe(
        self, platform: str, canonical_url: str, expected_identity: str
    ) -> str | None:
        result = self.probe_metadata(platform, canonical_url, expected_identity)
        return (result.cover_url or None) if result is not None else None

    def probe_metadata(
        self, platform: str, canonical_url: str, expected_identity: str
    ) -> DouyinMetadataSupplement | None:
        if platform != "douyin":
            return None
        target = _validate_douyin_target(canonical_url, expected_identity)
        if (
            target is None
            or self._disabled.is_set()
            or not self._slot.acquire(blocking=False)
        ):
            return None
        if self.process_isolation:
            try:
                return _validated_metadata_supplement(self._probe_isolated(target, expected_identity))
            finally:
                self._slot.release()
        completed = threading.Event()
        cancelled = threading.Event()
        outcome: list[DouyinMetadataSupplement | None] = []

        def run() -> None:
            try:
                outcome.append(
                    None
                    if cancelled.is_set()
                    else self._probe_acquired(target, expected_identity)
                )
            except Exception:
                outcome.append(None)
            finally:
                self._slot.release()
                completed.set()

        worker = threading.Thread(
            target=run,
            name="rendered-cover-probe",
            daemon=True,
        )
        worker.start()
        if not completed.wait(timeout=self.timeout_seconds):
            cancelled.set()
            if self.diagnostic_sink is not None:
                self.diagnostic_sink("watchdog-timeout")
            return None
        return _validated_metadata_supplement(outcome[0]) if outcome else None

    def _probe_isolated(self, target: str, expected_identity: str) -> DouyinMetadataSupplement | None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=self.isolated_worker_target,
            args=(
                sender,
                self.browser_channel,
                self.timeout_seconds,
                target,
                expected_identity,
            ),
            name="rendered-cover-isolate",
            daemon=False,
        )
        deadline = time.monotonic() + self.timeout_seconds
        result: DouyinMetadataSupplement | None = None
        try:
            process.start()
            sender.close()
            wait_seconds = max(0.0, deadline - time.monotonic())
            if receiver.poll(wait_seconds):
                try:
                    candidate, diagnostic = receiver.recv()
                except (EOFError, OSError, ValueError):
                    candidate, diagnostic = None, "isolated-worker-failed"
                if diagnostic and self.diagnostic_sink is not None:
                    self.diagnostic_sink(str(diagnostic))
                if diagnostic == "containment-unavailable":
                    self._disabled.set()
                result = _validated_metadata_supplement(candidate)
            else:
                if self.diagnostic_sink is not None:
                    self.diagnostic_sink("watchdog-timeout")
        except Exception:
            if self.diagnostic_sink is not None:
                self.diagnostic_sink("isolated-worker-failed")
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
                    if self.diagnostic_sink is not None:
                        self.diagnostic_sink("isolate-cleanup-failed")
        if time.monotonic() > deadline or process.exitcode not in {0, None}:
            return None
        return result

    def _probe_acquired(self, target: str, expected_identity: str) -> DouyinMetadataSupplement | None:
        driver = browser = context = page = cdp = None
        deadline = time.monotonic() + self.timeout_seconds

        def remaining_ms() -> int:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError("rendered cover deadline exceeded")
            return remaining

        stage = "proxy"

        def note(value: str) -> None:
            if self.diagnostic_sink is not None:
                self.diagnostic_sink(value)

        result: DouyinMetadataSupplement | None = None
        cleanup_failed = False
        request_limit_exceeded = threading.Event()
        popup_seen = threading.Event()
        download_seen = threading.Event()
        try:
            with self.proxy_factory(
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
                        storage_state={"cookies": [], "origins": []},
                        proxy={"server": proxy.server_url},
                        viewport={"width": 1280, "height": 720},
                    )
                    context.set_default_timeout(remaining_ms())
                    stage = "route"
                    context.route(
                        "**/*",
                        self._route_handler(
                            request_limit_exceeded.set, expected_identity
                        ),
                    )
                    stage = "page"
                    page = context.new_page()
                    stage = "response-guard"
                    cdp = context.new_cdp_session(page)

                    def guard_document_response(event: Mapping[str, Any]) -> None:
                        request = event.get("request")
                        if (
                            event.get("resourceType") != "Document"
                            or not isinstance(request, Mapping)
                        ):
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
                                    {
                                        "requestId": request_id,
                                        "errorReason": "BlockedByClient",
                                    },
                                )
                            else:
                                cdp.send(
                                    "Fetch.continueResponse",
                                    {"requestId": request_id},
                                )
                        except Exception:
                            download_seen.set()

                    cdp.on("Fetch.requestPaused", guard_document_response)
                    cdp.send(
                        "Fetch.enable",
                        {
                            "patterns": [
                                {
                                    "urlPattern": "*",
                                    "resourceType": "Document",
                                    "requestStage": "Response",
                                }
                            ]
                        },
                    )
                    detail_responses: list[Any] = []
                    page.on(
                        "response",
                        lambda response: detail_responses.append(response)
                        if len(detail_responses) < 2
                        and _detail_response_matches(response.url, expected_identity)
                        else None,
                    )
                    def reject_download(download: Any) -> None:
                        download_seen.set()
                        try:
                            download.cancel()
                        except Exception:
                            pass

                    page.on("download", reject_download)
                    def reject_popup(popup: Any) -> None:
                        if popup is page:
                            return
                        popup_seen.set()
                        popup.close()

                    context.on("page", reject_popup)
                    stage = "goto"
                    page.goto(
                        target,
                        wait_until="domcontentloaded",
                        timeout=remaining_ms(),
                    )
                    if _validate_douyin_target(page.url, expected_identity) is None:
                        note("identity-rejected")
                    else:
                        stage = "detail"
                        for response in tuple(detail_responses):
                            result = _metadata_from_detail_response(
                                response, expected_identity
                            )
                            if result is not None:
                                break
                        if result is None:
                            stage = "detail-wait"
                            waited_response = None
                            wait_budget = min(3_000, remaining_ms() - 500)
                            if wait_budget > 0:
                                try:
                                    waited_response = page.wait_for_event(
                                        "response",
                                        predicate=lambda candidate: _detail_response_matches(
                                            candidate.url, expected_identity
                                        ),
                                        timeout=wait_budget,
                                    )
                                    if (
                                        len(detail_responses) < 2
                                        and all(
                                            waited_response is not recorded
                                            for recorded in detail_responses
                                        )
                                    ):
                                        detail_responses.append(waited_response)
                                except Exception:
                                    pass
                            if waited_response is not None:
                                result = _metadata_from_detail_response(
                                    waited_response, expected_identity
                                )
                        if (
                            request_limit_exceeded.is_set()
                            or popup_seen.is_set()
                            or download_seen.is_set()
                        ):
                            note(
                                "request-limit"
                                if request_limit_exceeded.is_set()
                                else "popup-rejected"
                                if popup_seen.is_set()
                                else "download-rejected"
                            )
                            result = None
                finally:
                    # Waiting for a response or reading its body may dispatch a
                    # later navigation. Recheck after both before accepting data.
                    if result is not None and page is not None:
                        try:
                            final_identity_valid = _validate_douyin_target(
                                page.url, expected_identity
                            ) is not None
                        except Exception:
                            final_identity_valid = False
                        if not final_identity_valid:
                            result = None
                            note("identity-rejected")
                    if (
                        request_limit_exceeded.is_set()
                        or popup_seen.is_set()
                        or download_seen.is_set()
                    ):
                        result = None
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
                            if resource is driver:
                                resource.stop()
                            else:
                                resource.close()
                        except Exception:
                            cleanup_failed = True
                    if (
                        request_limit_exceeded.is_set()
                        or popup_seen.is_set()
                        or download_seen.is_set()
                    ):
                        result = None
            if time.monotonic() >= deadline:
                cleanup_failed = True
            if cleanup_failed:
                note("failed-cleanup")
                return None
            return result
        except Exception:
            note(f"failed-{stage}")
            return None

    @staticmethod
    def _route_handler(
        on_request_limit: Callable[[], None] | None = None,
        expected_identity: str | None = None,
    ) -> Callable[[Any], None]:
        request_count = 0

        def handle(route: Any) -> None:
            nonlocal request_count
            request_count += 1
            request = route.request
            if request_count > _MAX_BROWSER_REQUESTS and on_request_limit is not None:
                on_request_limit()
            if (
                request_count > _MAX_BROWSER_REQUESTS
                or _should_block_request(
                    request.resource_type, request.url, expected_identity
                )
            ):
                route.abort("blockedbyclient")
                return
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.casefold() not in {
                    "authorization",
                    "cookie",
                    "proxy-authorization",
                    "referer",
                }
            }
            route.continue_(headers=headers)

        return handle
