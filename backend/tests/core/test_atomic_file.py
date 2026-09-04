"""app/core/atomic_file.py: fail_if_exists exclusive-claim semantics."""

import tempfile
import threading
from pathlib import Path

import pytest

from app.core.atomic_file import (
    atomic_write_bytes,
    atomic_write_text,
    AtomicFileExistsError,
)


class TestUploadConcurrency:
    """Verify that concurrent uploads with the same filename are safe."""

    def test_fail_if_exists_rejects_existing_file(self):
        """fail_if_exists=True should refuse to overwrite."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "upload.bin"
            # Write initial content
            atomic_write_bytes(p, b"original")
            # Attempt to overwrite with fail_if_exists=True
            with pytest.raises(AtomicFileExistsError):
                atomic_write_bytes(p, b"new-content", fail_if_exists=True)
            # Original must be intact
            assert p.read_bytes() == b"original"

    def test_fail_if_exists_allows_new_file(self):
        """fail_if_exists=True should succeed when file doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "new_file.bin"
            atomic_write_bytes(p, b"fresh", fail_if_exists=True)
            assert p.read_bytes() == b"fresh"

    def test_concurrent_same_name_only_one_succeeds(self):
        """Two threads writing the same new file with fail_if_exists=True.

        Exactly one should succeed; the other should get FileExistsError.
        The file must contain valid content from the winner.
        """
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "race.bin"
            errors: list[Exception] = []
            successes: list[str] = []

            def writer(label: str):
                try:
                    atomic_write_bytes(
                        p, label.encode(), fail_if_exists=True,
                    )
                    successes.append(label)
                except AtomicFileExistsError as e:
                    errors.append(e)

            t1 = threading.Thread(target=writer, args=("thread-A",))
            t2 = threading.Thread(target=writer, args=("thread-B",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(successes) == 1, (
                f"Expected exactly 1 success, got {len(successes)}"
            )
            assert len(errors) == 1, (
                f"Expected exactly 1 error, got {len(errors)}"
            )
            content = p.read_bytes().decode()
            assert content in ("thread-A", "thread-B")
            assert content == successes[0]


class TestExtractConcurrency:
    """Verify that extract-member semantics are safe under concurrency."""

    def test_overwrite_false_rejects_existing(self):
        """atomic_write_bytes with fail_if_exists=True simulates extract
        overwrite=False refusing to overwrite an existing file."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "member.txt"
            atomic_write_text(p, "original member")
            with pytest.raises(AtomicFileExistsError):
                atomic_write_text(p, "new member", fail_if_exists=True)
            assert p.read_text() == "original member"

    def test_overwrite_true_succeeds(self):
        """fail_if_exists=False (default) should always succeed."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "member.txt"
            atomic_write_text(p, "first")
            atomic_write_text(p, "second")  # default: fail_if_exists=False
            assert p.read_text() == "second"
