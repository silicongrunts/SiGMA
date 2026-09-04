"""Shared fixtures for the tests/files suite.

Four pieces of setup repeat across file tests and live here so each test
file states its scenario instead of re-declaring plumbing:

* ``sandbox`` — point ``file_service``'s project root at ``tmp_path``.
* ``sandbox_without_snapshots`` — ``sandbox`` plus ``_after_file_mutation``
  disabled, for write-path tests that do not own snapshot behavior.
* ``_clean_read_state`` (autouse) — the read-state cache is a process-wide
  singleton; stale entries would let one test's must-read-first state
  satisfy another test's write. Annotation/agent sub-loops key under
  ``annotation:<id>``/``agent:<kind>:<id>`` scopes rather than a session id,
  so every scope present in the store is cleared, not just ``sess``.
* ``_open_write_gate`` (autouse) — ``write_file_absolute`` refuses writes
  for inactive projects. Project lifecycle gating is owned by the
  tests/projects suite; file-tool unit tests run against a fake sandbox and
  patch the gate open.
"""

import pytest

from app.agents.tools.read_state import read_state_cache
from app.services.file_service import file_service


def clear_all_read_state_scopes() -> None:
    """Reset every read-state scope used by the tools.

    Scope keys beyond a plain session id (``annotation:*``, ``agent:*``) are
    only enumerable from the cache's store, so enumeration reads the store
    and each scope is dropped through the public ``clear`` API.
    """
    for scope in list(read_state_cache._store):
        read_state_cache.clear(scope)


@pytest.fixture(autouse=True)
def _clean_read_state():
    clear_all_read_state_scopes()
    yield
    clear_all_read_state_scopes()


@pytest.fixture(autouse=True)
def _open_write_gate(monkeypatch):
    from app.services.project_service import project_service
    monkeypatch.setattr(project_service, "is_project_active", lambda pid: True)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Patch file_service's project root to *tmp_path* and return it."""
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)
    return tmp_path


@pytest.fixture
def sandbox_without_snapshots(sandbox, monkeypatch):
    """The shared sandbox with post-mutation bookkeeping disabled.

    ``sandbox`` deliberately leaves ``_after_file_mutation`` real because
    snapshot behavior is owned by test_snapshot_trigger.py. Write-path tests
    that assert their own contract (conflict handling, upload persistence)
    opt into this fixture instead, so a write neither touches the real
    project registry nor triggers a git snapshot.
    """

    async def no_mutation(_project_id: str) -> None:
        return None

    monkeypatch.setattr(file_service, "_after_file_mutation", no_mutation)
    return sandbox
