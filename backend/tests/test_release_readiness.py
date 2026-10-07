from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.app.repositories.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REHEARSAL_SCRIPT = PROJECT_ROOT / "scripts" / "rehearse_sqlite_release.py"
TEST_TEMP_ROOT = PROJECT_ROOT / "var" / "wanan" / "temp"
REPORT_KEYS = {
    "slice",
    "status",
    "platform",
    "runtime",
    "versions",
    "counts",
    "checks",
    "hashes",
    "error_code",
}
REQUIRED_CHECKS = {
    "workspace_cleaned",
    "source_unchanged",
    "online_backup_used",
    "backup_equivalent",
    "source_v14_excludes_v16_collection_import",
    "backup_v14_excludes_v16_collection_import",
    "source_v14_excludes_v18_user_cover",
    "backup_v14_excludes_v18_user_cover",
    "rollback_preserved_v14",
    "rollback_v14_excludes_v16_collection_import",
    "retry_migrated_v15",
    "retry_has_v16_collection_import_schema",
    "retry_has_v17_source_topic_selection",
    "retry_has_v18_user_author",
    "retry_has_v18_user_cover_tables",
    "retry_v18_old_user_author_null",
    "retry_reached_current_schema",
    "restore_equivalent_v14",
    "restored_v14_excludes_v16_collection_import",
    "restored_v14_excludes_v18_user_cover",
    "canary_item_restored",
    "canary_idempotency_replayed",
    "canary_search_hit",
}
COLLECTION_IMPORT_V16_TABLES = {
    "collection_import_batches",
    "collection_import_batch_items",
    "collection_import_batch_idempotency",
    "collection_import_save_attempts",
}
COLLECTION_IMPORT_V16_DROP_ORDER = (
    "collection_import_save_attempts",
    "collection_import_batch_idempotency",
    "collection_import_batch_items",
    "collection_import_batches",
)
USER_COVER_V18_TABLES = {"user_cover_assets", "collection_user_covers"}


def _run_rehearsal() -> dict:
    from backend.app.release_readiness import run_sqlite_release_rehearsal

    return run_sqlite_release_rehearsal()


def _canonical_dump_hash(database_path: Path) -> str:
    with closing(sqlite3.connect(database_path)) as db:
        dump = "\n".join(line.strip() for line in db.iterdump())
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _collection_import_tables(db: sqlite3.Connection) -> set[str]:
    placeholders = ", ".join("?" for _ in COLLECTION_IMPORT_V16_TABLES)
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master "
            f"WHERE type='table' AND name IN ({placeholders})",
            tuple(sorted(COLLECTION_IMPORT_V16_TABLES)),
        ).fetchall()
    }


@contextmanager
def _temporary_directory():
    TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_root = TEST_TEMP_ROOT.resolve()
    directory = (test_root / f"rr1-contract-tests-{uuid.uuid4().hex}").resolve()
    if test_root not in directory.parents:
        raise RuntimeError("unsafe RR1 test temporary directory")
    directory.mkdir()
    try:
        yield str(directory)
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def _run_cli(*arguments: str, environment: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-B", str(REHEARSAL_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        timeout=60,
        check=False,
    )


class SQLiteReleaseRehearsalTests(unittest.TestCase):
    maxDiff = None

    def assert_redacted(self, text: str, *private_paths: Path) -> None:
        folded = text.casefold()
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(text, r"(?i)traceback|cookie|authorization|bearer|token")
        self.assertNotRegex(text, r"(?i)[a-z]:[\\/]")
        self.assertNotIn(".sqlite", folded)
        self.assertNotIn("rr1-db1-", folded)
        for private_path in private_paths:
            for spelling in {
                str(private_path),
                str(private_path).replace("\\", "/"),
            }:
                self.assertNotIn(spelling.casefold(), folded)

    def assert_success_report(self, report: dict) -> None:
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["slice"], "RR1-DB1")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["platform"], "Windows")
        self.assertIsNone(report["error_code"])
        self.assertEqual(
            report["runtime"],
            {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
            },
        )

        versions = report["versions"]
        self.assertEqual(
            {
                "source": versions["source"],
                "backup": versions["backup"],
                "rollback": versions["rollback"],
                "migrated": versions["migrated"],
                "restored": versions["restored"],
            },
            {
                "source": 14,
                "backup": 14,
                "rollback": 14,
                "migrated": 18,
                "restored": 14,
            },
        )

        checks = report["checks"]
        self.assertTrue(REQUIRED_CHECKS.issubset(checks))
        for name in REQUIRED_CHECKS:
            self.assertIs(checks[name], True, name)

        for name, count in report["counts"].items():
            self.assertIs(type(count), int, name)
            self.assertGreaterEqual(count, 0, name)
        for name in (
            "source_items",
            "backup_items",
            "migrated_items",
            "restored_items",
            "idempotency_records",
            "search_documents",
        ):
            self.assertIn(name, report["counts"])
        self.assertEqual(report["counts"]["source_items"], 1)
        self.assertEqual(report["counts"]["backup_items"], 1)
        self.assertEqual(report["counts"]["migrated_items"], 1)
        self.assertEqual(report["counts"]["restored_items"], 1)
        self.assertEqual(report["counts"]["idempotency_records"], 1)
        self.assertEqual(report["counts"]["search_documents"], 1)

        hashes = report["hashes"]
        for name in (
            "source_dump",
            "backup_dump",
            "rollback_dump",
            "migrated_dump",
            "restore_dump",
        ):
            self.assertRegex(hashes[name], r"^[0-9a-f]{64}$", name)
        self.assertEqual(hashes["backup_dump"], hashes["source_dump"])
        self.assertEqual(hashes["rollback_dump"], hashes["source_dump"])
        self.assertEqual(hashes["restore_dump"], hashes["source_dump"])
        self.assertNotEqual(hashes["migrated_dump"], hashes["source_dump"])

        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assert_redacted(encoded, Path.home(), PROJECT_ROOT)
        for deferred_claim in (
            "linux",
            "multi_instance",
            "service_restart",
            "staging",
            "deployment",
            "production",
        ):
            self.assertNotIn(deferred_claim, encoded.casefold())

    def test_isolated_rehearsal_preserves_workspace_and_configured_database(self):
        """RR1-DB-SCOPE-001: only the rehearsal-owned temporary root may change."""
        with _temporary_directory() as temporary_root:
            workspace = Path(temporary_root) / "workspace"
            workspace.mkdir()
            sentinel = workspace / "DO_NOT_TOUCH-user-database.sqlite3"
            with closing(sqlite3.connect(sentinel)) as db:
                db.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
                db.execute("INSERT INTO sentinel(value) VALUES ('private-value')")
                db.commit()
            before_bytes = sentinel.read_bytes()
            before_entries = sorted(path.name for path in workspace.iterdir())
            original_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {"VIDEO_DB_PATH": str(sentinel)},
                    clear=False,
                ):
                    report = _run_rehearsal()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(sentinel.read_bytes(), before_bytes)
            self.assertEqual(
                sorted(path.name for path in workspace.iterdir()),
                before_entries,
            )
            self.assert_success_report(report)

    def test_report_proves_backup_migration_rollback_and_restore_invariants(self):
        """RR1-DB-BACKUP/MIGRATION/ROLLBACK/RESTORE-001 through the public seam."""
        report = _run_rehearsal()
        self.assert_success_report(report)

    def test_repository_fault_hook_runs_after_v15_ddl_and_rolls_back(self):
        """RR1-DB-ROLLBACK-001: the agreed process-local fault seam is transactional."""
        with _temporary_directory() as temporary_root:
            database_path = Path(temporary_root) / "synthetic-v14.sqlite3"
            SQLiteRepository(database_path)
            with closing(sqlite3.connect(database_path)) as db:
                self.assertEqual(
                    _collection_import_tables(db), COLLECTION_IMPORT_V16_TABLES
                )
                for table in COLLECTION_IMPORT_V16_DROP_ORDER:
                    db.execute(f'DROP TABLE "{table}"')
                db.execute("DROP TRIGGER IF EXISTS collection_search_dirty_library_update")
                db.execute("DROP TABLE collection_user_covers")
                db.execute("DROP TABLE user_cover_assets")
                db.execute("ALTER TABLE library_items DROP COLUMN user_author")
                db.execute("ALTER TABLE library_items DROP COLUMN revision")
                db.execute("PRAGMA user_version=14")
                db.commit()
                self.assertEqual(_collection_import_tables(db), set())
            before_hash = _canonical_dump_hash(database_path)
            observed_stages: list[str] = []

            def fail_after_v15_ddl(stage: str, db: sqlite3.Connection) -> None:
                observed_stages.append(stage)
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(library_items)")
                }
                self.assertIn("revision", columns)
                self.assertEqual(_collection_import_tables(db), set())
                raise RuntimeError("RR1_DB1_INJECTED_V15_FAILURE")

            with self.assertRaisesRegex(
                RuntimeError, "RR1_DB1_INJECTED_V15_FAILURE"
            ):
                SQLiteRepository(
                    database_path,
                    migration_fault=fail_after_v15_ddl,
                )

            self.assertEqual(observed_stages, ["after_v15_ddl"])
            with closing(sqlite3.connect(database_path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 14)
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(library_items)")
                }
                self.assertNotIn("revision", columns)
                self.assertEqual(_collection_import_tables(db), set())
            self.assertEqual(_canonical_dump_hash(database_path), before_hash)

            recovered = SQLiteRepository(database_path)
            with closing(sqlite3.connect(database_path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 18)
                self.assertEqual(
                    _collection_import_tables(db), COLLECTION_IMPORT_V16_TABLES
                )
                columns = {row[1] for row in db.execute("PRAGMA table_info(library_items)")}
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("user_author", columns)
                self.assertTrue(USER_COVER_V18_TABLES.issubset(tables))
            self.assertIsNotNone(recovered)

    def test_cli_outputs_one_redacted_success_report_for_windows_runtime(self):
        """RR1-DB-REPORT-001 and RR1-WINDOWS-001."""
        self.assertTrue(REHEARSAL_SCRIPT.is_file())
        completed = _run_cli()
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, diagnostic)
        stderr = completed.stderr.decode("utf-8", errors="strict")
        stdout = completed.stdout.decode("utf-8", errors="strict").strip()
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.startswith("{") and stdout.endswith("}"), stdout)
        report = json.loads(stdout)
        self.assert_success_report(report)

    def test_cli_rejects_path_arguments_without_touching_or_disclosing_them(self):
        """RR1-DB-SCOPE-001 and failure-side RR1-DB-REPORT-001."""
        self.assertTrue(REHEARSAL_SCRIPT.is_file())
        with _temporary_directory() as temporary_root:
            sentinel = Path(temporary_root) / "PRIVATE-user-database.sqlite3"
            sentinel.write_bytes(b"unchanged-private-sentinel")
            before = sentinel.read_bytes()
            environment = os.environ.copy()
            environment["VIDEO_DB_PATH"] = str(sentinel)
            completed = _run_cli(
                "--source-db",
                str(sentinel),
                environment=environment,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(sentinel.read_bytes(), before)
            combined = (completed.stdout + completed.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertNotIn("\ufffd", combined)
            self.assertNotIn(str(sentinel), combined)
            self.assertNotIn(str(sentinel).replace("\\", "/"), combined)
            self.assertNotRegex(combined, r"(?i)traceback|private-value")
            stdout = completed.stdout.decode("utf-8", errors="strict").strip()
            if stdout.startswith("{"):
                failure = json.loads(stdout)
                self.assertEqual(set(failure), REPORT_KEYS)
                self.assertEqual(failure["slice"], "RR1-DB1")
                self.assertEqual(failure["status"], "failed")
                self.assertIsInstance(failure["error_code"], str)
                self.assertTrue(failure["error_code"])

    def test_cli_help_is_path_free_and_successful(self):
        """RR1-DB-SCOPE-001: help is the only accepted non-rehearsal argument."""
        completed = _run_cli("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        output = completed.stdout.decode("utf-8", errors="strict")
        self.assertIn("usage:", output)
        self.assertIn("path-free", output)
        self.assert_redacted(output, PROJECT_ROOT, Path.home())

    def test_cli_cancellation_cleans_owned_root_and_emits_redacted_json(self):
        """RR1-DB-REPORT-001: cancellation is stable, redacted, and self-cleaning."""
        cli = importlib.import_module("scripts.rehearse_sqlite_release")
        readiness = importlib.import_module("backend.app.release_readiness")
        owned_root: list[Path] = []

        def interrupt_after_root_created(database_path: Path) -> str:
            owned_root.append(database_path.parent)
            raise KeyboardInterrupt

        output = io.StringIO()
        with (
            patch.object(
                readiness,
                "_create_v14_synthetic_database",
                side_effect=interrupt_after_root_created,
            ),
            patch.object(sys, "argv", [str(REHEARSAL_SCRIPT)]),
            redirect_stdout(output),
        ):
            exit_code = cli.main()

        self.assertEqual(exit_code, 130)
        self.assertEqual(len(owned_root), 1)
        self.assertFalse(owned_root[0].exists())
        report = json.loads(output.getvalue())
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "RR1_DB1_CANCELLED")
        self.assert_redacted(output.getvalue(), owned_root[0], PROJECT_ROOT, Path.home())


if __name__ == "__main__":
    unittest.main()
