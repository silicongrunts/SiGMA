"""
Library Service - CRUD operations for library documents and folders.

All database access goes through UnitOfWork + LibraryRepository.
No direct SQLAlchemy or ORM model imports in this file.
"""
import re
from typing import List, Dict, Optional

from app.core.document_status import (
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
    STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    ACTIVE_STATUSES,
)
from app.core.utils import utcnow
from app.database.unit_of_work import UnitOfWork

from app.core.config import settings
from app.core.exceptions import RAGIndexModelMismatchError, DuplicateTitleError, ValidationError
from app.core.logging import get_logger
logger = get_logger(__name__)


class LibraryService:
    """Manages library documents for projects."""

    async def list_documents(
        self, project_id: str,
        parent_id: Optional[str] = None,
        sort: str = "updated_at", order: str = "desc",
        limit: Optional[int] = None, offset: Optional[int] = None,
    ) -> List[Dict]:
        """List documents in the project library with sorting, folder filtering, and pagination."""
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.list_all(
                parent_id=parent_id,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            )
            return [doc.to_summary_dict() for doc in docs]

    async def list_documents_paginated(
        self, project_id: str,
        parent_id: Optional[str] = None,
        sort: str = "updated_at", order: str = "desc",
        limit: Optional[int] = None, offset: Optional[int] = None,
    ) -> Dict:
        """List documents with pagination metadata including total count."""
        async with UnitOfWork(project_id) as uow:
            total = await uow.library.count_all(parent_id=parent_id)

            docs = await uow.library.list_all(
                parent_id=parent_id,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            )

        return {"documents": [doc.to_summary_dict() for doc in docs], "total": total}

    async def resolve_document(self, project_id: str, doc_id: str) -> tuple:
        """Resolve a document by exact ID.

        Returns (doc_orm, error_message). One of them is None.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if doc:
                return doc, None
            return None, f"No document found with ID '{doc_id}'"

    async def get_documents_by_ids(
        self, project_id: str, doc_ids: List[str]
    ) -> list:
        """Fetch multiple documents by exact IDs. Returns ORM objects."""
        async with UnitOfWork(project_id) as uow:
            return await uow.library.get_by_ids(doc_ids)

    async def create_library_document(self, project_id: str, **kwargs):
        """Create a library document. Returns the ORM object."""
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.create(**kwargs)
            return doc

    async def get_document(
        self,
        project_id: str,
        doc_id: str,
        include_content: bool = True,
    ) -> Optional[Dict]:
        """Get a single document by ID (full data including content)."""
        async with UnitOfWork(project_id) as uow:
            if not include_content:
                return await uow.library.get_summary_by_id(doc_id)
            doc = await uow.library.get_by_id(doc_id)
            return doc.to_dict() if doc else None

    async def get_ancestor_chain(self, project_id: str, doc_id: str) -> List[Dict]:
        """Return the folder breadcrumb chain from root to ``doc_id``'s parent.

        Each entry is ``{id, title}``, root first. Empty for a top-level doc
        or a missing doc. Used by chat citations to rebuild Library breadcrumbs
        before revealing a document.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.library.get_ancestor_chain(doc_id)

    async def get_document_file_info(self, project_id: str, doc_id: str) -> Optional[Dict]:
        """Get only file_path and file_name for download -- no content loaded."""
        async with UnitOfWork(project_id) as uow:
            return await uow.library.get_file_info(doc_id)

    async def get_download_file(self, project_id: str, doc_id: str) -> Dict:
        """Resolve download info for a document's source file.

        Returns dict with ``path`` (Path) and ``file_name`` (str).
        Raises ``DocumentNotFoundError`` if the document or its source file
        is missing.
        """
        from pathlib import Path
        from app.core.exceptions import DocumentNotFoundError, SourceFileNotFoundError

        file_info = await self.get_document_file_info(project_id, doc_id)
        if not file_info:
            raise DocumentNotFoundError(doc_id)

        file_path = file_info.get("file_path")
        file_name = file_info.get("file_name") or "document"

        if not file_path:
            raise SourceFileNotFoundError(doc_id)

        p = Path(file_path)
        if not p.exists():
            raise SourceFileNotFoundError(file_name)

        return {"path": p, "file_name": file_name}

    async def create_document(self, project_id: str, data: Dict) -> Dict:
        """Create a new library document."""
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.create(
                title=data.get("title", "Untitled"),
                description=data.get("description", ""),
                content=data.get("content", ""),
                source=data.get("source", ""),
                doc_type=data.get("doc_type", "text"),
                keywords=data.get("keywords"),
            )

        # Queue for RAG indexing via durable background task queue.
        if doc.content and doc.content.strip():
            async with UnitOfWork(project_id) as uow:
                await uow.library.update_processing_status(doc.id, status=STATUS_INDEXING)
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc.id)

        return doc.to_dict()

    async def update_document(self, project_id: str, doc_id: str, data: Dict) -> Optional[Dict]:
        """Update an existing document.

        data keys:
        - title, description, content, source, doc_type, keywords: direct field updates.
        - old_string + new_string (tool-style): atomic content replacement performed
          inside the transaction to close the TOCTOU window between read-count-replace
          and the final write.

        Folders only allow title updates; description and content edits (including
        old_string/new_string) are rejected. Folders never trigger RAG indexing.

        Raises DuplicateTitleError on name conflict, ValidationError on semantic
        violations (folder edits, non-unique old_string).
        """
        # Tool-style content replacement is popped here and handled below; it
        # must never reach repo.update() as a literal field name.
        old_string = data.pop("old_string", None) if isinstance(data, dict) else None
        new_string = data.pop("new_string", None) if isinstance(data, dict) else None

        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if not doc:
                return None

            is_folder = doc.is_folder

            # Folder-specific restrictions: only title is editable.
            if is_folder:
                forbidden = []
                if data.get("description"):
                    forbidden.append("description")
                if data.get("content"):
                    forbidden.append("content")
                if old_string is not None or new_string is not None:
                    forbidden.append("content (old_string/new_string)")
                if forbidden:
                    raise ValidationError(
                        "Folders only support title updates; rejected fields: "
                        + ", ".join(sorted(set(forbidden)))
                    )

            # Title conflict check (within same parent directory).
            new_title = data.get("title")
            if new_title and new_title != doc.title:
                duplicate = await uow.library.check_duplicate_title(
                    title=new_title,
                    parent_id=doc.parent_id,
                    exclude_id=doc_id,
                )
                if duplicate:
                    raise DuplicateTitleError(
                        f"A file or folder named '{new_title}' already exists in this location"
                    )

            # Atomic content replacement: read-count-replace-write inside the
            # same transaction so concurrent updates cannot slip in between.
            if old_string is not None or new_string is not None:
                if old_string is None or new_string is None:
                    raise ValidationError("old_string and new_string must be provided together")
                if old_string == new_string:
                    raise ValidationError("old_string and new_string are identical, nothing to change")
                content = doc.content or ""
                count = content.count(old_string)
                if count == 0:
                    raise ValidationError("specified text not found in document content")
                if count > 1:
                    raise ValidationError(
                        f"specified text found {count} times, please provide more context to make it unique"
                    )
                data["content"] = content.replace(old_string, new_string, 1)

            # If doc is being processed, cancel first so it can be re-processed
            needs_reprocess = doc.processing_status in ACTIVE_STATUSES
            if needs_reprocess:
                await self._cancel_processing(project_id, doc_id)

            doc = await uow.library.update(doc_id, data)

        # Side effects (outside transaction). Folders never need RAG indexing.
        if is_folder:
            pass
        elif needs_reprocess:
            # Non-completed doc modified → full re-process from scratch
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_document_process(
                project_id, doc_id, action="reprocess",
            )
        elif any(f in data and data[f] is not None for f in ("title", "description", "content")):
            # Completed doc content change → re-index only (no re-extraction)
            async with UnitOfWork(project_id) as uow:
                await uow.library.update_processing_status(doc_id, status=STATUS_INDEXING)
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc_id)

        return doc.to_dict()

    async def delete_document(self, project_id: str, doc_id: str) -> bool:
        """Delete a document/folder. Folders cascade-delete all children."""
        # Collect all IDs to delete
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if not doc:
                return False

            if doc.is_folder:
                all_ids = await uow.library.get_descendants(doc_id)
                all_ids.append(doc_id)
            else:
                all_ids = [doc_id]

        # Pre-cancel ALL processing tasks before any DB deletion.
        # This prevents CASCADE from deleting children before their
        # cancel signal is sent (which would waste running LLM tokens).
        for item_id in all_ids:
            await self._cancel_processing(project_id, item_id)

        # Now delete — cancel is already done so delete_single's cancel is a no-op
        for item_id in all_ids:
            await self.delete_single(project_id, item_id)

        await self._post_delete_cleanup(project_id)
        return True

    async def _cancel_processing(self, project_id: str, doc_id: str):
        """Signal running library background tasks to stop for a document.

        Two signals: DB status "cancelling" on the document + durable task
        cancellation in the background task table. Running workers observe both
        through periodic cancellation checks.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if not doc or doc.processing_status not in ACTIVE_STATUSES:
                return
            await uow.library.update_processing_status(
                doc_id, status=STATUS_CANCELLING,
                log_append="Cancelling processing...",
            )
        try:
            from app.services.background_task_service import background_task_service
            await background_task_service.cancel_document_tasks(project_id, doc_id)
        except Exception as exc:
            logger.warning("Failed to cancel background tasks for %s: %s", doc_id, exc, exc_info=True)

    async def delete_single(self, project_id: str, doc_id: str):
        """Delete a single document (cancel tasks + RAG + file + DB)."""
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if not doc:
                return

            # Signal running processing tasks to stop
            if not doc.is_folder and doc.processing_status in ACTIVE_STATUSES:
                await self._cancel_processing(project_id, doc_id)

            # Remove from RAG index
            if not doc.is_folder:
                try:
                    from app.services.rag_service import rag_service
                    await rag_service.remove_document(project_id, doc_id)
                except Exception as e:
                    logger.warning("Failed to remove document from RAG: %s", e, exc_info=True)

            # Delete source file from disk
            if doc.file_path:
                try:
                    from pathlib import Path
                    p = Path(doc.file_path)
                    if p.exists():
                        p.unlink()
                except Exception as e:
                    logger.warning("Failed to delete file %s: %s", doc.file_path, e, exc_info=True)

            await uow.library.delete(doc_id)

    async def _post_delete_cleanup(self, project_id: str):
        """Clean up orphan files and ChromaDB chunks after deletion."""
        async with UnitOfWork(project_id) as uow:
            all_docs = await uow.library.get_all()
        valid_doc_ids = {doc.id for doc in all_docs}
        valid_file_paths = {doc.file_path for doc in all_docs if doc.file_path}

        await self._cleanup_orphan_files(project_id, valid_file_paths)

        try:
            from app.services.rag_service import rag_service
            await rag_service.cleanup_orphans(project_id, valid_doc_ids)
        except Exception as e:
            logger.warning("Post-delete chunk cleanup failed: %s", e, exc_info=True)

    async def _cleanup_orphan_files(self, project_id: str, valid_file_paths: set):
        """Remove files in library directory not referenced by any DB record."""
        from pathlib import Path
        library_dir = settings.get_sigma_path(project_id) / "library"
        if not library_dir.exists():
            return

        removed = 0
        for f in library_dir.iterdir():
            if f.is_file() and str(f) not in valid_file_paths:
                try:
                    f.unlink()
                    removed += 1
                except Exception as e:
                    logger.warning("Failed to delete orphan file %s: %s", f, e, exc_info=True)
        if removed:
            logger.info(f"Cleaned {removed} orphan file(s) from library directory")

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    async def create_folder(self, project_id: str, name: str,
                            parent_id: Optional[str] = None) -> Dict:
        """Create a new folder. Raises if duplicate name in same directory."""
        async with UnitOfWork(project_id) as uow:
            # Check for duplicate folder name in the same directory
            duplicate = await uow.library.check_duplicate_title(
                title=name,
                parent_id=parent_id,
            )
            if duplicate:
                raise DuplicateTitleError(f"A folder named '{name}' already exists in this location")

            folder = await uow.library.create(
                title=name,
                content="",
                is_folder=True,
                parent_id=parent_id,
                processing_status=STATUS_COMPLETED,
            )
            return folder.to_summary_dict()

    async def move_items(self, project_id: str, ids: List[str],
                         target_folder_id: Optional[str]) -> Dict:
        """Move documents/folders to a target folder (None = root).

        Raises ValidationError if target does not exist or is not a folder, if a
        folder is moved into itself or its descendants, or on name conflict in
        the destination.
        """
        async with UnitOfWork(project_id) as uow:
            # Validate target existence (root is always valid).
            if target_folder_id:
                target = await uow.library.get_by_id(target_folder_id)
                if not target:
                    raise ValidationError(f"Target folder not found: {target_folder_id}")
                if not target.is_folder:
                    raise ValidationError(f"Target is not a folder: {target_folder_id}")

            # Prevent moving a folder into itself or its descendants
            if target_folder_id:
                folder_ids_to_move = []
                for item_id in ids:
                    doc = await uow.library.get_by_id(item_id)
                    if doc and doc.is_folder:
                        folder_ids_to_move.append(item_id)

                if folder_ids_to_move:
                    if target_folder_id in folder_ids_to_move:
                        raise ValidationError("Cannot move folder into itself")
                    # Check: is target a descendant of any folder being moved?
                    # This prevents creating circular parent-child chains.
                    for folder_id in folder_ids_to_move:
                        descendants = await uow.library.get_descendants(folder_id)
                        if target_folder_id in set(descendants):
                            raise ValidationError("Cannot move folder into its descendant")

            # Check for duplicate names in the target directory
            for item_id in ids:
                doc = await uow.library.get_by_id(item_id)
                if doc:
                    conflict = await uow.library.check_duplicate_title(
                        title=doc.title,
                        parent_id=target_folder_id,
                        exclude_id=doc.id,
                    )
                    if conflict:
                        raise DuplicateTitleError(f"An item named '{doc.title}' already exists in the target location")

            moved = await uow.library.move_items(ids, target_folder_id)

        return {"success": True, "moved": moved}

    async def batch_delete(self, project_id: str, ids: List[str]) -> Dict:
        """Delete multiple documents/folders. Folders cascade."""
        all_ids = set()
        async with UnitOfWork(project_id) as uow:
            for item_id in ids:
                doc = await uow.library.get_by_id(item_id)
                if doc:
                    all_ids.add(item_id)
                    if doc.is_folder:
                        all_ids.update(await uow.library.get_descendants(item_id))

        # Pre-cancel ALL processing tasks before any DB deletion
        for item_id in all_ids:
            await self._cancel_processing(project_id, item_id)

        deleted = 0
        for item_id in all_ids:
            await self.delete_single(project_id, item_id)
            deleted += 1

        await self._post_delete_cleanup(project_id)
        return {"success": True, "deleted": deleted}

    async def search_documents(
        self,
        project_id: str,
        query: str,
        parent_id: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """Keyword search across title, description, and content with snippet extraction."""
        allowed_ids = None
        if parent_id:
            async with UnitOfWork(project_id) as uow:
                allowed_ids = await uow.library.get_descendants(parent_id)
                allowed_ids.append(parent_id)

        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.search_keyword(
                query=query,
                allowed_ids=allowed_ids,
                limit=limit,
                offset=offset,
            )
            enriched = []
            for search_result in docs:
                doc = search_result["document"]
                matches = search_result["matches"]
                summary = doc.to_summary_dict()
                summary["search_matches"] = matches
                summary["search_snippets"] = [match["text"] for match in matches]
                enriched.append(summary)
            return enriched

    async def search_documents_paged(
        self,
        project_id: str,
        query: str,
        parent_id: str = None,
        limit: int = 10,
        offset: int = 0,
    ) -> Dict:
        """Keyword search with real pagination.

        Returns ``{"results": [...], "total": int}`` where ``total`` is the
        real match count (independent of limit/offset) so callers can show
        accurate pagination metadata. ``results`` follows the same enrichment
        shape as ``search_documents``.
        """
        allowed_ids = None
        if parent_id:
            async with UnitOfWork(project_id) as uow:
                allowed_ids = await uow.library.get_descendants(parent_id)
                allowed_ids.append(parent_id)

        async with UnitOfWork(project_id) as uow:
            total = await uow.library.count_search_keyword(
                query=query, allowed_ids=allowed_ids,
            )
            if total == 0:
                return {"results": [], "total": 0}
            docs = await uow.library.search_keyword(
                query=query,
                allowed_ids=allowed_ids,
                limit=limit,
                offset=offset,
            )
            enriched = []
            for search_result in docs:
                doc = search_result["document"]
                matches = search_result["matches"]
                summary = doc.to_summary_dict()
                summary["search_matches"] = matches
                summary["search_snippets"] = [match["text"] for match in matches]
                enriched.append(summary)
            return {"results": enriched, "total": total}

    async def rag_search(self, project_id: str, query: str, top_k: int | None = None, parent_id: str = None) -> List[Dict]:
        """Semantic search returning individual chunks. Same doc can appear multiple times."""
        try:
            from app.services.rag_service import rag_service
            top_k = top_k or settings.RAG_TOP_K

            # Pre-filter: only search within parent_id subtree
            allowed_doc_ids = None
            if parent_id:
                async with UnitOfWork(project_id) as uow:
                    allowed_doc_ids = await uow.library.get_descendants(parent_id)
                    allowed_doc_ids.append(parent_id)

            chunks = await rag_service.search(project_id, query, top_k, allowed_doc_ids)
            if not chunks:
                return []

            # Fetch parent documents for metadata
            doc_ids = list(set(c.doc_id for c in chunks))
            async with UnitOfWork(project_id) as uow:
                docs = await uow.library.get_by_ids(doc_ids)
                doc_map = {doc.id: doc for doc in docs}

            enriched = []
            for chunk in chunks:
                doc = doc_map.get(chunk.doc_id)
                if not doc:
                    continue
                summary = doc.to_summary_dict()
                summary["relevance_score"] = round(chunk.score, 4)
                summary["search_snippets"] = [chunk.chunk_text]
                summary["chunk_text"] = chunk.chunk_text
                summary["chunk_line_start"] = chunk.line_start
                enriched.append(summary)
            return enriched
        except RAGIndexModelMismatchError:
            raise
        except Exception as e:
            logger.warning("RAG search failed, falling back to keyword: %s", e, exc_info=True)
            return await self.search_documents(project_id, query, parent_id=parent_id)

    async def rebuild_index(self, project_id: str) -> Dict:
        """Rebuild RAG index for all documents in a project. Non-blocking.

        All documents with content are marked as "indexing" and enqueued
        as durable background tasks. The caller gets an immediate response
        without waiting for the actual indexing to complete.
        """
        # 1. Delete old ChromaDB collection so it will be recreated with current model
        from app.services.rag_service import rag_service
        await rag_service.reset_project_index(project_id)

        # 2. Get all docs with content + docs with active processing tasks
        doc_ids_to_reindex: List[str] = []
        active_doc_ids: List[str] = []
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.get_all()
            for doc in docs:
                if doc.content:
                    doc_ids_to_reindex.append(doc.id)
                if doc.processing_status in ACTIVE_STATUSES:
                    active_doc_ids.append(doc.id)

        # 3. Cancel all active processing/indexing tasks to prevent concurrent writes
        for doc_id in active_doc_ids:
            await self._cancel_processing(project_id, doc_id)

        # 4. Reset status to "indexing" and enqueue durable index tasks
        from app.services.background_task_service import background_task_service
        for doc_id in doc_ids_to_reindex:
            async with UnitOfWork(project_id) as uow:
                await uow.library.reset_processing(doc_id, status=STATUS_INDEXING)
            await background_task_service.enqueue_rag_index(project_id, doc_id)

        total = len(doc_ids_to_reindex)
        return {
            "success": True,
            "message": f"Rebuild started. {total} documents queued for indexing.",
            "total": total,
            "status": "queued",
        }

    def _extract_snippets(self, title: str, description: str, content: str,
                          query: str, context_chars: int = 60, max_snippets: int = 2) -> List[str]:
        """Extract text snippets around keyword matches."""
        if not query:
            return []
        try:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        except re.error:
            return []

        snippets = []
        for field_text in [title or "", description or "", content or ""]:
            if not field_text:
                continue
            for match in pattern.finditer(field_text):
                start = max(0, match.start() - context_chars)
                end = min(len(field_text), match.end() + context_chars)
                snippet = field_text[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(field_text):
                    snippet = snippet + "..."
                snippets.append(snippet)
                if len(snippets) >= max_snippets:
                    return snippets
        return snippets

    async def get_status_summary(self, project_id: str) -> Dict:
        """Return processing status counts and non-completed documents for a project."""
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.get_doc_status_summary()

        summary = {STATUS_PENDING: 0, STATUS_PROCESSING: 0, STATUS_INDEXING: 0,
                   STATUS_CANCELLING: 0, STATUS_COMPLETED: 0, STATUS_FAILED: 0}
        active_docs = []
        for d in docs:
            status = d["processing_status"]
            # Normalize pre-rename DB rows: "indexing_failed" was the value
            # used before the status constants were unified to STATUS_FAILED.
            # No code writes this value today, but existing project databases
            # may still contain it. Read-side normalization keeps these rows
            # visible in the summary instead of silently dropping them.
            if status == "indexing_failed":
                status = STATUS_FAILED
            summary[status] = summary.get(status, 0) + 1
            if status != STATUS_COMPLETED:
                active_docs.append(d)

        return {"summary": summary, "documents": active_docs}

    # ------------------------------------------------------------------
    # Status transitions — single entry points for state changes
    # ------------------------------------------------------------------

    async def mark_document_processing(self, project_id: str, doc_id: str) -> None:
        """Transition document to 'processing' state with started_at timestamp."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_processing_status(
                doc_id,
                status=STATUS_PROCESSING,
                started_at=utcnow(),
                log_append="Processing in progress...",
            )

    async def mark_document_indexing(self, project_id: str, doc_id: str,
                                      log_append: str = "Document processing done. Queued for RAG indexing.") -> None:
        """Transition document to 'indexing' state (ready for RAG)."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_processing_status(
                doc_id,
                status=STATUS_INDEXING,
                log_append=log_append,
            )

    async def mark_document_completed(self, project_id: str, doc_id: str) -> None:
        """Transition document to 'completed' state."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_processing_status(
                doc_id,
                status=STATUS_COMPLETED,
                completed_at=utcnow(),
            )

    async def mark_document_failed(self, project_id: str, doc_id: str,
                                     reason: str) -> None:
        """Transition document to 'failed' state with error message."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.mark_failed(doc_id, reason)

    async def append_processing_log(self, project_id: str, doc_id: str,
                                      message: str) -> None:
        """Append a timestamped log message to the document's processing log."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_processing_log(doc_id, message)

    async def update_document_content(self, project_id: str, doc_id: str,
                                        content: str) -> None:
        """Update document content in the database."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_content(doc_id, content)

    async def update_document_fields(self, project_id: str, doc_id: str,
                                       title: str = None,
                                       description: str = None,
                                       keywords: list = None) -> None:
        """Update document metadata fields (title, description, keywords)."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_fields(
                doc_id, title=title, description=description, keywords=keywords,
            )


library_service = LibraryService()
