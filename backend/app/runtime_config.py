from __future__ import annotations

import ipaddress
import math
import os
import platform
import shutil
import sqlite3
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

import idna


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COVER_CACHE_MARKER = ".collection-cover-cache-v1"
_COVER_CACHE_MARKER_BODY = b"collection-cover-cache-v1\n"
_MAX_COVER_RESPONSE_BYTES = 5 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS_FORBIDDEN_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)

CONFIG_ENV_VARS = (
    "VIDEO_ASR_ENDPOINT",
    "VIDEO_ASR_TOKEN",
    "VIDEO_ASR_MODEL",
    "VIDEO_EXTRACTION_ENDPOINT",
    "VIDEO_EXTRACTION_TOKEN",
    "VIDEO_FOCUSED_EXTRACTION_ENDPOINT",
    "VIDEO_DB_PATH",
    "VIDEO_TEMP_ROOT",
    "VIDEO_UPLOAD_ROOT",
    "VIDEO_MAX_UPLOAD_BYTES",
    "VIDEO_MEDIA_ROOT",
    "INSPIRATION_ASR_ENDPOINT",
    "INSPIRATION_ASR_TOKEN",
    "INSPIRATION_ASR_MODEL",
    "INSPIRATION_FFPROBE_PATH",
    "INSPIRATION_TEMP_ROOT",
    "COVER_CACHE_ROOT",
    "COVER_CACHE_FRESH_SECONDS",
    "COVER_CACHE_RETENTION_SECONDS",
    "COVER_CACHE_MAX_BYTES",
    "COVER_MAX_RESPONSE_BYTES",
    "COVER_CACHE_FAILURE_RETRY_SECONDS",
    "INSPIRATION_ASR_PROVIDER",
    "INSPIRATION_VOLC_APP_ID",
    "INSPIRATION_VOLC_ACCESS_TOKEN",
    "INSPIRATION_FFMPEG_PATH",
)

_DEFAULTS = {
    "INSPIRATION_ASR_PROVIDER": "http",
    "INSPIRATION_FFMPEG_PATH": "ffmpeg",
    "VIDEO_DB_PATH": "var/video_notes.sqlite3",
    "VIDEO_TEMP_ROOT": "var/tmp",
    "VIDEO_UPLOAD_ROOT": "var/uploads",
    "VIDEO_MAX_UPLOAD_BYTES": str(512 * 1024 * 1024),
    "VIDEO_MEDIA_ROOT": "var/media",
    "INSPIRATION_FFPROBE_PATH": "ffprobe",
    "INSPIRATION_TEMP_ROOT": "var/inspiration-recordings",
    "COVER_CACHE_ROOT": "var/cover-cache",
    "COVER_CACHE_FRESH_SECONDS": str(24 * 60 * 60),
    "COVER_CACHE_RETENTION_SECONDS": str(30 * 24 * 60 * 60),
    "COVER_CACHE_MAX_BYTES": str(512 * 1024 * 1024),
    "COVER_MAX_RESPONSE_BYTES": str(_MAX_COVER_RESPONSE_BYTES),
    "COVER_CACHE_FAILURE_RETRY_SECONDS": "60",
}

_ENDPOINT_FIELDS = (
    "VIDEO_ASR_ENDPOINT",
    "VIDEO_EXTRACTION_ENDPOINT",
    "VIDEO_FOCUSED_EXTRACTION_ENDPOINT",
    "INSPIRATION_ASR_ENDPOINT",
)
_MANAGED_ROOT_FIELDS = (
    "VIDEO_TEMP_ROOT",
    "VIDEO_UPLOAD_ROOT",
    "VIDEO_MEDIA_ROOT",
    "INSPIRATION_TEMP_ROOT",
    "COVER_CACHE_ROOT",
)
_FIELD_ORDER = {
    field: index
    for index, field in enumerate(
        (*CONFIG_ENV_VARS, "PLATFORM", "PYTHON", "SQLITE", "CLI")
    )
}


class RuntimeConfigError(ValueError):
    """A stable, redacted configuration failure."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


@dataclass(frozen=True, repr=False)
class RuntimeConfig:
    video_asr_endpoint: str | None
    video_asr_token: str | None
    video_asr_model: str | None
    video_extraction_endpoint: str | None
    video_extraction_token: str | None
    video_focused_extraction_endpoint: str | None
    video_db_path: Path
    video_temp_root: Path
    video_upload_root: Path
    video_max_upload_bytes: int
    video_media_root: Path
    inspiration_asr_endpoint: str | None
    inspiration_asr_token: str | None
    inspiration_asr_model: str | None
    inspiration_ffprobe_path: str
    inspiration_temp_root: Path
    cover_cache_root: Path
    cover_cache_fresh_seconds: float
    cover_cache_retention_seconds: float
    cover_cache_max_bytes: int
    cover_max_response_bytes: int
    cover_cache_failure_retry_seconds: float
    inspiration_asr_provider: str = "http"
    inspiration_volc_app_id: str | None = None
    inspiration_volc_access_token: str | None = None
    inspiration_ffmpeg_path: str = "ffmpeg"

    def __repr__(self) -> str:
        return "RuntimeConfig(<redacted>)"


def _raw_value(environ: Mapping[str, str], field: str) -> str | None:
    value = environ.get(field)
    if value is None:
        return None
    return str(value)


def _configured_value(environ: Mapping[str, str], field: str) -> str | None:
    raw = _raw_value(environ, field)
    if raw is None or not raw.strip():
        return None
    return raw


def _value_or_default(environ: Mapping[str, str], field: str) -> str:
    configured = _configured_value(environ, field)
    return _DEFAULTS[field] if configured is None else configured


def config_source_states(environ: Mapping[str, str]) -> dict[str, str]:
    states: dict[str, str] = {}
    for field in CONFIG_ENV_VARS:
        if _configured_value(environ, field) is not None:
            states[field] = "configured"
        elif field in _DEFAULTS:
            states[field] = "default"
        else:
            states[field] = "unset"
    return states


def _error(code: str, field: str) -> RuntimeConfigError:
    return RuntimeConfigError(code, field)


def _parse_positive_int(environ: Mapping[str, str], field: str) -> int:
    raw = _value_or_default(environ, field)
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise _error("ERROR_NUMBER_INVALID", field) from None
    if value <= 0:
        raise _error("ERROR_NUMBER_NON_POSITIVE", field)
    return value


def _parse_positive_float(environ: Mapping[str, str], field: str) -> float:
    raw = _value_or_default(environ, field)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _error("ERROR_NUMBER_INVALID", field) from None
    if not math.isfinite(value):
        raise _error("ERROR_NUMBER_NOT_FINITE", field)
    if value <= 0:
        raise _error("ERROR_NUMBER_NON_POSITIVE", field)
    return value


def _has_invalid_percent_escape(value: str) -> bool:
    hex_digits = frozenset("0123456789abcdefABCDEF")
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in hex_digits
            or value[index + 2] not in hex_digits
        ):
            return True
    return False


def _is_valid_endpoint_host(hostname: str, *, bracketed: bool) -> bool:
    if bracketed:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return False
        return True
    if hostname.endswith(".."):
        return False
    host = hostname[:-1] if hostname.endswith(".") else hostname
    host = host.lower()
    if not host or "%" in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) == 4 and all(
            label.isascii() and label.isdigit() for label in labels
        ):
            return False
        try:
            ascii_host = idna.encode(host).decode("ascii").casefold()
        except (idna.IDNAError, UnicodeError, ValueError):
            return False
        if ascii_host.endswith(".."):
            return False
        if ascii_host.endswith("."):
            ascii_host = ascii_host[:-1]
        ascii_labels = ascii_host.split(".")
        if len(ascii_labels) == 4 and all(
            label.isascii() and label.isdigit() for label in ascii_labels
        ):
            try:
                ipaddress.IPv4Address(ascii_host)
            except ValueError:
                return False
            return True
        return bool(ascii_host) and len(ascii_host) <= 253 and all(
            label
            and len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            and all(
                character.isascii()
                and (character.isalnum() or character == "-")
                for character in label
            )
            for label in ascii_host.split(".")
        )
    return True


def _is_valid_bracketed_netloc(netloc: str) -> bool:
    closing_bracket = netloc.find("]")
    if closing_bracket < 0:
        return False
    suffix = netloc[closing_bracket + 1 :]
    return not suffix or (
        suffix.startswith(":")
        and len(suffix) > 1
        and suffix[1:].isascii()
        and suffix[1:].isdigit()
    )


def _parse_endpoint(environ: Mapping[str, str], field: str) -> str | None:
    raw = _configured_value(environ, field)
    if raw is None:
        return None
    if (
        raw != raw.strip()
        or "\\" in raw
        or _has_invalid_percent_escape(raw)
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in raw
        )
        or any(character.isspace() for character in raw)
    ):
        raise _error("ERROR_ENDPOINT_INVALID", field)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise _error("ERROR_ENDPOINT_INVALID", field) from None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or (
            parsed.netloc.startswith("[")
            and not _is_valid_bracketed_netloc(parsed.netloc)
        )
        or not _is_valid_endpoint_host(
            hostname,
            bracketed=parsed.netloc.startswith("["),
        )
    ):
        raise _error("ERROR_ENDPOINT_INVALID", field)
    return raw


def _validate_windows_path_shape(candidate: Path, field: str) -> None:
    if os.name != "nt":
        return
    for part in candidate.parts:
        if part == candidate.anchor or part in {".", ".."}:
            continue
        if (
            part.endswith((" ", "."))
            or any(
                character in _WINDOWS_FORBIDDEN_PATH_CHARACTERS
                for character in part
            )
            or part.split(".", 1)[0].rstrip(" ").casefold()
            in _WINDOWS_RESERVED_PATH_NAMES
        ):
            raise _error("ERROR_PATH_INVALID", field)


def _lexical_absolute(raw: str, project_root: Path, field: str) -> Path:
    text = raw
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        for character in text
    ):
        raise _error("ERROR_PATH_INVALID", field)
    try:
        candidate = Path(text)
    except (OSError, ValueError):
        raise _error("ERROR_PATH_INVALID", field) from None
    if str(candidate).startswith("\\\\"):
        raise _error("ERROR_PATH_NETWORK_UNSUPPORTED", field)
    if candidate.root and not candidate.is_absolute():
        raise _error("ERROR_PATH_INVALID", field)
    if candidate.drive and not candidate.is_absolute():
        raise _error("ERROR_PATH_INVALID", field)
    _validate_windows_path_shape(candidate, field)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        return Path(os.path.abspath(os.fspath(candidate)))
    except (OSError, ValueError):
        raise _error("ERROR_PATH_INVALID", field) from None


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _lstat_or_none(path: Path, field: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _error("ERROR_PATH_UNINSPECTABLE", field) from None


def _validate_existing_chain(path: Path, field: str) -> os.stat_result | None:
    target_metadata: os.stat_result | None = None
    for index, candidate in enumerate((path, *path.parents)):
        metadata = _lstat_or_none(candidate, field)
        if metadata is None:
            continue
        if _is_reparse(metadata):
            raise _error("ERROR_PATH_REPARSE", field)
        if index > 0 and not stat.S_ISDIR(metadata.st_mode):
            raise _error("ERROR_PATH_ANCESTOR_NOT_DIRECTORY", field)
        if index == 0:
            target_metadata = metadata
    return target_metadata


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _contains_path(parent: Path, child: Path) -> bool:
    parent_key = _path_key(parent)
    child_key = _path_key(child)
    try:
        return os.path.commonpath((parent_key, child_key)) == parent_key
    except ValueError:
        return False


def _validate_managed_root(
    path: Path,
    field: str,
    *,
    project_root: Path,
) -> None:
    anchor = Path(path.anchor) if path.anchor else path
    forbidden = (project_root, Path.home(), Path.cwd())
    if path.parent == path or _same_path(path, anchor) or any(
        _same_path(path, candidate) for candidate in forbidden
    ):
        raise _error("ERROR_PATH_TOO_BROAD", field)
    metadata = _validate_existing_chain(path, field)
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise _error("ERROR_PATH_NOT_DIRECTORY", field)


def _read_exact_regular_marker(marker: Path, field: str) -> bool:
    before = _lstat_or_none(marker, field)
    if before is None or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        return False
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(marker, flags)
        opened = os.fstat(descriptor)
        if (
            _is_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            return False
        body = os.read(descriptor, len(_COVER_CACHE_MARKER_BODY) + 1)
        return body == _COVER_CACHE_MARKER_BODY
    except OSError:
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_cover_cache_marker(root: Path) -> None:
    root_metadata = _lstat_or_none(root, "COVER_CACHE_ROOT")
    if root_metadata is None:
        return
    marker = root / _COVER_CACHE_MARKER
    marker_metadata = _lstat_or_none(marker, "COVER_CACHE_ROOT")
    if marker_metadata is not None:
        if not _read_exact_regular_marker(marker, "COVER_CACHE_ROOT"):
            raise _error("ERROR_COVER_MARKER_INVALID", "COVER_CACHE_ROOT")
        return
    try:
        with os.scandir(root) as entries:
            nonempty = next(entries, None) is not None
    except OSError:
        raise _error("ERROR_PATH_UNINSPECTABLE", "COVER_CACHE_ROOT") from None
    if nonempty:
        raise _error("ERROR_COVER_MARKER_MISSING", "COVER_CACHE_ROOT")


def _validate_paths(config: RuntimeConfig, project_root: Path) -> None:
    database_metadata = _validate_existing_chain(
        config.video_db_path,
        "VIDEO_DB_PATH",
    )
    if database_metadata is not None and not stat.S_ISREG(database_metadata.st_mode):
        raise _error("ERROR_DATABASE_NOT_FILE", "VIDEO_DB_PATH")

    roots = (
        ("VIDEO_TEMP_ROOT", config.video_temp_root),
        ("VIDEO_UPLOAD_ROOT", config.video_upload_root),
        ("VIDEO_MEDIA_ROOT", config.video_media_root),
        ("INSPIRATION_TEMP_ROOT", config.inspiration_temp_root),
        ("COVER_CACHE_ROOT", config.cover_cache_root),
    )
    for index, (field, path) in enumerate(roots):
        _validate_managed_root(path, field, project_root=project_root)
        for _, previous in roots[:index]:
            if _contains_path(previous, path) or _contains_path(path, previous):
                raise _error("ERROR_PATH_OVERLAP", field)
    for _, root in roots:
        if _contains_path(root, config.video_db_path) or _contains_path(
            config.video_db_path,
            root,
        ):
            raise _error("ERROR_DATABASE_MANAGED_ROOT_OVERLAP", "VIDEO_DB_PATH")
    _validate_cover_cache_marker(config.cover_cache_root)


def _resolve_ffprobe_setting(raw: str, project_root: Path, field: str = "INSPIRATION_FFPROBE_PATH") -> str:
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        for character in raw
    ):
        raise _error("ERROR_PATH_INVALID", field)
    try:
        candidate = Path(raw)
    except (OSError, ValueError):
        raise _error("ERROR_PATH_INVALID", field) from None
    _validate_windows_path_shape(candidate, field)
    if (
        candidate.is_absolute()
        or candidate.parent != Path(".")
        or "/" in raw
        or "\\" in raw
    ):
        return os.fspath(_lexical_absolute(raw, project_root, field))
    return raw


def resolve_runtime_config(
    environ: Mapping[str, str],
    project_root: str | Path,
) -> RuntimeConfig:
    """Resolve and structurally validate runtime configuration without writes."""

    root = Path(os.path.abspath(os.fspath(Path(project_root))))
    inspiration_provider = _value_or_default(environ, "INSPIRATION_ASR_PROVIDER").strip()
    if inspiration_provider not in {"http", "volcengine_flash"}:
        raise _error("ERROR_PROVIDER_INVALID", "INSPIRATION_ASR_PROVIDER")
    if inspiration_provider == "volcengine_flash":
        for field in ("INSPIRATION_VOLC_APP_ID", "INSPIRATION_VOLC_ACCESS_TOKEN"):
            value = _configured_value(environ, field)
            if not value or len(value) > 512 or any(not (33 <= ord(char) <= 126) for char in value):
                raise _error("ERROR_PROVIDER_CREDENTIAL_INVALID", field)
        for field in ("INSPIRATION_ASR_ENDPOINT", "INSPIRATION_ASR_TOKEN", "INSPIRATION_ASR_MODEL"):
            if _configured_value(environ, field) is not None:
                raise _error("ERROR_PROVIDER_CONFIG_CONFLICT", field)
    cover_max_response_bytes = _parse_positive_int(
        environ,
        "COVER_MAX_RESPONSE_BYTES",
    )
    if cover_max_response_bytes > _MAX_COVER_RESPONSE_BYTES:
        raise _error("ERROR_COVER_RESPONSE_LIMIT", "COVER_MAX_RESPONSE_BYTES")

    config = RuntimeConfig(
        inspiration_asr_provider=inspiration_provider,
        inspiration_volc_app_id=_configured_value(environ, "INSPIRATION_VOLC_APP_ID"),
        inspiration_volc_access_token=_configured_value(environ, "INSPIRATION_VOLC_ACCESS_TOKEN"),
        inspiration_ffmpeg_path=_resolve_ffprobe_setting(
            _value_or_default(environ, "INSPIRATION_FFMPEG_PATH"), root, "INSPIRATION_FFMPEG_PATH"
        ),
        video_asr_endpoint=_parse_endpoint(environ, "VIDEO_ASR_ENDPOINT"),
        video_asr_token=_configured_value(environ, "VIDEO_ASR_TOKEN"),
        video_asr_model=_configured_value(environ, "VIDEO_ASR_MODEL"),
        video_extraction_endpoint=_parse_endpoint(
            environ,
            "VIDEO_EXTRACTION_ENDPOINT",
        ),
        video_extraction_token=_configured_value(
            environ,
            "VIDEO_EXTRACTION_TOKEN",
        ),
        video_focused_extraction_endpoint=_parse_endpoint(
            environ,
            "VIDEO_FOCUSED_EXTRACTION_ENDPOINT",
        ),
        video_db_path=_lexical_absolute(
            _value_or_default(environ, "VIDEO_DB_PATH"),
            root,
            "VIDEO_DB_PATH",
        ),
        video_temp_root=_lexical_absolute(
            _value_or_default(environ, "VIDEO_TEMP_ROOT"),
            root,
            "VIDEO_TEMP_ROOT",
        ),
        video_upload_root=_lexical_absolute(
            _value_or_default(environ, "VIDEO_UPLOAD_ROOT"),
            root,
            "VIDEO_UPLOAD_ROOT",
        ),
        video_max_upload_bytes=_parse_positive_int(
            environ,
            "VIDEO_MAX_UPLOAD_BYTES",
        ),
        video_media_root=_lexical_absolute(
            _value_or_default(environ, "VIDEO_MEDIA_ROOT"),
            root,
            "VIDEO_MEDIA_ROOT",
        ),
        inspiration_asr_endpoint=_parse_endpoint(
            environ,
            "INSPIRATION_ASR_ENDPOINT",
        ),
        inspiration_asr_token=_configured_value(
            environ,
            "INSPIRATION_ASR_TOKEN",
        ),
        inspiration_asr_model=_configured_value(
            environ,
            "INSPIRATION_ASR_MODEL",
        ),
        inspiration_ffprobe_path=_resolve_ffprobe_setting(
            _value_or_default(environ, "INSPIRATION_FFPROBE_PATH"),
            root,
        ),
        inspiration_temp_root=_lexical_absolute(
            _value_or_default(environ, "INSPIRATION_TEMP_ROOT"),
            root,
            "INSPIRATION_TEMP_ROOT",
        ),
        cover_cache_root=_lexical_absolute(
            _value_or_default(environ, "COVER_CACHE_ROOT"),
            root,
            "COVER_CACHE_ROOT",
        ),
        cover_cache_fresh_seconds=_parse_positive_float(
            environ,
            "COVER_CACHE_FRESH_SECONDS",
        ),
        cover_cache_retention_seconds=_parse_positive_float(
            environ,
            "COVER_CACHE_RETENTION_SECONDS",
        ),
        cover_cache_max_bytes=_parse_positive_int(
            environ,
            "COVER_CACHE_MAX_BYTES",
        ),
        cover_max_response_bytes=cover_max_response_bytes,
        cover_cache_failure_retry_seconds=_parse_positive_float(
            environ,
            "COVER_CACHE_FAILURE_RETRY_SECONDS",
        ),
    )
    _validate_paths(config, root)
    return config


def _sqlite_fts5_trigram_available() -> bool:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE VIRTUAL TABLE rr1_runtime_fts USING fts5("
            "body, tokenize='trigram case_sensitive 1')"
        )
        connection.execute(
            "INSERT INTO rr1_runtime_fts(body) VALUES (?)",
            ("abcdef",),
        )
        row = connection.execute(
            "SELECT count(*) FROM rr1_runtime_fts "
            "WHERE rr1_runtime_fts MATCH ?",
            ("bcd",),
        ).fetchone()
        return row is not None and row[0] == 1
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _ordinary_executable(path: str | Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(metadata.st_mode) and not _is_reparse(metadata)


def _ffprobe_available(config: RuntimeConfig) -> bool:
    setting = config.inspiration_ffprobe_path
    candidate = Path(setting)
    if candidate.is_absolute() or candidate.parent != Path("."):
        return _ordinary_executable(candidate)
    try:
        discovered = shutil.which(setting)
    except OSError:
        return False
    return discovered is not None and _ordinary_executable(discovered)


def _issue(code: str, field: str) -> dict[str, str]:
    return {"code": code, "field": field}


def _missing_path_issues(config: RuntimeConfig) -> list[dict[str, str]]:
    paths = (
        ("VIDEO_DB_PATH", config.video_db_path),
        ("VIDEO_TEMP_ROOT", config.video_temp_root),
        ("VIDEO_UPLOAD_ROOT", config.video_upload_root),
        ("VIDEO_MEDIA_ROOT", config.video_media_root),
        ("INSPIRATION_TEMP_ROOT", config.inspiration_temp_root),
        ("COVER_CACHE_ROOT", config.cover_cache_root),
    )
    return [
        _issue("WARN_PATH_NOT_PRESENT", field)
        for field, path in paths
        if _lstat_or_none(path, field) is None
    ]


def _provider_issues(config: RuntimeConfig) -> list[dict[str, str]]:
    endpoint_values = {
        "VIDEO_ASR_ENDPOINT": config.video_asr_endpoint,
        "VIDEO_EXTRACTION_ENDPOINT": config.video_extraction_endpoint,
        "VIDEO_FOCUSED_EXTRACTION_ENDPOINT": (
            config.video_focused_extraction_endpoint
        ),
        "INSPIRATION_ASR_ENDPOINT": config.inspiration_asr_endpoint,
    }
    issues = [
        _issue(
            "WARN_PROVIDER_UNCONFIGURED"
            if value is None
            else "WARN_PROVIDER_OFFLINE_UNVERIFIED",
            field,
        )
        for field, value in endpoint_values.items()
    ]
    if config.inspiration_asr_provider == "volcengine_flash":
        issues = [issue for issue in issues if issue["field"] != "INSPIRATION_ASR_ENDPOINT"]
        issues.append(_issue("WARN_PROVIDER_OFFLINE_UNVERIFIED", "INSPIRATION_ASR_PROVIDER"))
        tool_path = Path(config.inspiration_ffmpeg_path)
        discovered = str(tool_path) if tool_path.is_absolute() else shutil.which(str(tool_path))
        if not discovered or not _ordinary_executable(discovered):
            issues.append(_issue("WARN_FFMPEG_UNAVAILABLE", "INSPIRATION_FFMPEG_PATH"))
    else:
        for field, value in (("INSPIRATION_VOLC_APP_ID", config.inspiration_volc_app_id),
                             ("INSPIRATION_VOLC_ACCESS_TOKEN", config.inspiration_volc_access_token)):
            if value is not None:
                issues.append(_issue("WARN_PROVIDER_VALUE_IGNORED", field))
    if config.video_asr_endpoint is None:
        if config.video_asr_token is not None:
            issues.append(_issue("WARN_PROVIDER_VALUE_IGNORED", "VIDEO_ASR_TOKEN"))
        if config.video_asr_model is not None:
            issues.append(_issue("WARN_PROVIDER_VALUE_IGNORED", "VIDEO_ASR_MODEL"))
    if (
        config.video_extraction_endpoint is None
        and config.video_focused_extraction_endpoint is None
        and config.video_extraction_token is not None
    ):
        issues.append(
            _issue("WARN_PROVIDER_VALUE_IGNORED", "VIDEO_EXTRACTION_TOKEN")
        )
    if config.inspiration_asr_endpoint is None:
        if config.inspiration_asr_token is not None:
            issues.append(
                _issue("WARN_PROVIDER_VALUE_IGNORED", "INSPIRATION_ASR_TOKEN")
            )
        if config.inspiration_asr_model is not None:
            issues.append(
                _issue("WARN_PROVIDER_VALUE_IGNORED", "INSPIRATION_ASR_MODEL")
            )
    return issues


def _issue_sort_key(issue: dict[str, str]) -> tuple[int, str]:
    return (_FIELD_ORDER.get(issue["field"], len(_FIELD_ORDER)), issue["code"])


def _python_supported() -> bool:
    return tuple(sys.version_info[:2]) == (3, 12)


def _runtime_payload(
    *,
    python_supported: bool,
    sqlite_fts5_trigram: bool,
    ffprobe_available: bool,
) -> dict[str, str | bool]:
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "python_supported": python_supported,
        "sqlite_fts5_trigram": sqlite_fts5_trigram,
        "ffprobe_available": ffprobe_available,
    }


def inspect_runtime_config(
    environ: Mapping[str, str],
    project_root: str | Path,
) -> dict:
    """Return a deterministic, redacted Windows release-readiness report."""

    issues: list[dict[str, str]] = []
    config: RuntimeConfig | None
    try:
        config = resolve_runtime_config(environ, project_root)
    except RuntimeConfigError as exc:
        config = None
        issues.append(_issue(exc.code, exc.field))

    platform_name = platform.system()
    python_supported = _python_supported()
    sqlite_fts5_trigram = _sqlite_fts5_trigram_available()
    ffprobe_available = config is not None and _ffprobe_available(config)

    if platform_name.casefold() != "windows":
        issues.append(_issue("ERROR_PLATFORM_UNSUPPORTED", "PLATFORM"))
    if not python_supported:
        issues.append(_issue("ERROR_PYTHON_UNSUPPORTED", "PYTHON"))
    if not sqlite_fts5_trigram:
        issues.append(_issue("ERROR_SQLITE_TRIGRAM_UNAVAILABLE", "SQLITE"))
    if config is not None:
        issues.extend(_provider_issues(config))
        try:
            issues.extend(_missing_path_issues(config))
        except RuntimeConfigError as exc:
            issues.append(_issue(exc.code, exc.field))
        if not ffprobe_available:
            issues.append(
                _issue("WARN_FFPROBE_UNAVAILABLE", "INSPIRATION_FFPROBE_PATH")
            )

    issues.sort(key=_issue_sort_key)
    errors = [issue for issue in issues if issue["code"].startswith("ERROR_")]
    if errors:
        status = "invalid"
        error_code: str | None = errors[0]["code"]
    elif issues:
        status = "ready_with_warnings"
        error_code = None
    else:
        status = "ready"
        error_code = None
    return {
        "slice": "RR1-CONFIG1",
        "status": status,
        "platform": platform_name,
        "runtime": _runtime_payload(
            python_supported=python_supported,
            sqlite_fts5_trigram=sqlite_fts5_trigram,
            ffprobe_available=ffprobe_available,
        ),
        "config": config_source_states(environ),
        "issues": issues,
        "error_code": error_code,
    }


def fixed_failure_report(
    error_code: str,
    *,
    field: str = "CLI",
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Build a path-free report without probing files, SQLite, or executables."""

    source = {} if environ is None else environ
    return {
        "slice": "RR1-CONFIG1",
        "status": "invalid",
        "platform": platform.system(),
        "runtime": _runtime_payload(
            python_supported=_python_supported(),
            sqlite_fts5_trigram=False,
            ffprobe_available=False,
        ),
        "config": config_source_states(source),
        "issues": [_issue(error_code, field)],
        "error_code": error_code,
    }
