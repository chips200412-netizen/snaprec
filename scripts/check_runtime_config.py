from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.runtime_config import (  # noqa: E402
    fixed_failure_report,
    inspect_runtime_config,
)


def _write_report(report: dict) -> None:
    sys.stdout.write(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


def main() -> int:
    if sys.argv[1:] == ["--help"]:
        sys.stdout.write(
            "usage: check_runtime_config.py [--help]\n"
            "Runs the path-free RR1-CONFIG1 offline configuration check.\n"
        )
        return 0
    if sys.argv[1:]:
        _write_report(
            fixed_failure_report(
                "ERROR_ARGUMENTS_NOT_SUPPORTED",
                environ=os.environ,
            )
        )
        return 2
    try:
        report = inspect_runtime_config(os.environ, PROJECT_ROOT)
    except KeyboardInterrupt:
        _write_report(
            fixed_failure_report("ERROR_CANCELLED", environ=os.environ)
        )
        return 130
    except Exception:
        _write_report(
            fixed_failure_report("ERROR_INTERNAL", environ=os.environ)
        )
        return 1
    _write_report(report)
    return 1 if report["status"] == "invalid" else 0


if __name__ == "__main__":
    raise SystemExit(main())
