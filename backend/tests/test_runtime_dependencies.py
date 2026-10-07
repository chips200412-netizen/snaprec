"""Packaging contract for capture dependencies (DEP-RUNTIME-001)."""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_httpx_is_a_direct_runtime_dependency_with_the_existing_constraint():
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(
        encoding="utf-8"
    ).splitlines()

    assert "httpx>=0.27,<1" in project["dependencies"]
    assert "httpx>=0.27,<1" in requirements


def test_test_extra_contains_only_test_dependencies():
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert project["optional-dependencies"]["test"] == ["pytest>=8,<9"]
    assert set(project["dependencies"]).isdisjoint(
        project["optional-dependencies"]["test"]
    )
