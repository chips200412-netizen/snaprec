from __future__ import annotations

import hashlib
import heapq
import io
import json
import math
import os
import re
import stat
import threading
import time
import uuid
import warnings
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol

from PIL import Image

from .safe_http import SafeImageFetchResult, SafePublicImageFetcher


_CACHE_VERSION = 1
_CACHE_MARKER = ".collection-cover-cache-v1"
_LOCK_TABLE = ".collection-cover-cache-locks-v1"
_LOCK_STRIPES = 4096
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_RE = re.compile(r"^(?P<key>[0-9a-f]{64})\.(?P<digest>[0-9a-f]{64})\.img$")
_MANIFEST_RE = re.compile(r"^(?P<key>[0-9a-f]{64})\.json$")
_STALE_RE = re.compile(r"^(?P<key>[0-9a-f]{64})\.stale$")
_FAILURE_RE = re.compile(r"^(?P<key>[0-9a-f]{64})\.failure$")
_PART_RE = re.compile(
    r"^(?P<key>[0-9a-f]{64})\.(?P<token>[0-9a-f]{32})\.(?:content|manifest|stale|failure)\.part$"
)
_ALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PIL_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_IMAGE_DECODE_SLOTS = threading.BoundedSemaphore(2)
_CLEANUP_SCAN_QUANTUM = 10_000
_CLEANUP_CANDIDATE_LIMIT = 4_096


class CoverUnavailable(Exception):
    """A content-free signal that the caller should use the formal fallback."""


class ImageFetcher(Protocol):
    def fetch(self, url: str) -> SafeImageFetchResult: ...


@dataclass(frozen=True, slots=True)
class CoverAsset:
    body: bytes
    media_type: str
    content_sha256: str
    width: int
    height: int
    cache_status: Literal["HIT", "MISS", "REFRESH", "STALE"]

    @property
    def etag(self) -> str:
        return f'"sha256-{self.content_sha256}"'


@dataclass(frozen=True, slots=True)
class _CachedAsset:
    body: bytes
    media_type: str
    content_sha256: str
    width: int
    height: int
    fresh_until: float
    content_name: str
    stale_token: str


@dataclass(slots=True)
class _ThreadLockEntry:
    lock: threading.Lock
    references: int = 0


@dataclass(slots=True)
class _PosixLockTableEntry:
    stream: Any
    references: int = 0


@dataclass(frozen=True, slots=True)
class _CleanupCandidate:
    mtime: float
    key: str
    path: Path
    size: int
    kind: Literal["content", "failure", "stale", "manifest"]
    snapshot: tuple[int, int, int, int]


_LOCAL_FILE_LOCKS_GUARD = threading.Lock()
_LOCAL_FILE_LOCKS: dict[tuple[str, int], _ThreadLockEntry] = {}
_POSIX_LOCK_TABLES_GUARD = threading.Lock()
_POSIX_LOCK_TABLES: dict[tuple[str, int, int], _PosixLockTableEntry] = {}


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _finite_timestamp(value: float, delta: float) -> float:
    result = value + delta
    if not math.isfinite(result):
        raise CoverUnavailable()
    return result


class CoverCacheService:
    """Persist and safely refresh derived cover bytes without changing source facts."""

    def __init__(
        self,
        root: str | Path,
        *,
        fetcher: ImageFetcher | None = None,
        fresh_seconds: float = 24 * 60 * 60,
        retention_seconds: float = 30 * 24 * 60 * 60,
        max_cache_bytes: int = 512 * 1024 * 1024,
        max_image_bytes: int = 5 * 1024 * 1024,
        max_dimension: int = 8192,
        max_pixels: int = 32_000_000,
        lease_seconds: float = 60,
        wait_seconds: float = 2,
        failure_retry_seconds: float = 60,
        clock: Any | None = None,
    ) -> None:
        positive_limits = (
            fresh_seconds,
            retention_seconds,
            max_cache_bytes,
            max_image_bytes,
            max_dimension,
            max_pixels,
            lease_seconds,
            failure_retry_seconds,
        )
        if (
            not all(_is_finite_number(value) and value > 0 for value in positive_limits)
            or not _is_finite_number(wait_seconds)
            or wait_seconds < 0
            or lease_seconds > 300
            or wait_seconds > 300
        ):
            raise ValueError("cover cache limits must be positive")
        self.root = _prepare_cache_root(Path(root))
        self._owns_fetcher = fetcher is None
        self._closed = False
        self.fetcher = fetcher or SafePublicImageFetcher(
            max_response_bytes=max_image_bytes
        )
        self.fresh_seconds = float(fresh_seconds)
        self.retention_seconds = float(retention_seconds)
        self.max_cache_bytes = int(max_cache_bytes)
        self.max_image_bytes = int(max_image_bytes)
        self.max_dimension = int(max_dimension)
        self.max_pixels = int(max_pixels)
        self.lease_seconds = float(lease_seconds)
        self.wait_seconds = float(wait_seconds)
        self.failure_retry_seconds = float(failure_retry_seconds)
        self._clock = clock or time.time
        self._thread_locks_guard = threading.Lock()
        self._thread_locks: dict[str, _ThreadLockEntry] = {}
        self._cleanup_lock = threading.Lock()
        self._cleanup_iterator: Any | None = None
        self._cleanup_cycle_total = 0
        self._cleanup_candidates: list[
            tuple[float, int, _CleanupCandidate]
        ] = []
        self._cleanup_candidate_counter = 0
        self._cleanup_retention_candidates: list[
            tuple[float, int, _CleanupCandidate]
        ] = []
        self._cleanup_retention_candidate_counter = 0
        self._cleanup_retention_candidates_truncated = False
        self._cleanup_scan_active = False
        self._verified_images_lock = threading.Lock()
        self._verified_images: dict[tuple[str, str, int, int], None] = {}
        self._lock_table = self.root / _LOCK_TABLE
        self._initialize_lock_table()
        lock_metadata = self._lock_table.stat(follow_symlinks=False)
        self._lock_table_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
        root_metadata = self.root.stat(follow_symlinks=False)
        self._root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self._posix_lock_table_key: tuple[str, int, int] | None = None
        self._posix_lock_table_entry: _PosixLockTableEntry | None = None
        if os.name != "nt":
            self._register_posix_lock_table()
        self._maintenance_guard = threading.Lock()
        self._maintenance_operations = 0
        self._next_maintenance = time.monotonic() + 60
        self.cleanup()

    @staticmethod
    def cache_key(source_url: str) -> str:
        if not isinstance(source_url, str) or not source_url:
            raise CoverUnavailable()
        return hashlib.sha256(source_url.encode("utf-8")).hexdigest()

    def get(self, source_url: str) -> CoverAsset:
        if self._closed or not self._root_is_owned():
            raise CoverUnavailable()
        self._maybe_cleanup()
        key = self.cache_key(source_url)
        with self._singleflight(key):
            now = self._now()
            cached = self._read_cached(key)
            stale_token = self._read_token(self._stale_path(key))
            current_stale_token = stale_token or ""
            if (
                cached is not None
                and cached.fresh_until > now
                and cached.stale_token == current_stale_token
            ):
                self._touch_content(cached.content_name, now)
                return self._public_asset(cached, "HIT")
            if self._read_failure_retry_after(key, current_stale_token) > now:
                if cached is not None:
                    self._touch_content(cached.content_name, now)
                    return self._public_asset(cached, "STALE")
                raise CoverUnavailable()

            process_lock = self._acquire_process_lock(key, wait_seconds=0)
            if process_lock is None:
                if cached is not None:
                    self._touch_content(cached.content_name, now)
                    return self._public_asset(cached, "STALE")
                # A colliding stripe may belong to a different URL, so a cold
                # miss retries the mutex itself rather than only polling this
                # key's manifest.
                process_lock = self._acquire_process_lock(
                    key, wait_seconds=max(self.wait_seconds, self.lease_seconds)
                )
                if process_lock is None:
                    cached = self._read_cached(key)
                    if cached is not None:
                        self._touch_content(cached.content_name, self._now())
                        return self._public_asset(cached, "HIT")
                    raise CoverUnavailable()

            try:
                # A prior process may have committed between our first read and
                # process-mutex acquisition. Recheck before any network I/O.
                current = self._read_cached(key)
                current_stale_token = self._read_token(self._stale_path(key))
                now = self._now()
                if (
                    current is not None
                    and current.fresh_until > now
                    and current.stale_token == (current_stale_token or "")
                ):
                    self._touch_content(current.content_name, now)
                    return self._public_asset(current, "HIT")
                if current is not None:
                    cached = current
                stale_token = current_stale_token
                # A different process may have owned this cold miss while we
                # waited for the stripe and recorded a failure just before it
                # released the lock. Re-read the generation-bound backoff only
                # after acquiring ownership so an overlapping waiter cannot
                # immediately repeat the same failed remote request.
                if (
                    self._read_failure_retry_after(
                        key, current_stale_token or ""
                    )
                    > now
                ):
                    if cached is not None:
                        self._touch_content(cached.content_name, now)
                        return self._public_asset(cached, "STALE")
                    raise CoverUnavailable()
                try:
                    fetched = self.fetcher.fetch(source_url)
                    width, height = validate_image(
                        fetched.body,
                        fetched.media_type,
                        max_bytes=self.max_image_bytes,
                        max_dimension=self.max_dimension,
                        max_pixels=self.max_pixels,
                    )
                    committed = self._commit(
                        key,
                        fetched.body,
                        fetched.media_type,
                        width,
                        height,
                        stale_token=stale_token or "",
                        now=self._now(),
                    )
                    self._owned_unlink(self._failure_path(key))
                    status: Literal["MISS", "REFRESH"] = (
                        "REFRESH" if cached is not None else "MISS"
                    )
                    self.cleanup()
                    return self._public_asset(committed, status)
                except Exception:
                    # Remote, validation and derived-storage failures are all
                    # local to the cover. Preserve last-known-good and never let
                    # transport or path details escape through this boundary.
                    self._record_failure(
                        key, self._now(), stale_token or ""
                    )
                    if cached is not None:
                        self._touch_content(cached.content_name, self._now())
                        return self._public_asset(cached, "STALE")
                    raise CoverUnavailable() from None
            finally:
                self._release_process_lock(process_lock)

    def get_cached(self, source_url: str) -> CoverAsset:
        """Return last-known-good bytes without refreshing or mutating the cache.

        This read path is intentionally separate from ``get`` so callers that
        must not contact the source can still reuse a validated cached cover.
        Cold, invalid, or missing entries fall back through ``CoverUnavailable``.
        """
        if self._closed or not self._root_is_owned():
            raise CoverUnavailable()
        key = self.cache_key(source_url)
        cached = self._read_cached(key)
        if cached is None:
            raise CoverUnavailable()
        stale_token = self._read_token(self._stale_path(key)) or ""
        status: Literal["HIT", "STALE"] = (
            "HIT"
            if cached.fresh_until > self._now()
            and cached.stale_token == stale_token
            else "STALE"
        )
        return self._public_asset(cached, status)

    def mark_stale(self, source_url: str) -> None:
        """Invalidate one URL without deleting its last-known-good bytes."""
        if self._closed or not self._root_is_owned():
            raise CoverUnavailable()
        self._maybe_cleanup()
        key = self.cache_key(source_url)
        self._owned_unlink(self._failure_path(key))
        token = uuid.uuid4().hex
        target = self._stale_path(key)
        part = self.root / f"{key}.{uuid.uuid4().hex}.stale.part"
        payload = json.dumps(
            {"version": _CACHE_VERSION, "token": token},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        try:
            _write_new_file(part, payload)
            if not self._root_is_owned():
                raise CoverUnavailable()
            os.replace(part, target)
        finally:
            self._owned_unlink(part)

    def cleanup(self) -> None:
        """Advance one bounded, non-recursive service-owned cleanup cycle.

        A directory can legitimately contain more than one scan quantum.  Keep
        the scandir cursor between calls and enforce the byte budget only after
        reaching EOF, so later entries cannot be permanently invisible.  The
        accumulator and oldest-candidate heap are bounded; if one pass cannot
        reclaim enough, the next complete cycle continues convergence.
        """
        if not self._root_is_owned():
            self._reset_cleanup_cycle()
            return
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            try:
                now = self._now()
            except CoverUnavailable:
                self._reset_cleanup_cycle_locked()
                return
            if self._cleanup_iterator is None:
                try:
                    self._cleanup_iterator = os.scandir(self.root)
                except OSError:
                    self._reset_cleanup_cycle_locked()
                    return
                self._cleanup_cycle_total = 0
                self._cleanup_candidates.clear()
                self._cleanup_candidate_counter = 0
                self._cleanup_retention_candidates.clear()
                self._cleanup_retention_candidate_counter = 0
                self._cleanup_retention_candidates_truncated = False
                self._cleanup_scan_active = True

            exhausted = False
            for _index in range(_CLEANUP_SCAN_QUANTUM):
                try:
                    entry = next(self._cleanup_iterator)
                except StopIteration:
                    exhausted = True
                    break
                except OSError:
                    self._reset_cleanup_cycle_locked()
                    return
                if entry.name in {_CACHE_MARKER, _LOCK_TABLE}:
                    continue
                matched = next(
                    (
                        pattern.fullmatch(entry.name)
                        for pattern in (
                            _MANIFEST_RE,
                            _CONTENT_RE,
                            _STALE_RE,
                            _FAILURE_RE,
                            _PART_RE,
                        )
                        if pattern.fullmatch(entry.name) is not None
                    ),
                    None,
                )
                if matched is None:
                    continue
                path = self.root / entry.name
                try:
                    # pathlib's Windows stat path exposes stable file IDs;
                    # os.DirEntry.stat may report zero dev/ino placeholders.
                    metadata = path.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _is_reparse_stat(metadata) or not stat.S_ISREG(metadata.st_mode):
                    continue
                self._cleanup_cycle_total += metadata.st_size
                removed_size = self._inspect_cleanup_entry(
                    entry.name, path, metadata, now
                )
                self._cleanup_cycle_total = max(
                    0, self._cleanup_cycle_total - removed_size
                )

            if not exhausted:
                # `_maybe_cleanup` advances an active cursor on every subsequent
                # get/mark operation instead of waiting another 60s/128 calls.
                self._cleanup_scan_active = True
                return

            iterator = self._cleanup_iterator
            self._cleanup_iterator = None
            if iterator is not None:
                try:
                    iterator.close()
                except OSError:
                    pass
            reclaimed_size = 0
            retention_candidates = sorted(
                (entry[2] for entry in self._cleanup_retention_candidates),
                key=lambda candidate: (candidate.mtime, candidate.path.name),
            )
            for candidate in retention_candidates:
                removed_size = self._evict_content(
                    candidate.key,
                    candidate.path.name,
                    candidate.path,
                    older_than=now - self.retention_seconds,
                    expected_snapshot=candidate.snapshot,
                )
                reclaimed_size += removed_size
                self._cleanup_cycle_total = max(
                    0, self._cleanup_cycle_total - removed_size
                )

            over_budget = max(0, self._cleanup_cycle_total - self.max_cache_bytes)
            if over_budget:
                candidates = sorted(
                    (entry[2] for entry in self._cleanup_candidates),
                    key=lambda candidate: (candidate.mtime, candidate.path.name),
                )
                for candidate in candidates:
                    removed_size = self._reclaim_cleanup_candidate(candidate)
                    reclaimed_size += removed_size
                    over_budget = max(0, over_budget - removed_size)
                    if over_budget == 0:
                        break
            # Deletions, active parts, concurrent commits, or a candidate heap
            # smaller than the required reclaim set all merit another complete
            # pass.  In a quiescent directory each pass is exact and eventually
            # converges without unbounded per-call work or memory.
            # Continue immediately only when this cycle made progress (for
            # example, a bounded candidate heap needs another full pass).
            # If all excess bytes are protected by a live retry/lease or every
            # candidate is currently locked, defer to normal maintenance
            # instead of rescanning the whole directory on every request.
            self._cleanup_scan_active = reclaimed_size > 0 and (
                over_budget > 0 or self._cleanup_retention_candidates_truncated
            )
            self._cleanup_cycle_total = 0
            self._cleanup_candidates.clear()
            self._cleanup_candidate_counter = 0
            self._cleanup_retention_candidates.clear()
            self._cleanup_retention_candidate_counter = 0
            self._cleanup_retention_candidates_truncated = False
        finally:
            self._cleanup_lock.release()

    def _inspect_cleanup_entry(
        self,
        name: str,
        path: Path,
        metadata: os.stat_result,
        now: float,
    ) -> int:
        age = max(0.0, now - metadata.st_mtime)
        part_match = _PART_RE.fullmatch(name)
        if part_match is not None:
            if age > self.lease_seconds * 2:
                self._remove_old_part(part_match.group("key"), path, now)
                return metadata.st_size if not path.exists() else 0
            return 0

        manifest_match = _MANIFEST_RE.fullmatch(name)
        if manifest_match is not None:
            key = manifest_match.group("key")
            if (
                age > self.lease_seconds * 2
                and self._read_cached(key) is None
            ):
                self._remove_invalid_manifest(key, path)
                if not path.exists():
                    return metadata.st_size
            if path.exists() and self._read_manifest_only(key) is None:
                self._consider_cleanup_candidate(
                    self._cleanup_candidate(
                        metadata, key, path, "manifest"
                    )
                )
            return 0

        content_match = _CONTENT_RE.fullmatch(name)
        if content_match is not None:
            key = content_match.group("key")
            manifest = self._read_manifest_only(key)
            referenced = (
                manifest is not None
                and manifest.get("content_name") == name
            )
            if not referenced and age > self.lease_seconds * 2:
                self._remove_unreferenced_content(key, name, path, now)
                return metadata.st_size if not path.exists() else 0
            if referenced and age > self.retention_seconds:
                # Coupled content + manifest eviction is deferred until EOF,
                # after both files have been included in the complete-cycle
                # byte total. That keeps accounting exact for every valid,
                # unspecified ``scandir`` order without an unbounded seen set.
                self._consider_retention_candidate(
                    self._cleanup_candidate(metadata, key, path, "content")
                )
            if path.exists():
                self._consider_cleanup_candidate(
                    self._cleanup_candidate(metadata, key, path, "content")
                )
            return 0

        stale_match = _STALE_RE.fullmatch(name)
        if stale_match is not None:
            key = stale_match.group("key")
            if age > self.retention_seconds:
                self._remove_expired_auxiliary(
                    key, path, metadata, stale=True
                )
                if not path.exists():
                    return metadata.st_size
            # A stale token paired with a valid manifest is active cache
            # invalidation, not disposable bookkeeping. Only an orphan that
            # has also cleared the in-flight lease grace can be a byte-budget
            # candidate.
            if (
                path.exists()
                and age > self.lease_seconds * 2
                and self._read_manifest_only(key) is None
            ):
                self._consider_cleanup_candidate(
                    self._cleanup_candidate(metadata, key, path, "stale")
                )
            return 0

        failure_match = _FAILURE_RE.fullmatch(name)
        if failure_match is not None:
            key = failure_match.group("key")
            if age > self.retention_seconds:
                self._remove_expired_auxiliary(
                    key, path, metadata, stale=False
                )
                if not path.exists():
                    return metadata.st_size
            # Keep the short failure-backoff window intact. The exact retry
            # generation is checked again while reclaiming under the key lock.
            if path.exists() and age > self.failure_retry_seconds:
                self._consider_cleanup_candidate(
                    self._cleanup_candidate(metadata, key, path, "failure")
                )
        return 0

    @staticmethod
    def _cleanup_candidate(
        metadata: os.stat_result,
        key: str,
        path: Path,
        kind: Literal["content", "failure", "stale", "manifest"],
    ) -> _CleanupCandidate:
        return _CleanupCandidate(
            mtime=metadata.st_mtime,
            key=key,
            path=path,
            size=metadata.st_size,
            kind=kind,
            snapshot=(
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_size,
            ),
        )

    def _consider_cleanup_candidate(self, candidate: _CleanupCandidate) -> None:
        self._cleanup_candidate_counter += 1
        item = (-candidate.mtime, self._cleanup_candidate_counter, candidate)
        if len(self._cleanup_candidates) < _CLEANUP_CANDIDATE_LIMIT:
            heapq.heappush(self._cleanup_candidates, item)
            return
        newest_kept = -self._cleanup_candidates[0][0]
        if candidate.mtime < newest_kept:
            heapq.heapreplace(self._cleanup_candidates, item)

    def _consider_retention_candidate(
        self, candidate: _CleanupCandidate
    ) -> None:
        self._cleanup_retention_candidate_counter += 1
        item = (
            -candidate.mtime,
            self._cleanup_retention_candidate_counter,
            candidate,
        )
        if len(self._cleanup_retention_candidates) < _CLEANUP_CANDIDATE_LIMIT:
            heapq.heappush(self._cleanup_retention_candidates, item)
            return
        self._cleanup_retention_candidates_truncated = True
        newest_kept = -self._cleanup_retention_candidates[0][0]
        if candidate.mtime < newest_kept:
            heapq.heapreplace(self._cleanup_retention_candidates, item)

    def _reclaim_cleanup_candidate(self, candidate: _CleanupCandidate) -> int:
        if candidate.kind == "content":
            return self._evict_content(
                candidate.key,
                candidate.path.name,
                candidate.path,
                expected_snapshot=candidate.snapshot,
            )
        if candidate.kind == "manifest":
            self._remove_invalid_manifest(candidate.key, candidate.path)
        else:
            self._remove_auxiliary_snapshot(
                candidate.key,
                candidate.path,
                candidate.snapshot,
                kind=candidate.kind,
            )
        return candidate.size if not candidate.path.exists() else 0

    def _remove_auxiliary_snapshot(
        self,
        key: str,
        path: Path,
        snapshot: tuple[int, int, int, int],
        *,
        kind: Literal["failure", "stale"],
    ) -> None:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return
        try:
            try:
                current = path.stat(follow_symlinks=False)
            except OSError:
                return
            identity = (
                current.st_dev,
                current.st_ino,
                current.st_mtime_ns,
                current.st_size,
            )
            if identity != snapshot or not _safe_regular_file(path):
                return
            if kind == "stale" and self._read_manifest_only(key) is not None:
                return
            if kind == "failure":
                stale_token = self._read_token(self._stale_path(key)) or ""
                if self._read_failure_retry_after(key, stale_token) > self._now():
                    return
            self._owned_unlink(path)
        except CoverUnavailable:
            return
        finally:
            self._release_process_lock(process_lock)

    def _reset_cleanup_cycle(self) -> None:
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            self._reset_cleanup_cycle_locked()
        finally:
            self._cleanup_lock.release()

    def _reset_cleanup_cycle_locked(self) -> None:
        iterator = self._cleanup_iterator
        self._cleanup_iterator = None
        if iterator is not None:
            try:
                iterator.close()
            except OSError:
                pass
        self._cleanup_cycle_total = 0
        self._cleanup_candidates.clear()
        self._cleanup_candidate_counter = 0
        self._cleanup_retention_candidates.clear()
        self._cleanup_retention_candidate_counter = 0
        self._cleanup_retention_candidates_truncated = False
        self._cleanup_scan_active = False

    def _remove_old_part(self, key: str, path: Path, now: float) -> None:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return
        try:
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                return
            if (
                _safe_regular_file(path)
                and now - metadata.st_mtime > self.lease_seconds * 2
            ):
                self._owned_unlink(path)
        finally:
            self._release_process_lock(process_lock)

    def _remove_unreferenced_content(
        self, key: str, content_name: str, path: Path, now: float
    ) -> None:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return
        try:
            current = self._read_manifest_only(key)
            if current is not None and current.get("content_name") == content_name:
                return
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                return
            if now - metadata.st_mtime > self.lease_seconds * 2:
                self._owned_unlink(path)
        finally:
            self._release_process_lock(process_lock)

    def _remove_invalid_manifest(self, key: str, path: Path) -> None:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return
        try:
            if self._read_cached(key) is None:
                self._owned_unlink(path)
        finally:
            self._release_process_lock(process_lock)

    def _remove_expired_auxiliary(
        self,
        key: str,
        path: Path,
        snapshot: os.stat_result,
        *,
        stale: bool,
    ) -> None:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return
        try:
            try:
                current = path.stat(follow_symlinks=False)
            except OSError:
                return
            current_now = self._now()
            if (
                (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size)
                != (
                    snapshot.st_dev,
                    snapshot.st_ino,
                    snapshot.st_mtime_ns,
                    snapshot.st_size,
                )
                or current_now - current.st_mtime <= self.retention_seconds
            ):
                return
            if stale:
                token = self._read_token(path)
                manifest = self._read_manifest_only(key)
                # A valid token that is either pending or recorded by the
                # current manifest remains active metadata. Once its content is
                # evicted, the manifest disappears and the orphan is reclaimed
                # on the next bounded maintenance pass.
                if token is not None and manifest is not None:
                    return
            else:
                stale_token = self._read_token(self._stale_path(key)) or ""
                # Retention is a storage bound, not permission to bypass an
                # otherwise live generation-bound failure retry window.
                if (
                    self._read_failure_retry_after(key, stale_token)
                    > current_now
                ):
                    return
            self._owned_unlink(path)
        except CoverUnavailable:
            return
        finally:
            self._release_process_lock(process_lock)

    def _evict_content(
        self,
        key: str,
        content_name: str,
        path: Path,
        *,
        older_than: float | None = None,
        expected_snapshot: tuple[int, int, int, int] | None = None,
    ) -> int:
        process_lock = self._acquire_process_lock(key, wait_seconds=0)
        if process_lock is None:
            return 0
        try:
            current = self._read_manifest_only(key)
            if current is None or current.get("content_name") != content_name:
                return 0
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                return 0
            identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_size,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _is_reparse_stat(metadata)
                or (
                    expected_snapshot is not None
                    and identity != expected_snapshot
                )
            ):
                return 0
            if older_than is not None and metadata.st_mtime > older_than:
                return 0
            self._owned_unlink(path)
            if path.exists():
                return 0
            removed_size = metadata.st_size
            latest = self._read_manifest_only(key)
            if latest is not None and latest.get("content_name") == content_name:
                manifest_path = self._manifest_path(key)
                try:
                    manifest_metadata = manifest_path.stat(follow_symlinks=False)
                except OSError:
                    manifest_metadata = None
                self._owned_unlink(manifest_path)
                if (
                    manifest_metadata is not None
                    and stat.S_ISREG(manifest_metadata.st_mode)
                    and not _is_reparse_stat(manifest_metadata)
                    and not manifest_path.exists()
                ):
                    removed_size += manifest_metadata.st_size
            return removed_size
        finally:
            self._release_process_lock(process_lock)

    @contextmanager
    def _singleflight(self, key: str) -> Iterator[None]:
        with self._thread_locks_guard:
            entry = self._thread_locks.get(key)
            if entry is None:
                entry = _ThreadLockEntry(threading.Lock())
                self._thread_locks[key] = entry
            entry.references += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._thread_locks_guard:
                entry.references -= 1
                if entry.references == 0:
                    self._thread_locks.pop(key, None)

    def _manifest_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def _stale_path(self, key: str) -> Path:
        return self.root / f"{key}.stale"

    def _failure_path(self, key: str) -> Path:
        return self.root / f"{key}.failure"

    def _read_manifest_only(self, key: str) -> dict[str, Any] | None:
        if _KEY_RE.fullmatch(key) is None:
            return None
        path = self._manifest_path(key)
        raw = _read_small_regular_file(path, 4096)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        expected = {
            "version",
            "key",
            "content_name",
            "media_type",
            "size",
            "sha256",
            "width",
            "height",
            "fetched_at",
            "fresh_until",
            "stale_token",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            return None
        if (
            type(payload.get("version")) is not int
            or payload.get("version") != _CACHE_VERSION
            or payload.get("key") != key
        ):
            return None
        content_name = payload.get("content_name")
        digest = payload.get("sha256")
        match = _CONTENT_RE.fullmatch(str(content_name))
        if (
            match is None
            or match.group("key") != key
            or match.group("digest") != digest
            or _DIGEST_RE.fullmatch(str(digest)) is None
            or payload.get("media_type") not in _ALLOWED_MEDIA_TYPES
            or type(payload.get("size")) is not int
            or payload["size"] <= 0
            or payload["size"] > self.max_image_bytes
            or type(payload.get("width")) is not int
            or payload["width"] <= 0
            or payload["width"] > self.max_dimension
            or type(payload.get("height")) is not int
            or payload["height"] <= 0
            or payload["height"] > self.max_dimension
            or payload["width"] * payload["height"] > self.max_pixels
            or not _is_finite_number(payload.get("fetched_at"))
            or not _is_finite_number(payload.get("fresh_until"))
            or payload["fresh_until"] < payload["fetched_at"]
            or not isinstance(payload.get("stale_token"), str)
            or (
                payload.get("stale_token")
                and re.fullmatch(r"[0-9a-f]{32}", payload["stale_token"]) is None
            )
        ):
            return None
        return payload

    def _read_cached(self, key: str) -> _CachedAsset | None:
        payload = self._read_manifest_only(key)
        if payload is None:
            return None
        size = int(payload["size"])
        if size <= 0 or size > self.max_image_bytes:
            return None
        path = self.root / str(payload["content_name"])
        body = _read_small_regular_file(path, self.max_image_bytes)
        if body is None or len(body) != size:
            return None
        digest = hashlib.sha256(body).hexdigest()
        if digest != payload["sha256"]:
            return None
        media_type = str(payload["media_type"])
        width = int(payload["width"])
        height = int(payload["height"])
        verification_key = (digest, media_type, width, height)
        if not self._verified_image(verification_key):
            try:
                decoded_width, decoded_height = validate_image(
                    body,
                    media_type,
                    max_bytes=self.max_image_bytes,
                    max_dimension=self.max_dimension,
                    max_pixels=self.max_pixels,
                )
            except CoverUnavailable:
                return None
            if (decoded_width, decoded_height) != (width, height):
                return None
            self._remember_verified_image(verification_key)
        return _CachedAsset(
            body=body,
            media_type=media_type,
            content_sha256=digest,
            width=width,
            height=height,
            fresh_until=float(payload["fresh_until"]),
            content_name=str(payload["content_name"]),
            stale_token=str(payload["stale_token"]),
        )

    def _verified_image(self, key: tuple[str, str, int, int]) -> bool:
        with self._verified_images_lock:
            if key not in self._verified_images:
                return False
            self._verified_images.pop(key)
            self._verified_images[key] = None
            return True

    def _remember_verified_image(self, key: tuple[str, str, int, int]) -> None:
        with self._verified_images_lock:
            self._verified_images.pop(key, None)
            self._verified_images[key] = None
            while len(self._verified_images) > 4096:
                oldest = next(iter(self._verified_images))
                self._verified_images.pop(oldest, None)

    def _commit(
        self,
        key: str,
        body: bytes,
        media_type: str,
        width: int,
        height: int,
        *,
        stale_token: str,
        now: float,
    ) -> _CachedAsset:
        if not self._root_is_owned():
            raise CoverUnavailable()
        if not _is_finite_number(now):
            raise CoverUnavailable()
        fresh_until = _finite_timestamp(now, self.fresh_seconds)
        digest = hashlib.sha256(body).hexdigest()
        content_name = f"{key}.{digest}.img"
        content_path = self.root / content_name
        token = uuid.uuid4().hex
        content_part = self.root / f"{key}.{token}.content.part"
        manifest_part = self.root / f"{key}.{token}.manifest.part"
        old = self._read_manifest_only(key)
        try:
            _write_new_file(content_part, body)
            if not self._root_is_owned():
                raise CoverUnavailable()
            os.replace(content_part, content_path)
            manifest = {
                "version": _CACHE_VERSION,
                "key": key,
                "content_name": content_name,
                "media_type": media_type,
                "size": len(body),
                "sha256": digest,
                "width": width,
                "height": height,
                "fetched_at": now,
                "fresh_until": fresh_until,
                "stale_token": stale_token,
            }
            encoded = json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            _write_new_file(manifest_part, encoded)
            if not self._root_is_owned():
                raise CoverUnavailable()
            os.replace(manifest_part, self._manifest_path(key))
        except Exception:
            self._owned_unlink(content_part)
            self._owned_unlink(manifest_part)
            # If publication did not switch the manifest, the new version is an
            # orphan and can be removed without touching last-known-good bytes.
            current = self._read_manifest_only(key)
            if current is None or current.get("content_name") != content_name:
                self._owned_unlink(content_path)
            raise
        finally:
            self._owned_unlink(content_part)
            self._owned_unlink(manifest_part)
        if old is not None and old.get("content_name") != content_name:
            self._owned_unlink(self.root / str(old["content_name"]))
        self._remember_verified_image((digest, media_type, width, height))
        return _CachedAsset(
            body=body,
            media_type=media_type,
            content_sha256=digest,
            width=width,
            height=height,
            fresh_until=fresh_until,
            content_name=content_name,
            stale_token=stale_token,
        )

    def _initialize_lock_table(self) -> None:
        if self._lock_table.exists():
            if _wait_for_regular_file_size(
                self._lock_table, _LOCK_STRIPES, timeout_seconds=1
            ):
                return
            raise ValueError("cover cache lock table is invalid")
        try:
            _write_new_file(self._lock_table, b"\0" * _LOCK_STRIPES)
        except FileExistsError:
            if not _wait_for_regular_file_size(
                self._lock_table, _LOCK_STRIPES, timeout_seconds=1
            ):
                raise ValueError("cover cache lock table is invalid") from None

    def _register_posix_lock_table(self) -> None:
        try:
            path_metadata = self._lock_table.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("cover cache lock table is invalid") from exc
        identity = (path_metadata.st_dev, path_metadata.st_ino)
        if identity != self._lock_table_identity:
            raise ValueError("cover cache lock table identity changed")
        key = (str(self._lock_table), *identity)
        with _POSIX_LOCK_TABLES_GUARD:
            entry = _POSIX_LOCK_TABLES.get(key)
            if entry is not None:
                # Never open-and-close another descriptor for an inode that may
                # have active POSIX record locks: closing any such descriptor
                # would release this process's locks for the entire inode.
                entry.references += 1
                self._posix_lock_table_key = key
                self._posix_lock_table_entry = entry
                return
            descriptor = _open_verified_regular(self._lock_table, write=True)
            if descriptor is None:
                raise ValueError("cover cache lock table is invalid")
            try:
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) != identity:
                    raise ValueError("cover cache lock table identity changed")
                entry = _PosixLockTableEntry(
                    os.fdopen(descriptor, "r+b", buffering=0), references=1
                )
                descriptor = -1
                _POSIX_LOCK_TABLES[key] = entry
                self._posix_lock_table_key = key
                self._posix_lock_table_entry = entry
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

    def _acquire_process_lock(
        self, key: str, *, wait_seconds: float
    ) -> tuple[Any, int, tuple[str, int], _ThreadLockEntry, bool] | None:
        offset = int(key[:3], 16)
        identity = (str(self._lock_table), offset)
        deadline = time.monotonic() + wait_seconds
        with _LOCAL_FILE_LOCKS_GUARD:
            entry = _LOCAL_FILE_LOCKS.get(identity)
            if entry is None:
                entry = _ThreadLockEntry(threading.Lock())
                _LOCAL_FILE_LOCKS[identity] = entry
            entry.references += 1
        acquired = (
            entry.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
            if wait_seconds
            else entry.lock.acquire(blocking=False)
        )
        if not acquired:
            self._release_local_file_lock(identity, entry, acquired=False)
            return None
        close_stream = os.name == "nt"
        if close_stream:
            descriptor = _open_verified_regular(self._lock_table, write=True)
            if descriptor is None:
                self._release_local_file_lock(identity, entry, acquired=True)
                return None
            stream = os.fdopen(descriptor, "r+b", buffering=0)
        else:
            if (
                self._posix_lock_table_entry is None
                or not self._root_is_owned()
            ):
                self._release_local_file_lock(identity, entry, acquired=True)
                return None
            stream = self._posix_lock_table_entry.stream
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(offset)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.lockf(
                        stream.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                        1,
                        offset,
                        os.SEEK_SET,
                    )
                return stream, offset, identity, entry, close_stream
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    if close_stream:
                        stream.close()
                    self._release_local_file_lock(identity, entry, acquired=True)
                    return None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _release_process_lock(
        process_lock: tuple[Any, int, tuple[str, int], _ThreadLockEntry, bool]
    ) -> None:
        stream, offset, identity, entry, close_stream = process_lock
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(offset)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.lockf(
                    stream.fileno(), fcntl.LOCK_UN, 1, offset, os.SEEK_SET
                )
        except OSError:
            pass
        finally:
            if close_stream:
                stream.close()
            CoverCacheService._release_local_file_lock(
                identity, entry, acquired=True
            )

    @staticmethod
    def _release_local_file_lock(
        identity: tuple[str, int], entry: _ThreadLockEntry, *, acquired: bool
    ) -> None:
        if acquired:
            entry.lock.release()
        with _LOCAL_FILE_LOCKS_GUARD:
            entry.references -= 1
            if entry.references == 0:
                _LOCAL_FILE_LOCKS.pop(identity, None)

    def _read_token(self, path: Path) -> str | None:
        raw = _read_small_regular_file(path, 1024)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            isinstance(payload, dict)
            and type(payload.get("version")) is int
            and payload.get("version") == _CACHE_VERSION
            and isinstance(payload.get("token"), str)
            and re.fullmatch(r"[0-9a-f]{32}", payload["token"])
        ):
            return payload["token"]
        return None

    def _read_failure_retry_after(self, key: str, stale_token: str) -> float:
        raw = _read_small_regular_file(self._failure_path(key), 1024)
        if raw is None:
            return 0
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError):
            return 0
        if (
            isinstance(payload, dict)
            and set(payload) == {"version", "retry_after", "stale_token"}
            and type(payload.get("version")) is int
            and payload.get("version") == _CACHE_VERSION
            and _is_finite_number(payload.get("retry_after"))
            and payload.get("stale_token") == stale_token
        ):
            return float(payload["retry_after"])
        return 0

    def _record_failure(self, key: str, now: float, stale_token: str) -> None:
        if not _is_finite_number(now) or not self._root_is_owned():
            return
        target = self._failure_path(key)
        part = self.root / f"{key}.{uuid.uuid4().hex}.failure.part"
        try:
            retry_after = _finite_timestamp(now, self.failure_retry_seconds)
            payload = json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "retry_after": retry_after,
                    "stale_token": stale_token,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            _write_new_file(part, payload)
            if not self._root_is_owned():
                raise OSError("cover cache root identity changed")
            os.replace(part, target)
        except (CoverUnavailable, OSError, TypeError, ValueError):
            pass
        finally:
            self._owned_unlink(part)

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        should_run = False
        with self._maintenance_guard:
            self._maintenance_operations += 1
            if (
                self._cleanup_scan_active
                or now >= self._next_maintenance
                or self._maintenance_operations >= 128
            ):
                self._maintenance_operations = 0
                self._next_maintenance = now + 60
                should_run = True
        if should_run:
            self.cleanup()

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise CoverUnavailable() from None
        if not math.isfinite(value):
            raise CoverUnavailable()
        return value

    def _owned_unlink(self, path: Path | None) -> None:
        if (
            path is None
            or path.parent != self.root
            or not self._root_is_owned()
        ):
            return
        _safe_unlink(path)

    def _root_is_owned(self) -> bool:
        try:
            metadata = self.root.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or _is_reparse_stat(metadata)
                or (metadata.st_dev, metadata.st_ino) != self._root_identity
            ):
                return False
            lock_metadata = self._lock_table.stat(follow_symlinks=False)
            return (
                _read_small_regular_file(self.root / _CACHE_MARKER, 128)
                == b"collection-cover-cache-v1\n"
                and _safe_regular_file(self._lock_table)
                and lock_metadata.st_size == _LOCK_STRIPES
                and (lock_metadata.st_dev, lock_metadata.st_ino)
                == self._lock_table_identity
            )
        except OSError:
            return False

    def _touch_content(self, content_name: str, now: float) -> None:
        path = self.root / content_name
        if self._root_is_owned() and _safe_regular_file(path):
            try:
                os.utime(path, (now, now), follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._cleanup_lock:
            self._reset_cleanup_cycle_locked()
        key = self._posix_lock_table_key
        entry = self._posix_lock_table_entry
        self._posix_lock_table_key = None
        self._posix_lock_table_entry = None
        stream_to_close = None
        if key is not None and entry is not None:
            with _POSIX_LOCK_TABLES_GUARD:
                current = _POSIX_LOCK_TABLES.get(key)
                if current is entry:
                    entry.references -= 1
                    if entry.references == 0:
                        _POSIX_LOCK_TABLES.pop(key, None)
                        stream_to_close = entry.stream
            if stream_to_close is not None:
                try:
                    stream_to_close.close()
                except OSError:
                    pass
        if self._owns_fetcher:
            close = getattr(self.fetcher, "close", None)
            if callable(close):
                close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _public_asset(
        cached: _CachedAsset,
        status: Literal["HIT", "MISS", "REFRESH", "STALE"],
    ) -> CoverAsset:
        return CoverAsset(
            body=cached.body,
            media_type=cached.media_type,
            content_sha256=cached.content_sha256,
            width=cached.width,
            height=cached.height,
            cache_status=status,
        )


def validate_image(
    body: bytes,
    media_type: str,
    *,
    max_bytes: int,
    max_dimension: int,
    max_pixels: int,
) -> tuple[int, int]:
    if not body or len(body) > max_bytes or media_type not in _ALLOWED_MEDIA_TYPES:
        raise CoverUnavailable()
    if media_type == "image/png":
        width, height = _png_dimensions(body)
    elif media_type == "image/jpeg":
        width, height = _jpeg_dimensions(body)
    else:
        width, height = _webp_dimensions(body)
    if (
        width <= 0
        or height <= 0
        or width > max_dimension
        or height > max_dimension
        or width * height > max_pixels
    ):
        raise CoverUnavailable()
    _decoder_validate_image(body, media_type, width, height)
    return width, height


def _decoder_validate_image(
    body: bytes,
    media_type: str,
    width: int,
    height: int,
) -> None:
    """Require bytes that the production decoder can fully load, not just parse."""
    try:
        with _IMAGE_DECODE_SLOTS, warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as image:
                if (
                    image.format != _PIL_FORMATS[media_type]
                    or image.size != (width, height)
                    or getattr(image, "is_animated", False)
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise CoverUnavailable()
                image.verify()
            with Image.open(io.BytesIO(body)) as image:
                if (
                    image.format != _PIL_FORMATS[media_type]
                    or image.size != (width, height)
                    or getattr(image, "is_animated", False)
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise CoverUnavailable()
                image.load()
                if image.format != _PIL_FORMATS[media_type] or image.size != (width, height):
                    raise CoverUnavailable()
    except CoverUnavailable:
        raise
    except Exception as exc:
        raise CoverUnavailable() from exc


def _png_dimensions(body: bytes) -> tuple[int, int]:
    if len(body) < 57 or body[:8] != b"\x89PNG\r\n\x1a\n":
        raise CoverUnavailable()
    position = 8
    width = height = 0
    saw_idat = False
    chunk_index = 0
    while position + 12 <= len(body):
        chunk_size = int.from_bytes(body[position:position + 4], "big")
        chunk_type = body[position + 4:position + 8]
        data_start = position + 8
        data_end = data_start + chunk_size
        crc_end = data_end + 4
        if data_end < data_start or crc_end > len(body):
            raise CoverUnavailable()
        expected_crc = int.from_bytes(body[data_end:crc_end], "big")
        if zlib.crc32(chunk_type + body[data_start:data_end]) & 0xFFFFFFFF != expected_crc:
            raise CoverUnavailable()
        if chunk_index == 0:
            if chunk_type != b"IHDR" or chunk_size != 13:
                raise CoverUnavailable()
            width = int.from_bytes(body[data_start:data_start + 4], "big")
            height = int.from_bytes(body[data_start + 4:data_start + 8], "big")
        elif chunk_type == b"IHDR":
            raise CoverUnavailable()
        if chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if chunk_size != 0 or not saw_idat or crc_end != len(body):
                raise CoverUnavailable()
            return width, height
        position = crc_end
        chunk_index += 1
    raise CoverUnavailable()


def _jpeg_dimensions(body: bytes) -> tuple[int, int]:
    if len(body) < 12 or not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
        raise CoverUnavailable()
    position = 2
    dimensions: tuple[int, int] | None = None
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while position + 4 <= len(body):
        if body[position] != 0xFF:
            position += 1
            continue
        while position < len(body) and body[position] == 0xFF:
            position += 1
        if position >= len(body):
            break
        marker = body[position]
        position += 1
        if marker in {0x00, 0x01, *range(0xD0, 0xD9)}:
            continue
        if position + 2 > len(body):
            break
        segment_length = int.from_bytes(body[position:position + 2], "big")
        if segment_length < 2 or position + segment_length > len(body):
            raise CoverUnavailable()
        if marker in start_of_frame:
            if segment_length < 7:
                raise CoverUnavailable()
            height = int.from_bytes(body[position + 3:position + 5], "big")
            width = int.from_bytes(body[position + 5:position + 7], "big")
            dimensions = (width, height)
        if marker == 0xDA:
            scan_start = position + segment_length
            if dimensions is None or scan_start >= len(body) - 2:
                raise CoverUnavailable()
            return dimensions
        position += segment_length
    raise CoverUnavailable()


def _webp_dimensions(body: bytes) -> tuple[int, int]:
    if (
        len(body) < 30
        or body[:4] != b"RIFF"
        or body[8:12] != b"WEBP"
        or int.from_bytes(body[4:8], "little") + 8 != len(body)
    ):
        raise CoverUnavailable()
    position = 12
    canvas: tuple[int, int] | None = None
    image: tuple[int, int] | None = None
    while position + 8 <= len(body):
        chunk = body[position:position + 4]
        chunk_size = int.from_bytes(body[position + 4:position + 8], "little")
        data_start = position + 8
        data_end = data_start + chunk_size
        next_position = data_end + (chunk_size & 1)
        if data_end < data_start or next_position > len(body):
            raise CoverUnavailable()
        data = body[data_start:data_end]
        if chunk in {b"ANIM", b"ANMF"}:
            raise CoverUnavailable()
        if chunk == b"VP8X":
            if canvas is not None or chunk_size != 10 or data[0] & 0x02:
                raise CoverUnavailable()
            canvas = (
                1 + int.from_bytes(data[4:7], "little"),
                1 + int.from_bytes(data[7:10], "little"),
            )
        elif chunk == b"VP8L":
            if image is not None or chunk_size < 5 or data[0] != 0x2F:
                raise CoverUnavailable()
            bits = int.from_bytes(data[1:5], "little")
            image = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        elif chunk == b"VP8 ":
            if image is not None or chunk_size < 10 or data[3:6] != b"\x9d\x01\x2a":
                raise CoverUnavailable()
            image = (
                int.from_bytes(data[6:8], "little") & 0x3FFF,
                int.from_bytes(data[8:10], "little") & 0x3FFF,
            )
        position = next_position
    if position != len(body) or image is None:
        raise CoverUnavailable()
    if canvas is not None:
        if image[0] > canvas[0] or image[1] > canvas[1]:
            raise CoverUnavailable()
        return canvas
    return image


def _prepare_cache_root(root: Path) -> Path:
    if not root:
        raise ValueError("cover cache root is required")
    candidate = root.absolute()
    if candidate.parent == candidate:
        raise ValueError("cover cache root cannot be a filesystem root")
    for ancestor in (candidate, *candidate.parents):
        if not ancestor.exists():
            continue
        try:
            metadata = ancestor.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("cover cache root cannot be inspected") from exc
        if _is_reparse_stat(metadata):
            raise ValueError("cover cache root cannot traverse a reparse point")
        if ancestor == candidate and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("cover cache root must be a directory")
    candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve(strict=True)
    if resolved.parent == resolved:
        raise ValueError("cover cache root cannot be a filesystem root")
    marker = resolved / _CACHE_MARKER
    marker_body = b"collection-cover-cache-v1\n"
    if marker.exists():
        if not _wait_for_exact_regular_file(
            marker, marker_body, timeout_seconds=1
        ):
            raise ValueError("cover cache marker is invalid")
        return resolved
    try:
        has_entries = any(resolved.iterdir())
    except OSError as exc:
        raise ValueError("cover cache root cannot be inspected") from exc
    if has_entries:
        # Another process may have atomically created the marker after our
        # initial existence check and before this directory scan.
        if _wait_for_exact_regular_file(
            marker, marker_body, timeout_seconds=1
        ):
            return resolved
        raise ValueError("non-empty cover cache root is missing its marker")
    try:
        _write_new_file(marker, marker_body)
    except FileExistsError:
        if not _wait_for_exact_regular_file(
            marker, marker_body, timeout_seconds=1
        ):
            raise ValueError("cover cache marker is invalid") from None
    return resolved


def _write_new_file(path: Path, body: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _wait_for_exact_regular_file(
    path: Path, expected: bytes, *, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _read_small_regular_file(path, len(expected)) == expected:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _wait_for_regular_file_size(
    path: Path, expected_size: int, *, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            metadata = path.stat(follow_symlinks=False)
            if (
                stat.S_ISREG(metadata.st_mode)
                and not _is_reparse_stat(metadata)
                and metadata.st_size == expected_size
            ):
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _read_small_regular_file(path: Path, max_bytes: int) -> bytes | None:
    descriptor = _open_verified_regular(path, write=False)
    if descriptor is None:
        return None
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size < 0 or metadata.st_size > max_bytes:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(max_bytes + 1)
        return body if len(body) <= max_bytes else None
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_verified_regular(path: Path, *, write: bool) -> int | None:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(before.st_mode) or _is_reparse_stat(before):
        return None
    flags = os.O_RDWR if write else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or _is_reparse_stat(after)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            os.close(descriptor)
            return None
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        return None
    return descriptor


def _safe_regular_file(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not _is_reparse_stat(metadata)


def _is_reparse_stat(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _safe_unlink(path: Path | None) -> None:
    if path is None or not _safe_regular_file(path):
        return
    try:
        path.unlink()
    except OSError:
        pass
