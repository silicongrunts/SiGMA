"""
Document Processing Service - Handles file uploads, docling conversion,
and AI field extraction for library documents.
"""
import asyncio
import os
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional, List, Any

from app.core.config import settings
from app.core.document_status import (
    STATUS_PENDING, STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_INDEXING,
)
from app.core.atomic_file import ProjectFileLock
from app.core.utils import sanitize_filename, to_iso
from app.database.unit_of_work import UnitOfWork

from app.core.logging import get_logger
from app.core.exceptions import (
    DocumentNotFoundError, ServiceException, FileMissingError, LLMResponseError,
    DocumentConversionError, AIExtractionError, FileSystemError,
)
logger = get_logger(__name__)

# Text file extensions that can be read directly
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml",
    ".html", ".htm", ".js", ".ts", ".jsx", ".tsx", ".py",
    ".java", ".c", ".cpp", ".h", ".rs", ".go", ".rb",
    ".php", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".tex", ".sty", ".cls", ".bst", ".bib",
    ".yaml", ".yml", ".ini", ".conf", ".log",
    ".ipynb", ".toml",
}

# Docling-supported file types
DOCLING_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".epub", ".html", ".htm",
}

# All allowed upload extensions
UPLOADABLE_EXTENSIONS = TEXT_EXTENSIONS | DOCLING_EXTENSIONS


class DocumentProcessingService:
    """Processes uploaded documents: converts, extracts fields, indexes for RAG."""

    # Max processing time per document in seconds (4 hours)
    MAX_PROCESSING_SECONDS = 4 * 60 * 60

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._running = False

    def is_running(self) -> bool:
        return self._running

    async def start(self):
        self._running = True
        logger.info("Document processing service started")

    async def stop(self):
        self._running = False
        self._executor.shutdown(wait=False)
        logger.info("Document processing service stopped")

    # ------------------------------------------------------------------
    # Cancellation helpers — in-process event first, DB fallback
    # ------------------------------------------------------------------
    async def _should_stop(self, project_id: str, doc_id: str,
                           cancel_event: asyncio.Event = None,
                           task_context=None,
                           expected_revision: int | None = None) -> bool:
        """Check if processing should stop. In-process event first, then DB fallback."""
        if cancel_event and cancel_event.is_set():
            return True
        if task_context and await task_context.is_cancelling():
            return True
        try:
            async with UnitOfWork(project_id) as uow:
                doc = await uow.library.get_by_id(doc_id)
            if not doc or doc.processing_status == STATUS_CANCELLING:
                return True
            return expected_revision is not None and doc.revision != expected_revision
        except Exception:
            logger.debug("Failed to check document stop state for %s", doc_id, exc_info=True)
            return True

    async def _cancellable_llm_call(self, project_id: str, doc_id: str,
                                    coro, cancel_event: asyncio.Event = None,
                                    task_context=None,
                                    expected_revision: int | None = None):
        """Wrap an LLM coroutine with cancellation checks.

        When cancel_event (TCP) is available, checks every 0.5s.
        DB check throttled to every 5s as fallback (handles worker-initiated deletes).
        Returns the LLM result, or None if cancelled.
        """
        task = asyncio.ensure_future(coro)
        last_db_check = time.monotonic()
        try:
            while not task.done():
                timeout = 0.5 if cancel_event else 5.0
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if done:
                    break
                # TCP check (instant)
                if cancel_event and cancel_event.is_set():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass  # Expected during task cancellation cleanup
                    except Exception:
                        logger.debug("Task cancellation cleanup raised non-cancelled error", exc_info=True)
                    logger.info("LLM call cancelled for doc %s", doc_id)
                    return None
                if task_context:
                    alive = await task_context.heartbeat()
                    if not alive or await task_context.is_cancelling():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass  # Expected during task cancellation cleanup
                        except Exception:
                            logger.debug("Task cancellation cleanup raised non-cancelled error", exc_info=True)
                        logger.info("LLM call cancelled for doc %s (task signal)", doc_id)
                        return None
                # DB check (throttled to 5s)
                now = time.monotonic()
                if now - last_db_check >= 5.0:
                    last_db_check = now
                    if await self._should_stop(
                        project_id, doc_id,
                        task_context=task_context,
                        expected_revision=expected_revision,
                    ):
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass  # Expected during task cancellation cleanup
                        except Exception:
                            logger.debug("Task cancellation cleanup raised non-cancelled error", exc_info=True)
                        logger.info("LLM call cancelled for doc %s (DB signal)", doc_id)
                        return None
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected during task cancellation cleanup
            except Exception:
                logger.debug("Task cancellation cleanup raised non-cancelled error", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------
    async def upload_files(
        self,
        project_id: str,
        file_list: List[Any],
        folder_id: Optional[str] = None,
        relative_paths: Optional[List[str]] = None,
    ) -> Dict:
        """
        Upload files to the project's .SiGMA/library/ directory.
        Creates DB records, starts background processing.
        Returns dict with 'documents' (created) and 'errors' (skipped with reasons).
        """
        sigma_dir = settings.get_sigma_path(project_id)
        library_dir = sigma_dir / "library"
        library_dir.mkdir(parents=True, exist_ok=True)

        results = []
        errors = []
        for index, upload_file in enumerate(file_list):
            try:
                raw_name = upload_file.filename
                if not raw_name:
                    continue
                # Sanitize: reject traversal, hidden names; extract safe basename
                try:
                    file_name = sanitize_filename(raw_name)
                except Exception:
                    logger.warning("Invalid filename rejected: %s", raw_name, exc_info=True)
                    errors.append({"file": raw_name, "reason": "Invalid filename"})
                    continue

                relative_path = self._relative_path_for_upload(
                    relative_paths, index, file_name,
                )
                try:
                    directory_parts, relative_file_name = self._parse_upload_relative_path(
                        relative_path, file_name,
                    )
                except Exception as e:
                    logger.warning("Invalid upload relative path rejected: %s", relative_path, exc_info=True)
                    errors.append({"file": raw_name, "reason": str(e)})
                    continue

                ext = Path(file_name).suffix.lower()
                if ext and ext not in UPLOADABLE_EXTENSIONS:
                    logger.warning("Unsupported extension rejected: %s", ext)
                    errors.append({
                        "file": raw_name,
                        "reason": f"Unsupported file type: {ext}",
                    })
                    continue

                target_parent_id = await self._ensure_upload_folder_path(
                    project_id, folder_id, directory_parts,
                )

                # Handle duplicate names with streaming atomic write.
                stem = Path(file_name).stem
                target_path = await self._write_upload_unique(library_dir / file_name, upload_file)

                upload_title = stem

                # Check for duplicate title in the library (same parent)
                async with UnitOfWork(project_id) as uow:
                    if await uow.library.check_duplicate_title(
                        upload_title, parent_id=target_parent_id
                    ):
                        upload_title = f"{stem}_{time.time()}"

                    doc = await uow.library.create(
                        title=upload_title,
                        content="",
                        source="user upload",
                        doc_type=ext.lstrip(".") if ext else "file",
                        file_name=relative_file_name,
                        file_path=str(target_path),
                        processing_status=STATUS_PENDING,
                        parent_id=target_parent_id,
                    )

                results.append(doc.to_summary_dict())

                if self._running:
                    from app.services.background_task_service import background_task_service
                    await background_task_service.enqueue_document_process(project_id, doc.id)

            except Exception as e:
                logger.error("Failed to upload file %s: %s", upload_file.filename, e, exc_info=True)
                errors.append({"file": upload_file.filename or "unknown", "reason": str(e)})

        return {"documents": results, "errors": errors}

    @staticmethod
    def _relative_path_for_upload(
        relative_paths: Optional[List[str]],
        index: int,
        file_name: str,
    ) -> str:
        if not relative_paths or index >= len(relative_paths):
            return file_name
        return relative_paths[index] or file_name

    @staticmethod
    def _parse_upload_relative_path(
        relative_path: str,
        file_name: str,
    ) -> tuple[list[str], str]:
        normalized = str(relative_path).replace("\\", "/")
        if not normalized:
            return [], file_name
        if normalized.startswith("/"):
            raise FileSystemError("Relative path must not be absolute")
        parts = normalized.split("/")
        if any(part == "" for part in parts):
            raise FileSystemError("Relative path must not contain empty path segments")

        safe_parts = [sanitize_filename(part) for part in parts]
        if safe_parts[-1] != file_name:
            raise FileSystemError("Relative path filename does not match uploaded file")
        return safe_parts[:-1], safe_parts[-1]

    async def _ensure_upload_folder_path(
        self,
        project_id: str,
        parent_id: Optional[str],
        directory_parts: list[str],
    ) -> Optional[str]:
        current_parent_id = parent_id
        for folder_name in directory_parts:
            async with UnitOfWork(project_id) as uow:
                existing = await uow.library.get_child_by_title(
                    folder_name,
                    parent_id=current_parent_id,
                )
                if existing:
                    if not existing.is_folder:
                        raise FileSystemError(
                            f"Cannot create folder '{folder_name}': a document with that name already exists"
                        )
                    current_parent_id = existing.id
                    continue

                folder = await uow.library.create(
                    title=folder_name,
                    content="",
                    is_folder=True,
                    parent_id=current_parent_id,
                    processing_status=STATUS_COMPLETED,
                )
                current_parent_id = folder.id
        return current_parent_id

    async def _write_upload_unique(self, path: Path, upload_file: Any) -> Path:
        """Stream an UploadFile to a unique path using temp-file replace."""
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        stem = path.stem
        suffix = path.suffix
        attempt = 0
        while True:
            target = path if attempt == 0 else path.parent / f"{stem}_{attempt}{suffix}"
            attempt += 1
            with ProjectFileLock(target):
                if target.exists():
                    continue
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(target.parent),
                    prefix=".upload_",
                    suffix=target.suffix or ".tmp",
                )
                tmp_path = Path(tmp_name)
                try:
                    with os.fdopen(fd, "wb") as out:
                        while True:
                            chunk = await upload_file.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(tmp_path, target)
                    return target
                except BaseException:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------
    async def _process_document_in_background(self, project_id: str, doc_id: str,
                                               cancel_event: asyncio.Event = None,
                                               expected_revision: int | None = None,
                                               task_context=None):
        """Run document processing with a per-document timeout.

        Fatal errors intentionally propagate to the durable task runner. The
        runner owns retry accounting and the final document failure state.
        """
        if not self._running:
            return
        try:
            await asyncio.wait_for(
                self._run_processing_logic(
                    project_id, doc_id,
                    cancel_event=cancel_event,
                    expected_revision=expected_revision,
                    task_context=task_context,
                ),
                timeout=self.MAX_PROCESSING_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Processing timed out (exceeded {self.MAX_PROCESSING_SECONDS} seconds)"
            ) from exc

    async def _run_processing_logic(self, project_id: str, doc_id: str,
                                    cancel_event: asyncio.Event = None,
                                    expected_revision: int | None = None,
                                    task_context=None):
        """Main processing: convert file if needed, extract AI fields, index for RAG."""
        from app.services.library_service import library_service

        # Check cancel before marking processing
        if await self._should_stop(project_id, doc_id, cancel_event, task_context, expected_revision):
            return

        # 1. Mark as processing
        await library_service.mark_document_processing(project_id, doc_id)
        if task_context:
            await task_context.heartbeat()

        if await self._should_stop(project_id, doc_id, cancel_event, task_context, expected_revision):
            return

        # 2. Load document
        doc = await library_service.get_document(project_id, doc_id)
        if not doc:
            return

        # 3. Content extraction
        if doc.get("file_path") and Path(doc["file_path"]).exists():
            file_path_obj = Path(doc["file_path"])
            ext = file_path_obj.suffix.lower()

            if ext in TEXT_EXTENSIONS:
                await library_service.append_processing_log(
                    project_id, doc_id, "Text file detected, reading content directly...")
                if not (doc.get("content") and doc["content"].strip()):
                    content = file_path_obj.read_text(encoding="utf-8", errors="replace")
                    if await self._should_stop(
                        project_id, doc_id, cancel_event, task_context, expected_revision
                    ):
                        return
                    await library_service.update_document_content(project_id, doc_id, content)
                    await library_service.append_processing_log(
                        project_id, doc_id, f"Read {len(content)} characters.")
                    if task_context:
                        await task_context.heartbeat()
                doc = await library_service.get_document(project_id, doc_id)
            else:
                await library_service.append_processing_log(
                    project_id, doc_id, f"Non-text file ({ext}), converting with Docling...")
                content = await self._convert_with_docling(str(file_path_obj))
                if task_context:
                    await task_context.heartbeat()
                if not content or not content.strip():
                    raise DocumentConversionError(str(file_path_obj), doc_id=doc_id)
                if await self._should_stop(
                    project_id, doc_id, cancel_event, task_context, expected_revision
                ):
                    return
                await library_service.update_document_content(project_id, doc_id, content)
                await library_service.append_processing_log(
                    project_id, doc_id,
                    f"Docling conversion done. Content length: {len(content)} chars.")
                doc = await library_service.get_document(project_id, doc_id)

        elif doc.get("content") and doc["content"].strip():
            await library_service.append_processing_log(
                project_id, doc_id, "Content already provided, skipping file extraction.")
        else:
            # Empty or no content — mark as indexing and enqueue RAG indexing
            # (indexer will immediately mark completed since there's nothing to index)
            await library_service.append_processing_log(
                project_id, doc_id, "No content to process. Queuing for indexing.")
            await library_service.mark_document_indexing(
                project_id, doc_id,
                log_append="Empty document. Queued for indexing.")
            try:
                from app.services.background_task_service import background_task_service
                await background_task_service.enqueue_rag_index(project_id, doc_id)
            except Exception as e:
                logger.warning("Failed to enqueue empty document for RAG indexing: %s", e, exc_info=True)
                await library_service.append_processing_log(
                    project_id, doc_id, f"RAG indexing queue warning: {e}")
            return

        if await self._should_stop(project_id, doc_id, cancel_event, task_context, expected_revision):
            return

        # 4. AI field extraction -- only if enabled and description/keywords are both empty
        content_for_ai = doc.get("content") or ""
        doc_description = doc.get("description") or ""
        doc_keywords = doc.get("keywords") or []
        needs_ai = (not doc_description or not doc_description.strip()) and not doc_keywords

        if content_for_ai.strip() and needs_ai and settings.AUTO_AI_METADATA_ENABLED:
            await library_service.append_processing_log(
                project_id, doc_id, "Starting AI field extraction...")
            try:
                extract_fields = None if doc.get("source") == "user upload" else ["description", "keywords"]
                ai_fields = await self._extract_fields_with_ai(
                    content_for_ai,
                    current_title=doc.get("title") or "",
                    extract_fields=extract_fields,
                    project_id=project_id,
                    doc_id=doc_id,
                    cancel_event=cancel_event,
                    expected_revision=expected_revision,
                    task_context=task_context,
                )
                if ai_fields is None:
                    return  # Cancelled during AI extraction
                if ai_fields:
                    if await self._should_stop(
                        project_id, doc_id, cancel_event, task_context, expected_revision
                    ):
                        return
                    title = ai_fields.get("title") or doc.get("title", "")
                    description = ai_fields.get("description", "")
                    keywords = ai_fields.get("keywords", [])
                    await library_service.update_document_fields(
                        project_id, doc_id,
                        title=title, description=description,
                        keywords=keywords if isinstance(keywords, list) else [],
                    )
                    await library_service.append_processing_log(
                        project_id, doc_id, f"AI extraction done. Title: {title}")
                else:
                    await library_service.append_processing_log(
                        project_id, doc_id, "AI extraction returned empty result, keeping defaults.")
            except AIExtractionError as e:
                # Non-fatal: document still indexes with empty metadata
                await library_service.append_processing_log(
                    project_id, doc_id, f"AI extraction failed (non-fatal): {e}")
            except Exception as e:
                # Catch-all for unexpected AI extraction errors (also non-fatal)
                logger.warning("Unexpected AI extraction error for doc %s: %s", doc_id, e, exc_info=True)
                await library_service.append_processing_log(
                    project_id, doc_id, f"AI extraction unexpected error (non-fatal): {e}")
        elif not content_for_ai.strip():
            await library_service.append_processing_log(
                project_id, doc_id, "No content for AI extraction, skipping.")
        elif needs_ai and not settings.AUTO_AI_METADATA_ENABLED:
            await library_service.append_processing_log(
                project_id, doc_id, "AI extraction skipped (disabled in settings).")
        else:
            await library_service.append_processing_log(
                project_id, doc_id, "AI extraction skipped (fields already populated).")

        if await self._should_stop(project_id, doc_id, cancel_event, task_context, expected_revision):
            return

        # 5. Mark processing as done — ready for RAG indexing
        await library_service.mark_document_indexing(project_id, doc_id)
        if task_context:
            await task_context.heartbeat()

        if await self._should_stop(project_id, doc_id, cancel_event, task_context, expected_revision):
            return

        # 6. Enqueue RAG indexing via durable background task queue.
        try:
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc_id)
            await library_service.append_processing_log(
                project_id, doc_id, "RAG indexing queued.")
        except Exception as e:
            logger.warning("Failed to enqueue document for RAG indexing: %s", e, exc_info=True)
            await library_service.append_processing_log(
                project_id, doc_id, f"RAG indexing queue warning: {e}")

    # ------------------------------------------------------------------
    # Docling conversion
    # ------------------------------------------------------------------
    async def _convert_with_docling(self, file_path: str) -> str:
        """Convert a file to markdown using Docling (runs in thread pool)."""
        def _do_convert() -> str:
            from docling.document_converter import DocumentConverter
            converter = DocumentConverter()
            result = converter.convert(file_path)
            return result.document.export_to_markdown()

        return await asyncio.get_event_loop().run_in_executor(self._executor, _do_convert)

    # ------------------------------------------------------------------
    # AI field extraction
    # ------------------------------------------------------------------
    async def _extract_fields_with_ai(
        self,
        content: str,
        current_title: str = "",
        extract_fields: list[str] | None = None,
        project_id: str = "",
        doc_id: str = "",
        cancel_event: asyncio.Event = None,
        expected_revision: int | None = None,
        task_context=None,
    ) -> Dict | None:
        """Extract title/description/keywords using the RA model.

        Args:
            content: Document text to analyze.
            current_title: Existing title. The prompt tells the model to keep
                it unchanged unless it is clearly meaningless or unrelated.
            extract_fields: If provided, only return these fields from the AI result.
                           None = return all fields (backward compatible).
            project_id: If provided, enables DB fallback cancellation checks.
            doc_id: If provided, enables DB fallback cancellation checks.
            cancel_event: TCP cancel signal (instant, preferred over DB).

        Returns:
            Dict with extracted fields, or None if cancelled.
        """
        max_tokens = self._metadata_input_token_budget()
        truncated = self._truncate_to_tokens(content, max_tokens)
        if not truncated.strip():
            return {}

        from app.agents.prompt_service import prompt_service
        prompt = prompt_service.render(
            "tools/document_extractor",
            max_tokens=max_tokens,
            current_title=current_title or "",
            content=truncated,
        )

        cancellable = bool(cancel_event or task_context or (project_id and doc_id))

        max_attempts = 1 if cancellable else 3
        for attempt in range(1, max_attempts + 1):
            # Check cancellation between retries
            if cancellable and await self._should_stop(
                project_id, doc_id, cancel_event, task_context, expected_revision
            ):
                logger.info("AI extraction cancelled for doc %s at attempt %d", doc_id, attempt)
                return None

            try:
                from app.services.llm_service import llm_service

                coro = llm_service.call_json(
                    prompt=prompt,
                    system="You are a document metadata extractor. Return ONLY valid JSON, no markdown, no explanation, no code fences.",
                    model_role="ra",
                    timeout=3600.0,
                    max_tokens=settings.AI_METADATA_OUTPUT_TOKENS,
                )

                if cancellable:
                    # Cancellable: TCP 0.5s + DB 5s, stops within 0.5s of TCP signal
                    result = await self._cancellable_llm_call(
                        project_id, doc_id, coro, cancel_event=cancel_event,
                        expected_revision=expected_revision,
                        task_context=task_context,
                    )
                    if result is None:
                        return None
                else:
                    # Non-cancellable (backward compatible: sync route, manual extraction)
                    result = await asyncio.wait_for(coro, timeout=3600)

                result = self._normalize_ai_metadata_result(result)

                # Filter to requested fields if specified
                if extract_fields is not None:
                    result = {k: v for k, v in result.items() if k in extract_fields}
                return result
            except asyncio.TimeoutError:
                logger.warning(f"AI extraction attempt {attempt} timed out (3600s limit)")
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning("AI extraction attempt %s failed: %s", attempt, e, exc_info=True)
                await asyncio.sleep(1)

        raise AIExtractionError(doc_id=doc_id)

    @staticmethod
    def _normalize_ai_metadata_result(result: dict) -> dict:
        if not isinstance(result, dict):
            return {}
        return {
            key: result[key]
            for key in ("title", "description", "keywords")
            if key in result
        }

    @staticmethod
    def _metadata_input_token_budget() -> int:
        """Budget automatic metadata extraction input by RA context and YAML."""
        try:
            ra_context = settings.max_context_length_for_role("ra")
        except Exception:
            logger.debug("Failed to read RA context budget; using fallback", exc_info=True)
            ra_context = 64_000
        context_budget = max(1000, int(ra_context) - 30_000)
        return max(1, min(40_000, context_budget, settings.AI_METADATA_MAX_INPUT_TOKENS))

    @staticmethod
    def _truncate_to_tokens(content: str, max_tokens: int) -> str:
        """Truncate text with tiktoken when available; fall back conservatively."""
        if not content:
            return ""
        try:
            from app.services.chunker import _get_encoding
            encoding = _get_encoding()
            tokens = encoding.encode(content)
            if len(tokens) <= max_tokens:
                return content
            return encoding.decode(tokens[:max_tokens])
        except Exception:
            logger.debug("Token truncation failed; using character fallback", exc_info=True)
            # Worst-case-ish fallback for CJK/locales without spaces.
            return content[:max_tokens * 2]

    # ------------------------------------------------------------------
    # Manual (synchronous) field extraction — called from route
    # ------------------------------------------------------------------
    async def extract_fields_sync(self, project_id: str, doc_id: str) -> Dict:
        """Run AI field extraction synchronously and return the result.

        Used when the user manually clicks the AI extract button — the
        frontend awaits the full response so the shimmer animation persists
        until extraction completes.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        if not doc:
            raise DocumentNotFoundError(doc_id)

        content = doc.content or ""
        if not content.strip():
            raise ServiceException("Document has no content for extraction")

        ai_fields = await self._extract_fields_with_ai(content, current_title=doc.title)
        if ai_fields:
            title = ai_fields.get("title") or doc.title
            description = ai_fields.get("description", "")
            keywords = ai_fields.get("keywords", [])
            async with UnitOfWork(project_id) as uow:
                await uow.library.update_fields(
                    doc_id,
                    title=title,
                    description=description,
                    keywords=keywords if isinstance(keywords, list) else [],
                    bump_revision=True,
                )
                await uow.library.update_processing_status(doc_id, STATUS_INDEXING)
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc_id)
            return {
                "success": True,
                "message": "AI field extraction completed",
                "fields": {"title": title, "description": description, "keywords": keywords},
            }
        raise LLMResponseError("AI extraction returned empty result")

    async def reprocess_failed(self, project_id: str, doc_id: str) -> Dict:
        """Re-run processing for a single failed document."""
        try:
            async with UnitOfWork(project_id) as uow:
                doc = await uow.library.get_by_id(doc_id)
            if not doc:
                raise DocumentNotFoundError(doc_id)

            if not doc.file_path or not Path(doc.file_path).exists():
                raise FileMissingError(doc.file_path)

            async with UnitOfWork(project_id) as uow:
                await uow.library.reset_processing(doc_id)

            if self._running:
                from app.services.background_task_service import background_task_service
                await background_task_service.enqueue_document_process(
                    project_id, doc_id, action="reprocess",
                )

            return {"success": True, "message": "Reprocessing started"}
        except Exception as e:
            raise ServiceException(str(e))

    async def reprocess_all_failed(self, project_id: str) -> Dict:
        """Re-run processing for all failed documents in the project."""
        count = 0
        errors = []
        try:
            async with UnitOfWork(project_id) as uow:
                docs = await uow.library.list_by_status(STATUS_FAILED)
            for doc in docs:
                try:
                    result = await self.reprocess_failed(project_id, doc.id)
                    if result.get("success"):
                        count += 1
                    else:
                        errors.append(f"{doc.title}: {result.get('error')}")
                except Exception as exc:
                    logger.warning("Failed to reprocess document %s: %s", doc.id, exc, exc_info=True)
                    errors.append(f"{doc.title}: {exc}")

            return {
                "success": True,
                "message": f"Reprocessing started for {count} failed documents",
                "count": count,
                "errors": errors,
            }
        except Exception as e:
            raise ServiceException(str(e), details={"count": count})

    # ------------------------------------------------------------------
    # Get processing log
    # ------------------------------------------------------------------
    async def get_processing_log(self, project_id: str, doc_id: str) -> Optional[Dict]:
        """Get the processing log for a document."""
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        if not doc:
            return None
        return {
            "id": doc.id,
            "title": doc.title,
            "processing_status": doc.processing_status,
            "processing_log": doc.processing_log or "",
            "processing_started_at": to_iso(doc.processing_started_at),
            "processing_completed_at": to_iso(doc.processing_completed_at),
        }


# Singleton
document_processing_service = DocumentProcessingService()


# ---------------------------------------------------------------------------
# Background-task handler registration
# ---------------------------------------------------------------------------
#
# Register the document-processing handler with the library task protocol
# at module load.  ``services.background_task_service`` dispatches by
# querying the registry — it does not import this module — which keeps the
# dependency graph acyclic (this module still imports the queue manager to
# enqueue work; the queue manager never imports it back).

async def _handle_document_process_task(ctx, payload: dict) -> None:
    """Run one document-processing task on the library queue."""
    if not document_processing_service.is_running():
        await document_processing_service.start()
    await document_processing_service._process_document_in_background(
        ctx.project_id,
        payload["doc_id"],
        expected_revision=payload.get("doc_revision"),
        task_context=ctx,
    )


def _register_library_handler() -> None:
    from app.services.library_task_protocol import (
        KIND_DOCUMENT_PROCESS, register_task_handler,
    )
    register_task_handler(KIND_DOCUMENT_PROCESS, _handle_document_process_task)


_register_library_handler()
