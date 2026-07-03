from types import SimpleNamespace

import pytest

from app.core.exceptions import DocumentNotFoundError, DuplicateTitleError
from app.models.requests import CreateFolderRequest, UpdateDocumentRequest
from app.routes import library


@pytest.mark.route
@pytest.mark.asyncio
async def test_get_document_maps_missing_document_to_not_found(monkeypatch):
    async def get_document(project_id, doc_id, include_content=True):
        assert project_id == "project-1"
        assert doc_id == "missing"
        assert include_content is False
        return None

    monkeypatch.setattr(library, "library_service", SimpleNamespace(get_document=get_document))

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await library.get_document("project-1", "missing", include_content=False)

    assert exc_info.value.code == "DOCUMENT_NOT_FOUND"


@pytest.mark.route
@pytest.mark.asyncio
async def test_update_document_maps_duplicate_title(monkeypatch):
    async def update_document(*args, **kwargs):
        raise ValueError("duplicate title")

    monkeypatch.setattr(library, "library_service", SimpleNamespace(update_document=update_document))

    with pytest.raises(DuplicateTitleError):
        await library.update_document(
            "project-1",
            "doc-1",
            UpdateDocumentRequest(title="Existing"),
        )


@pytest.mark.route
@pytest.mark.asyncio
async def test_update_document_maps_missing_result_to_not_found(monkeypatch):
    async def update_document(project_id, doc_id, data):
        assert data == {"title": "Updated"}
        return None

    monkeypatch.setattr(library, "library_service", SimpleNamespace(update_document=update_document))

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await library.update_document(
            "project-1",
            "missing",
            UpdateDocumentRequest(title="Updated"),
        )

    assert exc_info.value.code == "DOCUMENT_NOT_FOUND"


@pytest.mark.route
@pytest.mark.asyncio
async def test_create_folder_maps_duplicate_title(monkeypatch):
    async def create_folder(*args, **kwargs):
        raise ValueError("duplicate folder")

    monkeypatch.setattr(library, "library_service", SimpleNamespace(create_folder=create_folder))

    with pytest.raises(DuplicateTitleError):
        await library.create_folder("project-1", CreateFolderRequest(name="Existing"))


@pytest.mark.route
@pytest.mark.asyncio
async def test_list_documents_can_skip_status_summary(monkeypatch):
    calls = {"summary": 0}

    async def list_documents_paginated(*args, **kwargs):
        return {"documents": [{"id": "doc-1"}], "total": 1}

    async def get_status_summary(*args, **kwargs):
        calls["summary"] += 1
        return {}

    monkeypatch.setattr(
        library,
        "library_service",
        SimpleNamespace(
            list_documents_paginated=list_documents_paginated,
            get_status_summary=get_status_summary,
        ),
    )

    result = await library.list_documents("project-1", include_status_summary=False)

    assert result["success"] is True
    assert result["data"] == {"documents": [{"id": "doc-1"}], "total": 1}
    assert calls["summary"] == 0
