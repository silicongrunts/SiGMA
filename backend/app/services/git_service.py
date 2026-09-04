import json
import os
import re
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote
from pathlib import Path
from typing import List, Optional, Dict, Any

from app.core.exceptions import (
    FileSystemError, ProjectNotFoundError, FileMissingError, ValidationError,
)
from app.core.atomic_file import ProjectFileLock, atomic_replace_bytes
from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import is_within

logger = get_logger(__name__)

SNAPSHOT_CATEGORY_ORDER = ("added", "deleted", "modified")
SNAPSHOT_MESSAGE_PREFIX = "sigma:snapshot:v1:"

# Read-side git commands (log, diff, blob) stay fast at this budget. Mutation
# commands (add/commit) over multi-GB worktrees can legitimately need minutes,
# so they get a separate, larger budget.
GIT_READ_TIMEOUT_SEC = 30
GIT_WRITE_TIMEOUT_SEC = 600

# Kill escalation: SIGTERM first (git removes its own index.lock on SIGTERM),
# SIGKILL after the grace period, then a bounded reap so a process stuck in
# uninterruptible I/O cannot wedge the calling thread forever.
_TERM_GRACE_SEC = 5.0
_KILL_REAP_SEC = 10.0

# A lock older than this with no live git process on the repo is a corpse
# from a killed operation (SIGKILL skips git's own lock cleanup); leaving it
# in place makes every later snapshot fail instantly, forever.
STALE_LOCK_MIN_AGE_SEC = 60

# Freshness gate (auto snapshots only): a file modified within this window
# may still be mid-write (agent-side dataset downloads, builds), so the
# worktree counts as unstable and the snapshot defers until writes settle.
# SiGMA's own writes are atomic (temp + rename), but they land milliseconds
# before the save-triggered check, so the auto path must wait out the
# window; manual commits skip the gate and commit unconditionally.
FRESH_FILE_SEC = 10.0

# Wait longer than the longest git write so a queued legitimate snapshot
# never times out behind a slow one; only a genuinely stuck holder does.
SNAPSHOT_LOCK_WAIT_SEC = GIT_WRITE_TIMEOUT_SEC + 60

# LaTeX build artifacts to keep out of snapshot history. These are regenerated
# by every compile (``latexmk -jobname=output``), so versioning them bloats
# the per-project git repo with large, diff-unfriendly binaries. The names
# mirror ``LATEX_KEEP_OUTPUTS`` in latex_service — the files that survive
# ``_cleanup_latex_outputs`` and would otherwise be swept up by ``git add -A``.
# Kept as bare filenames (no leading slash) so the rule matches the artifact
# wherever the main TeX file lives, including subdirectories; this avoids any
# wildcard that could clobber a user's source such as ``figures/*.pdf``.
GITIGNORE_LATEX_OUTPUTS = ("output.pdf", "output.synctex.gz")

# Tag names become positional git arguments, so a leading dash or option-like
# value must be impossible. The whitelist (alphanumerics plus . _ -, no
# slashes) also stays inside git's own refname rules; the extra checks reject
# the remaining git-forbidden forms that the charset alone still allows.
TAG_NAME_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

# Default per-project cap for files that have never appeared in repository
# history. Existing versioned paths remain protected even after growing past
# the cap; only newly introduced bulk files are excluded.
DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB = 5
DEFAULT_SNAPSHOT_MAX_NEW_FILE_BYTES = (
    DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB * 1024 * 1024
)


def parse_max_new_file_mb(raw) -> int:
    """Parse a configured snapshot file-size cap (MiB), defaulting on
    invalid input. Single source for the int >= 1 rule shared by the
    project-config read and the snapshot pipelines."""
    try:
        value = int(raw)
        if value >= 1:
            return value
    except (ValueError, TypeError):
        pass
    logger.warning("Invalid snapshot_max_new_file_mb value %r; using %d",
                   raw, DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB)
    return DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB


# Guard rule appended to .git/info/attributes. Imported content (HuggingFace
# datasets, cloned repos) ships .gitattributes routing matched files through
# external filters such as git-lfs; with the filter configured in the user's
# global gitconfig but the binary missing, `git add -A` fails hard
# (`filter.lfs.required=true`). Snapshot repos are a purely local versioning
# store, so no external filter process may ever decide their availability.
# info/attributes outranks every in-tree .gitattributes, and gitattributes
# resolves later lines first — so the guard is kept as the file's LAST line
# and relocated there whenever something is appended below it. User lines
# are preserved; they simply cannot re-enable a filter.
FILTER_ISOLATION_HEADER = "# SiGMA: snapshot repos never invoke external filter processes"
FILTER_ISOLATION_RULE = "* -filter"

# Pathspec prefix keeping excluded paths literal: dataset filenames routinely
# contain spaces, non-ASCII characters, and glob metacharacters.
_EXCLUDE_PATHSPEC_PREFIX = ":(exclude,literal)"


def _validate_tag_name(name: str) -> str:
    """Return the stripped tag name, or raise if git would reject it."""
    name = name.strip()
    if not TAG_NAME_PATTERN.match(name) or ".." in name or name.endswith(".lock"):
        raise ValidationError(f"Invalid tag name: {name!r}")
    return name


def _validate_commit_hash(commit: str) -> str:
    """Reject anything that is not a plain hex commit id (7-64 chars)."""
    if not (7 <= len(commit) <= 64 and all(c in "0123456789abcdefABCDEF" for c in commit)):
        raise FileSystemError("Invalid commit hash", code="INVALID_INPUT")
    return commit


def _run_subprocess_with_grace(cmd: List[str], timeout: float) -> tuple:
    """Run a subprocess with graceful kill escalation.

    Returns (stdout_bytes, stderr_bytes, returncode). On timeout the process
    group receives SIGTERM first — git removes its own index.lock on SIGTERM
    — then SIGKILL after a grace period, so a timed-out mutation never leaks
    git's lock file. Reaping is bounded: a process parked in uninterruptible
    I/O must not block the caller indefinitely.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode
    except subprocess.TimeoutExpired:
        logger.warning("Git command timed out after %.0fs: %s", timeout, cmd[:4])

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=_TERM_GRACE_SEC)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()

    try:
        # Drain the pipes and reap; on exotic states (uninterruptible I/O)
        # give up after a bounded wait rather than hanging the caller.
        proc.communicate(timeout=_KILL_REAP_SEC)
    except subprocess.TimeoutExpired:
        logger.error(
            "Git process ignored SIGKILL (uninterruptible I/O?); abandoning: %s",
            cmd[:4],
        )
    raise FileSystemError("Git command timed out", code="INTERNAL_ERROR")


class GitService:
    def __init__(self):
        self.USERDATA_DIR = settings.USERDATA_DIR.resolve()

    def get_project_path(self, project_id: str) -> Path:
        path = (self.USERDATA_DIR / project_id).resolve()
        if not path.exists() or not is_within(path, self.USERDATA_DIR):
            raise ProjectNotFoundError(project_id)
        return path

    def _run_git(self, project_id: str, args: List[str], as_binary=False,
                 timeout: float = GIT_READ_TIMEOUT_SEC) -> tuple:
        """Run a git command. Returns (stdout, stderr, returncode).
        If as_binary=True, stdout is bytes instead of str.
        """
        project_path = self.get_project_path(project_id)
        # quotepath=false: git C-quotes non-ASCII paths by default
        # (e.g. "gpt\347\273\231....md"); every caller needs the raw
        # path so it can be passed back to git or shown to the user.
        stdout, stderr, rc = _run_subprocess_with_grace(
            ["git", "-c", "core.quotepath=false",
             "--git-dir", str(project_path / ".git"),
             "-C", str(project_path)] + args,
            timeout=timeout,
        )
        if as_binary:
            return stdout, stderr.decode('utf-8', errors='replace'), rc
        return stdout.decode('utf-8'), stderr.decode('utf-8', errors='replace'), rc

    def init_git(
        self,
        project_id: str,
        max_new_file_bytes: int = DEFAULT_SNAPSHOT_MAX_NEW_FILE_BYTES,
    ) -> bool:
        """Initialize a git repo for a new project."""
        try:
            project_path = self.get_project_path(project_id)
            # Init with main as the default branch (git 2.28+ supports --initial-branch)
            self._run_git(project_id, ["init", "--initial-branch=main"])

            self._run_git(project_id, ["config", "user.name", "SiGMA User"])
            self._run_git(project_id, ["config", "user.email", "user@sigma.local"])

            gitignore = project_path / ".gitignore"
            if not gitignore.exists():
                artifact_lines = "\n".join(GITIGNORE_LATEX_OUTPUTS)
                gitignore.write_text(
                    "# SiGMA auto-generated\n"
                    ".SiGMA/\n"
                    ".upload_*\n"
                    ".cache/\n"
                    f"# LaTeX build artifacts (regenerated on compile)\n"
                    f"{artifact_lines}\n",
                    encoding="utf-8",
                )

            self._ensure_filter_isolation(project_path)
            oversized = self._oversized_never_committed_files(
                project_id, project_path, max_new_file_bytes)
            self._remove_excluded_paths_from_index(project_id, oversized)
            self._run_git_stage(project_id, self._staging_args(oversized))
            if oversized:
                logger.info("Initial snapshot skipped %d file(s) over the size cap: %s",
                            len(oversized),
                            ", ".join(e["path"] for e in oversized[:5]))
            self._run_git(project_id, ["commit", "-m", "Initial commit"],
                          timeout=GIT_WRITE_TIMEOUT_SEC)
            return True
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Git init failed: {e}", code="INTERNAL_ERROR")

    def _run_git_stage(self, project_id: str, args: List[str]) -> bool:
        """Run a staging command (``add`` / ``rm --cached``), healing a stale
        index.lock once if it is what made the command fail, then retrying."""
        stdout, stderr, rc = self._run_git(project_id, args, timeout=GIT_WRITE_TIMEOUT_SEC)
        if rc == 0:
            return True
        if "index.lock" in stderr and self._heal_stale_index_lock(
                self.get_project_path(project_id)):
            stdout, stderr, rc = self._run_git(project_id, args,
                                               timeout=GIT_WRITE_TIMEOUT_SEC)
        if rc != 0:
            raise FileSystemError(f"Git staging failed: {stderr}", code="INTERNAL_ERROR")
        return True

    def create_snapshot_commit(
        self,
        project_id: str,
        defer_unstable: bool = False,
        max_new_file_bytes: int = DEFAULT_SNAPSHOT_MAX_NEW_FILE_BYTES,
    ) -> Dict[str, Any]:
        """Stage the working tree and commit it as one snapshot step.

        Shared by auto-snapshot and the manual-commit route. The index lock
        serializes overlapping callers (auto and manual, multiple tabs) so
        staged changes and commit messages can never interleave; the wait is
        bounded so a stuck holder cannot wedge every later caller.

        With ``defer_unstable`` (auto snapshots), the commit is held while
        non-ignored files were modified within the freshness window — they
        may be mid-write (bulk transfers, builds), so the snapshot waits for
        writes to settle. Manual commits pass False and commit
        unconditionally: the user asked for a version right now.

        Files over the size cap are excluded only when their path has never
        appeared in repository history. Already-versioned paths remain fully
        protected even after growing past the cap. Exclusions are listed in
        the commit message and returned result so the gap stays visible.
        """
        project_path = self.get_project_path(project_id)
        with ProjectFileLock(project_path / ".git" / "index",
                             timeout=SNAPSHOT_LOCK_WAIT_SEC):
            self._ensure_filter_isolation(project_path)
            if defer_unstable and not self._worktree_is_stable(
                    project_id, project_path):
                return {
                    "success": False,
                    "reason": "deferred",
                    "detail": "files still being written",
                }
            oversized = self._oversized_never_committed_files(
                project_id, project_path, max_new_file_bytes)
            self._remove_excluded_paths_from_index(project_id, oversized)
            self._run_git_stage(project_id, self._staging_args(oversized))
            message = self.build_staged_snapshot_message(project_id, oversized)
            result = self.commit(project_id, message)
            if oversized:
                result["skipped_large_files"] = oversized
                logger.info(
                    "Snapshot excluded %d file(s) over %.0f MB for %s: %s",
                    len(oversized), max_new_file_bytes / (1024 * 1024),
                    project_id, ", ".join(e["path"] for e in oversized[:5]),
                )
            return result

    def _ensure_filter_isolation(self, project_path: Path) -> bool:
        """Keep the filter guard as the last line of ``.git/info/attributes``.

        Later lines win in gitattributes, so only the final position makes
        the guard unconditional. Whenever other lines appear below it, the
        guard block is rewritten underneath them; user-authored lines are
        never removed — they just cannot override the guard.
        """
        attrs = project_path / ".git" / "info" / "attributes"
        content = ""
        try:
            content = attrs.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except OSError:
            return False
        guard_block = (FILTER_ISOLATION_HEADER, FILTER_ISOLATION_RULE)
        user_lines = [
            line for line in content.splitlines()
            if line.strip() not in guard_block
        ]
        if content and user_lines + list(guard_block) == content.splitlines():
            return False
        attrs.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(
            attrs,
            "\n".join(user_lines + list(guard_block)).encode("utf-8") + b"\n",
        )
        return True

    def _oversized_never_committed_files(
        self,
        project_id: str,
        project_path: Path,
        max_new_file_bytes: int,
    ) -> List[Dict[str, Any]]:
        """Untracked oversized paths that have never appeared in Git history.

        Returns ``{"path", "size"}`` entries — the size rides along so the UI
        can show how much each exclusion weighs. User-managed ignore rules
        are honoured exactly as ``git add -A`` sees them. A currently
        untracked path may still have historical commits after being
        deleted, so oversized candidates are filtered against one batched
        history walk.
        """
        untracked_stdout, stderr, rc = self._run_git(project_id, [
            "ls-files", "-o", "--exclude-standard", "-z",
        ], as_binary=True)
        if rc != 0:
            raise FileSystemError(
                f"Git size scan failed: {stderr}", code="INTERNAL_ERROR")

        staged_stdout, stderr, rc = self._run_git(project_id, [
            "diff", "--cached", "--diff-filter=A", "--name-only", "-z",
        ], as_binary=True)
        if rc != 0:
            raise FileSystemError(
                f"Git staged-file scan failed: {stderr}", code="INTERNAL_ERROR")

        candidates: List[Dict[str, Any]] = []
        for raw in dict.fromkeys(
                (untracked_stdout + staged_stdout).split(b"\0")):
            if not raw:
                continue
            path = raw.decode("utf-8", errors="replace")
            if path == ".SiGMA" or path.startswith(".SiGMA/"):
                continue
            try:
                size = os.lstat(project_path / path).st_size
            except OSError:
                continue
            if size > max_new_file_bytes:
                candidates.append({"path": path, "size": size})
        if not candidates:
            return []

        # Size-capped paths stay untracked, so every snapshot re-checks them;
        # this one walk replaces a per-candidate `git log` that each had to
        # traverse the entire history before concluding the path is new.
        # -z prints names NUL-separated and unmunged, so even names
        # containing newlines or glob metacharacters match exactly.
        stdout, stderr, rc = self._run_git(project_id, [
            "log", "--all", "--format=", "--name-only", "-z",
        ], as_binary=True)
        if rc != 0:
            raise FileSystemError(
                f"Git history scan failed: {stderr}", code="INTERNAL_ERROR")
        historic = {
            raw.decode("utf-8", errors="replace")
            for raw in stdout.split(b"\0") if raw
        }
        return [entry for entry in candidates
                if entry["path"] not in historic]

    def _remove_excluded_paths_from_index(
        self, project_id: str, excluded: List[Dict[str, Any]],
    ) -> None:
        """Keep pre-staged oversized new paths out of the snapshot index."""
        if not excluded:
            return
        literal_paths = [f":(literal){entry['path']}" for entry in excluded]
        self._run_git_stage(project_id, [
            "rm", "--cached", "-q", "-f", "--ignore-unmatch", "--",
            *literal_paths,
        ])

    @staticmethod
    def _staging_args(excluded: List[Dict[str, Any]]) -> List[str]:
        """``git add`` args staging everything except the given paths."""
        if not excluded:
            return ["add", "-A"]
        return ["add", "-A", "--"] + [
            f"{_EXCLUDE_PATHSPEC_PREFIX}{entry['path']}" for entry in excluded
        ]

    def _worktree_is_stable(self, project_id: str, project_path: Path) -> bool:
        """True when no git-visible file was modified within the window.

        Enumeration goes through ``git ls-files`` so user-managed ignore
        rules are honoured exactly as ``git add -A`` sees them: writes into
        an ignored directory (dataset scratch) must never block snapshots.
        ``.SiGMA`` state is skipped unconditionally — its DB and WAL churn
        on every save, so even an un-ignored copy would read as forever
        unstable. Future mtimes (clock skew) count as old, or one skewed
        file would defer snapshots forever.
        """
        stdout, stderr, rc = self._run_git(project_id, [
            "ls-files", "-m", "-o", "--exclude-standard", "-z",
        ], as_binary=True)
        if rc != 0:
            raise FileSystemError(
                f"Git status check failed: {stderr}", code="INTERNAL_ERROR")
        now = time.time()
        for raw in stdout.split(b"\0"):
            if not raw:
                continue
            path = raw.decode("utf-8", errors="replace")
            if path == ".SiGMA" or path.startswith(".SiGMA/"):
                continue
            try:
                mtime = os.lstat(project_path / path).st_mtime
            except OSError:
                continue
            if 0 <= now - mtime < FRESH_FILE_SEC:
                return False
        return True

    @staticmethod
    def _git_process_alive(project_path: Path) -> bool:
        """True when a live git process is operating on this repo.

        SiGMA-side invocations carry the repo paths in their command line;
        agent-run git only carries the worktree as its working directory
        (possibly a subdirectory of it), so the process cwd is checked as
        well.
        """
        git_dir_b = str(project_path / ".git").encode()
        worktree_b = str(project_path).encode()
        worktree = os.path.realpath(project_path)
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read()
                if not cmd or not cmd.split(b"\0", 1)[0].endswith(b"git"):
                    continue
                if git_dir_b in cmd or worktree_b in cmd:
                    return True
                cwd = os.path.realpath(f"/proc/{pid}/cwd")
                if cwd == worktree or cwd.startswith(worktree + os.sep):
                    return True
            except OSError:
                continue
        return False

    def _heal_stale_index_lock(self, project_path: Path) -> bool:
        """Remove a stale ``.git/index.lock`` left by a killed git operation.

        The lock qualifies only when it is older than the grace age AND no
        live git process is operating on the repo: SIGKILLed git cannot clean
        up after itself, and without this healing every later snapshot fails
        instantly with "index.lock exists" — permanently, with no recovery.
        """
        lock_path = project_path / ".git" / "index.lock"
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            return False
        if age < STALE_LOCK_MIN_AGE_SEC or self._git_process_alive(project_path):
            return False
        lock_path.unlink()
        logger.warning(
            "Removed stale git index.lock (age %.0fs, no live git process) for %s",
            age, project_path.name,
        )
        return True

    def heal_stale_lock(self, project_id: str) -> Dict[str, Any]:
        """Repair entry: remove the repo's stale index lock if present."""
        removed = self._heal_stale_index_lock(self.get_project_path(project_id))
        return {"lock_removed": removed}

    def _ensure_generated_gitignore(self, project_path: Path) -> bool:
        """Extend SiGMA-generated .gitignore files with the .cache rule.

        Only files carrying the SiGMA auto-generated header are touched;
        user-authored .gitignore files are never modified.
        """
        gitignore = project_path / ".gitignore"
        try:
            content = gitignore.read_text(encoding="utf-8")
        except OSError:
            return False
        if ".cache" in content or not content.startswith("# SiGMA auto-generated"):
            return False
        gitignore.write_text(
            content.replace(".upload_*\n", ".upload_*\n.cache/\n", 1),
            encoding="utf-8",
        )
        return True

    def startup_maintenance(self) -> Dict[str, int]:
        """Boot-time per-repo upkeep: refresh generated .gitignore rules,
        install the filter guard, and clear stale index locks (no SiGMA git
        operation is in flight yet)."""
        counts = {
            "gitignore_updated": 0,
            "filter_guard_added": 0,
            "stale_locks_removed": 0,
        }
        try:
            entries = list(self.USERDATA_DIR.iterdir())
        except OSError:
            return counts
        for entry in entries:
            if not entry.is_dir() or not (entry / ".git").exists():
                continue
            try:
                counts["gitignore_updated"] += int(
                    self._ensure_generated_gitignore(entry))
                counts["filter_guard_added"] += int(
                    self._ensure_filter_isolation(entry))
                counts["stale_locks_removed"] += int(
                    self._heal_stale_index_lock(entry))
            except Exception:
                logger.warning("Git startup maintenance failed for %s",
                               entry.name, exc_info=True)
        if any(counts.values()):
            logger.info("Git startup maintenance: %s", counts)
        return counts

    def get_snapshot_zip(self, project_id: str, commit: str) -> bytes:
        """Get a ZIP archive of the project at a specific commit using git archive.
        Returns the raw ZIP bytes."""
        project_path = self.get_project_path(project_id)
        fd, zip_path = tempfile.mkstemp(prefix="sigma-snapshot-", suffix=".zip")
        os.close(fd)
        try:
            stdout, stderr, rc = _run_subprocess_with_grace(
                ["git", "--git-dir", str(project_path / ".git"),
                 "-C", str(project_path),
                 "archive", "--output", zip_path, commit],
                timeout=GIT_WRITE_TIMEOUT_SEC,
            )
            if rc != 0:
                raise FileSystemError(f"Git archive failed: {stderr.decode('utf-8', errors='replace')}", code="INTERNAL_ERROR")
            with open(zip_path, "rb") as f:
                zip_data = f.read()
            return zip_data
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to create snapshot: {e}", code="INTERNAL_ERROR")
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass

    def commit(self, project_id: str, message: str,
                author_name: str = "SiGMA User",
                author_email: str = "user@sigma.local") -> Dict[str, Any]:
        """Create a commit with staged changes."""
        try:
            self._run_git(project_id, ["config", "user.name", author_name])
            self._run_git(project_id, ["config", "user.email", author_email])

            stdout, stderr, rc = self._run_git(project_id, ["commit", "-m", message],
                                               timeout=GIT_WRITE_TIMEOUT_SEC)
            if rc != 0:
                combined = f"{stdout}\n{stderr}".lower()
                # The third variant arises with the size cap: pending work
                # exists only as excluded untracked files, and git reports
                # it on stdout — it is a clean noop, not a commit failure.
                if any(marker in combined for marker in (
                        "nothing to commit",
                        "no changes",
                        "nothing added to commit",
                )):
                    return {"success": False, "reason": "no changes"}
                raise FileSystemError(f"Commit failed: {stderr}", code="INTERNAL_ERROR")

            stdout, stderr, rc = self._run_git(project_id, ["rev-parse", "HEAD"])
            commit_hash = stdout.strip()
            return {"success": True, "commit": commit_hash[:7]}
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Commit failed: {e}", code="INTERNAL_ERROR")

    def build_staged_snapshot_message(self, project_id: str,
                                      skipped: List[Dict[str, Any]] = ()) -> str:
        """Build an auto-snapshot title from currently staged Git changes.

        ``skipped`` names size-capped worktree files (``{"path", "size"}``
        entries) that were excluded from staging; they are recorded in the
        message so the gap in coverage is visible in the history panel.
        """
        changes = self._get_staged_snapshot_changes(project_id)
        return self._format_snapshot_message(
            changes, [entry["path"] for entry in skipped])

    @staticmethod
    def _parse_name_status_z(stdout: bytes) -> List[Dict[str, str]]:
        """Parse NUL-separated `--name-status -z` output into status/path pairs.

        Renames and copies carry two paths (old, new); only the new path is
        kept. Paths are raw bytes until here because NUL is the only byte that
        cannot appear in a filename — this is what makes -z safe for names
        containing quotes, newlines, tabs, or non-ASCII characters, none of
        which survive line/tab-based parsing reliably.
        """
        entries: List[Dict[str, str]] = []
        fields = stdout.split(b"\0")
        i = 0
        while i < len(fields):
            status = fields[i]
            if not status:
                i += 1
                continue
            status_code = status.decode("ascii", errors="replace").strip().upper()[:1]
            path_count = 2 if status_code in ("R", "C") else 1
            path_fields = fields[i + 1:i + 1 + path_count]
            if len(path_fields) < path_count:
                break
            entries.append({
                "status": status_code,
                "path": path_fields[-1].decode("utf-8", errors="replace"),
            })
            i += 1 + path_count
        return entries

    def _get_staged_snapshot_changes(self, project_id: str) -> Dict[str, List[str]]:
        stdout, stderr, rc = self._run_git(
            project_id, ["diff", "--cached", "--name-status", "-z"], as_binary=True,
        )
        if rc != 0:
            raise FileSystemError(f"Git diff --cached failed: {stderr}", code="INTERNAL_ERROR")

        changes: Dict[str, List[str]] = {category: [] for category in SNAPSHOT_CATEGORY_ORDER}
        for entry in self._parse_name_status_z(stdout):
            status = entry["status"]
            if status in ("A", "C"):
                category = "added"
            elif status == "D":
                category = "deleted"
            else:
                category = "modified"
            name = self._short_display_name(entry["path"])
            if name and name not in changes[category]:
                changes[category].append(name)
        return changes

    @staticmethod
    def _short_display_name(path: str) -> str:
        """Truncated basename for snapshot messages (full paths stay in git)."""
        name = Path(path).name
        return name[:10] + "..." if len(name) > 10 else name

    @staticmethod
    def _short_display_names(paths: List[str]) -> List[str]:
        """Deduplicated short display names preserving first occurrence."""
        names: List[str] = []
        for path in paths:
            name = GitService._short_display_name(path)
            if name and name not in names:
                names.append(name)
        return names

    @staticmethod
    def _format_snapshot_message(changes: Dict[str, List[str]],
                                 skipped: List[str] = ()) -> str:
        """Build a structured, locale-neutral auto-snapshot commit subject."""
        non_empty_categories = [
            category for category in SNAPSHOT_CATEGORY_ORDER
            if changes.get(category)
        ]
        if not non_empty_categories and not skipped:
            return "Auto-snapshot"

        slots = {category: 1 for category in non_empty_categories}
        remaining_slots = 3 - len(non_empty_categories)
        for category in SNAPSHOT_CATEGORY_ORDER:
            if remaining_slots <= 0:
                break
            names = changes.get(category, [])
            if not names:
                continue
            extra = min(len(names) - slots[category], remaining_slots)
            if extra > 0:
                slots[category] += extra
                remaining_slots -= extra

        payload: Dict[str, Dict[str, Any]] = {}
        for category in SNAPSHOT_CATEGORY_ORDER:
            names = changes.get(category, [])
            if not names:
                continue
            shown_count = slots[category]
            payload[category] = {
                "names": names[:shown_count],
                "total": len(names),
            }
        if skipped:
            # Display names are deduped (shards of one dataset often share a
            # truncated prefix), but the total counts real files — coverage
            # reporting must not shrink because names collide.
            names = GitService._short_display_names(skipped)
            payload["skipped"] = {
                "names": names[:3],
                "total": len(skipped),
            }
        encoded = quote(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), safe="")
        return f"{SNAPSHOT_MESSAGE_PREFIX}{encoded}"

    def get_log(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        before: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Get commit log. Uses tab-separated format for safe parsing."""
        try:
            # Build command: git log --pretty=format:"..."
            # Each field on its own line, commits separated by a known marker
            fmt_lines = [
                "COMMIT_START_MARKER",
                "HASH:%H",
                "SHORT:%h",
                "SUBJECT:%s",
                "DATE:%ai"
            ]
            fmt = "\n".join(fmt_lines)
            args = ["log", "-n", str(limit), f"--pretty={fmt}"]
            if before:
                _validate_commit_hash(before)
                args.extend(["--skip=1", before])
            else:
                args.append(f"--skip={offset}")
            stdout, stderr, rc = self._run_git(project_id, args)
            if rc != 0:
                raise FileSystemError(f"Git log failed: {stderr}", code="INTERNAL_ERROR")

            commits = []
            blocks = stdout.strip().split("COMMIT_START_MARKER\n")
            for block in blocks:
                block = block.strip()
                if not block:
                    continue
                info = {}
                for line in block.split('\n'):
                    if line.startswith("HASH:"):
                        info["hash"] = line[5:].strip()
                    elif line.startswith("SHORT:"):
                        info["short_hash"] = line[6:].strip()
                    elif line.startswith("SUBJECT:"):
                        info["message"] = line[8:].strip()
                    elif line.startswith("DATE:"):
                        info["date"] = line[5:].strip()
                if "hash" in info:
                    commits.append(info)
            return commits
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to get log: {e}", code="INTERNAL_ERROR")

    def get_commit_files(self, project_id: str,
                           commit: str,
                           parent_commit: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get the list of files changed in a commit."""
        try:
            if parent_commit:
                stdout, stderr, rc = self._run_git(project_id, [
                    "diff", "--name-status", "-z", f"{parent_commit}..{commit}"
                ], as_binary=True)
            else:
                # Root commit has no parent to diff against; --root diffs it
                # against the empty tree so its files are listed too.
                stdout, stderr, rc = self._run_git(project_id, [
                    "diff-tree", "--no-commit-id", "-r", "--name-status", "--root", "-z", commit
                ], as_binary=True)

            if rc != 0 or not stdout.strip(b"\0").strip():
                return []

            files = []
            for entry in self._parse_name_status_z(stdout):
                # Renames and copies are reported as modifications of the new path.
                status_code = "M" if entry["status"] in ("R", "C") else entry["status"]
                files.append({
                    "path": entry["path"],
                    "name": Path(entry["path"]).name,
                    "status": status_code,
                })
            return files
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to get commit files: {e}", code="INTERNAL_ERROR")

    def get_blob(self, project_id: str, path: str, commit: str) -> Dict[str, Any]:
        """Get file content from a specific commit for preview/download.
        Returns: {success, path, name, size, is_text, content, is_previewable}
        """
        project_path = self.get_project_path(project_id)
        full_path = project_path / path
        if not is_within(full_path, project_path):
            raise FileSystemError("Path traversal attempt detected", code="PERMISSION_DENIED", status_code=403)

        try:
            # Use git show to get the file content in binary mode
            source_ref = f"{commit}:{path}"
            stdout, stderr, rc = self._run_git(project_id, [
                "show", source_ref
            ], as_binary=True)
            if rc != 0:
                # A file deleted in this commit has no blob at `commit`; read
                # the parent's version so previews show the content as it was
                # just before deletion.
                source_ref = f"{commit}^:{path}"
                stdout, stderr, rc = self._run_git(project_id, [
                    "show", source_ref
                ], as_binary=True)

            if rc != 0:
                raise FileMissingError(path)

            # Get file size
            # Use git cat-file -s to get the blob size
            size_out, size_err, size_rc = self._run_git(project_id, [
                "cat-file", "-s", source_ref
            ])
            file_size = int(size_out.strip()) if size_rc == 0 and size_out.strip() else len(stdout)

            # Try to read as UTF-8 text (only up to 2MB for preview)
            MAX_PREVIEW_SIZE = 2 * 1024 * 1024  # 2MB

            is_text = False
            content_text = None

            if file_size <= MAX_PREVIEW_SIZE:
                try:
                    # Try UTF-8 first
                    content_text = stdout.decode('utf-8')
                    is_text = True
                except (UnicodeDecodeError, ValueError):
                    # Not UTF-8, try to check if it looks like a text file
                    try:
                        # Check for null bytes (binary indicator)
                        if b'\x00' in stdout[:8192]:
                            is_text = False
                        else:
                            # Try as Latin-1 which never fails
                            content_text = stdout.decode('utf-8', errors='replace')
                            # Further check: ratio of printable chars
                            printable = sum(
                                (c.isprintable() or c in '\n\r\t') for c in content_text[:8192]
                            )
                            if printable / max(len(content_text[:8192]), 1) < 0.7:
                                is_text = False
                    except Exception:
                        logger.debug("Failed to classify git file content as text", exc_info=True)
                        is_text = False
            else:
                # File > 2MB, try first 8KB to determine if it's text
                sample = stdout[:8192]
                try:
                    if b'\x00' in sample:
                        is_text = False
                    else:
                        sample_text = sample.decode('utf-8')
                        printable = sum(c.isprintable() or c in '\n\r\t' for c in sample_text)
                        if printable / max(len(sample_text), 1) < 0.7:
                            is_text = False
                        else:
                            is_text = True
                except (UnicodeDecodeError, ValueError):
                    is_text = False

            name = Path(path).name

            return {
                "success": True,
                "path": path,
                "name": name,
                "size": file_size,
                "is_text": is_text,
                "can_preview": is_text and file_size <= MAX_PREVIEW_SIZE,
                "content": content_text if (is_text and file_size <= MAX_PREVIEW_SIZE) else None,
            }
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to read file: {e}", code="INTERNAL_ERROR")

    def get_blob_raw(self, project_id: str, path: str, commit: str) -> Dict[str, Any]:
        """Get raw file bytes at a commit, for download (binary-safe)."""
        project_path = self.get_project_path(project_id)
        full_path = project_path / path
        if not is_within(full_path, project_path):
            raise FileSystemError("Path traversal attempt detected", code="PERMISSION_DENIED", status_code=403)

        try:
            stdout, stderr, rc = self._run_git(project_id, [
                "show", f"{commit}:{path}"
            ], as_binary=True)
            if rc != 0:
                # A file deleted in this commit has no blob at `commit`; fall
                # back to the parent so the pre-deletion version downloads.
                stdout, stderr, rc = self._run_git(project_id, [
                    "show", f"{commit}^:{path}"
                ], as_binary=True)
            if rc != 0:
                raise FileMissingError(path)
            return {"name": Path(path).name, "content": stdout}
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to read file: {e}", code="INTERNAL_ERROR")

    def get_diff(self, project_id: str, path: str,
                  commit: str, short_hash: str,
                  parent_commit: Optional[str] = None) -> Dict[str, Any]:
        """Get diff for a specific file in a commit."""
        project_path = self.get_project_path(project_id)
        full_path = project_path / path
        if not is_within(full_path, project_path):
            raise FileSystemError("Path traversal attempt detected", code="PERMISSION_DENIED", status_code=403)

        try:
            if parent_commit:
                stdout, stderr, rc = self._run_git(project_id, [
                    "diff", f"{parent_commit}..{commit}", "--", path
                ])
            else:
                # First commit - show the file content as all additions
                stdout, stderr, rc = self._run_git(project_id, [
                    "show", f"{commit}:{path}"
                ])

            if rc != 0:
                stdout = ""

            # Parse diff or raw content into typed lines
            lines = []
            raw_lines = stdout.strip().split('\n')

            for raw_line in raw_lines:
                if not parent_commit and (not raw_line.startswith('+') and not raw_line.startswith('-') and not raw_line.startswith('diff') and not raw_line.startswith('@@')):
                    # For first-commit view (raw content), treat each line as an addition
                    lines.append({"type": "add", "content": raw_line})
                elif raw_line.startswith('diff') or raw_line.startswith('index') or raw_line.startswith('---') or raw_line.startswith('+++'):
                    lines.append({"type": "header", "content": raw_line})
                elif raw_line.startswith('@@'):
                    lines.append({"type": "hunk", "content": raw_line})
                elif raw_line.startswith('+') and not raw_line.startswith('+++'):
                    lines.append({"type": "add", "content": raw_line[1:]})
                elif raw_line.startswith('-') and not raw_line.startswith('---'):
                    lines.append({"type": "remove", "content": raw_line[1:]})
                elif raw_line.startswith(' '):
                    lines.append({"type": "context", "content": raw_line[1:]})

            # Add line numbers for content lines
            annotated = []
            for line in lines:
                if line["type"] in ("add", "remove", "context"):
                    annotated.append({**line, "line_number": len(annotated) + 1})
                else:
                    annotated.append(line)

            return {
                "path": path,
                "commit": short_hash,
                "lines": annotated,
                "raw": stdout,
            }
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to get diff: {e}", code="INTERNAL_ERROR")

    def get_file_history(self, project_id: str, path: str) -> List[Dict[str, Any]]:
        """Get commit history for a specific file."""
        project_path = self.get_project_path(project_id)
        full_path = project_path / path
        if not is_within(full_path, project_path):
            raise FileSystemError("Path traversal attempt detected", code="PERMISSION_DENIED", status_code=403)

        try:
            fmt_lines = [
                "ENTRY_START",
                "HASH:%H",
                "SUBJECT:%s",
                "DATE:%ai"
            ]
            fmt = "\n".join(fmt_lines)
            stdout, stderr, rc = self._run_git(project_id, [
                "log", "--follow", f"--pretty={fmt}", "--", path
            ])
            if rc != 0:
                return []

            history = []
            blocks = stdout.strip().split("ENTRY_START\n")
            for block in blocks:
                block = block.strip()
                if not block:
                    continue
                info = {}
                for line in block.split('\n'):
                    if line.startswith("HASH:"):
                        info["hash"] = line[5:].strip()
                    elif line.startswith("SUBJECT:"):
                        info["message"] = line[8:].strip()
                    elif line.startswith("DATE:"):
                        info["date"] = line[5:].strip()
                if "hash" in info:
                    info["short_hash"] = info["hash"][:7]
                    history.append(info)
            return history
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Failed to get file history: {e}", code="INTERNAL_ERROR")


    def get_diff_with_defaults(self, project_id: str, path: str,
                                commit: str = None, short_hash: str = None,
                                parent_commit: str = None) -> dict:
        """Get diff for a file, resolving defaults for commit/short_hash."""
        if not commit:
            commits = self.get_log(project_id, 1)
            if not commits:
                raise FileMissingError("No commits found")
            commit = commits[0]["hash"]
            short_hash = commits[0]["short_hash"]
        return self.get_diff(project_id, path, commit, short_hash or commit[:7], parent_commit)

    def list_tags(self, project_id: str) -> List[Dict[str, Any]]:
        """List tags with the commit each one points at.

        Only lightweight tags are created by this service, so ``%(objectname)``
        is the commit hash directly — no annotated-tag peeling needed.
        """
        stdout, stderr, rc = self._run_git(project_id, [
            "for-each-ref", "refs/tags",
            "--format=%(refname:short)%09%(objectname)",
        ])
        if rc != 0:
            raise FileSystemError(f"Failed to list tags: {stderr}", code="INTERNAL_ERROR")

        tags = []
        for line in stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or not parts[0].strip():
                continue
            commit_hash = parts[1].strip()
            tags.append({
                "name": parts[0].strip(),
                "commit": commit_hash,
                "short_hash": commit_hash[:7],
            })
        return tags

    def create_tag(self, project_id: str, name: str, commit: str) -> Dict[str, Any]:
        """Create a lightweight tag pointing at a commit."""
        name = _validate_tag_name(name)
        _validate_commit_hash(commit)
        stdout, stderr, rc = self._run_git(project_id, ["tag", name, commit])
        if rc != 0:
            if "already exists" in stderr:
                raise FileSystemError(f"Tag already exists: {name}", code="TAG_EXISTS", status_code=409)
            raise FileSystemError(f"Failed to create tag: {stderr}", code="INTERNAL_ERROR")
        return {"success": True, "name": name, "commit": commit}

    def delete_tag(self, project_id: str, name: str) -> Dict[str, Any]:
        """Delete a tag. Commits the tag pointed at are untouched."""
        name = _validate_tag_name(name)
        stdout, stderr, rc = self._run_git(project_id, ["tag", "-d", name])
        if rc != 0:
            if "not found" in stderr:
                raise FileSystemError(f"Tag not found: {name}", code="TAG_NOT_FOUND", status_code=404)
            raise FileSystemError(f"Failed to delete tag: {stderr}", code="INTERNAL_ERROR")
        return {"success": True, "name": name}


git_service = GitService()
