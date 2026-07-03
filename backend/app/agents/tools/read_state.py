"""
Read-state cache — tracks which files have been read in each session.

Used by the ``write``/``edit`` tools to enforce the must-read-first contract:
the LLM may not modify a file it has not read in the current compact-segment
of the conversation. Paginated reads satisfy this contract.

Scope and lifecycle
-------------------
* Keyed by ``session_id`` so reads in one session never satisfy the contract
  in another.
* Persists across turns within the same session — the LLM does not need to
  re-read just because the user sent another message.
* Cleared by the compaction flow (see ``query_loop._prepare_messages`` and
  ``query_loop.compact_active``). After a compaction the LLM must re-read a
  file before editing it — this matches the conversation boundary that the
  LLM itself sees (post-compact, prior tool output is no longer in context).
* In-memory only. SiGMA is a single-user local product; on restart the LLM
  re-reads as needed.

Why a per-session dict
----------------------
Read state must survive across turns in the same session until compaction
creates a new visible context boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ReadStateEntry:
    """Snapshot of a file at the time it was read."""

    content: str
    mtime: float
    is_partial: bool  # True if the read used offset/limit; informational only.


class ReadStateCache:
    """Per-session registry of read files.

    The cache is intentionally simple — a plain dict-of-dicts. Methods are
    sync because every operation is in-process and O(1). Concurrency is not
    a concern: SiGMA's query loop is single-threaded per session, and even
    with concurrent tool calls the GIL makes dict ops atomic.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, ReadStateEntry]] = {}

    def record_read(
        self,
        session_id: str,
        file_path: str,
        content: str,
        mtime: float,
        is_partial: bool,
    ) -> None:
        """Record (or refresh) a read of *file_path* in *session_id*."""
        self._store.setdefault(session_id, {})[file_path] = ReadStateEntry(
            content=content,
            mtime=mtime,
            is_partial=is_partial,
        )

    def was_read_full(self, session_id: str, file_path: str) -> bool:
        """True iff *file_path* was read in *session_id*.

        Kept for compatibility with older call sites; paginated reads now
        satisfy the must-read-first contract.
        """
        entry = self._store.get(session_id, {}).get(file_path)
        return entry is not None

    def get(self, session_id: str, file_path: str) -> Optional[ReadStateEntry]:
        """Return the recorded entry, or ``None`` if not read in this session."""
        return self._store.get(session_id, {}).get(file_path)

    def clear(self, session_id: str) -> None:
        """Drop all read-state for *session_id*. Called after a compaction."""
        self._store.pop(session_id, None)


read_state_cache = ReadStateCache()


def path_read_state_key(path: str | Path) -> str:
    """Return the canonical read-state key for a filesystem path."""
    return str(Path(path).resolve())


def path_mtime(path: str | Path) -> float | None:
    """Return the current mtime for a path, or None if it cannot be stat'd."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def record_path_read(
    session_id: str,
    path: str | Path,
    content: str,
    is_partial: bool = False,
) -> None:
    """Record that a session has read a filesystem path."""
    mtime = path_mtime(path)
    read_state_cache.record_read(
        session_id,
        path_read_state_key(path),
        content,
        mtime if mtime is not None else 0.0,
        is_partial,
    )


def verify_path_readable_fresh(session_id: str, path: str | Path) -> bool:
    """Return True iff a path was read in this session and has not changed."""
    target = Path(path)
    if not target.is_file():
        return True

    entry = read_state_cache.get(session_id, path_read_state_key(target))
    if entry is None:
        return False

    current_mtime = path_mtime(target)
    if current_mtime is None:
        return False
    return entry.mtime == current_mtime
