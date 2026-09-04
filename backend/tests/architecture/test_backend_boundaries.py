"""Static dependency-boundary checks for the backend layering.

Forbidden edges:
- ``routes/`` and ``agents/tools/`` must not import the database boundary
  (``app.database``) or SQLAlchemy directly.
- ``services/`` must not import route modules.

Absolute imports, relative imports (``from ..database import x``), and
``from package import submodule`` forms are all resolved to fully-qualified
module names before matching, so the check cannot be bypassed by import
style. Scans are recursive over each directory tree. Dynamic imports
(``importlib`` / ``__import__``) are not visible to this check.
"""

import ast
from pathlib import Path

import pytest


APP_DIR = Path(__file__).resolve().parents[2] / "app"


def _imported_modules(path: Path) -> set[str]:
    """Fully-qualified module names imported by *path*.

    Relative imports are resolved against the file's package (its directory
    path under ``app/``, rooted at ``app``); ``from package import name``
    also contributes ``package.name`` so prefix matching sees submodules.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    under_app = path.relative_to(APP_DIR).with_suffix("").parts
    package = ("app",) + under_app[:-1]

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                resolved = list(base)
            else:
                resolved = []
            if node.module:
                resolved += node.module.split(".")
            module = ".".join(resolved)
            if not module:
                continue
            modules.add(module)
            for alias in node.names:
                if alias.name != "*":
                    modules.add(f"{module}.{alias.name}")
    return modules


def _boundary_violations(
    directory: Path, forbidden_prefixes: tuple[str, ...]
) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for path in sorted(directory.rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        hits = sorted(
            module
            for module in _imported_modules(path)
            for prefix in forbidden_prefixes
            if module == prefix or module.startswith(f"{prefix}.")
        )
        if hits:
            violations[path.relative_to(APP_DIR.parent).as_posix()] = hits
    return violations


@pytest.mark.architecture
def test_routes_do_not_import_database_boundary_directly():
    assert _boundary_violations(
        APP_DIR / "routes", ("app.database", "sqlalchemy")
    ) == {}


@pytest.mark.architecture
def test_agent_tools_do_not_import_database_boundary_directly():
    assert _boundary_violations(
        APP_DIR / "agents" / "tools", ("app.database", "sqlalchemy")
    ) == {}


@pytest.mark.architecture
def test_services_do_not_import_route_modules():
    assert _boundary_violations(APP_DIR / "services", ("app.routes",)) == {}
