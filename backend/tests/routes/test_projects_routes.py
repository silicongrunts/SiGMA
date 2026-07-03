import pytest

from app.core.exceptions import FileSystemError
from app.routes import projects


class FakeUpload:
    def __init__(self, filename: str | None, content: bytes = b"", size: int | None = None):
        self.filename = filename
        self._content = content
        self.size = size
        self.read_called = False

    async def read(self):
        self.read_called = True
        return self._content


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_project_rejects_non_zip_filename():
    upload = FakeUpload("paper.txt", b"not zip")

    with pytest.raises(FileSystemError) as exc_info:
        await projects.import_project(upload)

    assert exc_info.value.code == "INVALID_ZIP_FILENAME"
    assert upload.read_called is False


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_project_rejects_missing_filename():
    upload = FakeUpload(None, b"")

    with pytest.raises(FileSystemError) as exc_info:
        await projects.import_project(upload)

    assert exc_info.value.code == "INVALID_ZIP_FILENAME"
    assert upload.read_called is False


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_project_rejects_declared_oversize_before_read():
    upload = FakeUpload(
        "project.zip",
        b"",
        size=projects.MAX_IMPORT_ZIP_BYTES + 1,
    )

    with pytest.raises(FileSystemError) as exc_info:
        await projects.import_project(upload)

    assert exc_info.value.code == "ZIP_TOO_LARGE"
    assert upload.read_called is False


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_project_rejects_actual_oversize_after_read(monkeypatch):
    max_size = 4
    upload = FakeUpload("project.zip", b"12345", size=None)
    monkeypatch.setattr(projects, "MAX_IMPORT_ZIP_BYTES", max_size)

    with pytest.raises(FileSystemError) as exc_info:
        await projects.import_project(upload)

    assert exc_info.value.code == "ZIP_TOO_LARGE"
    assert upload.read_called is True
