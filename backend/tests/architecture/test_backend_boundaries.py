import ast
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[2] / "app"


def _imports_for(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _line_numbers_for_imports(path: Path, prefixes: tuple[str, ...]) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        if any(module == prefix or module.startswith(f"{prefix}.") for module in modules for prefix in prefixes):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.architecture
def test_routes_do_not_import_database_boundary_directly():
    forbidden = (
        "app.database",
        "sqlalchemy",
    )
    violations = {}
    for path in sorted((APP_DIR / "routes").glob("*.py")):
        if path.name == "__init__.py":
            continue
        lines = _line_numbers_for_imports(path, forbidden)
        if lines:
            violations[path.relative_to(APP_DIR.parent).as_posix()] = lines

    assert violations == {}


@pytest.mark.architecture
def test_agent_tools_do_not_import_database_boundary_directly():
    forbidden = (
        "app.database",
        "sqlalchemy",
    )
    violations = {}
    for path in sorted((APP_DIR / "agents" / "tools").glob("*.py")):
        if path.name == "__init__.py":
            continue
        lines = _line_numbers_for_imports(path, forbidden)
        if lines:
            violations[path.relative_to(APP_DIR.parent).as_posix()] = lines

    assert violations == {}


@pytest.mark.architecture
def test_services_do_not_import_route_modules():
    violations = {}
    for path in sorted((APP_DIR / "services").glob("*.py")):
        imports = _imports_for(path)
        route_imports = sorted(
            module for module in imports
            if module == "app.routes" or module.startswith("app.routes.")
        )
        if route_imports:
            violations[path.relative_to(APP_DIR.parent).as_posix()] = route_imports

    assert violations == {}
