"""Library deletion keeps durable rows until side effects succeed."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import FileSystemError, ServiceException
from app.services.rag_service import rag_service
from app.services import library_service as library_module


class _LibraryRepo:
    def __init__(self, document):
        self.document = document
        self.deleted = False

    async def get_by_id(self, _doc_id):
        return None if self.deleted else self.document

    async def delete(self, _doc_id):
        self.deleted = True


class _Uow:
    repo = None

    def __init__(self, _project_id):
        self.library = self.repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.fixture
def delete_context(tmp_path, monkeypatch):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    source = library_dir / "source.txt"
    source.write_text("body", encoding="utf-8")
    document = SimpleNamespace(
        id="doc-1",
        is_folder=False,
        file_path=str(source),
        processing_status="completed",
    )
    repo = _LibraryRepo(document)
    _Uow.repo = repo
    monkeypatch.setattr(library_module, "UnitOfWork", _Uow)
    monkeypatch.setattr(
        library_module.settings.__class__,
        "get_sigma_path",
        lambda _settings, _project_id: tmp_path,
    )
    monkeypatch.setattr(library_module.library_service, "_cancel_processing", _noop)
    return repo, source


async def _noop(_project_id, _doc_id):
    return None


@pytest.mark.asyncio
async def test_rag_failure_preserves_row_and_file(delete_context, monkeypatch):
    repo, source = delete_context

    async def fail_remove(_project_id, _doc_id):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(rag_service, "remove_document", fail_remove)

    with pytest.raises(ServiceException) as exc_info:
        await library_module.library_service.delete_single("project", "doc-1")

    assert exc_info.value.code == "LIBRARY_DELETE_FAILED"
    assert repo.deleted is False
    assert source.exists()


@pytest.mark.asyncio
async def test_unlink_failure_preserves_row_and_retry_deletes(delete_context, monkeypatch):
    repo, source = delete_context
    monkeypatch.setattr(
        rag_service, "remove_document", _noop,
    )
    original_unlink = Path.unlink
    failed = True

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path == source and failed:
            failed = False
            raise OSError("unlink failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)

    with pytest.raises(FileSystemError) as exc_info:
        await library_module.library_service.delete_single("project", "doc-1")

    assert exc_info.value.code == "LIBRARY_DELETE_FAILED"
    assert repo.deleted is False
    assert source.exists()

    await library_module.library_service.delete_single("project", "doc-1")
    assert repo.deleted is True
    assert not source.exists()


@pytest.mark.asyncio
async def test_file_outside_library_is_rejected_before_delete(delete_context, monkeypatch, tmp_path):
    repo, source = delete_context
    outside = tmp_path / "outside.txt"
    outside.write_text("body", encoding="utf-8")
    repo.document.file_path = str(outside)
    monkeypatch.setattr(rag_service, "remove_document", _noop)

    with pytest.raises(FileSystemError) as exc_info:
        await library_module.library_service.delete_single("project", "doc-1")

    assert exc_info.value.code == "INVALID_LIBRARY_PATH"
    assert repo.deleted is False
    assert outside.exists()
    assert source.exists()
