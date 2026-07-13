"""
Regression tests for the offline ``reset_password.py`` script.

The script is a process-level CLI, so these tests invoke it as a subprocess
with ``SIGMA_USERDATA_DIR`` pointed at a temp dir — exactly the isolation rule
the suite requires (no writes to the real ``userdata/``), and the most faithful
way to exercise the real import path, settings resolution, and restart
detection.

Why subprocess, not import-and-call: the script mutates ``settings.yaml`` and
``auth_secret.key`` via module-level references that are computed at import
time from ``SIGMA_USERDATA_DIR``. A fresh subprocess with an env override is
the cleanest way to aim those writes at a temp tree without monkeypatching
internals.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPT = BACKEND_DIR / "scripts" / "reset_password.py"
PY = sys.executable


def _run(tmp_path: Path, stdin: str, extra_args=None) -> subprocess.CompletedProcess:
    """Run the script against an isolated userdata dir, returning its result."""
    env = dict(os.environ)
    env["SIGMA_USERDATA_DIR"] = str(tmp_path)
    return subprocess.run(
        [PY, str(SCRIPT), *(extra_args or [])],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
        timeout=60,
    )


def _read_hash(tmp_path: Path) -> str:
    settings_path = tmp_path / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    return (data.get("security") or {}).get("password_hash", "")


def _secret_path(tmp_path: Path) -> Path:
    return tmp_path / ".SiGMA" / "auth_secret.key"


@pytest.mark.security
@pytest.mark.regression
def test_set_password_persists_hash_and_secret(tmp_path):
    """Setting a password writes a valid bcrypt hash and provisions the signing
    secret, so the change is durable and cookies get re-keyed."""
    result = _run(tmp_path, "correct-horse\n" * 2)
    assert result.returncode == 0, result.stderr
    assert "Access password updated" in result.stdout

    new_hash = _read_hash(tmp_path)
    assert new_hash.startswith("$2b$")  # bcrypt
    assert len(new_hash) >= 50

    # The signing secret is provisioned with restrictive perms (0600) and a
    # non-trivial length — rotate_auth_secret ran.
    secret = _secret_path(tmp_path)
    assert secret.exists()
    assert len(secret.read_bytes()) >= 32
    assert (secret.stat().st_mode & 0o777) == 0o600


@pytest.mark.security
@pytest.mark.regression
def test_set_password_hash_verifies(tmp_path):
    """The persisted hash actually verifies against the typed password."""
    _run(tmp_path, "s3cret-pass\n" * 2)
    new_hash = _read_hash(tmp_path)
    # Import bcrypt lazily so a missing dep surfaces here, not at collection.
    import bcrypt
    assert bcrypt.checkpw(b"s3cret-pass", new_hash.encode("utf-8"))
    assert not bcrypt.checkpw(b"wrong-password", new_hash.encode("utf-8"))


@pytest.mark.security
@pytest.mark.regression
def test_mismatched_password_is_rejected_then_accepted(tmp_path):
    """A mismatched confirmation must not write anything; a retry then succeeds."""
    result = _run(tmp_path, "first-pw-1\nsecond-pw-2\nsecond-pw-2\nsecond-pw-2\n")
    assert result.returncode == 0, result.stderr
    assert "did not match" in result.stdout
    # Despite the first mismatch, the script looped and the final write happened.
    assert _read_hash(tmp_path).startswith("$2b$")


@pytest.mark.security
@pytest.mark.regression
def test_short_password_rejected(tmp_path):
    """Passwords shorter than MIN_PASSWORD_LENGTH are rejected. After a rejected
    attempt the user gives up (stdin closes → EOF); the script cancels cleanly
    instead of crashing, and nothing is written."""
    result = _run(tmp_path, "ab\n")  # short, then EOF on the retry
    assert result.returncode == 0, result.stderr
    assert "at least" in result.stdout
    assert "Cancelled" in result.stdout
    # Nothing was written — the read-modify-write never fired.
    assert _read_hash(tmp_path) == ""
    assert not _secret_path(tmp_path).exists()


@pytest.mark.security
@pytest.mark.regression
def test_short_password_then_valid_accepted(tmp_path):
    """After a rejected short attempt, a subsequent valid password is accepted."""
    result = _run(tmp_path, "ab\n" + "valid-pw-99\n" * 2)
    assert result.returncode == 0, result.stderr
    assert "at least" in result.stdout
    # The valid attempt after the rejection still completed the write.
    assert _read_hash(tmp_path).startswith("$2b$")


@pytest.mark.security
@pytest.mark.regression
def test_empty_input_cancels(tmp_path):
    """An empty first prompt cancels immediately without touching settings.
    Clearing requires an explicit --clear, never an accidental blank entry."""
    result = _run(tmp_path, "\n")
    assert result.returncode == 0
    assert "Cancelled" in result.stdout
    assert _read_hash(tmp_path) == ""
    assert not _secret_path(tmp_path).exists()


@pytest.mark.security
@pytest.mark.regression
def test_clear_flag_removes_existing_password(tmp_path):
    """--clear wipes an existing hash and rotates the secret, after confirmation."""
    # First establish a password.
    _run(tmp_path, "initial-pw-9\n" * 2)
    first_hash = _read_hash(tmp_path)
    assert first_hash.startswith("$2b$")
    first_secret = _secret_path(tmp_path).read_bytes()

    # Now clear it with explicit confirmation.
    result = _run(tmp_path, "y\n", extra_args=["--clear"])
    assert result.returncode == 0, result.stderr
    assert "cleared" in result.stdout
    assert _read_hash(tmp_path) == ""
    # Secret rotated: new bytes differ from the pre-clear secret.
    assert _secret_path(tmp_path).read_bytes() != first_secret


@pytest.mark.security
@pytest.mark.regression
def test_clear_flag_requires_confirmation(tmp_path):
    """--clear without 'y' confirmation must not clear the password."""
    _run(tmp_path, "keep-me-pw-7\n" * 2)
    result = _run(tmp_path, "n\n", extra_args=["--clear"])
    assert result.returncode == 0
    assert "Cancelled" in result.stdout
    # Password untouched.
    assert _read_hash(tmp_path).startswith("$2b$")


@pytest.mark.security
@pytest.mark.regression
def test_restart_falls_back_when_no_supervisor(tmp_path):
    """Outside supervisord the script tells the caller to restart manually
    rather than guessing/killing a host process."""
    result = _run(tmp_path, "any-pw-123\n" * 2)
    assert result.returncode == 0
    # The dev/test env has no supervisorctl socket, so the fallback message fires.
    assert "manually" in result.stdout


@pytest.mark.security
@pytest.mark.regression
def test_report_status_reflects_current_state(tmp_path):
    """The pre-change status report reads the on-disk settings, so an operator
    sees the real current protection state before acting."""
    # Fresh userdata → disabled.
    result1 = _run(tmp_path, "n\n", extra_args=["--clear"])
    assert "disabled" in result1.stdout
    # After setting a password, a subsequent invocation reports ENABLED.
    _run(tmp_path, "set-pw-456\n" * 2)
    result2 = _run(tmp_path, "n\n", extra_args=["--clear"])
    assert "ENABLED" in result2.stdout
