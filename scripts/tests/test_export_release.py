import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.export_release import ReleaseError, collect_files, export_release


def test_project_public_snapshot_excludes_unapproved_fallback():
    root = Path(__file__).resolve().parents[2]
    _, files = collect_files(root)
    assert "web/public/assets/material-cover-fallback.webp" not in files


def source(tmp_path, files=None):
    root = tmp_path / "source"
    root.mkdir()
    files = files or {"README.md": b"# Synthetic release\n", ".env.example": b"INSPIRATION_VOLC_ACCESS_TOKEN=\n"}
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "release-manifest.json").write_text(json.dumps({"schema_version": 1, "version": "0.1.0-alpha", "files": sorted(files)}), encoding="utf-8")
    return root


def test_export_whitelist_excludes_unlisted_data_and_history(tmp_path):
    root = source(tmp_path)
    for name in ("API.txt", ".env", "var/real.db", ".git/config", ".pnpm-store/user-owned"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic excluded content", encoding="utf-8")
    result = export_release(root)
    with zipfile.ZipFile(root / result["archive"]) as archive:
        assert len(archive.namelist()) == 3
        assert all(name.split("/", 1)[1] in {"README.md", ".env.example", "RELEASE_MANIFEST.json"} for name in archive.namelist())
    assert (root / "API.txt").read_text() == "synthetic excluded content"
    assert result["git_history_included"] is False


def test_hash_manifest_matches_and_repeated_export_is_fresh(tmp_path):
    root = source(tmp_path)
    first, second = export_release(root), export_release(root)
    assert first["directory"] != second["directory"]
    assert first["archive"] == first["directory"] + ".zip"
    assert second["archive"] == second["directory"] + ".zip"
    for result in (first, second):
        directory = root / result["directory"]
        report = json.loads((directory / "RELEASE_MANIFEST.json").read_text())
        for file in report["files"]:
            assert hashlib.sha256((directory / file["path"]).read_bytes()).hexdigest() == file["sha256"]
        assert hashlib.sha256((root / result["archive"]).read_bytes()).hexdigest() == result["archive_sha256"]


@pytest.mark.parametrize("name", ["API.txt", ".env", "var/user.db", "HANDOFF.md", ".git/config", "spec/private.md", "a/../README.md", "C:/private.txt", "a\\README.md", "scripts/start-local-phone.ps1"])
def test_manifest_rejects_forbidden_paths(tmp_path, name):
    root = source(tmp_path)
    (root / "release-manifest.json").write_text(json.dumps({"schema_version": 1, "version": "0.1.0-alpha", "files": [name]}))
    with pytest.raises(ReleaseError):
        collect_files(root)
    assert not (root / "var").exists()


@pytest.mark.parametrize("content", [b"INSPIRATION_VOLC_ACCESS_TOKEN=synthetic-value", b"-----BEGIN " + b"PRIVATE KEY-----", b"https://" + b"test-user:synthetic-pass@example.com"])
def test_sensitive_content_rejection_never_echoes_value(tmp_path, content):
    root = source(tmp_path, {".env.example": content})
    with pytest.raises(ReleaseError) as error:
        collect_files(root)
    assert "synthetic" not in str(error.value)
    assert not (root / "var").exists()


def test_duplicate_missing_unknown_binary_and_link_rejected(tmp_path):
    root = source(tmp_path, {"README.md": b"[broken](missing.md)"})
    with pytest.raises(ReleaseError, match="unresolved-document-link"):
        collect_files(root)
    (root / "release-manifest.json").write_text(json.dumps({"schema_version": 1, "version": "0.1.0-alpha", "files": ["README.md", "readme.md"]}))
    with pytest.raises(ReleaseError, match="duplicate-manifest-path"):
        collect_files(root)
    (root / "release-manifest.json").write_text(json.dumps({"schema_version": 1, "version": "0.1.0-alpha", "files": ["missing.py"]}))
    with pytest.raises(ReleaseError, match="missing-file"):
        collect_files(root)


def test_symlink_and_output_junction_equivalent_rejected(tmp_path):
    root = source(tmp_path)
    original = root / "README.md"
    original.rename(root / "outside.md")
    try:
        original.symlink_to(root / "outside.md")
    except OSError:
        pytest.skip("Windows symlink permission unavailable")
    with pytest.raises(ReleaseError, match="unsafe-filesystem-link"):
        collect_files(root)


def test_check_does_not_create_runtime_paths(tmp_path):
    root = source(tmp_path)
    assert collect_files(root)[0] == "0.1.0-alpha"
    assert not (root / "var").exists()


def test_reviewed_synthetic_url_exception_is_exact_multiset(tmp_path):
    sample = b"https://" + b"fixture-user:fixture-pass@example.com"
    name = "backend/tests/test_synthetic.py"
    root = source(tmp_path, {name: sample})
    manifest_path = root / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["synthetic_fixture_exceptions"] = [{"file": name, "category": "credential-in-url", "match_count": 1, "match_sha256": [hashlib.sha256(sample).hexdigest()]}]
    manifest_path.write_text(json.dumps(manifest))
    assert collect_files(root)[1][name] == sample
    (root / name).write_bytes(sample + b"\n" + sample)
    with pytest.raises(ReleaseError, match="sensitive-content"):
        collect_files(root)
    (root / name).write_bytes(sample.replace(b"fixture-pass", b"changed-pass"))
    with pytest.raises(ReleaseError, match="sensitive-content"):
        collect_files(root)
    (root / name).write_bytes(b"no URL remains")
    with pytest.raises(ReleaseError, match="sensitive-content"):
        collect_files(root)


def test_fixture_exception_cannot_approve_production_or_other_categories(tmp_path):
    root = source(tmp_path)
    path = root / "release-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["synthetic_fixture_exceptions"] = [{"file": "README.md", "category": "credential-in-url", "match_count": 1, "match_sha256": ["0" * 64]}]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ReleaseError, match="invalid-fixture-exception"):
        collect_files(root)


def test_unknown_binary_and_invalid_utf8_fail_closed(tmp_path):
    root = source(tmp_path, {"data.bin": b"synthetic"})
    with pytest.raises(ReleaseError, match="unreviewed-binary"):
        collect_files(root)
    manifest = {"schema_version": 1, "version": "0.1.0-alpha", "files": ["bad.py"]}
    (root / "bad.py").write_bytes(b"\xff\xfe")
    (root / "release-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ReleaseError, match="invalid-text-encoding"):
        collect_files(root)


@pytest.mark.parametrize("body", ["[]", "null", "42", "{not json}"])
def test_malformed_manifest_is_stable_rejection(tmp_path, body):
    root = source(tmp_path)
    (root / "release-manifest.json").write_text(body)
    with pytest.raises(ReleaseError, match="invalid-manifest"):
        collect_files(root)


def test_windows_reparse_output_root_rejected_without_symlink_privilege(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat

    root = source(tmp_path)
    target = root / "var" / "releases"
    original = Path.lstat

    def metadata(path):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(ReleaseError, match="unsafe-filesystem-link"):
        export_release(root)
    assert not (root / "var").exists()
