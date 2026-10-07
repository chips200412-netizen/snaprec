from __future__ import annotations

import json
import platform
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _failure_report(error_code: str) -> dict:
    return {
        "slice": "RR1-CONCURRENCY1",
        "status": "failed",
        "platform": platform.system(),
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "start_method": "spawn",
        },
        "counts": {},
        "outcomes": {},
        "checks": {},
        "error_code": error_code,
    }


def _write_report(report: dict) -> None:
    text = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
    else:
        buffer.write(text.encode("utf-8"))
        buffer.flush()


def run_sqlite_concurrency_rehearsal() -> dict:
    from backend.app.concurrency_readiness import (
        run_sqlite_concurrency_rehearsal as run_rehearsal,
    )

    return run_rehearsal()


def main() -> int:
    if sys.argv[1:] == ["--help"]:
        sys.stdout.write(
            "usage: rehearse_sqlite_concurrency.py [--help]\n"
            "Runs the path-free RR1-CONCURRENCY1 synthetic Windows rehearsal.\n"
        )
        return 0
    if sys.argv[1:]:
        _write_report(
            _failure_report("RR1_CONCURRENCY_ARGUMENTS_NOT_SUPPORTED")
        )
        return 2

    try:
        report = run_sqlite_concurrency_rehearsal()
        exit_code = 0 if report["status"] == "passed" else 1
        _write_report(report)
    except KeyboardInterrupt:
        _write_report(_failure_report("RR1_CONCURRENCY_CANCELLED"))
        return 130
    except Exception:
        _write_report(_failure_report("RR1_CONCURRENCY_REHEARSAL_FAILED"))
        return 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
