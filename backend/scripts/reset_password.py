#!/usr/bin/env python3
"""
Offline password reset / change for SiGMA.

Run on the server when the access password is lost, or when the settings UI is
unreachable. It does exactly what ``POST /auth/password`` does — hash the new
password, persist it into ``settings.yaml``, rotate the signing secret so every
outstanding session cookie stops validating — then restarts the service so the
running processes pick up the new hash and secret.

Usage (inside the container, or in the dev env)::

    docker exec -it <container> /opt/venv-sigma/bin/python \
        /app/backend/scripts/reset_password.py            # set / change
    docker exec -it <container> /opt/venv-sigma/bin/python \
        /app/backend/scripts/reset_password.py --clear     # disable protection

The plaintext password is read via ``getpass`` (never echoed, never logged) and
asked twice for confirmation. Must be run with ``cwd`` at or below
``backend/`` so ``import app.*`` resolves — the same convention as the test
suite and ``supervisord``'s ``directory=/app/backend``.

Security model
--------------
This is a local-filesystem recovery tool, equivalent to ``passwd`` or a DB
password reset: anyone who can run it can already read/write ``userdata/`` and
is therefore fully trusted. It does not weaken the web access gate, which only
protects network access — and cannot defend against an attacker with shell +
filesystem access. Recorded as an intentional exception in
``RULES/SECURITY.md``.
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve app.* against this file's location, so the script works regardless of
# the caller's cwd. ``backend/`` (the parent of ``scripts/``) is on sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.auth import (  # noqa: E402  — path setup must precede the import
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    hash_password,
    rotate_auth_secret,
)
from app.core.config import (  # noqa: E402
    SETTINGS_FILE,
    load_settings_file,
    update_password_hash,
)

# supervisorctl is the same restart path used by POST /system/restart. The socket
# only exists when SiGMA runs under supervisord (the container); on a dev box the
# caller restarts manually.
_SUPERVISOR_SOCKET = Path("/var/run/supervisor.sock")


def _read_new_password() -> str:
    """Prompt for a new password twice, enforcing the shared length bounds.

    The plaintext never leaves this function. Empty input is treated as a
    cancellation, not a silent password-clear — clearing requires ``--clear``.
    EOF (Ctrl-D) or Ctrl-C also cancels cleanly rather than dumping a traceback.
    """
    try:
        while True:
            pw = getpass.getpass("New password: ")
            if not pw:
                print("Cancelled.")
                sys.exit(0)
            if len(pw) < MIN_PASSWORD_LENGTH:
                print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters. Try again.")
                continue
            if len(pw) > MAX_PASSWORD_LENGTH:
                print(f"Password must be at most {MAX_PASSWORD_LENGTH} characters. Try again.")
                continue
            confirm = getpass.getpass("Confirm password: ")
            if pw != confirm:
                print("Passwords did not match. Try again.")
                continue
            return pw
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)


def _restart_services() -> bool:
    """Restart web + worker via supervisorctl. Return False if unavailable.

    Matches the POST /system/restart flow. Outside supervisord (dev box) the
    caller is told to restart manually — guessing/killing a host uvicorn would
    be fragile and unsafe.
    """
    if not _SUPERVISOR_SOCKET.exists() or not shutil.which("supervisorctl"):
        return False
    try:
        result = subprocess.run(
            ["supervisorctl", "restart", "web", "worker"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  supervisorctl restart failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"  supervisorctl restart failed: {result.stdout.strip()}", file=sys.stderr)
        return False
    return True


def _report_status() -> None:
    """Print the current access-protection state for confirmation."""
    cfg = load_settings_file()
    enabled = bool(cfg.security.password_hash)
    print(f"  settings: {SETTINGS_FILE}")
    print(f"  access protection: {'ENABLED' if enabled else 'disabled'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set, change, or clear the SiGMA access password (offline)."
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Disable access protection (clear the password). Requires confirmation.",
    )
    args = parser.parse_args(argv)

    print("SiGMA password reset")
    print("Current state:")
    _report_status()
    print()

    if args.clear:
        try:
            confirm = input("Disable access protection entirely? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 0
        if confirm != "y":
            print("Cancelled.")
            return 0
        update_password_hash("")
        rotate_auth_secret()
        print("\nAccess password cleared. Protection is now disabled.")
    else:
        print(f"Enter a new password ({MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} chars).")
        new_password = _read_new_password()
        new_hash = hash_password(new_password)
        update_password_hash(new_hash)
        # Same rotation as POST /auth/password: invalidate every outstanding
        # cookie, including this browser's, so all sessions must re-login.
        rotate_auth_secret()
        print("\nAccess password updated.")

    # The running processes cache the old hash + secret in memory; they must be
    # restarted or the change has no effect.
    print("\nRestarting services...")
    if _restart_services():
        print("  web + worker restarted. The new password is now active.")
        return 0
    print(
        "  supervisorctl unavailable (not under supervisord?). Restart SiGMA\n"
        "  manually so the new password takes effect."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
