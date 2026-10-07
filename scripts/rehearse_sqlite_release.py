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
        "slice": "RR1-DB1",
        "status": "failed",
        "platform": platform.system(),
        "runtime": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "versions": {},
        "counts": {},
        "checks": {},
        "hashes": {},
        "error_code": error_code,
    }


def _write_report(report: dict) -> None:
    sys.stdout.write(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def main() -> int:
    if sys.argv[1:] == ["--help"]:
        sys.stdout.write(
            "usage: rehearse_sqlite_release.py [--help]\n"
            "Runs the path-free RR1-DB1 synthetic Windows rehearsal.\n"
        )
        return 0
    if sys.argv[1:]:
        _write_report(_failure_report("RR1_DB1_ARGUMENTS_NOT_SUPPORTED"))
        return 2
    try:
        from backend.app.release_readiness import run_sqlite_release_rehearsal

        report = run_sqlite_release_rehearsal()
    except KeyboardInterrupt:
        _write_report(_failure_report("RR1_DB1_CANCELLED"))
        return 130
    except Exception:
        _write_report(_failure_report("RR1_DB1_REHEARSAL_FAILED"))
        return 1
    _write_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
