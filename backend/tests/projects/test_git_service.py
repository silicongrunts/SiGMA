import json
import os
import subprocess
from urllib.parse import unquote

import pytest

import app.services.git_service as git_module
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
