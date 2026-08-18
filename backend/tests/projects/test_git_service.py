import json
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

import app.core.config as config_module
import app.services.git_service as git_module
from app.core.exceptions import FileMissingError, FileSystemError, ValidationError
from app.services.git_service import GitService, SNAPSHOT_MESSAGE_PREFIX


def _decode_snapshot_subject(subject):
    assert subject.startswith(SNAPSHOT_MESSAGE_PREFIX)
    return json.loads(unquote(subject.removeprefix(SNAPSHOT_MESSAGE_PREFIX)))


def test_snapshot_message_allocates_three_names_across_categories():
    changes = {
        "added": ["a.md", "b.md"],
        "deleted": ["old.tex"],
        "modified": ["main.md"],
    }

    assert _decode_snapshot_subject(GitService._format_snapshot_message(changes)) == {
        "added": {"names": ["a.md"], "total": 2},
        "deleted": {"names": ["old.tex"], "total": 1},
        "modified": {"names": ["main.md"], "total": 1},
    }


def test_snapshot_message_fills_remaining_slots_by_category_order():
    changes = {
        "added": ["a.md", "b.md"],
        "deleted": [],
        "modified": ["m1.md", "m2.md", "m3.md", "m4.md", "m5.md"],
    }

    assert _decode_snapshot_subject(GitService._format_snapshot_message(changes)) == {
        "added": {"names": ["a.md", "b.md"], "total": 2},
        "modified": {"names": ["m1.md"], "total": 5},
    }


def test_commit_treats_no_changes_stdout_as_noop():
    service = GitService()
    calls = []

    def fake_run_git(project_id, args, as_binary=False):
        calls.append(args)
        if args[:2] == ["config", "user.name"]:
            return "", "", 0
        if args[:2] == ["config", "user.email"]:
            return "", "", 0
        if args[:1] == ["commit"]:
            return "On branch main\nnothing to commit, working tree clean\n", "", 1
        raise AssertionError(f"unexpected git args: {args}")

    service._run_git = fake_run_git

    assert service.commit("p1", "Auto-snapshot") == {
        "success": False,
        "reason": "no changes",
    }
    assert ["rev-parse", "HEAD"] not in calls


@pytest.mark.timeout(10)
def test_init_git_ignores_internal_sigma_and_commits_template_files(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = tmp_path / project_id
    project_path.mkdir()
    (project_path / "main.md").write_text("# Title\n", encoding="utf-8")
    internal_dir = project_path / ".SiGMA"
    internal_dir.mkdir()
    (internal_dir / "project_data.db").write_text("internal", encoding="utf-8")

    assert service.init_git(project_id) is True

    gitignore = (project_path / ".gitignore").read_text(encoding="utf-8")
    assert ".SiGMA/" in gitignore
    assert ".upload_*" in gitignore

    stdout, stderr, rc = service._run_git(project_id, ["ls-tree", "-r", "--name-only", "HEAD"])
    assert rc == 0, stderr
    tracked_files = set(stdout.splitlines())
    assert "main.md" in tracked_files
    assert ".gitignore" in tracked_files
    assert ".SiGMA/project_data.db" not in tracked_files


def test_snapshot_zip_uses_temp_file_outside_project_and_cleans_up(tmp_path, monkeypatch):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_path = tmp_path / "project1"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    archive_path = tmp_path / "snapshot-test.zip"
    seen_output_paths = []

    def fake_mkstemp(prefix, suffix):
        fd = os.open(archive_path, os.O_CREAT | os.O_RDWR)
        return fd, str(archive_path)

    def fake_run(args, capture_output, timeout):
        output_path = args[args.index("--output") + 1]
        seen_output_paths.append(output_path)
        with open(output_path, "wb") as archive:
            archive.write(b"zip-data")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(git_module.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(git_module.subprocess, "run", fake_run)

    assert service.get_snapshot_zip("project1", "HEAD") == b"zip-data"
    assert seen_output_paths == [str(archive_path)]
    assert not archive_path.exists()
    assert not any(project_path.glob(".tmp_snapshot_*.zip"))


@pytest.mark.timeout(30)
def test_commit_files_and_blob_return_real_non_ascii_paths(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = tmp_path / project_id
    project_path.mkdir()
    chinese_name = "你是一个一个一个.md"
    tab_name = "str\tange.md"
    (project_path / chinese_name).write_text("# 啊啊啊啊啊\n", encoding="utf-8")
    (project_path / tab_name).write_text("tab name\n", encoding="utf-8")

    assert service.init_git(project_id) is True
    root_commit = service.get_log(project_id, 1)[0]["hash"]

    # Root commit must list its files; paths must be the real names, not the
    # C-quoted form git emits by default.
    files = {f["path"]: f for f in service.get_commit_files(project_id, root_commit)}
    assert chinese_name in files
    assert files[chinese_name]["status"] == "A"
    assert tab_name in files

    blob = service.get_blob(project_id, chinese_name, root_commit)
    assert blob["success"] is True
    assert blob["content"] == "# 啊啊啊啊啊\n"
    assert service.get_file_history(project_id, chinese_name)


@pytest.mark.timeout(30)
def test_blob_raw_returns_exact_bytes_for_text_and_binary(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = tmp_path / project_id
    project_path.mkdir()
    text_bytes = "# 你好\n".encode("utf-8")
    binary_bytes = bytes([0x00, 0xFF, 0x10, 0x42]) + b"\n"
    (project_path / "doc.md").write_bytes(text_bytes)
    (project_path / "img.bin").write_bytes(binary_bytes)

    assert service.init_git(project_id) is True
    commit = service.get_log(project_id, 1)[0]["hash"]

    raw = service.get_blob_raw(project_id, "doc.md", commit)
    assert raw["name"] == "doc.md"
    assert raw["content"] == text_bytes

    raw = service.get_blob_raw(project_id, "img.bin", commit)
    assert raw["name"] == "img.bin"
    assert raw["content"] == binary_bytes

    with pytest.raises(FileMissingError):
        service.get_blob_raw(project_id, "missing.md", commit)


@pytest.mark.timeout(30)
def test_commit_files_reports_renamed_non_ascii_file_as_modified(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = tmp_path / project_id
    project_path.mkdir()
    old_name = "你是一个一个一个.md"
    new_name = "いいよ！こいよ.md"
    (project_path / old_name).write_text("content\n", encoding="utf-8")

    service.init_git(project_id)
    parent = service.get_log(project_id, 1)[0]["hash"]

    (project_path / old_name).rename(project_path / new_name)
    service.stage_all(project_id)
    message = service.build_staged_snapshot_message(project_id)
    assert service.commit(project_id, message)["success"] is True

    changes = _decode_snapshot_subject(message)
    assert changes["modified"]["names"] == [new_name]

    commit = service.get_log(project_id, 1)[0]["hash"]
    assert service.get_commit_files(project_id, commit, parent_commit=parent) == [
        {"path": new_name, "name": new_name, "status": "M"},
    ]
    assert service.get_blob(project_id, new_name, commit)["content"] == "content\n"


@pytest.mark.timeout(30)
def test_blob_falls_back_to_parent_for_file_deleted_in_commit(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = tmp_path / project_id
    project_path.mkdir()
    chinese_name = "中文测试.md"
    (project_path / chinese_name).write_text("final content\n", encoding="utf-8")

    service.init_git(project_id)
    parent = service.get_log(project_id, 1)[0]["hash"]

    (project_path / chinese_name).unlink()
    service.stage_all(project_id)
    assert service.commit(project_id, "delete file")["success"] is True
    commit = service.get_log(project_id, 1)[0]["hash"]

    assert service.get_commit_files(project_id, commit, parent_commit=parent) == [
        {"path": chinese_name, "name": chinese_name, "status": "D"},
    ]

    # The file has no blob at the deleting commit; the preview must show the
    # content as it was just before deletion.
    blob = service.get_blob(project_id, chinese_name, commit)
    assert blob["success"] is True
    assert blob["content"] == "final content\n"

    with pytest.raises(FileMissingError):
        service.get_blob(project_id, "不存在的文件.md", commit)


def _init_real_repo(service: GitService, tmp_path, project_id="project1") -> Path:
    """Create a real initialized repo under tmp_path and return its path."""
    project_path = tmp_path / project_id
    project_path.mkdir()
    (project_path / "main.md").write_text("# Title\n", encoding="utf-8")
    assert service.init_git(project_id) is True
    return project_path


def test_create_tag_rejects_invalid_names_before_running_git():
    service = GitService()

    def fail_run_git(*args, **kwargs):
        raise AssertionError("validation must reject the name before git runs")

    service._run_git = fail_run_git
    invalid_names = [
        "", "  ", "spa ce", "-option", "--force", "a..b", "v1.lock",
        "a/b", "x" * 65, "尾部空白 ",
    ]
    for name in invalid_names:
        with pytest.raises((ValidationError, FileSystemError)):
            service.create_tag("p1", name, "0" * 40)

    with pytest.raises(FileSystemError):
        service.create_tag("p1", "valid-name", "not-a-hash!")


@pytest.mark.timeout(30)
def test_tag_crud_on_real_repo(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    _init_real_repo(service, tmp_path, project_id)
    head = service.get_log(project_id, 1)[0]["hash"]

    assert service.list_tags(project_id) == []

    created = service.create_tag(project_id, "milestone-1", head)
    assert created == {"success": True, "name": "milestone-1", "commit": head}
    assert [(t["name"], t["commit"], t["short_hash"]) for t in service.list_tags(project_id)] == [
        ("milestone-1", head, head[:7]),
    ]

    with pytest.raises(FileSystemError) as exc_info:
        service.create_tag(project_id, "milestone-1", head)
    assert exc_info.value.code == "TAG_EXISTS"

    assert service.delete_tag(project_id, "milestone-1") == {"success": True, "name": "milestone-1"}
    assert service.list_tags(project_id) == []

    with pytest.raises(FileSystemError) as exc_info:
        service.delete_tag(project_id, "milestone-1")
    assert exc_info.value.code == "TAG_NOT_FOUND"


@pytest.mark.timeout(30)
def test_create_snapshot_commit_commits_staged_changes_and_reports_clean_tree(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(config_module, "SIGMA_DIR", tmp_path / "locks")
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = _init_real_repo(service, tmp_path, project_id)

    # init_git already committed everything — a clean tree reports no changes.
    assert service.create_snapshot_commit(project_id)["success"] is False

    (project_path / "main.md").write_text("# Title\n\nNew paragraph.\n", encoding="utf-8")
    result = service.create_snapshot_commit(project_id)
    assert result["success"] is True
    head = service.get_log(project_id, 1)
    assert head[0]["hash"].startswith(result["commit"])
    assert head[0]["message"].startswith(SNAPSHOT_MESSAGE_PREFIX)

    assert service.create_snapshot_commit(project_id)["success"] is False


@pytest.mark.timeout(30)
def test_commit_files_covers_non_adjacent_commit_range(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "project1"
    project_path = _init_real_repo(service, tmp_path, project_id)
    c1 = service.get_log(project_id, 1)[0]["hash"]

    (project_path / "notes.md").write_text("notes v1\n", encoding="utf-8")
    service.create_snapshot_commit(project_id)
    c2 = service.get_log(project_id, 1)[0]["hash"]

    (project_path / "main.md").write_text("# Title (edited)\n", encoding="utf-8")
    service.create_snapshot_commit(project_id)
    c3 = service.get_log(project_id, 1)[0]["hash"]

    # The c1..c3 range aggregates changes from both intermediate commits.
    files = {f["path"]: f["status"] for f in service.get_commit_files(project_id, c3, parent_commit=c1)}
    assert files == {"notes.md": "A", "main.md": "M"}

    # An edit followed by a full revert cancels out of the net range diff.
    (project_path / "main.md").write_text("# Title\n", encoding="utf-8")
    service.create_snapshot_commit(project_id)
    c4 = service.get_log(project_id, 1)[0]["hash"]
    files = {f["path"]: f["status"] for f in service.get_commit_files(project_id, c4, parent_commit=c1)}
    assert files == {"notes.md": "A"}
