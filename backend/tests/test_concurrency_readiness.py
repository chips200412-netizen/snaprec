from __future__ import annotations

import importlib
import inspect
import io
import json
import multiprocessing
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REHEARSAL_SCRIPT = PROJECT_ROOT / "scripts" / "rehearse_sqlite_concurrency.py"
TEST_TEMP_ROOT = PROJECT_ROOT / "var" / "wanan" / "temp"

REPORT_KEYS = {
    "slice",
    "status",
    "platform",
    "runtime",
    "counts",
    "outcomes",
    "checks",
    "error_code",
}
RUNTIME_KEYS = {"python", "sqlite", "start_method"}
COUNT_KEYS = {
    "workers_started",
    "workers_completed",
    "collections",
    "idempotency_records",
    "search_documents",
    "dirty_documents",
}
OUTCOME_KEYS = {
    "distinct_writes",
    "same_key_replay",
    "key_reused",
    "cas",
    "busy",
    "crash_recovery",
}
CHECK_KEYS = {
    "independent_processes",
    "bounded_processes",
    "integrity",
    "foreign_keys",
    "winner_graph_atomic",
    "idempotency",
    "search",
    "crash_rollback",
    "workspace_cleanup",
}


def _load_public_function():
    module = importlib.import_module("backend.app.concurrency_readiness")
    return module.run_sqlite_concurrency_rehearsal


def _run_rehearsal() -> dict:
    return _load_public_function()()


@contextmanager
def _controlled_temporary_directory():
    TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    parent = TEST_TEMP_ROOT.resolve()
    root = (parent / f"rr1-concurrency-contract-{uuid.uuid4().hex}").resolve()
    if parent not in root.parents:
        raise RuntimeError("unsafe RR1 concurrency test root")
    root.mkdir()
    try:
        yield root
    finally:
        if root.exists():
            shutil.rmtree(root)


def _run_cli(*arguments: str, environment: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-B", str(REHEARSAL_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        timeout=180,
        check=False,
    )


class SQLiteConcurrencyRehearsalTests(unittest.TestCase):
    maxDiff = None

    def assert_redacted(self, text: str, *private_values: object) -> None:
        folded = text.casefold()
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(
            text,
            r"(?i)traceback|cookie|authorization|bearer|token|request[_ -]?hash",
        )
        self.assertNotRegex(text, r"(?i)[a-z]:[\\/]")
        self.assertNotRegex(
            text,
            r"(?i)\b(select|insert|update|delete|pragma|begin immediate)\b",
        )
        self.assertNotIn(".sqlite", folded)
        self.assertNotIn("example.invalid", folded)
        for private_value in private_values:
            value = str(private_value)
            for spelling in {value, value.replace("\\", "/")}:
                self.assertNotIn(spelling.casefold(), folded)

    def assert_report_has_only_aggregate_values(self, report: dict) -> None:
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assert_redacted(encoded, PROJECT_ROOT, Path.home())
        forbidden_key_fragments = {
            "path",
            "root",
            "file",
            "pid",
            "url",
            "title",
            "tag",
            "inspiration",
            "hash",
            "sql",
            "elapsed",
            "duration",
            "timing",
        }

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    folded_key = str(key).casefold()
                    self.assertFalse(
                        any(
                            fragment in folded_key
                            for fragment in forbidden_key_fragments
                            if fragment != "sql"
                        )
                        or ("sql" in folded_key and folded_key != "sqlite"),
                        key,
                    )
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(report)

    def assert_success_report(self, report: dict) -> None:
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["slice"], "RR1-CONCURRENCY1")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["platform"], "Windows")
        self.assertIsNone(report["error_code"])
        self.assertEqual(set(report["runtime"]), RUNTIME_KEYS)
        self.assertEqual(report["runtime"]["python"], platform.python_version())
        self.assertEqual(report["runtime"]["sqlite"], sqlite3.sqlite_version)
        self.assertEqual(report["runtime"]["start_method"], "spawn")

        counts = report["counts"]
        self.assertEqual(set(counts), COUNT_KEYS)
        for name, count in counts.items():
            self.assertIs(type(count), int, name)
            self.assertGreaterEqual(count, 0, name)
        self.assertGreaterEqual(counts["workers_started"], 1)
        self.assertLessEqual(counts["workers_completed"], counts["workers_started"])
        self.assertGreaterEqual(counts["collections"], 1)
        self.assertGreaterEqual(counts["idempotency_records"], 1)
        self.assertEqual(counts["search_documents"], counts["collections"])
        self.assertEqual(counts["dirty_documents"], 0)
        self.assertEqual(
            counts,
            {
                "workers_started": 18,
                "workers_completed": 18,
                "collections": 10,
                "idempotency_records": 10,
                "search_documents": 10,
                "dirty_documents": 0,
            },
        )

        outcomes = report["outcomes"]
        self.assertEqual(set(outcomes), OUTCOME_KEYS)
        for stage, aggregates in outcomes.items():
            if type(aggregates) is int:
                self.assertGreater(aggregates, 0, stage)
                continue
            self.assertIsInstance(aggregates, dict, stage)
            self.assertTrue(aggregates, stage)
            for name, count in aggregates.items():
                self.assertIsInstance(name, str, stage)
                self.assertIs(type(count), int, f"{stage}.{name}")
                self.assertGreaterEqual(count, 0, f"{stage}.{name}")
            self.assertTrue(any(count > 0 for count in aggregates.values()), stage)
        self.assertEqual(
            outcomes,
            {
                "distinct_writes": {"committed": 4},
                "same_key_replay": {"responses": 3, "records": 1},
                "key_reused": {"committed": 1, "rejected": 1},
                "cas": {"committed": 1, "conflicts": 1},
                "busy": {
                    "prewrite_rejected": 1,
                    "waited_committed": 1,
                    "retried_committed": 1,
                    "same_identity_retried": 1,
                },
                "crash_recovery": {
                    "rolled_back": 1,
                    "retried_committed": 1,
                },
            },
        )

        checks = report["checks"]
        self.assertEqual(set(checks), CHECK_KEYS)
        for name, passed in checks.items():
            self.assertIs(passed, True, name)
        self.assert_report_has_only_aggregate_values(report)

    def assert_failure_report(self, report: dict, error_code: str) -> None:
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["slice"], "RR1-CONCURRENCY1")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["platform"], "Windows")
        self.assertEqual(report["error_code"], error_code)
        self.assertIsInstance(report["runtime"], dict)
        self.assertTrue(set(report["runtime"]).issubset(RUNTIME_KEYS))
        self.assertIsInstance(report["counts"], dict)
        self.assertIsInstance(report["outcomes"], dict)
        self.assertIsInstance(report["checks"], dict)
        self.assert_report_has_only_aggregate_values(report)

    def test_public_rehearsal_is_isolated_bounded_and_covers_fixed_matrix(self):
        """Cover the complete fixed matrix through the one public function.

        RR1-CONCURRENCY-SCOPE-001; RR1-CONCURRENCY-SPAWN-001;
        RR1-CONCURRENCY-WRITES-001; RR1-CONCURRENCY-IDEMPOTENCY-001;
        RR1-CONCURRENCY-CAS-001; RR1-CONCURRENCY-BUSY-001;
        RR1-CONCURRENCY-FTS-001; RR1-CONCURRENCY-RECOVERY-001.
        """
        function = _load_public_function()
        self.assertEqual(list(inspect.signature(function).parameters), [])

        with _controlled_temporary_directory() as controlled_root:
            workspace = controlled_root / "workspace"
            workspace.mkdir()
            sentinel = workspace / "PRIVATE-existing-user-database.sqlite3"
            with closing(sqlite3.connect(sentinel)) as database:
                database.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
                database.execute("INSERT INTO sentinel(value) VALUES ('PRIVATE-user-content')")
                database.commit()
            sentinel_before = sentinel.read_bytes()
            workspace_before = sorted(path.name for path in workspace.iterdir())
            temp_before = sorted(path.name for path in TEST_TEMP_ROOT.iterdir())
            active_before = set(multiprocessing.active_children())
            original_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                with patch.dict(
                    os.environ,
                    {"VIDEO_DB_PATH": str(sentinel)},
                    clear=False,
                ):
                    report = function()
            finally:
                os.chdir(original_cwd)

            active_after = set(multiprocessing.active_children())
            self.assertEqual(active_after - active_before, set())
            self.assertEqual(sentinel.read_bytes(), sentinel_before)
            self.assertEqual(
                sorted(path.name for path in workspace.iterdir()), workspace_before
            )
            self.assertEqual(
                sorted(path.name for path in TEST_TEMP_ROOT.iterdir()), temp_before
            )
            self.assert_success_report(report)
            encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
            self.assert_redacted(
                encoded,
                controlled_root,
                sentinel,
                "PRIVATE-user-content",
            )

    def test_repository_busy_budget_is_explicit_and_does_not_enable_wal(self):
        """RR1-CONCURRENCY-BUSY-001: preserve the bounded rollback-journal policy."""
        from backend.app.repositories.sqlite import SQLiteRepository

        with _controlled_temporary_directory() as controlled_root:
            repository = SQLiteRepository(controlled_root / "synthetic.db")
            with repository._connect() as database:
                busy_timeout = int(
                    database.execute("PRAGMA busy_timeout").fetchone()[0]
                )
                journal_mode = str(
                    database.execute("PRAGMA journal_mode").fetchone()[0]
                ).casefold()

            self.assertEqual(busy_timeout, 5_000)
            self.assertNotEqual(journal_mode, "wal")

    def test_normal_wait_observation_is_bound_to_actual_create_connection(self):
        """RR1-CONCURRENCY-BUSY-001: the formal write itself must wait."""
        readiness = importlib.import_module("backend.app.concurrency_readiness")
        with _controlled_temporary_directory() as controlled_root:
            database_path = controlled_root / "synthetic.db"
            with closing(sqlite3.connect(database_path, timeout=5.0)) as holder:
                holder.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
                holder.commit()
                holder.execute("BEGIN IMMEDIATE")

                begin_attempted = threading.Event()
                begin_acquired = threading.Event()
                result_queue: Queue = Queue()
                observed_repositories: list[object] = []

                def create_on_observed_repository(
                    service, request, idempotency_key
                ):
                    del request, idempotency_key
                    observed_repositories.append(service.repository)
                    with service.repository._connect() as database:
                        database.execute("BEGIN IMMEDIATE")
                    return SimpleNamespace(id="synthetic-item")

                with (
                    patch.object(
                        readiness.CollectionService,
                        "create",
                        autospec=True,
                        side_effect=create_on_observed_repository,
                    ),
                    patch.object(
                        readiness,
                        "_record_matches_item",
                        return_value=True,
                    ),
                ):
                    worker = threading.Thread(
                        target=readiness._waiting_create_worker,
                        args=(
                            str(database_path),
                            500,
                            "rr1-busy-wait",
                            begin_attempted,
                            begin_acquired,
                            result_queue,
                        ),
                    )
                    worker.start()
                    try:
                        self.assertTrue(begin_attempted.wait(timeout=5))
                        time.sleep(0.25)
                        self.assertFalse(begin_acquired.is_set())
                    finally:
                        holder.commit()
                    self.assertTrue(begin_acquired.wait(timeout=5))
                    worker.join(timeout=5)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(observed_repositories), 1)
                self.assertIsInstance(
                    observed_repositories[0],
                    readiness._ObservedWaitSQLiteRepository,
                )
                result = result_queue.get_nowait()
                self.assertEqual(result["category"], "success")
                self.assertTrue(result["record_match"])
                self.assertTrue(result["wait_observed"])

    def test_process_registration_and_cleanup_fail_closed(self):
        """RR1-CONCURRENCY-SPAWN-001: cancellation cannot escape the registry."""
        readiness = importlib.import_module("backend.app.concurrency_readiness")

        class StartInterruptedProcess:
            pid = None

            def start(self):
                self.pid = 4242
                raise KeyboardInterrupt("synthetic start interruption")

        interrupted = StartInterruptedProcess()

        class FakeContext:
            @staticmethod
            def Process(*, target, args):
                del target, args
                return interrupted

        registry: list[object] = []
        with self.assertRaises(KeyboardInterrupt):
            readiness._start_process(FakeContext(), registry, object(), ())
        self.assertEqual(registry, [interrupted])

        class FakeProcess:
            def __init__(self, *, terminate_fails: bool, remains_alive: bool):
                self.pid = 4343
                self.alive = True
                self.terminate_fails = terminate_fails
                self.remains_alive = remains_alive
                self.join_calls = 0
                self.terminate_calls = 0
                self.kill_calls = 0

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                del timeout
                self.join_calls += 1

            def terminate(self):
                self.terminate_calls += 1
                if self.terminate_fails:
                    raise OSError("synthetic terminate failure")
                if not self.remains_alive:
                    self.alive = False

            def kill(self):
                self.kill_calls += 1
                if not self.remains_alive:
                    self.alive = False

            def close(self):
                pass

        first = FakeProcess(terminate_fails=True, remains_alive=False)
        stubborn = FakeProcess(terminate_fails=False, remains_alive=True)
        with (
            patch.object(readiness, "_TERMINATE_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(readiness.ConcurrencyRehearsalError),
        ):
            readiness._stop_owned_processes([first, stubborn])
        self.assertGreaterEqual(first.kill_calls, 1)
        self.assertGreaterEqual(stubborn.kill_calls, 1)

    def test_inflight_cancellation_reaps_child_and_owned_root(self):
        """RR1-CONCURRENCY-REPORT-001: cancel a live holder, then prove cleanup."""
        readiness = importlib.import_module("backend.app.concurrency_readiness")
        temp_before = sorted(path.name for path in TEST_TEMP_ROOT.iterdir())
        active_before = set(multiprocessing.active_children())
        queues: list[object] = []
        cleanup_events: list[str] = []
        original_stop = readiness._stop_owned_processes
        original_close_queues = readiness._close_owned_queues

        def observed_stop(registry):
            cleanup_events.append("stop")
            return original_stop(registry)

        def observed_close_queues(owned_queues):
            cleanup_events.append("queues")
            return original_close_queues(owned_queues)

        def interrupt_with_live_holder(context, registry, target, argument_rows):
            del target
            result_queue = context.Queue()
            queues.append(result_queue)
            acquired = context.Event()
            release = context.Event()
            database_path = argument_rows[0][0]
            readiness._start_process(
                context,
                registry,
                readiness._lock_holder_worker,
                (database_path, acquired, release, result_queue),
            )
            self.assertTrue(acquired.wait(timeout=15))
            raise KeyboardInterrupt("synthetic in-flight cancellation")

        try:
            with (
                patch.object(
                    readiness,
                    "_run_barrier_group",
                    side_effect=interrupt_with_live_holder,
                ),
                patch.object(readiness, "_TERMINATE_TIMEOUT_SECONDS", 0.5),
                patch.object(
                    readiness,
                    "_stop_owned_processes",
                    side_effect=observed_stop,
                ),
                patch.object(
                    readiness,
                    "_close_owned_queues",
                    side_effect=observed_close_queues,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                readiness.run_sqlite_concurrency_rehearsal()
        finally:
            for result_queue in queues:
                readiness._close_queue(result_queue)

        self.assertEqual(
            set(multiprocessing.active_children()) - active_before,
            set(),
        )
        self.assertEqual(
            sorted(path.name for path in TEST_TEMP_ROOT.iterdir()),
            temp_before,
        )
        self.assertGreaterEqual(len(cleanup_events), 2)
        self.assertEqual(cleanup_events[:2], ["stop", "queues"])

    def test_cli_success_is_one_canonical_redacted_report(self):
        """RR1-CONCURRENCY-REPORT-001: exit 0 and canonical redacted JSON."""
        self.assertTrue(REHEARSAL_SCRIPT.is_file())
        completed = _run_cli()
        diagnostic = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(completed.returncode, 0, diagnostic)
        self.assertEqual(completed.stderr, b"")
        stdout = completed.stdout.decode("utf-8", errors="strict")
        self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
        report = json.loads(stdout)
        self.assert_success_report(report)
        expected = json.dumps(
            report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(stdout, expected + "\n")

    def test_cli_rejects_all_path_and_load_arguments_without_touching_sentinel(self):
        """RR1-CONCURRENCY-SCOPE-001 / RR1-CONCURRENCY-REPORT-001: exit 2."""
        self.assertTrue(REHEARSAL_SCRIPT.is_file())
        with _controlled_temporary_directory() as controlled_root:
            sentinel = controlled_root / "PRIVATE-existing-user-database.sqlite3"
            sentinel.write_bytes(b"PRIVATE-unchanged-sentinel")
            before = sentinel.read_bytes()
            environment = os.environ.copy()
            environment["VIDEO_DB_PATH"] = str(sentinel)
            arguments = (
                ("--database", str(sentinel)),
                ("--workers", "4"),
                ("--timeout", "1"),
            )
            for rejected in arguments:
                with self.subTest(arguments=rejected[0]):
                    completed = _run_cli(*rejected, environment=environment)
                    self.assertEqual(completed.returncode, 2)
                    self.assertEqual(completed.stderr, b"")
                    self.assertEqual(sentinel.read_bytes(), before)
                    stdout = completed.stdout.decode("utf-8", errors="strict")
                    self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
                    report = json.loads(stdout)
                    self.assert_failure_report(
                        report, "RR1_CONCURRENCY_ARGUMENTS_NOT_SUPPORTED"
                    )
                    self.assert_redacted(stdout, sentinel, controlled_root)

    def test_cli_help_is_the_only_non_rehearsal_success(self):
        """RR1-CONCURRENCY-REPORT-001: fixed path-free help exits 0."""
        self.assertTrue(REHEARSAL_SCRIPT.is_file())
        completed = _run_cli("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        output = completed.stdout.decode("utf-8", errors="strict")
        self.assertIn("usage:", output)
        self.assertIn("path-free", output.casefold())
        self.assert_redacted(output, PROJECT_ROOT, Path.home())

    def test_cli_runtime_failure_and_cancellation_use_stable_redacted_exits(self):
        """RR1-CONCURRENCY-REPORT-001: runtime failure exits 1 and cancellation 130."""
        cli = importlib.import_module("scripts.rehearse_sqlite_concurrency")
        readiness = importlib.import_module("backend.app.concurrency_readiness")
        patch_target = (
            cli
            if hasattr(cli, "run_sqlite_concurrency_rehearsal")
            else readiness
        )
        cases = (
            (
                RuntimeError("PRIVATE runtime exception must not escape"),
                1,
                "RR1_CONCURRENCY_REHEARSAL_FAILED",
            ),
            (
                KeyboardInterrupt("PRIVATE cancellation must not escape"),
                130,
                "RR1_CONCURRENCY_CANCELLED",
            ),
        )
        for side_effect, expected_exit, expected_code in cases:
            with self.subTest(exit_code=expected_exit):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(
                        patch_target,
                        "run_sqlite_concurrency_rehearsal",
                        side_effect=side_effect,
                    ),
                    patch.object(sys, "argv", [str(REHEARSAL_SCRIPT)]),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    exit_code = cli.main()
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(len(stdout.getvalue().strip().splitlines()), 1)
                report = json.loads(stdout.getvalue())
                self.assert_failure_report(report, expected_code)
                self.assert_redacted(
                    stdout.getvalue(),
                    "PRIVATE runtime exception must not escape",
                    "PRIVATE cancellation must not escape",
                )


if __name__ == "__main__":
    unittest.main()
