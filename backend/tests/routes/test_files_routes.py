from types import SimpleNamespace

import pytest

from app.models.requests import FileContent, FileExtractRequest
from app.routes import files
from app.services.file_service import MAX_UI_READ_BYTES
from tests.factories.uploads import ChunkedUpload


@pytest.mark.route
@pytest.mark.asyncio
async def test_get_content_applies_ui_read_cap(monkeypatch):
    """The content route must pass the UI whole-read cap so oversized text
    files fail fast (FILE_TOO_LARGE) instead of streaming to the browser."""
    calls = {}

    async def read_file(project_id, path, max_bytes=None):
        calls["args"] = (project_id, path)
        calls["max_bytes"] = max_bytes
        return "hello"

    fake_service = SimpleNamespace(read_file=read_file, compute_hash=lambda text: "hash-1")
    monkeypatch.setattr(files, "file_service", fake_service)

    response = await files.get_content("project-1", "notes.md")

    assert calls["args"] == ("project-1", "notes.md")
    assert calls["max_bytes"] == MAX_UI_READ_BYTES
    assert response.body == b"hello"
    assert response.headers["X-Content-Hash"] == "hash-1"
    assert response.media_type == "text/plain"


@pytest.mark.route
@pytest.mark.asyncio
async def test_update_content_requires_expected_hash(monkeypatch):
    calls = {}

    async def write_file(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"path": args[1]}

    fake_service = SimpleNamespace(write_file=write_file)
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.update_content(
        "project-1",
        FileContent(path="paper.tex", content="body", force=True, hash="known"),
    )

    assert result["success"] is True
    assert calls["args"] == ("project-1", "paper.tex", "body")
    assert calls["kwargs"] == {
        "force": True,
        "expected_hash": "known",
        "require_expected_hash": True,
    }


@pytest.mark.route
@pytest.mark.asyncio
async def test_extract_archive_returns_conflicts_without_extracting(monkeypatch):
    calls = {"extract": 0}

    async def check_extract_conflicts(project_id, path):
        assert project_id == "project-1"
        assert path == "bundle.zip"
        return ["existing.txt"]

    async def extract_archive(*args, **kwargs):
        calls["extract"] += 1
        return {"ok": True}

    fake_service = SimpleNamespace(
        check_extract_conflicts=check_extract_conflicts,
        extract_archive=extract_archive,
    )
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.extract_archive(
        "project-1",
        FileExtractRequest(path="bundle.zip", overwrite=False, skip_conflicts=False),
    )

    assert result["success"] is True
    assert result["data"] == {"conflicts": ["existing.txt"]}
    assert calls["extract"] == 0


@pytest.mark.route
@pytest.mark.asyncio
async def test_extract_archive_skip_conflicts_bypasses_preflight(monkeypatch):
    calls = {"check": 0}

    async def check_extract_conflicts(*args, **kwargs):
        calls["check"] += 1
        return ["existing.txt"]

    async def extract_archive(project_id, path, overwrite=False):
        assert project_id == "project-1"
        assert path == "bundle.zip"
        assert overwrite is False
        return {"extracted": ["new.txt"]}

    fake_service = SimpleNamespace(
        check_extract_conflicts=check_extract_conflicts,
        extract_archive=extract_archive,
    )
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.extract_archive(
        "project-1",
        FileExtractRequest(path="bundle.zip", overwrite=False, skip_conflicts=True),
    )

    assert result["success"] is True
    assert result["data"] == {"extracted": ["new.txt"]}
    assert calls["check"] == 0


@pytest.mark.route
@pytest.mark.asyncio
async def test_upload_files_streams_upload_to_service(monkeypatch):
    """The route hands the raw upload stream to save_upload instead of
    buffering the body in memory; the service owns the size cap."""
    calls = {}

    async def save_upload(project_id, filename, file, path, overwrite=False):
        calls["args"] = (project_id, filename, file, path)
        calls["overwrite"] = overwrite
        return "data.bin"

    fake_service = SimpleNamespace(save_upload=save_upload)
    monkeypatch.setattr(files, "file_service", fake_service)

    upload = ChunkedUpload(b"payload", filename="data.bin")

    result = await files.upload_files("project-1", upload, path="docs", overwrite=True)

    assert result["success"] is True
    assert result["data"] == {"filename": "data.bin"}
    assert calls["args"] == ("project-1", "data.bin", upload, "docs")
    assert calls["overwrite"] is True


# ---------------------------------------------------------------------------
# Error translation (HTTP level): service exceptions must reach the client as
# the exception's status code with the unified error envelope
# {"request_id", "success", "error", "data"}.
# ---------------------------------------------------------------------------

@pytest.mark.route
@pytest.mark.asyncio
async def test_oversize_upload_translates_to_413_error_envelope(client, no_password, monkeypatch):
    """The upload size cap lives in the service: a body over the limit raises
    FileSystemError(FILE_TOO_LARGE, 413) (mirroring write_upload_bounded).
    The route must surface it as an HTTP 413 with the unified envelope and
    not swallow it into a 200."""
    from app.core.exceptions import FileSystemError

    async def save_upload(project_id, filename, file, path, overwrite=False):
        raise FileSystemError(
            "File exceeds the upload limit",
            code="FILE_TOO_LARGE",
            status_code=413,
        )

    monkeypatch.setattr(files, "file_service", SimpleNamespace(save_upload=save_upload))

    r = await client.post(
        "/api/v1/files/project-1/upload",
        files={"file": ("big.bin", b"x" * 16, "application/octet-stream")},
    )

    assert r.status_code == 413
    body = r.json()
    assert set(body) == {"request_id", "success", "error", "data"}
    assert body["success"] is False
    assert body["error"] == "File exceeds the upload limit"
    assert body["data"] is None
