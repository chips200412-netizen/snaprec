"""Verify the frontend's frozen Python 3.12 / Unicode 15 tables.

This is intentionally a read-only maintenance check. Run it with the project's
Python 3.12 interpreter whenever the generated TypeScript tables change.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASEFOLD_PATH = ROOT / "web" / "src" / "unicode-casefold.ts"
NORMALIZE_PATH = ROOT / "web" / "src" / "unicode-normalize.ts"
MAX_CODE_POINT = 0x10FFFF


def _require_runtime() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(
            "verify_unicode15_frontend.py requires Python 3.12; "
            f"got {sys.version.split()[0]}"
        )
    if unicodedata.unidata_version != "15.0.0":
        raise SystemExit(
            "verify_unicode15_frontend.py requires Unicode 15.0.0; "
            f"got {unicodedata.unidata_version}"
        )


def _constant(source: str, name: str) -> str:
    match = re.search(
        rf"export const {re.escape(name)} = (?P<value>\"[^\"]+\"|\d+);",
        source,
    )
    if not match:
        raise AssertionError(f"missing TypeScript constant: {name}")
    return match.group("value").strip('"')


def _template(source: str, name: str) -> str:
    match = re.search(
        rf"const {re.escape(name)} = `\s*(?P<data>.*?)\s*`;",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing TypeScript data table: {name}")
    return match.group("data")


def _casefold_mapping(source: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for row in _template(source, "caseFoldData").splitlines():
        source_hex, target_hex = row.split(";", 1)
        code_point = int(source_hex, 16)
        if code_point in result:
            raise AssertionError(f"duplicate casefold source: U+{code_point:04X}")
        result[code_point] = "".join(chr(int(value, 16)) for value in target_hex.split())
    return result


def _assigned_code_points(source: str) -> tuple[set[int], int]:
    result: set[int] = set()
    rows = _template(source, "assignedRangeData").splitlines()
    previous_end = -1
    for row in rows:
        start_hex, end_hex = row.split("-", 1)
        start, end = int(start_hex, 16), int(end_hex, 16)
        if start > end or start <= previous_end:
            raise AssertionError(f"invalid or overlapping assigned range: {row}")
        result.update(range(start, end + 1))
        previous_end = end
    return result, len(rows)


def _describe_mapping_delta(
    actual: dict[int, str], expected: dict[int, str]
) -> str:
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    wrong = sorted(
        code_point
        for code_point in actual.keys() & expected.keys()
        if actual[code_point] != expected[code_point]
    )
    sample = lambda values: ", ".join(f"U+{value:04X}" for value in values[:8]) or "none"
    return (
        f"missing={len(missing)} [{sample(missing)}], "
        f"extra={len(extra)} [{sample(extra)}], "
        f"wrong={len(wrong)} [{sample(wrong)}]"
    )


def main() -> None:
    _require_runtime()
    casefold_source = CASEFOLD_PATH.read_text(encoding="utf-8")
    normalize_source = NORMALIZE_PATH.read_text(encoding="utf-8")

    assert _constant(casefold_source, "PYTHON_CASEFOLD_UNICODE_VERSION") == "15.0.0"
    assert _constant(normalize_source, "PYTHON_UNICODE_VERSION") == "15.0.0"

    actual_casefold = _casefold_mapping(casefold_source)
    expected_casefold = {
        code_point: folded
        for code_point in range(MAX_CODE_POINT + 1)
        if (folded := chr(code_point).casefold()) != chr(code_point)
    }
    mapping_delta = _describe_mapping_delta(actual_casefold, expected_casefold)
    if actual_casefold != expected_casefold:
        raise AssertionError(f"casefold table differs from Python 3.12: {mapping_delta}")
    assert len(actual_casefold) == int(
        _constant(casefold_source, "PYTHON_CASEFOLD_MAPPING_COUNT")
    )

    actual_assigned, range_count = _assigned_code_points(normalize_source)
    expected_assigned = {
        code_point
        for code_point in range(MAX_CODE_POINT + 1)
        if unicodedata.category(chr(code_point)) != "Cn"
    }
    missing_assigned = expected_assigned - actual_assigned
    extra_assigned = actual_assigned - expected_assigned
    if missing_assigned or extra_assigned:
        raise AssertionError(
            "assigned ranges differ from Python 3.12: "
            f"missing={len(missing_assigned)}, extra={len(extra_assigned)}"
        )
    assert range_count == int(
        _constant(normalize_source, "UNICODE15_ASSIGNED_RANGE_COUNT")
    )

    python_whitespace = {
        code_point
        for code_point in range(MAX_CODE_POINT + 1)
        if chr(code_point).isspace()
    }
    assert len(python_whitespace) == int(
        _constant(normalize_source, "PYTHON_WHITESPACE_CODE_POINT_COUNT")
    )

    nfkc_changes = sum(
        unicodedata.normalize("NFKC", chr(code_point)) != chr(code_point)
        for code_point in range(MAX_CODE_POINT + 1)
    )
    print(
        "Unicode 15 frontend tables verified: "
        f"casefold={len(actual_casefold)}, assigned_ranges={range_count}, "
        f"assigned_code_points={len(actual_assigned)}, "
        f"python_whitespace={len(python_whitespace)}, nfkc_changes={nfkc_changes}"
    )


if __name__ == "__main__":
    main()
