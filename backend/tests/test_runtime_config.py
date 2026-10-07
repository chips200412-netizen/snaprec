from __future__ import annotations

import dataclasses
import builtins
import hashlib
import importlib
import io
import json
import os
import platform
import runpy
import shutil
import socket
import sqlite3
import subprocess
import sys
import types
import unittest
import uuid
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.app import runtime_config
from backend.app.runtime_config import (
    RuntimeConfigError,
    inspect_runtime_config,
    resolve_runtime_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_SCRIPT = PROJECT_ROOT / "scripts" / "check_runtime_config.py"
TEST_TEMP_ROOT = PROJECT_ROOT / "var" / "wanan" / "temp"
COVER_CACHE_MARKER = ".collection-cover-cache-v1"
COVER_CACHE_MARKER_BODY = b"collection-cover-cache-v1\n"

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

CONFIG_ATTRIBUTE_NAMES = (
    "video_asr_endpoint",
    "video_asr_token",
    "video_asr_model",
    "video_extraction_endpoint",
    "video_extraction_token",
    "video_focused_extraction_endpoint",
    "video_db_path",
    "video_temp_root",
    "video_upload_root",
    "video_max_upload_bytes",
    "video_media_root",
    "inspiration_asr_endpoint",
    "inspiration_asr_token",
    "inspiration_asr_model",
    "inspiration_ffprobe_path",
    "inspiration_temp_root",
    "cover_cache_root",
    "cover_cache_fresh_seconds",
    "cover_cache_retention_seconds",
    "cover_cache_max_bytes",
    "cover_max_response_bytes",
    "cover_cache_failure_retry_seconds",
    "inspiration_asr_provider",
    "inspiration_volc_app_id",
    "inspiration_volc_access_token",
    "inspiration_ffmpeg_path",
)

ENDPOINT_FIELDS = (
    "VIDEO_ASR_ENDPOINT",
    "VIDEO_EXTRACTION_ENDPOINT",
    "VIDEO_FOCUSED_EXTRACTION_ENDPOINT",
    "INSPIRATION_ASR_ENDPOINT",
)

MANAGED_ROOT_FIELDS = (
    "VIDEO_TEMP_ROOT",
    "VIDEO_UPLOAD_ROOT",
    "VIDEO_MEDIA_ROOT",
    "INSPIRATION_TEMP_ROOT",
    "COVER_CACHE_ROOT",
)

REPORT_KEYS = {
    "slice",
    "status",
    "platform",
    "runtime",
    "config",
    "issues",
    "error_code",
}

RUNTIME_KEYS = {
    "python",
    "sqlite",
    "python_supported",
    "sqlite_fts5_trigram",
    "ffprobe_available",
}


@contextmanager
def _temporary_project():
    TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    owner = TEST_TEMP_ROOT.resolve()
    project = (owner / f"rr1-config-contract-{uuid.uuid4().hex}").resolve()
    if owner not in project.parents:
        raise RuntimeError("unsafe RR1-CONFIG1 test root")
    project.mkdir()
    try:
        yield project
    finally:
        if project.exists():
            shutil.rmtree(project)


def _blank_process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in CONFIG_ENV_VARS:
        environment.pop(name, None)
    return environment


def _safe_cli_environment(project: Path) -> dict[str, str]:
    environment = _blank_process_environment()
    environment.update(
        {
            "VIDEO_DB_PATH": str(project / "database.sqlite3"),
            "VIDEO_TEMP_ROOT": str(project / "video-work"),
            "VIDEO_UPLOAD_ROOT": str(project / "uploads"),
            "VIDEO_MEDIA_ROOT": str(project / "retained-media"),
            "INSPIRATION_TEMP_ROOT": str(project / "inspiration-recordings"),
            "COVER_CACHE_ROOT": str(project / "cover-cache"),
        }
    )
    return environment


def _run_cli(*arguments: str, environment: dict[str, str]):
    return subprocess.run(
        [sys.executable, "-B", str(CONFIG_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            entries.append((relative, "directory", ""))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((relative, "file", digest))
    return tuple(entries)


def _issues_for(report: dict, field: str) -> list[dict[str, str]]:
    return [issue for issue in report["issues"] if issue["field"] == field]


def _has_issue_prefix(report: dict, field: str, prefix: str) -> bool:
    return any(
        issue["code"].startswith(prefix)
        for issue in _issues_for(report, field)
    )


class RuntimeConfigContractTests(unittest.TestCase):
    maxDiff = None

    def assert_report_shape(self, report: dict) -> None:
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["slice"], "RR1-CONFIG1")
        self.assertIn(report["status"], {"ready", "ready_with_warnings", "invalid"})
        self.assertIsInstance(report["platform"], str)
        self.assertEqual(set(report["runtime"]), RUNTIME_KEYS)
        self.assertIsInstance(report["runtime"]["python"], str)
        self.assertIsInstance(report["runtime"]["sqlite"], str)
        for key in (
            "python_supported",
            "sqlite_fts5_trigram",
            "ffprobe_available",
        ):
            self.assertIs(type(report["runtime"][key]), bool, key)

        self.assertEqual(tuple(report["config"]), CONFIG_ENV_VARS)
        self.assertTrue(
            set(report["config"].values()).issubset(
                {"default", "configured", "unset"}
            )
        )
        self.assertIsInstance(report["issues"], list)
        for issue in report["issues"]:
            self.assertEqual(set(issue), {"code", "field"})
            self.assertRegex(issue["code"], r"^(?:WARN|ERROR)_[A-Z0-9_]+$")
            self.assertIsInstance(issue["field"], str)
            self.assertTrue(issue["field"])

        if report["status"] == "invalid":
            self.assertRegex(report["error_code"], r"^ERROR_[A-Z0-9_]+$")
            self.assertTrue(
                any(
                    issue["code"] == report["error_code"]
                    for issue in report["issues"]
                )
            )
        else:
            self.assertIsNone(report["error_code"])

    def assert_report_redacted(self, report_or_text, *secret_values: object) -> None:
        text = (
            report_or_text
            if isinstance(report_or_text, str)
            else json.dumps(report_or_text, ensure_ascii=False, sort_keys=True)
        )
        folded = text.casefold()
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(
            text,
            r"(?i)traceback|authorization|bearer|cookie|password|private-value",
        )
        self.assertNotRegex(text, r"(?i)[a-z]:[\\/]")
        for private_value in (PROJECT_ROOT, Path.home(), *secret_values):
            if private_value is None:
                continue
            value = str(private_value)
            for spelling in {value, value.replace("\\", "/")}:
                self.assertNotIn(spelling.casefold(), folded)

    def assert_invalid_field(self, environment: dict[str, str], field: str) -> None:
        with _temporary_project() as project:
            with self.assertRaises(RuntimeConfigError) as raised:
                resolve_runtime_config(environment, project)
            error = raised.exception
            self.assertEqual(error.field, field)
            self.assertRegex(error.code, r"^ERROR_[A-Z0-9_]+$")
            self.assertNotIn(environment[field], str(error))

            report = inspect_runtime_config(environment, project)
            self.assert_report_shape(report)
            self.assertEqual(report["status"], "invalid")
            self.assertTrue(_has_issue_prefix(report, field, "ERROR_"))
            self.assert_report_redacted(report, environment[field], project)

    def test_default_environment_resolves_frozen_contract_with_asr_web_extension(self):
        """RR1-CONFIG-DEFAULTS-001 and RR1-CONFIG-PARSE-001."""
        with _temporary_project() as project:
            before = _tree_snapshot(project)
            config = resolve_runtime_config({}, project)

            self.assertTrue(dataclasses.is_dataclass(config))
            self.assertTrue(config.__dataclass_params__.frozen)
            self.assertEqual(
                tuple(field.name for field in dataclasses.fields(config)),
                CONFIG_ATTRIBUTE_NAMES,
            )
            self.assertEqual(config.video_db_path, project / "var/video_notes.sqlite3")
            self.assertEqual(config.video_temp_root, project / "var/tmp")
            self.assertEqual(config.video_upload_root, project / "var/uploads")
            self.assertEqual(config.video_media_root, project / "var/media")
            self.assertEqual(
                config.inspiration_temp_root,
                project / "var/inspiration-recordings",
            )
            self.assertEqual(config.cover_cache_root, project / "var/cover-cache")
            self.assertEqual(config.video_max_upload_bytes, 512 * 1024 * 1024)
            self.assertEqual(config.cover_cache_fresh_seconds, 24 * 60 * 60)
            self.assertEqual(config.cover_cache_retention_seconds, 30 * 24 * 60 * 60)
            self.assertEqual(config.cover_cache_max_bytes, 512 * 1024 * 1024)
            self.assertEqual(config.cover_max_response_bytes, 5 * 1024 * 1024)
            self.assertEqual(config.cover_cache_failure_retry_seconds, 60)
            for name in (
                "video_asr_endpoint",
                "video_asr_token",
                "video_asr_model",
                "video_extraction_endpoint",
                "video_extraction_token",
                "video_focused_extraction_endpoint",
                "inspiration_asr_endpoint",
                "inspiration_asr_token",
                "inspiration_asr_model",
            ):
                self.assertIsNone(getattr(config, name), name)

            with self.assertRaises(dataclasses.FrozenInstanceError):
                config.video_max_upload_bytes = 1
            self.assertEqual(_tree_snapshot(project), before)

    def test_default_inspection_is_warning_only_and_does_not_create_paths(self):
        """RR1-CONFIG-DEFAULTS-001: safe project-owned defaults remain absent."""
        with _temporary_project() as project:
            before = _tree_snapshot(project)
            report = inspect_runtime_config({}, project)

            self.assert_report_shape(report)
            self.assertEqual(report["status"], "ready_with_warnings")
            for field in ENDPOINT_FIELDS:
                self.assertTrue(_has_issue_prefix(report, field, "WARN_"), field)
                self.assertEqual(report["config"][field], "unset")
            for field in set(CONFIG_ENV_VARS) - set(ENDPOINT_FIELDS) - {
                "INSPIRATION_VOLC_APP_ID",
                "INSPIRATION_VOLC_ACCESS_TOKEN",
                "VIDEO_ASR_TOKEN",
                "VIDEO_ASR_MODEL",
                "VIDEO_EXTRACTION_TOKEN",
                "INSPIRATION_ASR_TOKEN",
                "INSPIRATION_ASR_MODEL",
            }:
                self.assertEqual(report["config"][field], "default")
            self.assertEqual(_tree_snapshot(project), before)
            self.assert_report_redacted(report, project)

    def test_blank_values_use_unset_or_default_semantics(self):
        """RR1-CONFIG-PARSE-001: surrounding whitespace never becomes a value."""
        environment = {name: " \t " for name in CONFIG_ENV_VARS}
        with _temporary_project() as project:
            config = resolve_runtime_config(environment, project)
            self.assertEqual(config.video_db_path, project / "var/video_notes.sqlite3")
            self.assertEqual(config.video_max_upload_bytes, 512 * 1024 * 1024)
            self.assertIsNone(config.video_asr_endpoint)
            self.assertIsNone(config.video_asr_token)
            self.assertEqual(config.inspiration_ffprobe_path, "ffprobe")

            report = inspect_runtime_config(environment, project)
            for field in ENDPOINT_FIELDS:
                self.assertEqual(report["config"][field], "unset")
            self.assertEqual(report["config"]["VIDEO_DB_PATH"], "default")
            self.assertEqual(
                report["config"]["VIDEO_MAX_UPLOAD_BYTES"],
                "default",
            )

    def test_valid_numeric_boundaries_enter_the_config(self):
        """RR1-CONFIG-PARSE-001: positive finite values and the 5 MiB cap pass."""
        environment = {
            "VIDEO_MAX_UPLOAD_BYTES": "1",
            "COVER_CACHE_FRESH_SECONDS": "0.25",
            "COVER_CACHE_RETENTION_SECONDS": "1e2",
            "COVER_CACHE_MAX_BYTES": "2",
            "COVER_MAX_RESPONSE_BYTES": str(5 * 1024 * 1024),
            "COVER_CACHE_FAILURE_RETRY_SECONDS": "0.5",
        }
        with _temporary_project() as project:
            config = resolve_runtime_config(environment, project)
            self.assertEqual(config.video_max_upload_bytes, 1)
            self.assertEqual(config.cover_cache_fresh_seconds, 0.25)
            self.assertEqual(config.cover_cache_retention_seconds, 100.0)
            self.assertEqual(config.cover_cache_max_bytes, 2)
            self.assertEqual(config.cover_max_response_bytes, 5 * 1024 * 1024)
            self.assertEqual(config.cover_cache_failure_retry_seconds, 0.5)

    def test_invalid_numeric_matrix_returns_stable_field_errors(self):
        """RR1-CONFIG-PARSE-001: malformed, non-finite, and non-positive fail."""
        invalid_cases = (
            ("VIDEO_MAX_UPLOAD_BYTES", "1.5"),
            ("VIDEO_MAX_UPLOAD_BYTES", "0"),
            ("VIDEO_MAX_UPLOAD_BYTES", "-1"),
            ("COVER_CACHE_MAX_BYTES", "not-an-int"),
            ("COVER_MAX_RESPONSE_BYTES", str(5 * 1024 * 1024 + 1)),
            ("COVER_CACHE_FRESH_SECONDS", "0"),
            ("COVER_CACHE_FRESH_SECONDS", "nan"),
            ("COVER_CACHE_RETENTION_SECONDS", "Infinity"),
            ("COVER_CACHE_FAILURE_RETRY_SECONDS", "-0.5"),
        )
        for field, value in invalid_cases:
            with self.subTest(field=field, value=value):
                self.assert_invalid_field({field: value}, field)

    def test_relative_paths_anchor_to_project_and_missing_roots_only_warn(self):
        """RR1-CONFIG-PATH-001: resolution is cwd-independent and read-only."""
        environment = {
            "VIDEO_DB_PATH": "state/database.sqlite3",
            "VIDEO_TEMP_ROOT": "state/video-work",
            "VIDEO_UPLOAD_ROOT": "state/uploads",
            "VIDEO_MEDIA_ROOT": "state/media",
            "INSPIRATION_TEMP_ROOT": "state/inspiration",
            "COVER_CACHE_ROOT": "state/covers",
        }
        with _temporary_project() as project:
            before = _tree_snapshot(project)
            original_cwd = Path.cwd()
            config = resolve_runtime_config(environment, project)
            report = inspect_runtime_config(environment, project)

            self.assertEqual(config.video_db_path, project / "state/database.sqlite3")
            self.assertEqual(config.video_temp_root, project / "state/video-work")
            self.assertEqual(config.video_upload_root, project / "state/uploads")
            self.assertEqual(config.video_media_root, project / "state/media")
            self.assertEqual(
                config.inspiration_temp_root,
                project / "state/inspiration",
            )
            self.assertEqual(config.cover_cache_root, project / "state/covers")
            for field in MANAGED_ROOT_FIELDS:
                self.assertTrue(_has_issue_prefix(report, field, "WARN_"), field)
            self.assertEqual(Path.cwd(), original_cwd)
            self.assertEqual(_tree_snapshot(project), before)

    def test_dangerous_path_shapes_fail_without_disclosure(self):
        """RR1-CONFIG-PATH-001: broad targets, files, overlap, and DB nesting fail."""
        with _temporary_project() as project:
            ordinary_file = project / "ordinary-file"
            ordinary_file.write_bytes(b"private-value")
            cases = (
                ({"VIDEO_TEMP_ROOT": str(project)}, "VIDEO_TEMP_ROOT"),
                ({"VIDEO_TEMP_ROOT": str(Path.home())}, "VIDEO_TEMP_ROOT"),
                ({"VIDEO_TEMP_ROOT": str(Path(project.anchor))}, "VIDEO_TEMP_ROOT"),
                ({"VIDEO_TEMP_ROOT": str(ordinary_file)}, "VIDEO_TEMP_ROOT"),
                ({"VIDEO_DB_PATH": str(project)}, "VIDEO_DB_PATH"),
                (
                    {
                        "VIDEO_TEMP_ROOT": "state/shared",
                        "VIDEO_UPLOAD_ROOT": "state/shared/child",
                    },
                    "VIDEO_UPLOAD_ROOT",
                ),
                (
                    {
                        "VIDEO_TEMP_ROOT": "state/video-work",
                        "VIDEO_DB_PATH": "state/video-work/database.sqlite3",
                    },
                    "VIDEO_DB_PATH",
                ),
            )
            for environment, expected_field in cases:
                with self.subTest(environment=environment):
                    with self.assertRaises(RuntimeConfigError) as raised:
                        resolve_runtime_config(environment, project)
                    self.assertEqual(raised.exception.field, expected_field)
                    report = inspect_runtime_config(environment, project)
                    self.assertEqual(report["status"], "invalid")
                    self.assertTrue(
                        _has_issue_prefix(report, expected_field, "ERROR_")
                    )
                    self.assert_report_redacted(
                        report,
                        *environment.values(),
                        project,
                    )

    def test_non_directory_ancestor_fails_before_startup_side_effects(self):
        """RR1-CONFIG-PATH-001 and RR1-CONFIG-STARTUP-001: fail in resolve."""
        with _temporary_project() as project:
            blocker = project / "README.md"
            blocker.write_bytes(b"private-value-must-remain-unchanged")
            cases = (
                ("VIDEO_DB_PATH", "README.md/database.sqlite3"),
                ("VIDEO_TEMP_ROOT", "README.md/video-work"),
                ("VIDEO_UPLOAD_ROOT", "README.md/uploads"),
                ("VIDEO_MEDIA_ROOT", "README.md/retained-media"),
                ("INSPIRATION_TEMP_ROOT", "README.md/inspiration"),
                ("COVER_CACHE_ROOT", "README.md/covers"),
            )
            before = _tree_snapshot(project)
            unexpectedly_accepted: list[str] = []

            for field, value in cases:
                with self.subTest(field=field):
                    try:
                        resolve_runtime_config({field: value}, project)
                    except RuntimeConfigError as error:
                        self.assertEqual(error.field, field)
                        self.assertRegex(error.code, r"^ERROR_[A-Z0-9_]+$")
                        self.assertNotIn(value, str(error))
                        report = inspect_runtime_config({field: value}, project)
                        self.assertEqual(report["status"], "invalid")
                        self.assertEqual(report["error_code"], error.code)
                        self.assertTrue(_has_issue_prefix(report, field, "ERROR_"))
                        self.assert_report_redacted(report, blocker, value, project)
                    else:
                        unexpectedly_accepted.append(field)
                    self.assertEqual(_tree_snapshot(project), before)

            self.assertEqual(unexpectedly_accepted, [])

    @unittest.skipUnless(os.name == "nt", "Windows rooted-relative contract")
    def test_windows_rooted_relative_paths_fail_in_resolve_and_stay_redacted(self):
        """RR1-CONFIG-PATH-001 and RR1-CONFIG-STARTUP-001: reject rooted-relative."""
        fields = (
            "VIDEO_DB_PATH",
            *MANAGED_ROOT_FIELDS,
            "INSPIRATION_FFPROBE_PATH",
        )
        rooted_values = (r"\rr1-config-rooted", "/rr1-config-rooted")
        unexpectedly_accepted: list[tuple[str, str]] = []

        with _temporary_project() as project:
            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()

            for field in fields:
                for value in rooted_values:
                    environment = {field: value}
                    environment_before = environment.copy()
                    label = "backslash" if value.startswith("\\") else "slash"
                    with self.subTest(field=field, rooted=label):
                        try:
                            resolve_runtime_config(environment, project)
                        except RuntimeConfigError as error:
                            self.assertEqual(error.field, field)
                            self.assertRegex(error.code, r"^ERROR_[A-Z0-9_]+$")
                            self.assertNotIn(value, str(error))
                            report = inspect_runtime_config(environment, project)
                            self.assertEqual(report["status"], "invalid")
                            self.assertEqual(report["error_code"], error.code)
                            self.assertTrue(
                                _has_issue_prefix(report, field, "ERROR_")
                            )
                            self.assert_report_redacted(report, value, project)
                        else:
                            unexpectedly_accepted.append((field, label))

                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

        self.assertEqual(unexpectedly_accepted, [])

    @unittest.skipUnless(os.name == "nt", "Windows lexical path contract")
    def test_windows_uncreatable_or_alias_path_segments_fail_lexically(self):
        """RR1-CONFIG-PATH-001 and RR1-CONFIG-STARTUP-001: reject aliases."""
        fields = (
            "VIDEO_DB_PATH",
            *MANAGED_ROOT_FIELDS,
            "INSPIRATION_FFPROBE_PATH",
        )
        invalid_paths = (
            ("invalid_character", "state/bad<name"),
            ("reserved_device", "state/NUL.txt"),
            ("trailing_dot", "state/trailing."),
            ("trailing_space", "state/trailing "),
            ("alternate_data_stream", "state/file:stream"),
        )
        valid_paths = (
            "state/普通素材",
            "state/middle space/name",
            "state/release.v1",
        )
        unexpected_results: list[tuple[str, str, str]] = []

        with _temporary_project() as project:
            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()

            for field in fields:
                attribute = CONFIG_ATTRIBUTE_NAMES[CONFIG_ENV_VARS.index(field)]
                for label, value in invalid_paths:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, invalid=label):
                        try:
                            resolve_runtime_config(environment, project)
                        except RuntimeConfigError as error:
                            self.assertEqual(error.field, field)
                            self.assertNotIn(value, str(error))
                            report = inspect_runtime_config(environment, project)
                            self.assertEqual(report["status"], "invalid")
                            self.assertEqual(report["error_code"], error.code)
                            self.assertTrue(
                                _has_issue_prefix(report, field, "ERROR_")
                            )
                            self.assert_report_redacted(report, value, project)
                            if error.code != "ERROR_PATH_INVALID":
                                unexpected_results.append(
                                    (field, label, error.code)
                                )
                        else:
                            unexpected_results.append((field, label, "accepted"))

                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

                for value in valid_paths:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, valid=value):
                        config = resolve_runtime_config(environment, project)
                        expected = project / value
                        actual = getattr(config, attribute)
                        if field == "INSPIRATION_FFPROBE_PATH":
                            self.assertEqual(actual, os.fspath(expected))
                        else:
                            self.assertEqual(actual, expected)
                        report = inspect_runtime_config(environment, project)
                        self.assertNotEqual(report["status"], "invalid")
                        self.assert_report_redacted(
                            report,
                            value,
                            expected,
                            project,
                        )
                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

        self.assertEqual(unexpected_results, [])

    def test_database_parent_of_managed_root_fails_before_startup_side_effects(self):
        """RR1-CONFIG-PATH-001 and RR1-CONFIG-STARTUP-001: reject containment."""
        child_names = {
            "VIDEO_TEMP_ROOT": "video-work",
            "VIDEO_UPLOAD_ROOT": "uploads",
            "VIDEO_MEDIA_ROOT": "retained-media",
            "INSPIRATION_TEMP_ROOT": "inspiration",
            "COVER_CACHE_ROOT": "covers",
        }
        unexpectedly_accepted: list[str] = []

        with _temporary_project() as project:
            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            for field, child in child_names.items():
                environment = {
                    "VIDEO_DB_PATH": "state/db-slot",
                    field: f"state/db-slot/{child}",
                }
                environment_before = environment.copy()
                with self.subTest(field=field):
                    try:
                        resolve_runtime_config(environment, project)
                    except RuntimeConfigError as error:
                        first_error = (error.code, error.field)
                        self.assertRegex(error.code, r"^ERROR_[A-Z0-9_]+$")
                        self.assertIn(error.field, {"VIDEO_DB_PATH", field})
                        self.assertNotIn(environment["VIDEO_DB_PATH"], str(error))
                        with self.assertRaises(RuntimeConfigError) as repeated:
                            resolve_runtime_config(environment, project)
                        self.assertEqual(
                            (repeated.exception.code, repeated.exception.field),
                            first_error,
                        )
                        report = inspect_runtime_config(environment, project)
                        self.assertEqual(report["status"], "invalid")
                        self.assertEqual(report["error_code"], error.code)
                        self.assertTrue(
                            _has_issue_prefix(report, error.field, "ERROR_")
                        )
                        self.assert_report_redacted(
                            report,
                            *environment.values(),
                            project,
                        )
                    else:
                        unexpectedly_accepted.append(field)

                    self.assertEqual(environment, environment_before)
                    self.assertEqual(os.environ, process_environment_before)
                    self.assertEqual(_tree_snapshot(project), tree_before)

            with self.assertRaises(RuntimeConfigError):
                resolve_runtime_config(
                    {
                        "VIDEO_DB_PATH": "state/root/database.sqlite3",
                        "VIDEO_TEMP_ROOT": "state/root",
                    },
                    project,
                )

            adjacent = {
                "VIDEO_DB_PATH": "state/db-slot.sqlite3",
                "VIDEO_TEMP_ROOT": "state/db-slot-video",
                "VIDEO_UPLOAD_ROOT": "state/db-slot-uploads",
                "VIDEO_MEDIA_ROOT": "state/db-slot-media",
                "INSPIRATION_TEMP_ROOT": "state/db-slot-inspiration",
                "COVER_CACHE_ROOT": "state/db-slot-covers",
            }
            adjacent_config = resolve_runtime_config(adjacent, project)
            self.assertEqual(
                adjacent_config.video_db_path,
                project / "state/db-slot.sqlite3",
            )
            self.assertEqual(_tree_snapshot(project), tree_before)

        self.assertEqual(unexpectedly_accepted, [])

    def test_symlink_or_reparse_paths_fail_closed_when_host_supports_them(self):
        """RR1-CONFIG-PATH-001: links and linked ancestors are never followed."""
        with _temporary_project() as project:
            outside = project / "outside"
            outside.mkdir()
            link = project / "linked-root"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable on this Windows host")

            cases = (
                ({"VIDEO_TEMP_ROOT": str(link)}, "VIDEO_TEMP_ROOT"),
                (
                    {"VIDEO_DB_PATH": str(link / "database.sqlite3")},
                    "VIDEO_DB_PATH",
                ),
            )
            for environment, expected_field in cases:
                with self.subTest(environment=environment):
                    with self.assertRaises(RuntimeConfigError) as raised:
                        resolve_runtime_config(environment, project)
                    self.assertEqual(raised.exception.field, expected_field)
                    self.assertRegex(
                        raised.exception.code,
                        r"^ERROR_[A-Z0-9_]+$",
                    )

    def test_cover_cache_nonempty_root_requires_exact_regular_marker(self):
        """RR1-CONFIG-PATH-001: existing cache ownership is proven read-only."""
        with _temporary_project() as project:
            cover_root = project / "covers"
            cover_root.mkdir()
            environment = {"COVER_CACHE_ROOT": str(cover_root)}

            empty_report = inspect_runtime_config(environment, project)
            self.assertNotEqual(empty_report["status"], "invalid")

            payload = cover_root / "cached.bin"
            payload.write_bytes(b"private-value")
            missing_report = inspect_runtime_config(environment, project)
            self.assertEqual(missing_report["status"], "invalid")
            self.assertTrue(
                _has_issue_prefix(missing_report, "COVER_CACHE_ROOT", "ERROR_")
            )

            marker = cover_root / COVER_CACHE_MARKER
            marker.write_bytes(b"wrong-marker\n")
            wrong_report = inspect_runtime_config(environment, project)
            self.assertEqual(wrong_report["status"], "invalid")

            marker.write_bytes(COVER_CACHE_MARKER_BODY)
            before = _tree_snapshot(project)
            valid_report = inspect_runtime_config(environment, project)
            self.assertNotEqual(valid_report["status"], "invalid")
            self.assertEqual(_tree_snapshot(project), before)
            self.assert_report_redacted(valid_report, payload, project)

    def test_malformed_provider_endpoint_matrix_fails_offline_and_redacted(self):
        """RR1-CONFIG-PROVIDER-001: only strict HTTP(S) URL shape is accepted."""
        malformed_values = (
            "ftp://example.invalid/api",
            "https:///missing-host",
            "https://user:password@example.invalid/api",
            "https://example.invalid:not-a-port/api",
            "https://example.invalid/api\nprivate-value",
        )
        for field in ENDPOINT_FIELDS:
            for value in malformed_values:
                with self.subTest(field=field, value=value):
                    self.assert_invalid_field({field: value}, field)

    def test_provider_percent_encoding_rejects_malformed_hosts_only(self):
        """RR1-CONFIG-PROVIDER-001: malformed host escapes fail, valid URL escapes pass."""
        malformed_hosts = (
            "http://%zz/",
            "http://exa%mple.test/",
        )
        for field in ENDPOINT_FIELDS:
            for value in malformed_hosts:
                with self.subTest(field=field, malformed=value):
                    self.assert_invalid_field({field: value}, field)

        valid_url = "https://example.test/api/%E4%B8%AD?mode=%31"
        for field in ENDPOINT_FIELDS:
            with self.subTest(field=field, valid=valid_url):
                with _temporary_project() as project:
                    config = resolve_runtime_config({field: valid_url}, project)
                    self.assertEqual(
                        getattr(
                            config,
                            CONFIG_ATTRIBUTE_NAMES[CONFIG_ENV_VARS.index(field)],
                        ),
                        valid_url,
                    )
                    report = inspect_runtime_config({field: valid_url}, project)
                    self.assertNotEqual(report["status"], "invalid")
                    self.assertTrue(_has_issue_prefix(report, field, "WARN_"))
                    self.assert_report_redacted(report, valid_url, project)

    def test_provider_url_edge_matrix_matches_modern_http_clients(self):
        """RR1-CONFIG-PROVIDER-001: reject ambiguous hosts, C1, and bad URL escapes."""
        invalid_urls = (
            ("emoji_idna_host", "https://😀.test/"),
            ("invalid_ascii_alabel", "https://xn--abc.test/"),
            ("invalid_ipv4_host", "http://999.999.999.999/"),
            ("port_zero", "https://example.test:0/"),
            ("empty_port", "https://example.test:/"),
            ("double_trailing_dot", "https://example.test../"),
            ("bracketed_ipvfuture", "https://[v1.foo]/"),
            ("c1_path_control", "https://example.test/api/\u0080payload"),
            ("surrogate_path", "https://example.test/api/\ud800"),
            ("surrogate_query", "https://example.test/?query=\ud800"),
            ("surrogate_fragment", "https://example.test/#\ud800"),
            ("path_invalid_percent", "https://example.test/api/%zz"),
            ("path_stray_percent", "https://example.test/api/%"),
            ("query_invalid_percent", "https://example.test/?query=%zz"),
            ("query_stray_percent", "https://example.test/?query=%"),
        )
        unexpectedly_accepted: list[tuple[str, str]] = []
        with _temporary_project() as invalid_project:
            for field in ENDPOINT_FIELDS:
                for label, value in invalid_urls:
                    with self.subTest(field=field, invalid=label):
                        try:
                            resolve_runtime_config({field: value}, invalid_project)
                        except RuntimeConfigError as error:
                            self.assertEqual(error.field, field)
                            self.assertRegex(error.code, r"^ERROR_[A-Z0-9_]+$")
                            self.assertNotIn(value, str(error))
                            report = inspect_runtime_config(
                                {field: value},
                                invalid_project,
                            )
                            self.assertEqual(report["status"], "invalid")
                            self.assertTrue(
                                _has_issue_prefix(report, field, "ERROR_")
                            )
                            self.assert_report_redacted(
                                report,
                                value,
                                invalid_project,
                            )
                        else:
                            unexpectedly_accepted.append((field, label))

        valid_urls = (
            "https://例子.测试/api/%E4%B8%AD?mode=%31",
            "https://xn--fsqu00a.xn--0zwm56d/api",
            "http://127.0.0.1:1/health",
            "https://[2001:db8::1]:65535/api",
            "https://[::1]:443/health",
        )
        for field in ENDPOINT_FIELDS:
            for value in valid_urls:
                with self.subTest(field=field, valid=value):
                    with _temporary_project() as project:
                        config = resolve_runtime_config({field: value}, project)
                        self.assertEqual(
                            getattr(
                                config,
                                CONFIG_ATTRIBUTE_NAMES[
                                    CONFIG_ENV_VARS.index(field)
                                ],
                            ),
                            value,
                        )
                        report = inspect_runtime_config({field: value}, project)
                        self.assertNotEqual(report["status"], "invalid")
                        self.assertTrue(_has_issue_prefix(report, field, "WARN_"))
                        self.assert_report_redacted(report, value, project)

        self.assertEqual(unexpectedly_accepted, [])

    def test_provider_bracket_suffix_and_idna_ipv4_are_rejected(self):
        """RR1-CONFIG-PROVIDER-001: reject bracket suffixes and mapped bad IPv4."""
        invalid_urls = (
            ("bracket_suffix_text", "https://[::1]x/"),
            ("bracket_suffix_domain", "https://[::1].example/"),
            ("bracket_suffix_before_port", "https://[::1]x:443/"),
            ("idna_mapped_invalid_ipv4", "https://999。999。999。999/"),
        )
        valid_urls = (
            "http://127。0。0。1/health",
            "https://[2001:db8::1]/api",
            "https://[::1]:443/health",
        )
        unexpectedly_accepted: list[tuple[str, str]] = []

        with _temporary_project() as project:
            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()

            for field in ENDPOINT_FIELDS:
                for label, value in invalid_urls:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, invalid=label):
                        try:
                            resolve_runtime_config(environment, project)
                        except RuntimeConfigError as error:
                            self.assertEqual(error.code, "ERROR_ENDPOINT_INVALID")
                            self.assertEqual(error.field, field)
                            self.assertNotIn(value, str(error))
                            report = inspect_runtime_config(environment, project)
                            self.assertEqual(report["status"], "invalid")
                            self.assertEqual(report["error_code"], error.code)
                            self.assertTrue(
                                _has_issue_prefix(report, field, "ERROR_")
                            )
                            self.assert_report_redacted(report, value, project)
                        else:
                            unexpectedly_accepted.append((field, label))

                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

                for value in valid_urls:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, valid=value):
                        config = resolve_runtime_config(environment, project)
                        self.assertEqual(
                            getattr(
                                config,
                                CONFIG_ATTRIBUTE_NAMES[
                                    CONFIG_ENV_VARS.index(field)
                                ],
                            ),
                            value,
                        )
                        report = inspect_runtime_config(environment, project)
                        self.assertNotEqual(report["status"], "invalid")
                        self.assertTrue(_has_issue_prefix(report, field, "WARN_"))
                        self.assert_report_redacted(report, value, project)
                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

        self.assertEqual(unexpectedly_accepted, [])

    def test_provider_idna_and_ascii_single_trailing_dot_matrix(self):
        """RR1-CONFIG-PROVIDER-001: allow one mapped trailing dot, never two."""
        valid_urls = (
            ("unicode_idna_single_dot", "https://例子。测试。/"),
            ("ascii_single_dot", "https://example.test./"),
        )
        invalid_urls = (
            ("unicode_idna_double_dot", "https://例子。测试。。/"),
            ("ascii_double_dot", "https://example.test../"),
        )
        unexpected_results: list[tuple[str, str, str]] = []

        with _temporary_project() as project:
            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()

            for field in ENDPOINT_FIELDS:
                attribute = CONFIG_ATTRIBUTE_NAMES[CONFIG_ENV_VARS.index(field)]
                for label, value in valid_urls:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, valid=label):
                        try:
                            config = resolve_runtime_config(environment, project)
                        except RuntimeConfigError as error:
                            unexpected_results.append(
                                (field, label, f"rejected:{error.code}")
                            )
                        else:
                            self.assertEqual(getattr(config, attribute), value)
                            report = inspect_runtime_config(environment, project)
                            self.assertNotEqual(report["status"], "invalid")
                            self.assertTrue(_has_issue_prefix(report, field, "WARN_"))
                            self.assert_report_redacted(report, value, project)

                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

                for label, value in invalid_urls:
                    environment = {field: value}
                    environment_before = environment.copy()
                    with self.subTest(field=field, invalid=label):
                        try:
                            resolve_runtime_config(environment, project)
                        except RuntimeConfigError as error:
                            self.assertEqual(error.code, "ERROR_ENDPOINT_INVALID")
                            self.assertEqual(error.field, field)
                            self.assertNotIn(value, str(error))
                            report = inspect_runtime_config(environment, project)
                            self.assertEqual(report["status"], "invalid")
                            self.assertEqual(report["error_code"], error.code)
                            self.assertTrue(
                                _has_issue_prefix(report, field, "ERROR_")
                            )
                            self.assert_report_redacted(report, value, project)
                        else:
                            unexpected_results.append((field, label, "accepted"))

                        self.assertEqual(environment, environment_before)
                        self.assertEqual(os.environ, process_environment_before)
                        self.assertEqual(Path.cwd(), cwd_before)
                        self.assertEqual(_tree_snapshot(project), tree_before)

        self.assertEqual(unexpected_results, [])

    def test_valid_provider_endpoints_are_never_resolved_or_disclosed(self):
        """RR1-CONFIG-PROVIDER-001: valid local/self-hosted shapes remain unverified."""
        endpoints = {
            "VIDEO_ASR_ENDPOINT": "http://127.0.0.1:8765/asr",
            "VIDEO_EXTRACTION_ENDPOINT": "https://extractor.example.invalid/full",
            "VIDEO_FOCUSED_EXTRACTION_ENDPOINT": "https://[::1]:9443/focused",
            "INSPIRATION_ASR_ENDPOINT": "https://speech.example.invalid/asr",
        }
        environment = {
            **endpoints,
            "VIDEO_ASR_TOKEN": "VIDEO-ASR-SECRET",
            "VIDEO_ASR_MODEL": "PRIVATE-VIDEO-MODEL",
            "VIDEO_EXTRACTION_TOKEN": "EXTRACTION-SECRET",
            "INSPIRATION_ASR_TOKEN": "INSPIRATION-SECRET",
            "INSPIRATION_ASR_MODEL": "PRIVATE-INSPIRATION-MODEL",
        }
        with _temporary_project() as project:
            with (
                patch.object(
                    socket,
                    "getaddrinfo",
                    side_effect=AssertionError("DNS is forbidden"),
                ),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network is forbidden"),
                ),
            ):
                report = inspect_runtime_config(environment, project)

            self.assert_report_shape(report)
            self.assertNotEqual(report["status"], "invalid")
            for field in ENDPOINT_FIELDS:
                self.assertTrue(_has_issue_prefix(report, field, "WARN_"), field)
                self.assertEqual(report["config"][field], "configured")
            self.assert_report_redacted(
                report,
                *environment.values(),
                project,
            )

    def test_provider_orphans_warn_and_secret_values_do_not_change_report(self):
        """RR1-CONFIG-PROVIDER-001 and RR1-CONFIG-REPORT-001."""
        first = {
            "VIDEO_ASR_TOKEN": "FIRST-VIDEO-TOKEN",
            "VIDEO_ASR_MODEL": "FIRST-VIDEO-MODEL",
            "VIDEO_EXTRACTION_TOKEN": "FIRST-EXTRACTION-TOKEN",
            "INSPIRATION_ASR_TOKEN": "FIRST-INSPIRATION-TOKEN",
            "INSPIRATION_ASR_MODEL": "FIRST-INSPIRATION-MODEL",
        }
        second = {
            field: value.replace("FIRST", "SECOND")
            for field, value in first.items()
        }
        with _temporary_project() as project:
            first_report = inspect_runtime_config(first, project)
            second_report = inspect_runtime_config(second, project)

            self.assertEqual(first_report, second_report)
            for field in first:
                self.assertTrue(
                    _has_issue_prefix(first_report, field, "WARN_"),
                    field,
                )
                self.assertEqual(first_report["config"][field], "configured")
            self.assert_report_redacted(
                first_report,
                *first.values(),
                *second.values(),
                project,
            )

    def test_runtime_probe_uses_only_in_memory_sqlite_and_ffprobe_metadata(self):
        """RR1-CONFIG-RUNTIME-001 and RR1-CONFIG-SCOPE-001."""
        with _temporary_project() as project:
            real_connect = sqlite3.connect
            opened_databases: list[str] = []

            def guarded_connect(database, *args, **kwargs):
                opened_databases.append(str(database))
                if str(database) != ":memory:":
                    raise AssertionError("file SQLite is forbidden")
                return real_connect(database, *args, **kwargs)

            with (
                patch.object(
                    runtime_config.sqlite3,
                    "connect",
                    side_effect=guarded_connect,
                ),
                patch.object(runtime_config.shutil, "which", return_value=None),
                patch.object(
                    subprocess,
                    "Popen",
                    side_effect=AssertionError("external processes are forbidden"),
                ),
            ):
                report = inspect_runtime_config({}, project)

            self.assertEqual(opened_databases, [":memory:"])
            self.assertFalse(report["runtime"]["ffprobe_available"])
            self.assertTrue(
                _has_issue_prefix(
                    report,
                    "INSPIRATION_FFPROBE_PATH",
                    "WARN_",
                )
            )

    def test_runtime_hard_gates_platform_python_and_sqlite_capability(self):
        """RR1-CONFIG-RUNTIME-001: only the approved Windows 3.12 lane passes."""
        with _temporary_project() as project:
            with patch.object(runtime_config.platform, "system", return_value="Linux"):
                linux_report = inspect_runtime_config({}, project)
            self.assertEqual(linux_report["status"], "invalid")
            self.assertEqual(linux_report["platform"], "Linux")
            self.assertTrue(
                any(
                    issue["code"].startswith("ERROR_")
                    and issue["field"] == "PLATFORM"
                    for issue in linux_report["issues"]
                )
            )

            with (
                patch.object(runtime_config.platform, "system", return_value="Windows"),
                patch.object(
                    runtime_config.sys,
                    "version_info",
                    (3, 11, 9, "final", 0),
                ),
            ):
                python_report = inspect_runtime_config({}, project)
            self.assertEqual(python_report["status"], "invalid")
            self.assertFalse(python_report["runtime"]["python_supported"])
            self.assertTrue(
                any(
                    issue["code"].startswith("ERROR_")
                    and issue["field"] == "PYTHON"
                    for issue in python_report["issues"]
                )
            )

            with patch.object(
                runtime_config.sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("private sqlite failure"),
            ):
                sqlite_report = inspect_runtime_config({}, project)
            self.assertEqual(sqlite_report["status"], "invalid")
            self.assertFalse(sqlite_report["runtime"]["sqlite_fts5_trigram"])
            self.assertTrue(
                any(
                    issue["code"].startswith("ERROR_")
                    and issue["field"] == "SQLITE"
                    for issue in sqlite_report["issues"]
                )
            )
            self.assert_report_redacted(sqlite_report, "private sqlite failure", project)

    def test_explicit_ffprobe_path_uses_metadata_and_is_never_executed(self):
        """RR1-CONFIG-RUNTIME-001: explicit ffprobe is discovered as a regular file."""
        with _temporary_project() as project:
            executable = project / "tools" / "ffprobe.exe"
            executable.parent.mkdir()
            executable.write_bytes(b"not-an-executable-and-must-never-run")
            environment = {"INSPIRATION_FFPROBE_PATH": "tools/ffprobe.exe"}

            with patch.object(
                subprocess,
                "Popen",
                side_effect=AssertionError("ffprobe execution is forbidden"),
            ):
                report = inspect_runtime_config(environment, project)

            self.assertTrue(report["runtime"]["ffprobe_available"])
            self.assertFalse(
                _has_issue_prefix(
                    report,
                    "INSPIRATION_FFPROBE_PATH",
                    "WARN_",
                )
            )
            self.assert_report_redacted(report, executable, project)

    @unittest.skipUnless(os.name == "nt", "Windows dot-relative path contract")
    def test_dot_relative_ffprobe_is_project_anchored_and_never_uses_which(self):
        """RR1-CONFIG-PATH-001, DEFAULTS-001, STARTUP-001: anchor dot-relative."""
        with _temporary_project() as project:
            project_ffprobe = project / "ffprobe.exe"
            project_ffprobe.write_bytes(b"project-owned-ffprobe-sentinel")
            anchored_ffprobe = project / "tools" / "ffprobe.exe"
            anchored_ffprobe.parent.mkdir()
            anchored_ffprobe.write_bytes(b"anchored-ffprobe-sentinel")
            cwd_lure = project / "cwd-lure"
            cwd_lure.mkdir()
            (cwd_lure / "ffprobe.exe").write_bytes(b"cwd-lure")
            path_lure = project / "path-lure"
            path_lure.mkdir()
            path_lure_ffprobe = path_lure / "ffprobe.exe"
            path_lure_ffprobe.write_bytes(b"path-lure")

            tree_before = _tree_snapshot(project)
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()
            unexpected_results: list[tuple[str, str]] = []
            dot_reports: list[dict] = []
            which_calls: list[tuple[str, Path, str]] = []

            def lure_which(command: str):
                which_calls.append(
                    (command, Path.cwd(), os.environ.get("PATH", ""))
                )
                return os.fspath(path_lure_ffprobe)

            with patch.dict(os.environ, {"PATH": os.fspath(path_lure)}):
                try:
                    os.chdir(cwd_lure)
                    checker_environment_before = os.environ.copy()

                    for label, value in (
                        ("backslash_dot_relative", r".\ffprobe.exe"),
                        ("slash_dot_relative", "./ffprobe.exe"),
                    ):
                        environment = {"INSPIRATION_FFPROBE_PATH": value}
                        environment_before = environment.copy()
                        which_calls.clear()
                        with self.subTest(dot_relative=label):
                            config = resolve_runtime_config(environment, project)
                            if config.inspiration_ffprobe_path != os.fspath(
                                project_ffprobe
                            ):
                                unexpected_results.append(
                                    (label, "not_project_anchored")
                                )

                            with patch.object(
                                runtime_config.shutil,
                                "which",
                                side_effect=lure_which,
                            ):
                                report = inspect_runtime_config(environment, project)
                            if which_calls:
                                unexpected_results.append((label, "used_which"))
                            dot_reports.append(report)
                            self.assertTrue(report["runtime"]["ffprobe_available"])
                            self.assertNotEqual(report["status"], "invalid")
                            self.assert_report_redacted(
                                report,
                                value,
                                project_ffprobe,
                                cwd_lure,
                                path_lure,
                                project,
                            )
                            self.assertEqual(environment, environment_before)
                            self.assertEqual(os.environ, checker_environment_before)
                            self.assertEqual(Path.cwd(), cwd_lure)
                            self.assertEqual(_tree_snapshot(project), tree_before)

                    which_calls.clear()
                    bare_environment = {"INSPIRATION_FFPROBE_PATH": "ffprobe"}
                    bare_config = resolve_runtime_config(bare_environment, project)
                    self.assertEqual(bare_config.inspiration_ffprobe_path, "ffprobe")
                    with patch.object(
                        runtime_config.shutil,
                        "which",
                        side_effect=lure_which,
                    ):
                        bare_report = inspect_runtime_config(
                            bare_environment,
                            project,
                        )
                    self.assertEqual(
                        [(call[0], call[1]) for call in which_calls],
                        [("ffprobe", cwd_lure)],
                    )
                    self.assertTrue(bare_report["runtime"]["ffprobe_available"])
                    self.assert_report_redacted(
                        bare_report,
                        cwd_lure,
                        path_lure,
                        project,
                    )

                    which_calls.clear()
                    anchored_environment = {
                        "INSPIRATION_FFPROBE_PATH": "tools/ffprobe.exe"
                    }
                    anchored_config = resolve_runtime_config(
                        anchored_environment,
                        project,
                    )
                    self.assertEqual(
                        anchored_config.inspiration_ffprobe_path,
                        os.fspath(anchored_ffprobe),
                    )
                    with patch.object(
                        runtime_config.shutil,
                        "which",
                        side_effect=lure_which,
                    ):
                        anchored_report = inspect_runtime_config(
                            anchored_environment,
                            project,
                        )
                    self.assertEqual(which_calls, [])
                    self.assertTrue(
                        anchored_report["runtime"]["ffprobe_available"]
                    )
                    self.assert_report_redacted(
                        anchored_report,
                        anchored_ffprobe,
                        cwd_lure,
                        path_lure,
                        project,
                    )
                    self.assertEqual(_tree_snapshot(project), tree_before)
                    self.assertEqual(os.environ, checker_environment_before)
                    self.assertEqual(Path.cwd(), cwd_lure)
                finally:
                    os.chdir(cwd_before)

            self.assertEqual(os.environ, process_environment_before)
            self.assertEqual(Path.cwd(), cwd_before)
            self.assertEqual(_tree_snapshot(project), tree_before)
            self.assertEqual(dot_reports[0], dot_reports[1])
            self.assertEqual(unexpected_results, [])

    def test_public_functions_leave_files_environment_cwd_and_services_untouched(self):
        """RR1-CONFIG-SCOPE-001: inspect has no persistent or external side effects."""
        with _temporary_project() as project:
            sentinel = project / "database.sqlite3"
            sentinel.write_bytes(b"not-a-real-database-private-value")
            environment = {
                "VIDEO_DB_PATH": str(sentinel),
                "VIDEO_TEMP_ROOT": str(project / "video-work"),
                "VIDEO_UPLOAD_ROOT": str(project / "uploads"),
                "VIDEO_MEDIA_ROOT": str(project / "retained-media"),
                "INSPIRATION_TEMP_ROOT": str(project / "inspiration-recordings"),
                "COVER_CACHE_ROOT": str(project / "cover-cache"),
            }
            environment_before = environment.copy()
            process_environment_before = os.environ.copy()
            cwd_before = Path.cwd()
            tree_before = _tree_snapshot(project)
            server_loaded_before = "backend.app.server" in sys.modules

            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        socket,
                        "getaddrinfo",
                        side_effect=AssertionError("DNS is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        socket,
                        "create_connection",
                        side_effect=AssertionError("network is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        subprocess,
                        "Popen",
                        side_effect=AssertionError("process launch is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        builtins,
                        "open",
                        side_effect=AssertionError("file open is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        Path,
                        "open",
                        side_effect=AssertionError("Path.open is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        Path,
                        "read_bytes",
                        side_effect=AssertionError("Path.read_bytes is forbidden"),
                    )
                )
                stack.enter_context(
                    patch.object(
                        Path,
                        "read_text",
                        side_effect=AssertionError("Path.read_text is forbidden"),
                    )
                )
                resolve_runtime_config(environment, project)
                report = inspect_runtime_config(environment, project)

            self.assert_report_shape(report)
            self.assertEqual(environment, environment_before)
            self.assertEqual(os.environ, process_environment_before)
            self.assertEqual(Path.cwd(), cwd_before)
            self.assertEqual(_tree_snapshot(project), tree_before)
            self.assertEqual(
                "backend.app.server" in sys.modules,
                server_loaded_before,
            )

    def test_formal_startup_resolves_first_and_reuses_one_config_object(self):
        """RR1-CONFIG-STARTUP-001: valid startup shares config and upload root."""
        config = types.SimpleNamespace(
            video_upload_root=object(),
            video_max_upload_bytes=object(),
            inspiration_temp_root=object(),
            cover_cache_root=object(),
            cover_cache_fresh_seconds=object(),
            cover_cache_retention_seconds=object(),
            cover_cache_max_bytes=object(),
            cover_max_response_bytes=object(),
            cover_cache_failure_retry_seconds=object(),
        )
        events: list[tuple] = []
        pipeline = object()
        resolution_service = object()
        transcription_provider = object()
        audio_probe = object()
        application = object()

        def resolve(environ, project_root):
            events.append(("resolve", environ, project_root))
            return config

        def build_pipeline(candidate):
            events.append(("pipeline", candidate))
            return pipeline

        def build_resolution_service(candidate):
            events.append(("resolution", candidate))
            return resolution_service

        def build_transcription(candidate):
            events.append(("transcription", candidate))
            return transcription_provider

        def build_probe(candidate):
            events.append(("probe", candidate))
            return audio_probe

        def create_app(candidate, **kwargs):
            events.append(("create_app", candidate, kwargs))
            return application

        namespace = self._execute_server_with_fakes(
            resolve=resolve,
            build_pipeline=build_pipeline,
            build_resolution_service=build_resolution_service,
            build_transcription=build_transcription,
            build_probe=build_probe,
            create_app=create_app,
        )

        self.assertEqual(
            [event[0] for event in events],
            ["resolve", "pipeline", "resolution", "transcription", "probe", "create_app"],
        )
        self.assertIs(events[1][1], config)
        self.assertIs(events[2][1], pipeline)
        self.assertIs(events[3][1], config)
        self.assertIs(events[4][1], config)
        create_kwargs = events[5][2]
        self.assertIs(events[5][1], pipeline)
        self.assertIs(create_kwargs["resolution_service"], resolution_service)
        self.assertIs(
            create_kwargs["inspiration_transcription_provider"],
            transcription_provider,
        )
        self.assertIs(create_kwargs["inspiration_audio_probe"], audio_probe)
        self.assertIs(create_kwargs["upload_root"], config.video_upload_root)
        self.assertIs(
            create_kwargs["max_upload_bytes"],
            config.video_max_upload_bytes,
        )
        self.assertIs(
            create_kwargs["inspiration_temp_root"],
            config.inspiration_temp_root,
        )
        self.assertIs(create_kwargs["cover_cache_root"], config.cover_cache_root)
        self.assertIs(namespace["pipeline"], pipeline)
        self.assertIs(namespace["app"], application)

    def test_formal_startup_invalid_config_precedes_every_constructor(self):
        """RR1-CONFIG-STARTUP-001: invalid config fails before any side effect seam."""
        events: list[str] = []
        expected = RuntimeError("synthetic invalid configuration")

        def resolve(environ, project_root):
            events.append("resolve")
            raise expected

        def forbidden(*args, **kwargs):
            events.append("forbidden")
            raise AssertionError("startup constructor ran before validation")

        with self.assertRaises(RuntimeError) as raised:
            self._execute_server_with_fakes(
                resolve=resolve,
                build_pipeline=forbidden,
                build_resolution_service=forbidden,
                build_transcription=forbidden,
                build_probe=forbidden,
                create_app=forbidden,
            )
        self.assertIs(raised.exception, expected)
        self.assertEqual(events, ["resolve"])

    def _execute_server_with_fakes(
        self,
        *,
        resolve,
        build_pipeline,
        build_resolution_service,
        build_transcription,
        build_probe,
        create_app,
    ) -> dict:
        fake_api_package = types.ModuleType("backend.app.api")
        fake_api_package.__path__ = []
        fake_api_main = types.ModuleType("backend.app.api.main")
        fake_api_main.create_app = create_app
        fake_bootstrap = types.ModuleType("backend.app.bootstrap")
        fake_bootstrap.build_pipeline = build_pipeline
        fake_bootstrap.build_resolution_service = build_resolution_service
        fake_bootstrap.build_inspiration_transcription_provider = build_transcription
        fake_bootstrap.build_inspiration_audio_probe = build_probe
        fake_runtime_config = types.ModuleType("backend.app.runtime_config")
        fake_runtime_config.resolve_runtime_config = resolve

        replacements = {
            "backend.app.api": fake_api_package,
            "backend.app.api.main": fake_api_main,
            "backend.app.bootstrap": fake_bootstrap,
            "backend.app.runtime_config": fake_runtime_config,
        }
        with patch.dict(sys.modules, replacements):
            return runpy.run_module(
                "backend.app.server",
                run_name="backend.app._rr1_runtime_config_startup_probe",
            )

    def test_cli_warning_invalid_unknown_and_help_contracts(self):
        """RR1-CONFIG-REPORT-001: subprocess exit and output semantics are stable."""
        self.assertTrue(CONFIG_SCRIPT.is_file())
        with _temporary_project() as project:
            sentinel = project / "database.sqlite3"
            sentinel.write_bytes(b"not-a-real-database-private-value")
            environment = _safe_cli_environment(project)
            before = _tree_snapshot(project)

            warning = _run_cli(environment=environment)
            self.assertEqual(warning.returncode, 0, warning.stderr)
            self.assertEqual(warning.stderr, b"")
            warning_text = warning.stdout.decode("utf-8", errors="strict").strip()
            warning_report = json.loads(warning_text)
            self.assert_report_shape(warning_report)
            self.assertEqual(warning_report["status"], "ready_with_warnings")

            invalid_value = "https://user:PRIVATE-PASSWORD@example.invalid/api"
            invalid_environment = environment | {
                "VIDEO_ASR_ENDPOINT": invalid_value,
                "VIDEO_ASR_TOKEN": "PRIVATE-TOKEN",
            }
            invalid = _run_cli(environment=invalid_environment)
            self.assertEqual(invalid.returncode, 1, invalid.stderr)
            self.assertEqual(invalid.stderr, b"")
            invalid_text = invalid.stdout.decode("utf-8", errors="strict").strip()
            invalid_report = json.loads(invalid_text)
            self.assert_report_shape(invalid_report)
            self.assertEqual(invalid_report["status"], "invalid")

            private_argument = str(project / "PRIVATE-ARGUMENT.sqlite3")
            unknown = _run_cli(
                "--database",
                private_argument,
                environment=environment,
            )
            self.assertEqual(unknown.returncode, 2)
            self.assertEqual(unknown.stderr, b"")
            unknown_text = unknown.stdout.decode("utf-8", errors="strict").strip()
            unknown_report = json.loads(unknown_text)
            self.assert_report_shape(unknown_report)
            self.assertEqual(unknown_report["status"], "invalid")

            help_result = _run_cli("--help", environment=environment)
            self.assertEqual(help_result.returncode, 0)
            self.assertEqual(help_result.stderr, b"")
            help_text = help_result.stdout.decode("utf-8", errors="strict")
            self.assertIn("usage:", help_text.casefold())

            combined = "\n".join(
                (warning_text, invalid_text, unknown_text, help_text)
            )
            self.assert_report_redacted(
                combined,
                sentinel,
                project,
                invalid_value,
                "PRIVATE-TOKEN",
                private_argument,
            )
            self.assertNotRegex(combined, r"(?i)traceback")
            self.assertEqual(_tree_snapshot(project), before)

    def test_cli_cancellation_and_unexpected_error_are_fixed_and_redacted(self):
        """RR1-CONFIG-REPORT-001: no exception detail reaches either output stream."""
        cli = importlib.import_module("scripts.check_runtime_config")

        for exception, expected_exit in (
            (KeyboardInterrupt(), 130),
            (RuntimeError("PRIVATE-UNEXPECTED-DETAIL"), 1),
        ):
            with self.subTest(exception=type(exception).__name__):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(
                        cli,
                        "inspect_runtime_config",
                        side_effect=exception,
                    ),
                    patch.object(sys, "argv", [str(CONFIG_SCRIPT)]),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    exit_code = cli.main()

                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(stderr.getvalue(), "")
                report = json.loads(stdout.getvalue())
                self.assert_report_shape(report)
                self.assertEqual(report["status"], "invalid")
                self.assert_report_redacted(
                    stdout.getvalue(),
                    "PRIVATE-UNEXPECTED-DETAIL",
                    PROJECT_ROOT,
                )


if __name__ == "__main__":
    unittest.main()
