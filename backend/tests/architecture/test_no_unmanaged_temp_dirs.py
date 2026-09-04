"""No unmanaged temp directories in the test suite.

Implements the RULES/TESTING.md "Test Isolation" rule that tests write
files under ``tmp_path`` (or an explicit isolated fixture) and a full run
leaves no business artifacts behind. ``tempfile.mkdtemp`` creates
directories outside pytest's tmp_path bookkeeping, so nobody cleans them
up on failure — they must not appear in test code. This check fails on:

- calls spelled ``tempfile.mkdtemp(...)`` via the canonical module name,
- ``from tempfile import mkdtemp`` imports, with or without an alias
  (``as``), whether or not the imported name is actually used.
"""

import ast
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parents[1]


def _python_test_files() -> list[Path]:
    return sorted(TESTS_DIR.rglob("test_*.py"))


def _unmanaged_mkdtemp_lines(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "mkdtemp"
                and isinstance(func.value, ast.Name)
                and func.value.id == "tempfile"
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            for alias in node.names:
                if alias.name == "mkdtemp":
                    lines.append(alias.lineno)
    return sorted(set(lines))


@pytest.mark.architecture
def test_tests_do_not_use_unmanaged_mkdtemp():
    violations: dict[str, list[int]] = {}
    for path in _python_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _unmanaged_mkdtemp_lines(tree)
        if lines:
            violations[path.relative_to(TESTS_DIR).as_posix()] = lines

    assert violations == {}
