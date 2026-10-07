"""Create a fresh, audited source snapshot; never copy the development Git history."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
FORBIDDEN_PARTS = frozenset({
    ".git", ".wanan", "var", "node_modules", ".pnpm-store", ".venv",
    "__pycache__", ".pytest_cache", "dist", "uploads", "tmp", "temp", "spec",
})
FORBIDDEN_NAMES = frozenset({
    "api.txt", "agents.md", "handoff.md", "next_chat.md", "经验学习.md",
})
FORBIDDEN_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3",
    ".log", ".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".zip",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".md", ".txt", ".lock", ".json", ".toml", ".yaml", ".yml",
    ".ts", ".tsx", ".js", ".mjs", ".css", ".html", ".webmanifest",
})
# High-signal checks only. Findings contain a category and relative path, NEVER text.
# This complements the explicit file list and human review; it is not a security guarantee.
SENSITIVE_PATTERNS = (
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("cloud-access-key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("local-user-context", re.compile(r"(?i)(?:[A-Z]:[\\/](?:Users[\\/]86130|xwechat_files)[\\/]|wxid_[a-z0-9_]+)")),
    ("credential-in-url", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@[^\s/]+")),
)


class ReleaseError(ValueError):
    """A stable, content-free release rejection."""


def _safe_chain(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            metadata = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ReleaseError("unsafe-filesystem-link")


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ReleaseError("invalid-manifest-path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {".", ".."} for part in path.parts):
        raise ReleaseError("invalid-manifest-path")
    lowered = tuple(part.casefold() for part in path.parts)
    if any(part in FORBIDDEN_PARTS for part in lowered) or path.name.casefold() in FORBIDDEN_NAMES:
        raise ReleaseError("forbidden-manifest-path")
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise ReleaseError("forbidden-manifest-path")
    if path.name.startswith(".env") and value != ".env.example":
        raise ReleaseError("forbidden-manifest-path")
    if "local-phone" in value or value.endswith("手机入口.cmd"):
        raise ReleaseError("private-phone-config")
    return value


def _read_file(root: Path, relative: str) -> bytes:
    path = root / relative
    _safe_chain(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ReleaseError(f"missing-file:{relative}") from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
        raise ReleaseError(f"invalid-file:{relative}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ReleaseError(f"file-too-large:{relative}")
    return data


def _is_text(relative: str) -> bool:
    return PurePosixPath(relative).suffix.casefold() in TEXT_SUFFIXES or relative in {".env.example", ".gitignore", ".gitattributes"}


def _check_content(relative: str, data: bytes, exceptions: dict | None = None) -> None:
    if not _is_text(relative):
        if relative.startswith("web/public/") and PurePosixPath(relative).suffix in {".png", ".webp"}:
            return  # Explicitly listed application icons/fallback, not arbitrary images.
        raise ReleaseError(f"unreviewed-binary:{relative}")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ReleaseError(f"invalid-text-encoding:{relative}") from None
    for category, pattern in SENSITIVE_PATTERNS:
        actual = Counter(hashlib.sha256(match.group().encode("utf-8")).hexdigest() for match in pattern.finditer(content))
        approved = (exceptions or {}).get((relative, category), Counter())
        if actual != approved:
            raise ReleaseError(f"sensitive-content:{category}:{relative}")
    if relative == ".env.example":
        for line in content.splitlines():
            if line.strip().startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if any(part in name.upper() for part in ("TOKEN", "KEY", "APP_ID", "SECRET", "PASSWORD")) and value.strip():
                raise ReleaseError("nonempty-template-credential:.env.example")


def _check_links(files: dict[str, bytes]) -> None:
    import posixpath

    for relative, data in files.items():
        if not relative.endswith(".md"):
            continue
        content = data.decode("utf-8-sig")
        for raw in re.findall(r"\]\(([^)]+)\)", content):
            target = raw.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            path = unquote(parsed.path)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), path))
            if resolved not in files:
                raise ReleaseError(f"unresolved-document-link:{relative}")


def collect_files(root: Path) -> tuple[str, dict[str, bytes]]:
    root = Path(os.path.abspath(root))
    _safe_chain(root)
    try:
        manifest = json.loads(_read_file(root, "release-manifest.json"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ReleaseError("invalid-manifest") from None
    if not isinstance(manifest, dict):
        raise ReleaseError("invalid-manifest")
    version = manifest.get("version")
    names = manifest.get("files")
    if manifest.get("schema_version") != 1 or not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", version):
        raise ReleaseError("invalid-manifest")
    if not isinstance(names, list) or not names or len(names) > 1000:
        raise ReleaseError("invalid-manifest")
    checked = [_safe_relative(name) for name in names]
    if len({name.casefold() for name in checked}) != len(checked):
        raise ReleaseError("duplicate-manifest-path")
    exceptions = {}
    records = manifest.get("synthetic_fixture_exceptions", [])
    if not isinstance(records, list) or len(records) > 50:
        raise ReleaseError("invalid-fixture-exception")
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseError("invalid-fixture-exception")
        file, category, hashes = record.get("file"), record.get("category"), record.get("match_sha256")
        if (file not in checked or not isinstance(file, str)
            or not (file.startswith("backend/tests/test_") or (file.startswith("web/src/") and ".test." in file) or file.startswith("scripts/tests/test_"))
            or category != "credential-in-url" or not isinstance(hashes, list)
            or not hashes or len(hashes) > 50 or record.get("match_count") != len(hashes)
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes)
            or (file, category) in exceptions):
            raise ReleaseError("invalid-fixture-exception")
        exceptions[(file, category)] = Counter(hashes)
    files: dict[str, bytes] = {}
    total = 0
    for relative in sorted(checked):
        data = _read_file(root, relative)
        _check_content(relative, data, exceptions)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ReleaseError("release-too-large")
        files[relative] = data
    _check_links(files)
    return version, files


def export_release(root: Path) -> dict:
    root = Path(os.path.abspath(root))
    version, files = collect_files(root)
    output_root = root / "var" / "releases"
    _safe_chain(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"snaprec-v{version}-", dir=output_root))
    archive = directory.with_name(directory.name + ".zip")
    # Fresh directory + exclusive files: never remove or overwrite previous artifacts.
    report = {
        "schema_version": 1,
        "version": version,
        "license": "not-selected",
        "git_history_included": False,
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()],
    }
    manifest_data = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    payload = {**files, "RELEASE_MANIFEST.json": manifest_data}
    with zipfile.ZipFile(archive, mode="x", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative, data in payload.items():
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(data)
            bundle.writestr(f"{directory.name}/{relative}", data)
    return {
        "status": "exported",
        "directory": directory.relative_to(root).as_posix(),
        "archive": archive.relative_to(root).as_posix(),
        "files": len(files),
        "bytes": sum(map(len, files.values())),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "git_history_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate without creating an export")
    arguments = parser.parse_args()
    try:
        if arguments.check:
            version, files = collect_files(PROJECT_ROOT)
            result = {"status": "checked", "version": version, "files": len(files), "bytes": sum(map(len, files.values()))}
        else:
            result = export_release(PROJECT_ROOT)
    except ReleaseError as exc:
        print(json.dumps({"status": "rejected", "code": str(exc)}))
        return 1
    except OSError:
        print(json.dumps({"status": "rejected", "code": "filesystem-error"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
