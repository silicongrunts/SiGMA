"""
Atomic file utilities — safe read/write for user project files.

Provides:
* File-lock guard (cross-process safe via ``fcntl.flock``)
* Atomic text / binary write (temp file → fsync → rename)
* Optional compare-and-swap (hash check under lock)
* Optional create-only mode (refuse to overwrite existing files, under lock)
* Streaming-write claim primitive (hard-link an existing temp onto a free name)
* Safe JSON read with corruption handling

Usage::

    # Simple atomic text write (lock + temp + fsync + rename)
    atomic_write_text(path, content)

    # Binary write — refuse to overwrite an existing file (race-safe)
    atomic_write_bytes(path, data, fail_if_exists=True)

    # Binary write with optional CAS
    atomic_write_bytes(path, data, expected_hash="abc123")

    # Custom lock + CAS pattern (e.g. for diff-based conflict detection)
    with ProjectFileLock(path):
        # custom read / compare logic …
        atomic_replace_bytes(path, data)

Lock semantics
--------------
All ``atomic_write_*`` helpers acquire an exclusive ``fcntl.flock`` on a
``.lock`` sidecar file.  The lock is held for the entire check + write
sequence so that no other process can modify the file in between.

``atomic_replace_bytes`` is a lower-level helper that performs **only** the
temp-file + fsync + rename step without locking.  Use it inside a manually
acquired ``ProjectFileLock`` when the caller needs custom CAS logic (e.g.
the file-service hash-conflict diff).
"""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HashMismatchError(Exception):
    """Raised when *expected_hash* does not match the current file content.

    Attributes:
        disk_content: The current file content (``str`` for text, ``bytes``
            for binary).
        disk_hash: The computed hash of the disk content.
    """

    def __init__(self, disk_content: str | bytes, disk_hash: str):
        self.disk_content = disk_content
        self.disk_hash = disk_hash
        super().__init__(
            f"Hash mismatch: expected_hash != disk_hash ({disk_hash})"
        )


class AtomicFileExistsError(Exception):
    """Raised by atomic writes when *fail_if_exists=True* and the file exists.

    Distinct from the built-in ``FileExistsError`` to avoid accidental
    collision with OS-level errors.  Callers that need to translate it into
    a domain-specific error (e.g. ``app.core.exceptions.FileAlreadyExistsError``)
    should catch this type.
    """

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"File already exists: {path}")


# ---------------------------------------------------------------------------
# File lock
# ---------------------------------------------------------------------------

class ProjectFileLock:
    """Context manager that acquires an ``fcntl.flock`` on a lock file.

    Lock files live under ``settings.SIGMA_DIR`` (which honours the
    ``SIGMA_USERDATA_DIR`` env var, so lock placement follows the actual
    userdata tree).  The filename is a SHA-256 hash of the data-file path,
    which keeps user-visible directories free of ``.lock`` artifacts.  The
    lock file is deleted on release.

    Uses exclusive (blocking) lock so only one writer/reader at a time.
    With ``timeout`` set, acquisition polls non-blockingly up to the deadline
    and raises ``TimeoutError`` instead of blocking a thread forever — a
    stuck previous holder must not wedge every later caller.

    Because the lock file is unlinked on release, a waiter that opened the
    file before its deletion can end up flocking a dead inode while another
    waiter already created a fresh one — two holders, no exclusion. Every
    acquisition therefore re-validates that the locked fd still refers to
    the live lock path and retries on a stale file.
    """

    _POLL_INTERVAL_SEC = 0.1

    def __init__(self, data_file: Path, timeout: float | None = None):
        self._data_file = data_file.resolve()
        self._timeout = timeout
        lock_name = hashlib.sha256(str(self._data_file).encode()).hexdigest() + ".lock"
        sigma_dir = settings.SIGMA_DIR
        sigma_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = sigma_dir / lock_name
        self._fd = None

    def __enter__(self):
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            self._fd = open(self._lock_path, "w")
            self._flock(deadline)
            if self._fd_is_current():
                return self
            # Stale lock file: the previous holder unlinked this path and
            # another waiter already created a fresh one, so the flock we
            # just acquired guards a dead inode. Release and retry on the
            # current file.
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()

    def _flock(self, deadline: float | None) -> None:
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if deadline is None:
                    fcntl.flock(self._fd, fcntl.LOCK_EX)
                    break
                if time.monotonic() >= deadline:
                    self._fd.close()
                    self._fd = None
                    raise TimeoutError(
                        f"File lock busy after {self._timeout}s: {self._data_file}"
                    )
                time.sleep(self._POLL_INTERVAL_SEC)

    def _fd_is_current(self) -> bool:
        try:
            return (
                os.stat(self._lock_path).st_ino
                == os.fstat(self._fd.fileno()).st_ino
            )
        except FileNotFoundError:
            return False

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        if self._fd:
            # Unlink while still holding the flock: once the lock is
            # released, a waiter may acquire this very inode and re-validate
            # against the path, so the file must already be gone from the
            # namespace by then. Unlinking after unlock would delete the
            # lock file out from under the next validated holder.
            try:
                os.unlink(self._lock_path)
            except OSError:
                pass
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
        return False


# ---------------------------------------------------------------------------
# Low-level claim primitive
# ---------------------------------------------------------------------------

def claim_path(tmp_path: Path, dest_path: Path) -> None:
    """Atomically claim ``dest_path`` with an already-written temp file.

    The claim is a single ``os.link``: ``dest_path`` is created as a hard
    link to ``tmp_path`` — the caller must have written the temp file into
    the destination directory, so both live on one filesystem — or
    ``FileExistsError`` is raised with the destination untouched. Existence
    check and creation are one kernel operation, so exactly one concurrent
    claimant can win; the temp link is removed after a successful claim.
    """
    os.link(tmp_path, dest_path)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_hash(data: bytes) -> str:
    """Compute SHA-256 hex digest of binary data."""
    return hashlib.sha256(data).hexdigest()


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Write *data* to a temp file, fsync, then atomically rename to *path*.

    This is the low-level atomic-replace primitive.  It does **not** acquire
    a file lock or perform a CAS check — use it inside a manually acquired
    ``ProjectFileLock`` when the caller needs custom pre-write logic.

    The parent directory must already exist.  On failure the temp file is
    cleaned up (best-effort).
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".tmp_", suffix=path.suffix or ".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        # Best-effort cleanup of temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API — high-level atomic writes (lock + optional CAS + replace)
# ---------------------------------------------------------------------------

def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
    *,
    expected_hash: str | None = None,
    fail_if_exists: bool = False,
    hash_func: Callable[[bytes], str] | None = None,
) -> None:
    """Write *content* as text to *path* atomically with optional CAS.

    Under an exclusive file lock:

    1. If *fail_if_exists* is ``True`` and the file already exists, raises
       ``AtomicFileExistsError`` (the write is **not** performed).
    2. If *expected_hash* is provided and the file exists, reads the current
       content and compares its hash.  Raises ``HashMismatchError`` on
       mismatch (the write is **not** performed).
    3. Writes to a temp file, fsyncs, and atomically renames to *path*.
    4. Cleans up the temp file on any failure.

    Args:
        path: Destination file path (will be resolved).
        content: Text content to write.
        encoding: Text encoding (default UTF-8).
        fail_if_exists: If ``True``, refuse to overwrite an existing file.
            The existence check is performed **under the lock**, preventing
            TOCTOU races between concurrent callers.
        expected_hash: If provided, the write only proceeds when the current
            file content's hash matches this value.
        hash_func: Callable ``bytes -> str`` for hashing.  Defaults to
            SHA-256.  Pass a custom function to match an existing hash
            convention (e.g. MD5 for file-service compatibility).
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    _hash = hash_func or _default_hash

    with ProjectFileLock(path):
        if fail_if_exists and path.exists():
            raise AtomicFileExistsError(path)
        if expected_hash is not None and path.exists():
            disk_bytes = path.read_bytes()
            disk_hash = _hash(disk_bytes)
            if disk_hash != expected_hash:
                raise HashMismatchError(
                    disk_content=disk_bytes.decode(encoding, errors="replace"),
                    disk_hash=disk_hash,
                )
        atomic_replace_bytes(path, content.encode(encoding))


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    expected_hash: str | None = None,
    fail_if_exists: bool = False,
    hash_func: Callable[[bytes], str] | None = None,
) -> None:
    """Write binary *data* to *path* atomically with optional CAS.

    Same semantics as ``atomic_write_text`` but for binary content.
    *fail_if_exists* checks are performed under the lock.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    _hash = hash_func or _default_hash

    with ProjectFileLock(path):
        if fail_if_exists and path.exists():
            raise AtomicFileExistsError(path)
        if expected_hash is not None and path.exists():
            disk_bytes = path.read_bytes()
            disk_hash = _hash(disk_bytes)
            if disk_hash != expected_hash:
                raise HashMismatchError(
                    disk_content=disk_bytes,
                    disk_hash=disk_hash,
                )
        atomic_replace_bytes(path, data)


# ---------------------------------------------------------------------------
# JSON convenience wrappers
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically (temp + fsync + rename).

    For JSON metadata that requires cross-process locking, wrap the call in
    a ``ProjectFileLock`` context (see ``project_service`` for an example).
    """
    raw = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace_bytes(path, raw)


def safe_read_json(path: Path) -> Dict[str, Any]:
    """Read and parse JSON from *path*.

    Raises ``json.JSONDecodeError`` on corruption instead of silently
    returning an empty dict.  The caller decides how to handle the error
    (backup + rebuild, or propagate).
    """
    text = path.read_text(encoding="utf-8")
    return json.loads(text)
