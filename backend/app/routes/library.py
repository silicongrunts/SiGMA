"""
Library API routes - CRUD + search for library documents.
"""
from fastapi import APIRouter, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from typing import List, Optional
from app.services.library_service import library_service
from app.services.document_processing_service import document_processing_service
from app.models.requests import (
    CreateDocumentRequest,
    UpdateDocumentRequest,
    SearchRequest,
    CreateFolderRequest,
    MoveItemsRequest,
    BatchDeleteRequest,
)
from app.core.response import ok
from app.core.exceptions import (
    DocumentNotFoundError, DuplicateTitleError,
)

router = APIRouter(prefix="/library", tags=["library"])


@router.get("/{project_id}/documents")
async def list_documents(
    project_id: str,
    sort: str = Query("updated_at", pattern="^(title|updated_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    parent_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_status_summary: bool = Query(True),
):
    """List documents in the project library with sorting, folder filtering, and pagination."""
    result = await library_service.list_documents_paginated(
        project_id, sort=sort, order=order, parent_id=parent_id,
        limit=limit, offset=offset,
    )
    payload = {
        "documents": result["documents"],
        "total": result.get("total", 0),
    }
    if include_status_summary:
        payload["status_summary"] = await library_service.get_status_summary(project_id)
    return ok(payload)


@router.get("/{project_id}/status-summary")
async def get_status_summary(project_id: str):
    """Return lightweight global library processing status."""
    return ok(await library_service.get_status_summary(project_id))


@router.get("/{project_id}/documents/{doc_id}")
async def get_document(
    project_id: str,
    doc_id: str,
    include_content: bool = Query(True),
):
    """Get a single document by ID."""
    doc = await library_service.get_document(
        project_id,
        doc_id,
        include_content=include_content,
    )
    if not doc:
        raise DocumentNotFoundError(doc_id)
    return ok(doc)


@router.post("/{project_id}/documents")
async def create_document(project_id: str, data: CreateDocumentRequest):
    """Create a new library document."""
    doc = await library_service.create_document(project_id, data.model_dump(exclude_unset=True))
    return ok(doc)


@router.put("/{project_id}/documents/{doc_id}")
async def update_document(project_id: str, doc_id: str, data: UpdateDocumentRequest):
    """Update an existing document."""
    try:
        doc = await library_service.update_document(project_id, doc_id, data.model_dump(exclude_unset=True))
    except ValueError as e:
        raise DuplicateTitleError(str(e))
    if not doc:
        raise DocumentNotFoundError(doc_id)
    return ok(doc)


@router.delete("/{project_id}/documents/{doc_id}")
async def delete_document(project_id: str, doc_id: str):
    """Delete a document."""
    success = await library_service.delete_document(project_id, doc_id)
    if not success:
        raise DocumentNotFoundError(doc_id)
    return ok({"success": True})


@router.post("/{project_id}/search")
async def search_documents(project_id: str, data: SearchRequest):
    """Keyword search across library documents."""
    docs = await library_service.search_documents(
        project_id,
        data.query,
        parent_id=data.parent_id,
        limit=data.limit,
        offset=data.offset,
    )
    return ok({"documents": docs})


@router.post("/{project_id}/rag-search")
async def rag_search(project_id: str, data: SearchRequest):
    """Semantic search using RAG."""
    docs = await library_service.rag_search(
        project_id,
        data.query,
        parent_id=data.parent_id,
    )
    return ok({"documents": docs})


# ------------------------------------------------------------------
# File upload
# ------------------------------------------------------------------
@router.post("/{project_id}/upload")
async def upload_files(
    project_id: str,
    files: List[UploadFile] = File(...),
    folder_id: Optional[str] = Form(None),
    relative_paths: Optional[List[str]] = Form(None),
):
    """Upload one or more files for processing."""
    result = await document_processing_service.upload_files(
        project_id, files, folder_id=folder_id, relative_paths=relative_paths,
    )
    return ok({
        "success": True,
        "count": len(result["documents"]),
        "documents": result["documents"],
        "errors": result["errors"],
    })


# ------------------------------------------------------------------
# Reprocess failed documents
# ------------------------------------------------------------------
@router.post("/{project_id}/reprocess/{doc_id}")
async def reprocess_failed_document(project_id: str, doc_id: str):
    """Re-run processing for a single failed document."""
    result = await document_processing_service.reprocess_failed(project_id, doc_id)
    return ok(result)


@router.post("/{project_id}/extract-fields/{doc_id}")
async def extract_fields(project_id: str, doc_id: str):
    """Run AI field extraction synchronously and return the result."""
    result = await document_processing_service.extract_fields_sync(project_id, doc_id)
    return ok(result)


@router.post("/{project_id}/reprocess-all")
async def reprocess_all_failed_documents(project_id: str):
    """Re-run processing for all failed documents."""
    result = await document_processing_service.reprocess_all_failed(project_id)
    return ok(result)


# ------------------------------------------------------------------
# Download source file
# ------------------------------------------------------------------
@router.get("/{project_id}/documents/{doc_id}/download")
async def download_document(project_id: str, doc_id: str):
    """Download the source file of a document."""
    info = await library_service.get_download_file(project_id, doc_id)
    return FileResponse(path=str(info["path"]), filename=info["file_name"], media_type="application/octet-stream")


# ------------------------------------------------------------------
# Rebuild RAG Index
# ------------------------------------------------------------------
@router.post("/{project_id}/rebuild-index")
async def rebuild_index(project_id: str):
    """Rebuild RAG index for all documents in the current project."""
    result = await library_service.rebuild_index(project_id)
    return ok(result)


# ------------------------------------------------------------------
# Processing log
# ------------------------------------------------------------------
@router.get("/{project_id}/processing-log/{doc_id}")
async def get_processing_log(project_id: str, doc_id: str):
    """Get the processing log for a document."""
    log_info = await document_processing_service.get_processing_log(project_id, doc_id)
    if not log_info:
        raise DocumentNotFoundError(doc_id)
    return ok(log_info)


# ------------------------------------------------------------------
# Folders
# ------------------------------------------------------------------
@router.post("/{project_id}/folders")
async def create_folder(project_id: str, data: CreateFolderRequest):
    """Create a new folder."""
    try:
        folder = await library_service.create_folder(
            project_id, data.name, parent_id=data.parent_id,
        )
    except ValueError as e:
        raise DuplicateTitleError(str(e))
    return ok(folder)


# ------------------------------------------------------------------
# Batch operations
# ------------------------------------------------------------------
@router.post("/{project_id}/move")
async def move_items(project_id: str, data: MoveItemsRequest):
    """Move documents/folders to a target folder."""
    try:
        result = await library_service.move_items(project_id, data.ids, data.target_folder_id)
    except ValueError as e:
        raise DuplicateTitleError(str(e))
    return ok(result)


@router.post("/{project_id}/batch-delete")
async def batch_delete(project_id: str, data: BatchDeleteRequest):
    """Delete multiple documents/folders. Folders cascade-delete contents."""
    result = await library_service.batch_delete(project_id, data.ids)
    return ok(result)
