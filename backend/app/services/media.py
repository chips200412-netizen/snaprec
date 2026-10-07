from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..domain.models import PlaybackCapability, RetainedMedia
from ..repositories.sqlite import SQLiteRepository

_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
}
_BROWSER_PLAYABLE_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "audio/mp4",
    "video/mp4",
    "video/webm",
}


def _is_reparse_point(path: Path) -> bool:
    return bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


class RetainedMediaError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedMedia:
    record: RetainedMedia
    path: Path
    created: bool
    superseded_path: Path | None = None


@dataclass(frozen=True)
class MediaStream:
    record: RetainedMedia
    path: Path


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeNotSatisfiable(ValueError):
    pass


def parse_single_range(value: str, size: int) -> ByteRange:
    if size <= 0 or len(value) > 200 or not value.startswith("bytes="):
        raise RangeNotSatisfiable()
    specification = value[6:]
    if "," in specification or specification.count("-") != 1:
        raise RangeNotSatisfiable()
    start_text, end_text = specification.split("-", 1)
    if not start_text and not end_text:
        raise RangeNotSatisfiable()
    if start_text and not start_text.isascii() or end_text and not end_text.isascii():
        raise RangeNotSatisfiable()
    if start_text and not start_text.isdigit():
        raise RangeNotSatisfiable()
    if end_text and not end_text.isdigit():
        raise RangeNotSatisfiable()
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise RangeNotSatisfiable()
        start = max(0, size - suffix)
        return ByteRange(start=start, end=size - 1)
    start = int(start_text)
    if start >= size:
        raise RangeNotSatisfiable()
    end = size - 1 if not end_text else int(end_text)
    if end < start:
        raise RangeNotSatisfiable()
    return ByteRange(start=start, end=min(end, size - 1))


def iter_media(path: Path, byte_range: ByteRange, chunk_size: int = 64 * 1024):
    remaining = byte_range.length
    with path.open("rb") as stream:
        stream.seek(byte_range.start)
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class RetainedMediaService:
    def __init__(self, repository: SQLiteRepository, root: str | Path):
        self.repository = repository
        candidate = Path(root)
        if candidate.exists() and (
            candidate.is_symlink() or _is_reparse_point(candidate)
        ):
            raise ValueError("retained media root must not be a symbolic link")
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve()
        forbidden = {Path.cwd().resolve(), Path.home().resolve()}
        if (
            resolved in forbidden
            or resolved == Path(resolved.anchor)
            or resolved.parent == resolved
        ):
            raise ValueError("retained media root is too broad")
        self.root = candidate
        self._resolved_root = resolved

    def prepare(self, source: str | Path, video_id: str) -> PreparedMedia:
        existing = self.repository.get_retained_media(
            video_id, platform="local_upload"
        )
        if existing is not None:
            try:
                existing_path = self._record_path(existing, require_exists=True)
                return PreparedMedia(existing, existing_path, False)
            except (FileNotFoundError, ValueError, OSError):
                try:
                    superseded_path = self._storage_path(existing)
                except (ValueError, OSError):
                    superseded_path = None
        else:
            superseded_path = None

        source_path = Path(source)
        if (
            not source_path.is_file()
            or source_path.is_symlink()
            or _is_reparse_point(source_path)
        ):
            raise RetainedMediaError(
                "MEDIA_RETENTION_FAILED", "媒体来源不是可保留的普通文件。"
            )
        mime_type = _MEDIA_TYPES.get(source_path.suffix.casefold())
        if mime_type is None:
            raise RetainedMediaError(
                "UNSUPPORTED_MEDIA_TYPE", "该媒体格式不能安全保留。"
            )
        storage_key = uuid.uuid4().hex
        final_path = self.root / storage_key
        temporary_path = self.root / f".{storage_key}.partial"
        digest = hashlib.sha256()
        size = 0
        try:
            with source_path.open("rb") as source_stream, temporary_path.open(
                "xb"
            ) as target_stream:
                while block := source_stream.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
                    target_stream.write(block)
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if size <= 0 or digest.hexdigest() != video_id:
                raise RetainedMediaError(
                    "MEDIA_RETENTION_FAILED", "保留媒体未通过完整性校验。"
                )
            os.replace(temporary_path, final_path)
            record = RetainedMedia(
                video_id=video_id,
                storage_key=storage_key,
                mime_type=mime_type,
                size_bytes=size,
            )
            return PreparedMedia(
                record, final_path, True, superseded_path=superseded_path
            )
        except RetainedMediaError:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        except (OSError, ValueError) as exc:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise RetainedMediaError(
                "MEDIA_RETENTION_FAILED", "无法安全保留本地媒体。"
            ) from exc

    def retain_for_existing(self, source: str | Path, video_id: str) -> None:
        prepared = self.prepare(source, video_id)
        if not prepared.created:
            return
        try:
            self.repository.save_retained_media(prepared.record)
            self.activate(prepared)
        except Exception as exc:
            self.discard(prepared)
            raise RetainedMediaError(
                "MEDIA_RETENTION_FAILED", "无法登记保留媒体。"
            ) from exc

    def activate(self, prepared: PreparedMedia) -> None:
        old_path = prepared.superseded_path
        if old_path is None or old_path == prepared.path or not old_path.exists():
            return
        try:
            old_path.unlink()
        except OSError:
            self.repository.add_system_warning(
                "RETAINED_MEDIA_CLEANUP_FAILED",
                "被替换的持久媒体清理失败，需要检查受控媒体目录。",
            )

    def discard(self, prepared: PreparedMedia) -> None:
        if not prepared.created:
            return
        try:
            prepared.path.unlink(missing_ok=True)
        except OSError:
            self.repository.add_system_warning(
                "RETAINED_MEDIA_CLEANUP_FAILED",
                "未登记的持久媒体清理失败，需要检查受控媒体目录。",
            )

    def capability(self, video_id: str, platform: str) -> PlaybackCapability:
        if platform != "local_upload":
            return PlaybackCapability(reason="unsupported_source")
        try:
            record = self.repository.get_retained_media(
                video_id, platform="local_upload"
            )
        except (ValueError, TypeError):
            return PlaybackCapability(reason="missing_file")
        if record is None:
            return PlaybackCapability(reason="not_retained")
        try:
            self._record_path(record, require_exists=True)
        except (FileNotFoundError, ValueError, OSError):
            return PlaybackCapability(
                kind="retained_local",
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
                reason="missing_file",
            )
        if record.mime_type not in _BROWSER_PLAYABLE_TYPES:
            return PlaybackCapability(
                kind="retained_local",
                mime_type=record.mime_type,
                size_bytes=record.size_bytes,
                reason="unsupported_browser_format",
            )
        encoded_id = quote(video_id, safe="")
        return PlaybackCapability(
            availability="available",
            kind="retained_local",
            stream_url=(
                f"/api/v1/videos/{encoded_id}/media?platform=local_upload"
            ),
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            supports_range=True,
            reason="available",
        )

    def open_stream(self, video_id: str, platform: str) -> MediaStream:
        capability = self.capability(video_id, platform)
        if capability.availability != "available":
            raise RetainedMediaError(
                "MEDIA_UNAVAILABLE", "该视频没有可安全播放的本地媒体。"
            )
        record = self.repository.get_retained_media(
            video_id, platform="local_upload"
        )
        if record is None:
            raise RetainedMediaError("MEDIA_UNAVAILABLE", "保留媒体不存在。")
        try:
            path = self._record_path(record, require_exists=True)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise RetainedMediaError("MEDIA_UNAVAILABLE", "保留媒体不可用。") from exc
        return MediaStream(record=record, path=path)

    def delete(self, video_id: str, platform: str) -> PlaybackCapability:
        if platform != "local_upload":
            return PlaybackCapability(reason="unsupported_source")
        try:
            record = self.repository.get_retained_media(
                video_id, platform="local_upload"
            )
        except (ValueError, TypeError) as exc:
            raise RetainedMediaError(
                "MEDIA_DELETE_FAILED", "保留媒体记录无效，已拒绝删除。"
            ) from exc
        if record is None:
            return PlaybackCapability(reason="not_retained")
        try:
            path = self._record_path(record, require_exists=False)
            if path.exists():
                if path.is_symlink() or _is_reparse_point(path) or not path.is_file():
                    raise ValueError("unsafe retained media target")
                path.unlink()
        except (OSError, ValueError) as exc:
            raise RetainedMediaError(
                "MEDIA_DELETE_FAILED", "无法安全删除保留媒体。"
            ) from exc
        try:
            self.repository.delete_retained_media(
                video_id, platform="local_upload"
            )
        except Exception as exc:
            raise RetainedMediaError(
                "MEDIA_DELETE_FAILED", "媒体文件已删除，但播放记录清理失败。"
            ) from exc
        return PlaybackCapability(reason="not_retained")

    def _record_path(
        self, record: RetainedMedia, *, require_exists: bool
    ) -> Path:
        candidate = self._storage_path(record)
        if not candidate.exists():
            if require_exists:
                raise FileNotFoundError(record.storage_key)
            return candidate
        if candidate.stat().st_size != record.size_bytes:
            raise ValueError("retained media size changed")
        return candidate

    def _storage_path(self, record: RetainedMedia) -> Path:
        if not _STORAGE_KEY_RE.fullmatch(record.storage_key):
            raise ValueError("invalid storage key")
        candidate = self.root / record.storage_key
        if not candidate.exists():
            if candidate.parent.resolve() != self._resolved_root:
                raise ValueError("retained media target escaped")
            return candidate
        if candidate.is_symlink() or _is_reparse_point(candidate):
            raise ValueError("retained media target is a link")
        resolved = candidate.resolve(strict=True)
        if (
            resolved.parent != self._resolved_root
            or not candidate.is_file()
        ):
            raise ValueError("retained media target escaped")
        return candidate
