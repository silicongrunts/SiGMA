import json
import re
import subprocess
import tempfile
import os
from urllib.parse import quote
from pathlib import Path
from typing import List, Optional, Dict, Any

from app.core.exceptions import (
    FileSystemError, ProjectNotFoundError, FileMissingError, ValidationError,
)
from app.core.atomic_file import ProjectFileLock
from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import is_within

logger = get_logger(__name__)

SNAPSHOT_CATEGORY_ORDER = ("added", "deleted", "modified")
SNAPSHOT_MESSAGE_PREFIX = "sigma:snapshot:v1:"

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


class GitService:
    def __init__(self):
        self.USERDATA_DIR = settings.USERDATA_DIR.resolve()

    def get_project_path(self, project_id: str) -> Path:
        path = (self.USERDATA_DIR / project_id).resolve()
        if not path.exists() or not is_within(path, self.USERDATA_DIR):
            raise ProjectNotFoundError(project_id)
        return path

    def _run_git(self, project_id: str, args: List[str], as_binary=False) -> tuple:
        """Run a git command. Returns (stdout, stderr, returncode).
        If as_binary=True, stdout is bytes instead of str.
        """
        project_path = self.get_project_path(project_id)
        try:
            result = subprocess.run(
                # quotepath=false: git C-quotes non-ASCII paths by default
                # (e.g. "gpt\347\273\231....md"); every caller needs the raw
                # path so it can be passed back to git or shown to the user.
                ["git", "-c", "core.quotepath=false",
                 "--git-dir", str(project_path / ".git"),
                 "-C", str(project_path)] + args,
                capture_output=True, timeout=30
            )
            if as_binary:
                return result.stdout, result.stderr.decode('utf-8', errors='replace'), result.returncode
            else:
                return result.stdout.decode('utf-8'), result.stderr.decode('utf-8', errors='replace'), result.returncode
        except subprocess.TimeoutExpired:
            raise FileSystemError("Git command timed out", code="INTERNAL_ERROR")

    def init_git(self, project_id: str) -> bool:
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
                    f"# LaTeX build artifacts (regenerated on compile)\n"
                    f"{artifact_lines}\n",
                    encoding="utf-8",
                )

            self._run_git(project_id, ["add", "-A"])
            self._run_git(project_id, ["commit", "-m", "Initial commit"])
            return True
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Git init failed: {e}", code="INTERNAL_ERROR")

    def stage_all(self, project_id: str) -> bool:
        """Stage all changes (git add -A). Used by auto-snapshot."""
        try:
            stdout, stderr, rc = self._run_git(project_id, ["add", "-A"])
            if rc != 0:
                raise FileSystemError(f"Git add -A failed: {stderr}", code="INTERNAL_ERROR")
            return True
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Stage all failed: {e}", code="INTERNAL_ERROR")

    def create_snapshot_commit(self, project_id: str) -> Dict[str, Any]:
        """Stage the working tree and commit it as one snapshot step.

        Shared by auto-snapshot and the manual-commit route. The index lock
        serializes overlapping callers (auto and manual, multiple tabs) so
        staged changes and commit messages can never interleave.
        """
        project_path = self.get_project_path(project_id)
        with ProjectFileLock(project_path / ".git" / "index"):
            self.stage_all(project_id)
            message = self.build_staged_snapshot_message(project_id)
            return self.commit(project_id, message)

    def get_snapshot_zip(self, project_id: str, commit: str) -> bytes:
        """Get a ZIP archive of the project at a specific commit using git archive.
        Returns the raw ZIP bytes."""
        project_path = self.get_project_path(project_id)
        fd, zip_path = tempfile.mkstemp(prefix="sigma-snapshot-", suffix=".zip")
        os.close(fd)
        try:
            result = subprocess.run(
                ["git", "--git-dir", str(project_path / ".git"),
                 "-C", str(project_path),
                 "archive", "--output", zip_path, commit],
                capture_output=True, timeout=30
            )
            if result.returncode != 0:
                raise FileSystemError(f"Git archive failed: {result.stderr.decode('utf-8', errors='replace')}", code="INTERNAL_ERROR")
            with open(zip_path, "rb") as f:
                zip_data = f.read()
            return zip_data
        except subprocess.TimeoutExpired:
            raise FileSystemError("Snapshot export timed out", code="INTERNAL_ERROR")
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

            stdout, stderr, rc = self._run_git(project_id, ["commit", "-m", message])
            if rc != 0:
                combined = f"{stdout}\n{stderr}".lower()
                if "nothing to commit" in combined or "no changes" in combined:
                    return {"success": False, "reason": "no changes"}
                raise FileSystemError(f"Commit failed: {stderr}", code="INTERNAL_ERROR")

            stdout, stderr, rc = self._run_git(project_id, ["rev-parse", "HEAD"])
            commit_hash = stdout.strip()
            return {"success": True, "commit": commit_hash[:7]}
        except FileSystemError:
            raise
        except Exception as e:
            raise FileSystemError(f"Commit failed: {e}", code="INTERNAL_ERROR")

    def build_staged_snapshot_message(self, project_id: str) -> str:
        """Build an auto-snapshot title from currently staged Git changes."""
        changes = self._get_staged_snapshot_changes(project_id)
        return self._format_snapshot_message(changes)

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
            name = Path(entry["path"]).name
            if len(name) > 10:
                name = name[:10] + "..."
            if name and name not in changes[category]:
                changes[category].append(name)
        return changes

    @staticmethod
    def _format_snapshot_message(changes: Dict[str, List[str]]) -> str:
        """Build a structured, locale-neutral auto-snapshot commit subject."""
        non_empty_categories = [
            category for category in SNAPSHOT_CATEGORY_ORDER
            if changes.get(category)
        ]
        if not non_empty_categories:
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
            args = ["log", f"-n", str(limit), f"--pretty={fmt}"]
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
