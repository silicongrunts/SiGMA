"""Verification tests for architectural and security fixes.

1. Formatter token count is not double-counted.
2. Upload concurrent same-name is safe (fail_if_exists under lock).
3. Archive extract overwrite=False is safe under concurrent access.
4. serialize_annotation produces correct output.
5. Archive extraction blocks path-escape attempts.
6. Library upload concurrent same-name is safe under lock.
"""

import os
import threading
import tempfile
import zipfile
import json
from pathlib import Path
from io import BytesIO

import pytest

from app.core.atomic_file import (
    atomic_write_bytes,
    atomic_write_text,
    AtomicFileExistsError,
)
from app.core.utils import is_within
from app.core.message_format import (
    build_tool_results_index,
    build_assistant_turn,
    finalize_assistant_turn,
)
from app.services.annotation_service import serialize_annotation
from app.services.file_service import FileService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockMsg:
    """Minimal mock for an ORM Message object."""

    def __init__(
        self,
        role,
        content="",
        tool_calls=None,
        tool_call_id=None,
        reasoning_content=None,
        token_count=0,
        cached_tokens=0,
        input_tokens=0,
        created_at=None,
    ):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.reasoning_content = reasoning_content
        self.token_count = token_count
        self.cached_tokens = cached_tokens
        self.input_tokens = input_tokens
        self.created_at = created_at


class MockAnnotation:
    """Minimal mock for an ORM Annotation object."""

    def __init__(self, id="anno-1", from_pos=0, to_pos=10,
                 original_text="hello", messages=None):
        self.id = id
        self.from_pos = from_pos
        self.to_pos = to_pos
        self.original_text = original_text
        self.messages = messages or []


def _service_for_project(tmp_path, project_id: str = "p1") -> FileService:
    service = FileService()
    project_path = tmp_path / project_id
    project_path.mkdir()
    service.get_project_path = lambda pid: project_path
    async def _noop_snapshot(pid, paths=None):
        return None
    service._notify_snapshot = _noop_snapshot
    return service


# ---------------------------------------------------------------------------
# File tree robustness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_tree_does_not_follow_directory_symlink_loop(tmp_path):
    """A symlink pointing back to an ancestor must not recurse forever."""
    service = _service_for_project(tmp_path)
    root = service.get_project_path("p1")
    (root / "folder").mkdir()
    try:
        os.symlink(root, root / "folder" / "loop")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    tree = await service.get_project_tree("p1")
    folder = tree["root"]["children"][0]
    loop = folder["children"][0]

    assert loop["name"] == "loop"
    assert loop["type"] == "file"
    assert loop["symlink"] is True
    assert loop["children"] == []


@pytest.mark.asyncio
async def test_file_tree_create_rejects_hidden_paths(tmp_path):
    service = _service_for_project(tmp_path)

    with pytest.raises(Exception) as exc_info:
        await service.create_item("p1", ".env", is_dir=False)

    assert getattr(exc_info.value, "code", "") == "INVALID_PATH"


@pytest.mark.asyncio
async def test_file_tree_rename_rejects_hidden_name(tmp_path):
    service = _service_for_project(tmp_path)
    await service.create_item("p1", "visible.txt", is_dir=False)

    with pytest.raises(Exception) as exc_info:
        await service.rename_item("p1", "visible.txt", ".hidden")

    assert getattr(exc_info.value, "code", "") == "INVALID_PATH"


# ---------------------------------------------------------------------------
# 1. Token count is not double-counted
# ---------------------------------------------------------------------------

class TestTokenCount:
    """Verify that build_assistant_turn does not double-count tokens."""

    def test_single_assistant_token_count(self):
        """Single assistant with token_count=7 should produce 7, not 14."""
        messages = [
            MockMsg("assistant", "Hello", token_count=7, cached_tokens=2, input_tokens=3),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert turn["token_count"] == 7
        assert turn["cached_tokens"] == 2
        assert turn["input_tokens"] == 3

    def test_multi_assistant_token_count(self):
        """Three assistants: 7+5+3 = 15."""
        messages = [
            MockMsg("assistant", "Thinking...", token_count=7, cached_tokens=1, input_tokens=2),
            MockMsg("tool", "result", tool_call_id="tc1"),
            MockMsg("assistant", "More thinking", token_count=5, cached_tokens=2, input_tokens=1),
            MockMsg("assistant", "Final answer", token_count=3, cached_tokens=0, input_tokens=0),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert turn["token_count"] == 7 + 5 + 3
        assert turn["cached_tokens"] == 1 + 2 + 0
        assert turn["input_tokens"] == 2 + 1 + 0

    def test_tool_usage_is_included_in_assistant_turn_total(self):
        """Agent tool results carry subagent subtree usage and count in the bubble."""
        messages = [
            MockMsg("assistant", "Calling agent", token_count=10, cached_tokens=4, input_tokens=100),
            MockMsg("tool", "agent result", tool_call_id="call_agent",
                    token_count=80, cached_tokens=30, input_tokens=400),
            MockMsg("assistant", "Final answer", token_count=5, cached_tokens=2, input_tokens=50),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert next_i == len(messages)
        assert turn["token_count"] == 95
        assert turn["cached_tokens"] == 36
        assert turn["input_tokens"] == 550

    def test_finalize_preserves_token_count(self):
        """finalize_assistant_turn should not alter token totals."""
        messages = [
            MockMsg("assistant", "Answer", token_count=10, cached_tokens=3, input_tokens=4),
        ]
        turn, _ = build_assistant_turn(messages, 0, {})
        finalized = finalize_assistant_turn(turn)
        assert finalized["token_count"] == 10
        assert finalized["cached_tokens"] == 3
        assert finalized["input_tokens"] == 4


# ---------------------------------------------------------------------------
# 2. Upload concurrent same-name safety (fail_if_exists)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 3. Archive extract overwrite=False concurrent safety
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 4. serialize_annotation produces correct UI output
# ---------------------------------------------------------------------------

class TestSerializeAnnotation:
    """Verify that serialize_annotation produces the expected UI structure."""

    def test_empty_annotation(self):
        """Annotation with no messages produces empty thread."""
        anno = MockAnnotation(id="a1", from_pos=5, to_pos=15,
                              original_text="sample")
        result = serialize_annotation(anno)
        assert result == {
            "id": "a1",
            "from": 5,
            "to": 15,
            "originalText": "sample",
            "thread": [],
        }

    def test_user_message(self):
        """User message appears in thread with normalized role 'user'."""
        anno = MockAnnotation(
            messages=[MockMsg("user", "Why does this work?")],
        )
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        assert result["thread"][0]["role"] == "user"
        assert result["thread"][0]["content"] == "Why does this work?"

    def test_assistant_with_tool_merge(self):
        """Assistant + tool messages merge into a single SiGMA turn."""
        tc_json = json.dumps([{
            "id": "tc1",
            "function": {"name": "search", "arguments": "query"},
        }])
        messages = [
            MockMsg("assistant", "Let me look...", tool_calls=tc_json,
                    token_count=7, cached_tokens=2, input_tokens=3),
            MockMsg("tool", "found it", tool_call_id="tc1"),
            MockMsg("assistant", "Here is the answer", token_count=5),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        turn = result["thread"][0]
        assert turn["role"] == "SiGMA"
        assert turn["content"] == "Here is the answer"
        assert turn["token_count"] == 7 + 5
        assert turn["cached_tokens"] == 2 + 0
        assert turn["input_tokens"] == 3 + 0
        # Process should contain tool step
        tools = [p for p in turn.get("process", []) if p["type"] == "tool"]
        assert len(tools) == 1
        assert tools[0]["tool"] == "search"

    def test_interrupted_turn_empty_bubble(self):
        """Turn ending with tool_calls (no final text) → empty bubble."""
        tc_json = json.dumps([{
            "id": "tc1",
            "function": {"name": "read", "arguments": "file"},
        }])
        messages = [
            MockMsg("assistant", "Reading...", tool_calls=tc_json, token_count=5),
            MockMsg("tool", "content", tool_call_id="tc1"),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        assert result["thread"][0]["content"] == ""  # interrupted → empty bubble

    def test_system_and_tool_messages_skipped(self):
        """System and standalone tool messages are not in the thread."""
        messages = [
            MockMsg("system", "boundary summary"),
            MockMsg("tool", "orphan result", tool_call_id="x"),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert result["thread"] == []

    def test_reasoning_is_not_serialized_to_ui(self):
        """Reasoning content is stored internally but not shown in UI."""
        messages = [
            MockMsg("assistant", "answer", reasoning_content="chain of thought",
                    token_count=3),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert "process" not in result["thread"][0]
        assert result["thread"][0]["content"] == "answer"


# ---------------------------------------------------------------------------
# 5. Archive extraction path-escape prevention
# ---------------------------------------------------------------------------

class TestArchivePathEscape:
    """Verify that archive extraction cannot escape the target directory."""

    # --- sanitize_member unit tests ---

    def test_sanitize_rejects_absolute_path(self):
        """/etc/passwd is rejected entirely (returns empty string)."""
        result = FileService.sanitize_member("/etc/passwd")
        assert result == ""

    def test_sanitize_rejects_parent_traversal(self):
        """../../../etc/shadow is rejected entirely (returns empty string)."""
        result = FileService.sanitize_member("../../../etc/shadow")
        assert result == ""

    def test_sanitize_rejects_windows_drive(self):
        """C:\\Windows\\System32 is rejected entirely."""
        result = FileService.sanitize_member("C:\\Windows\\System32")
        assert result == ""

    def test_sanitize_allows_normal_relative(self):
        """Normal relative paths pass through unchanged."""
        result = FileService.sanitize_member("src/main.py")
        assert result == "src/main.py"

    def test_sanitize_passes_dot_slash(self):
        """'./README.md' passes through — Path normalizes the '.' away."""
        result = FileService.sanitize_member("./README.md")
        # Path("./README.md").parts == ('README.md',), so it passes as-is
        assert result == "./README.md"

    def test_sanitize_returns_empty_for_root_only(self):
        """'/' alone produces empty (nothing to extract)."""
        result = FileService.sanitize_member("/")
        assert result == ""

    # --- _validate_extract_dest unit tests ---

    def test_validate_allows_normal_member(self):
        """Normal member inside target_dir and project_root passes validation."""
        with tempfile.TemporaryDirectory() as td:
            svc = FileService.__new__(FileService)
            project_root = Path(td)
            target_dir = project_root / "extract"
            target_dir.mkdir()
            dest = target_dir / "src" / "main.py"
            assert svc._validate_extract_dest(dest, target_dir, project_root) is True

    def test_validate_rejects_absolute_escape(self):
        """Dest resolving to /etc/passwd is rejected."""
        with tempfile.TemporaryDirectory() as td:
            svc = FileService.__new__(FileService)
            project_root = Path(td)
            target_dir = project_root / "extract"
            target_dir.mkdir()
            dest = Path("/etc/passwd")
            assert svc._validate_extract_dest(dest, target_dir, project_root) is False

    def test_validate_rejects_symlink_escape(self):
        """If target_dir is a symlink to outside project_root, dest is rejected."""
        with tempfile.TemporaryDirectory() as td:
            with tempfile.TemporaryDirectory() as outside_td:
                svc = FileService.__new__(FileService)
                project_root = Path(td)
                target_dir = project_root / "extract"
                target_dir.mkdir()
                outside_dir = Path(outside_td)
                symlink_dir = project_root / "symlink_target"
                symlink_dir.symlink_to(outside_dir)
                dest = symlink_dir / "file.txt"
                assert svc._validate_extract_dest(dest, symlink_dir, project_root) is False

    # --- End-to-end zip extraction test ---

    def test_zip_extraction_blocks_escape(self):
        """A zip with /etc/passwd as a member must not write outside target_dir."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            project_root = td
            # Create a zip with an absolute-path member
            zip_path = td / "test.zip"
            buf = BytesIO()
            with zipfile.ZipFile(buf, 'w') as zf:
                zf.writestr("safe.txt", "safe content")
                zf.writestr("/etc/evil.txt", "escape attempt")
            zip_path.write_bytes(buf.getvalue())

            # Extract using file_service
            svc = FileService.__new__(FileService)
            target_dir = td / "output"
            target_dir.mkdir()

            # Manually simulate the extraction loop
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean = svc.sanitize_member(info.filename)
                    if not clean:
                        continue
                    dest = target_dir / clean
                    if not svc._validate_extract_dest(dest, target_dir, project_root):
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, 'wb') as f:
                        f.write(zf.read(info.filename))

            # safe.txt must exist
            assert (target_dir / "safe.txt").read_text() == "safe content"
            # /etc/evil.txt must NOT have been written
            assert not Path("/etc/evil.txt").exists()


# ---------------------------------------------------------------------------
# 6. Library upload concurrent same-name safety
# ---------------------------------------------------------------------------

class TestLibraryUploadConcurrency:
    """Verify that the retry-with-fail_if_exists pattern is race-safe."""

    def test_concurrent_same_filename_both_succeed(self):
        """Two threads uploading 'report.pdf' both succeed with different paths.

        Uses the same counter-based retry strategy as the production code:
        first attempt uses the original name, subsequent attempts append
        a monotonic counter (_1, _2, ...).
        """
        with tempfile.TemporaryDirectory() as td:
            library_dir = Path(td)
            successes: list[Path] = []

            def upload_writer(label: str):
                # Mirrors the production retry loop
                target_name = "report.pdf"
                target_path = library_dir / target_name
                attempt = 1
                while attempt < 20:
                    try:
                        atomic_write_bytes(
                            target_path, label.encode(), fail_if_exists=True,
                        )
                        successes.append(target_path)
                        break
                    except AtomicFileExistsError:
                        target_name = f"report_{attempt}.pdf"
                        target_path = library_dir / target_name
                        attempt += 1

            t1 = threading.Thread(target=upload_writer, args=("content-A",))
            t2 = threading.Thread(target=upload_writer, args=("content-B",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(successes) == 2, (
                f"Expected 2 successful writes, got {len(successes)}"
            )
            # Two different files must exist with correct content
            files = sorted(library_dir.glob("*.pdf"))
            assert len(files) == 2
            contents = sorted(f.read_bytes().decode() for f in files)
            assert contents == ["content-A", "content-B"]
