import ast
from pathlib import Path

import pytest


TESTS_DIR = Path(__file__).resolve().parents[1]


def _python_test_files() -> list[Path]:
    return sorted(TESTS_DIR.rglob("test_*.py"))


@pytest.mark.architecture
def test_tests_do_not_use_unmanaged_mkdtemp():
    violations: dict[str, list[int]] = {}
    for path in _python_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "mkdtemp"
                and isinstance(func.value, ast.Name)
                and func.value.id == "tempfile"
            ):
                lines.append(node.lineno)
        if lines:
            violations[path.relative_to(TESTS_DIR).as_posix()] = sorted(lines)

    assert violations == {}
