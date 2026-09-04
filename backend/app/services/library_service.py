"""
Library Service - CRUD operations for library documents and folders.

All database access goes through UnitOfWork + LibraryRepository.
No direct SQLAlchemy or ORM model imports in this file.
"""
import asyncio
import time
from typing import List, Dict, Optional

from app.core.document_status import (
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
    STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    ACTIVE_STATUSES,
)
from app.core.utils import is_within, utcnow
from app.database.unit_of_work import UnitOfWork

from app.core.config import settings
from app.core.exceptions import (
    FileSystemError, RAGIndexModelMismatchError, DuplicateTitleError,
    ValidationError, ServiceException,
)
from app.core.logging import get_logger
from app.services.project_service import project_service
logger = get_logger(__name__)

# Files younger than this are never treated as orphans: a just-landed file
# is unreferenced until its DB row commits, and only the mtime bounds that
# window (in-flight upload temps live in the .uploading subdirectory, which
# cleanup never scans for deletions).
ORPHAN_FILE_GRACE_SECONDS = 3600

# Bounded wait for in-flight library task handlers to wind down before the
# destructive index reset; a handler past its last cancellation checkpoint
# may still write chunks into the collection being reset.
_REBUILD_DRAIN_TIMEOUT_SECONDS = 60.0
_REBUILD_DRAIN_POLL_SECONDS = 1.0


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
        content = data.get("content") or ""
        has_content = bool(content.strip())
        # The row is born in the status the pipeline actually starts from:
        # content documents go straight to "indexing" (the status the
        # maintenance sweep re-enqueues), so a crash after the create can
        # never strand a "completed" document whose chunks were never
        # written. A document without content has nothing to index.
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.create(
                title=data.get("title", "Untitled"),
                description=data.get("description", ""),
                content=content,
                source=data.get("source", ""),
                doc_type=data.get("doc_type", "text"),
                keywords=data.get("keywords"),
                processing_status=STATUS_INDEXING if has_content else STATUS_COMPLETED,
            )

        if has_content:
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc.id)

        project_service.touch_project(project_id)
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
        status_changed = False
        if is_folder:
            pass
        elif needs_reprocess:
            # Non-completed doc modified → full re-process from scratch. The
            # in-flight tasks were cancelled above, so put the document back
            # into the queue with the new revision: the reprocess task sees
            # status "pending" and the old task aborts on revision mismatch.
            from app.services.background_task_service import background_task_service
            async with UnitOfWork(project_id) as uow:
                current = await uow.library.get_by_id(doc_id)
                if current:
                    await uow.library.update_processing_status(
                        doc_id, status=STATUS_PENDING,
                        started_at=utcnow(),
                        expected_revision=current.revision,
                    )
            await background_task_service.enqueue_document_process(project_id, doc_id)
            status_changed = True
        elif any(f in data and data[f] is not None for f in ("title", "description", "content")):
            from app.services.background_task_service import background_task_service
            if doc.processing_status == STATUS_FAILED and not (doc.content or "").strip():
                # A failed conversion produced no content, so there is nothing
                # to re-index: the edit sends the document back through full
                # processing instead of silently completing an empty index.
                async with UnitOfWork(project_id) as uow:
                    current = await uow.library.get_by_id(doc_id)
                    if current:
                        await uow.library.update_processing_status(
                            doc_id, status=STATUS_PENDING,
                            started_at=utcnow(),
                            expected_revision=current.revision,
                        )
                await background_task_service.enqueue_document_process(project_id, doc_id)
            else:
                # Completed doc content change → re-index only (no re-extraction)
                async with UnitOfWork(project_id) as uow:
                    current = await uow.library.get_by_id(doc_id)
                    if current:
                        await uow.library.update_processing_status(
                            doc_id, status=STATUS_INDEXING,
                            expected_revision=current.revision,
                        )
                await background_task_service.enqueue_rag_index(project_id, doc_id)
            status_changed = True

        if status_changed:
            # The side-effect branches run in their own sessions; re-read so
            # the response reflects the post-transition status.
            async with UnitOfWork(project_id) as uow:
                refreshed = await uow.library.get_by_id(doc_id)
            if refreshed:
                doc = refreshed

        project_service.touch_project(project_id)
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
        cancellation in the background task table. Running tasks observe both
        through periodic cancellation checks.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            if not doc:
                return
            if doc.processing_status in ACTIVE_STATUSES:
                await uow.library.update_processing_status(
                    doc_id, status=STATUS_CANCELLING,
                    log_append="Cancelling processing...",
                    expected_revision=doc.revision,
                )
        try:
            from app.services.background_task_service import background_task_service
            await background_task_service.cancel_document_tasks(project_id, doc_id)
            if not await background_task_service.wait_for_document(project_id, doc_id):
                raise FileSystemError(
                    f"Document {doc_id} still has running processing work; retry later.",
                    code="DOCUMENT_DRAIN_TIMEOUT",
                )
        except Exception as exc:
            logger.warning("Failed to cancel background tasks for %s: %s", doc_id, exc, exc_info=True)
            raise

    async def delete_single(self, project_id: str, doc_id: str):
        """Delete a single document (cancel tasks + RAG + file + DB)."""
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        if not doc:
            return

        if not doc.is_folder:
            await self._cancel_processing(project_id, doc_id)

        if not doc.is_folder:
            try:
                from app.services.rag_service import rag_service
                await rag_service.remove_document(project_id, doc_id)
            except Exception as exc:
                raise ServiceException(
                    f"Could not remove document {doc_id} from the search index; retry later.",
                    code="LIBRARY_DELETE_FAILED", status_code=500,
                ) from exc

        if doc.file_path:
            from pathlib import Path
            library_dir = settings.get_sigma_path(project_id).joinpath("library").resolve()
            file_path = Path(doc.file_path).resolve()
            if not is_within(file_path, library_dir):
                raise FileSystemError(
                    f"Document file is outside the managed library directory: {doc.file_path}",
                    code="INVALID_LIBRARY_PATH",
                )
            if file_path.exists():
                if not file_path.is_file():
                    raise FileSystemError(
                        f"Document file is not a regular file: {doc.file_path}",
                        code="LIBRARY_DELETE_FAILED",
                    )
                try:
                    file_path.unlink()
                except OSError as exc:
                    raise FileSystemError(
                        f"Could not delete document file {doc_id}; retry later.",
                        code="LIBRARY_DELETE_FAILED",
                    ) from exc

        async with UnitOfWork(project_id) as uow:
            await uow.library.delete(doc_id)

        project_service.touch_project(project_id)

    async def _post_delete_cleanup(self, project_id: str):
        """Clean up orphan files and ChromaDB chunks after deletion."""
        async with UnitOfWork(project_id) as uow:
            all_docs = await uow.library.get_all()
        valid_file_paths = {doc.file_path for doc in all_docs if doc.file_path}

        await self._cleanup_orphan_files(project_id, valid_file_paths)

        try:
            from app.services.rag_service import rag_service

            async def valid_doc_ids() -> set:
                async with UnitOfWork(project_id) as uow:
                    docs = await uow.library.get_all()
                return {doc.id for doc in docs}

            await rag_service.cleanup_orphans(project_id, valid_doc_ids)
        except Exception as e:
            logger.warning("Post-delete chunk cleanup failed: %s", e, exc_info=True)

    async def cleanup_orphan_files(self, project_id: str) -> None:
        """Remove library files on disk that no document row points at.

        Public entry for the delete flow and the daily upkeep pass; the
        grace period and ``.uploading`` handling live in
        :meth:`_cleanup_orphan_files`.
        """
        async with UnitOfWork(project_id) as uow:
            all_docs = await uow.library.get_all()
        valid_file_paths = {doc.file_path for doc in all_docs if doc.file_path}
        await self._cleanup_orphan_files(project_id, valid_file_paths)

    async def _cleanup_orphan_files(self, project_id: str, valid_file_paths: set):
        """Remove files in library directory not referenced by any DB record.

        Unreferenced files younger than the grace period are kept (the DB row
        may not have committed yet); temp files of in-flight uploads live in
        the ``.uploading`` subdirectory and are only removed there once stale,
        so a crashed upload's temp cannot accumulate forever either.
        """
        from app.services.document_processing_service import document_processing_service

        library_dir = settings.get_sigma_path(project_id) / "library"
        if not library_dir.exists():
            return

        now = time.time()
        candidates = [
            f for f in library_dir.iterdir()
            if f.is_file() and str(f) not in valid_file_paths
        ]
        uploading_dir = library_dir / document_processing_service.UPLOADING_DIRNAME
        if uploading_dir.is_dir():
            candidates.extend(uploading_dir.iterdir())

        removed = 0
        for f in candidates:
            if not f.is_file():
                continue
            try:
                if now - f.stat().st_mtime < ORPHAN_FILE_GRACE_SECONDS:
                    continue
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

        project_service.touch_project(project_id)
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

        project_service.touch_project(project_id)
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
        limit: int = 50,
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
            folder_paths = await uow.library.get_folder_paths([s["id"] for s in enriched])
            for summary in enriched:
                summary["folder_path"] = folder_paths.get(summary["id"], "")
            return enriched

    async def search_documents_paged(
        self,
        project_id: str,
        query: str,
        parent_id: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict:
        """Keyword search with real pagination.

        Returns ``{"results": [...], "total": int}`` where ``total`` is the
        SQL candidate count (independent of limit/offset), an upper bound of
        the post-filtered matches, so callers can show pagination metadata
        without a content scan per page. ``results`` follows the same
        enrichment shape as ``search_documents``.
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
            folder_paths = await uow.library.get_folder_paths([s["id"] for s in enriched])
            for summary in enriched:
                summary["folder_path"] = folder_paths.get(summary["id"], "")
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
                folder_paths = await uow.library.get_folder_paths(doc_ids)

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
                summary["folder_path"] = folder_paths.get(doc.id, "")
                enriched.append(summary)
            return enriched
        except RAGIndexModelMismatchError:
            raise
        except ServiceException as e:
            if e.code == "RAG_INDEX_STORE_DAMAGED":
                # The user-actionable damaged-store error must reach the
                # client instead of degrading silently to keyword results.
                raise
            logger.warning("RAG search failed, falling back to keyword: %s", e, exc_info=True)
            return await self.search_documents(project_id, query, parent_id=parent_id)
        except Exception as e:
            logger.warning("RAG search failed, falling back to keyword: %s", e, exc_info=True)
            return await self.search_documents(project_id, query, parent_id=parent_id)

    async def rebuild_index(self, project_id: str) -> Dict:
        """Rebuild RAG index for all documents in a project. Non-blocking.

        Documents with content are marked as "indexing" and enqueued as
        durable background tasks; active documents without content go back
        to full processing. The caller gets an immediate response without
        waiting for the actual indexing to complete.
        """
        from app.services.rag_service import rag_service

        # 1. Snapshot docs that need re-indexing plus docs with active
        #    processing tasks.
        doc_ids_to_reindex: List[str] = []
        active_doc_ids: List[str] = []
        active_without_content: List[str] = []
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.get_all()
            for doc in docs:
                if doc.content:
                    doc_ids_to_reindex.append(doc.id)
                if doc.processing_status in ACTIVE_STATUSES:
                    active_doc_ids.append(doc.id)
                    if not doc.content:
                        active_without_content.append(doc.id)

        # 2. Cancel all active processing/indexing tasks BEFORE destroying
        #    the ChromaDB collection, so no queued task writes into the
        #    collection that is about to be reset.
        for doc_id in active_doc_ids:
            await self._cancel_processing(project_id, doc_id)

        # 3. Bounded-wait for in-flight handlers to wind down: cancellation
        #    is cooperative, and a handler past its last cancellation
        #    checkpoint may still write chunks into the collection that is
        #    about to be reset.
        if not await self._wait_for_inflight_tasks(project_id):
            logger.warning(
                "Rebuild: library tasks for project %s did not drain within %s seconds; "
                "leaving the existing index and state untouched",
                project_id, int(_REBUILD_DRAIN_TIMEOUT_SECONDS),
            )
            return {
                "success": False,
                "message": "Rebuild could not start because active library tasks did not drain; try again.",
                "status": "drain_timeout",
            }

        # 4. Move every affected document to its target status in a single
        #    transaction BEFORE the destructive reset: content documents to
        #    "indexing", content-less ones to "pending". From this point the
        #    60s maintenance sweep can see and re-enqueue all of them, so a
        #    crash at any later step self-heals instead of leaving documents
        #    "completed" while their chunks were deleted with the collection.
        targets = {doc_id: STATUS_INDEXING for doc_id in doc_ids_to_reindex}
        targets.update({doc_id: STATUS_PENDING for doc_id in active_without_content})
        reset_ids = set(targets)
        async with UnitOfWork(project_id) as uow:
            await uow.library.bulk_reset_processing(targets)

        # 5. Reset the collection so it is recreated with the current model
        await rag_service.reset_project_index(project_id)

        # 6. Re-enqueue from a FRESH post-reset read: a document that gained
        #    content or completed between the snapshot and the reset is
        #    captured here, and enqueue dedupes on revision so re-enqueueing
        #    snapshot entries is harmless. The bulk reset keeps every queued
        #    document visible to the maintenance sweep. A single failed
        #    enqueue must not strand the remaining documents — the sweep
        #    re-enqueues pending/indexing documents on its own.
        fresh_targets = {}
        async with UnitOfWork(project_id) as uow:
            for doc in await uow.library.get_all():
                if doc.is_folder:
                    continue
                if doc.content:
                    fresh_targets[doc.id] = STATUS_INDEXING
                elif doc.processing_status in ACTIVE_STATUSES:
                    fresh_targets[doc.id] = STATUS_PENDING
            fresh_targets = {
                doc_id: status for doc_id, status in fresh_targets.items()
                if doc_id not in reset_ids
            }
            await uow.library.bulk_reset_processing(fresh_targets)
        targets.update(fresh_targets)

        from app.services.background_task_service import background_task_service
        for doc_id, status in targets.items():
            try:
                if status == STATUS_INDEXING:
                    await background_task_service.enqueue_rag_index(project_id, doc_id)
                else:
                    await background_task_service.enqueue_document_process(project_id, doc_id)
            except Exception:
                logger.warning(
                    "Rebuild: failed to enqueue %s for document %s; the maintenance "
                    "sweep will recover it", status, doc_id, exc_info=True,
                )

        return {
            "success": True,
            "message": f"Rebuild started. {len(targets)} documents queued.",
            "total": len(targets),
            "status": "queued",
        }

    async def _wait_for_inflight_tasks(self, project_id: str) -> bool:
        """Poll until no library background task has a live handler.

        Returns False when tasks were still in flight when the bounded
        timeout elapsed; the caller decides whether to proceed anyway.
        """
        from app.services.library_task_protocol import QUEUE_LIBRARY

        deadline = time.monotonic() + _REBUILD_DRAIN_TIMEOUT_SECONDS
        while True:
            async with UnitOfWork(project_id) as uow:
                if not await uow.background_tasks.has_inflight(QUEUE_LIBRARY):
                    return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_REBUILD_DRAIN_POLL_SECONDS)

    async def get_status_summary(self, project_id: str) -> Dict:
        """Return processing status counts and non-completed documents for a project."""
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.get_doc_status_summary()

        summary = {STATUS_PENDING: 0, STATUS_PROCESSING: 0, STATUS_INDEXING: 0,
                   STATUS_CANCELLING: 0, STATUS_COMPLETED: 0, STATUS_FAILED: 0}
        active_docs = []
        for d in docs:
            status = d["processing_status"]
            # Existing project databases may still contain the raw value
            # "indexing_failed"; normalizing it here keeps such rows visible
            # in the summary instead of silently dropping them.
            if status == "indexing_failed":
                status = STATUS_FAILED
            summary[status] = summary.get(status, 0) + 1
            if status != STATUS_COMPLETED:
                active_docs.append(d)

        return {"summary": summary, "documents": active_docs}

    # ------------------------------------------------------------------
    # Status transitions — single entry points for state changes
    # ------------------------------------------------------------------

    async def mark_document_processing(self, project_id: str, doc_id: str,
                                        expected_revision: int | None = None) -> bool:
        """Transition document to 'processing' state with started_at timestamp.

        Returns False when the document is gone or was edited since the
        caller snapshotted ``expected_revision``, so a stale task exits
        instead of overwriting newer state.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.library.update_processing_status(
                doc_id,
                status=STATUS_PROCESSING,
                started_at=utcnow(),
                log_append="Processing in progress...",
                expected_revision=expected_revision,
            )

    async def mark_document_indexing(self, project_id: str, doc_id: str,
                                      log_append: str = "Document processing done. Queued for RAG indexing.",
                                      expected_revision: int | None = None) -> bool:
        """Transition document to 'indexing' state (ready for RAG).

        Returns False when the document is gone or was edited since the
        caller snapshotted ``expected_revision``, so a stale task exits
        instead of overwriting newer state.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.library.update_processing_status(
                doc_id,
                status=STATUS_INDEXING,
                log_append=log_append,
                expected_revision=expected_revision,
            )

    async def mark_document_completed(self, project_id: str, doc_id: str,
                                      expected_revision: int | None = None) -> bool:
        """Transition document to 'completed' state."""
        async with UnitOfWork(project_id) as uow:
            return await uow.library.update_processing_status(
                doc_id,
                status=STATUS_COMPLETED,
                completed_at=utcnow(),
                expected_revision=expected_revision,
            )

    async def mark_document_failed(self, project_id: str, doc_id: str,
                                     reason: str, expected_revision: int | None = None) -> bool:
        """Transition document to 'failed' state with error message."""
        async with UnitOfWork(project_id) as uow:
            return await uow.library.mark_failed(
                doc_id, reason, expected_revision=expected_revision,
            )

    async def append_processing_log(self, project_id: str, doc_id: str,
                                      message: str) -> None:
        """Append a timestamped log message to the document's processing log."""
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_processing_log(doc_id, message)

    async def update_document_content(self, project_id: str, doc_id: str,
                                        content: str,
                                        expected_revision: int | None = None) -> None:
        """Update document content in the database.

        When ``expected_revision`` is given, the write is skipped if the
        document was edited (revision bumped) since processing started, so a
        stale task cannot clobber newer user content.
        """
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_content(
                doc_id, content, expected_revision=expected_revision,
            )

    async def update_document_fields(self, project_id: str, doc_id: str,
                                       title: str = None,
                                       description: str = None,
                                       keywords: list = None,
                                       expected_revision: int | None = None) -> None:
        """Update document metadata fields (title, description, keywords).

        ``expected_revision`` guards against a stale task overwriting newer
        user edits, same as ``update_document_content``.
        """
        async with UnitOfWork(project_id) as uow:
            await uow.library.update_fields(
                doc_id, title=title, description=description, keywords=keywords,
                expected_revision=expected_revision,
            )


library_service = LibraryService()
