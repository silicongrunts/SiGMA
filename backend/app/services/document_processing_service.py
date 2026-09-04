"""
Document Processing Service - Handles file uploads, docling conversion,
and AI field extraction for library documents.
"""
import asyncio
import multiprocessing
import os
import time
import tempfile
from pathlib import Path
from typing import Dict, Optional, List, Any

from app.core.config import settings
from app.core.document_status import (
    STATUS_PENDING, STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_INDEXING,
)
from app.core.utils import sanitize_filename, to_iso
from app.database.unit_of_work import UnitOfWork

from app.core.logging import get_logger
from app.core.exceptions import (
    DocumentNotFoundError, ServiceException, FileMissingError, LLMResponseError,
    DocumentConversionError, AIExtractionError, FileSystemError,
)
from app.services.project_service import project_service
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

# Seconds between cancellation checks while a docling conversion runs in
# its worker process.
_CONVERSION_POLL_SECONDS = 2.0

# Grace given to terminate() before escalating to kill when reaping a
# conversion process; also the bound on the final post-kill join.
_REAP_JOIN_SECONDS = 5.0

# Seconds between stop-signal checks (task heartbeat/cancel + DB state) while
# an AI metadata call runs.
_STOP_CHECK_INTERVAL_SECONDS = 5.0


def _docling_convert_worker(file_path: str, conn) -> None:
    """Convert one file with Docling inside an isolated process.

    Sends ``(ok, payload)`` — the markdown text on success, an error
    description otherwise — then exits. The web process can terminate this
    process at any point without losing state.
    """
    try:
        from docling.document_converter import DocumentConverter
        converter = DocumentConverter()
        result = converter.convert(file_path)
        message = (True, result.document.export_to_markdown())
    except BaseException as exc:
        message = (False, f"{type(exc).__name__}: {exc}")
    try:
        conn.send(message)
    except Exception:
        pass
    finally:
        conn.close()


class DocumentProcessingService:
    """Processes uploaded documents: converts, extracts fields, indexes for RAG."""

    # ------------------------------------------------------------------
    # Cancellation checks
    # ------------------------------------------------------------------
    async def _should_stop(self, project_id: str, doc_id: str,
                           task_context=None,
                           expected_revision: int | None = None) -> bool:
        """Check if processing should stop (cancelled, deleted, superseded,
        or the task lost its lease to another owner)."""
        if task_context and await task_context.is_cancelling():
            return True
        if task_context and not await task_context.heartbeat():
            logger.info("Task lost lease ownership; stopping work on doc %s", doc_id)
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
                                    coro, task_context=None,
                                    expected_revision: int | None = None):
        """Wrap an LLM coroutine with cancellation checks.

        The task signal is checked on the task's heartbeat cadence; the DB
        check is throttled to every 5s. The call itself is bounded by the AI
        metadata timeout — the same per-attempt budget the sync route
        enforces with its outer wait_for — so a flaky endpoint cannot chain
        the LLM stack's internal retries into an hour of silent work.
        Returns the LLM result, or None if cancelled.
        """
        task = asyncio.ensure_future(
            asyncio.wait_for(coro, timeout=settings.AI_METADATA_TIMEOUT_SECONDS)
        )
        last_db_check = time.monotonic()
        try:
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=_STOP_CHECK_INTERVAL_SECONDS,
                )
                if done:
                    break
                if task_context:
                    alive = await task_context.heartbeat()
                    if not alive or await task_context.is_cancelling():
                        logger.info("LLM call cancelled for doc %s (task signal)", doc_id)
                        return None
                # DB check (throttled to the stop-check cadence)
                now = time.monotonic()
                if now - last_db_check >= _STOP_CHECK_INTERVAL_SECONDS:
                    last_db_check = now
                    if await self._should_stop(
                        project_id, doc_id,
                        task_context=task_context,
                        expected_revision=expected_revision,
                    ):
                        logger.info("LLM call cancelled for doc %s (DB signal)", doc_id)
                        return None
            return task.result()
        finally:
            # Any exit with the call still running (user cancel, lost lease,
            # or an exception from the stop checks or the outer task) must
            # cancel and reap the in-flight LLM task so no orphaned provider
            # request keeps running with nobody retrieving its result.
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # Expected during task cancellation cleanup
                except Exception:
                    logger.debug("Task cancellation cleanup raised non-cancelled error", exc_info=True)

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
        library_settings = getattr(settings, "library", None)
        max_files = getattr(library_settings, "upload_max_files", 100)
        if not isinstance(max_files, int):
            max_files = 100
        if len(file_list) > max_files:
            return {
                "documents": [],
                "errors": [{
                    "file": "batch",
                    "reason": f"Too many files ({len(file_list)}); upload at most {max_files} at a time",
                }],
            }
        batch_bytes = 0
        batch_max_mb = getattr(library_settings, "upload_batch_max_mb", 500)
        if not isinstance(batch_max_mb, int):
            batch_max_mb = 500
        batch_limit = batch_max_mb * 1024 * 1024
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
                file_bytes = target_path.stat().st_size
                if batch_bytes + file_bytes > batch_limit:
                    target_path.unlink(missing_ok=True)
                    errors.append({
                        "file": raw_name,
                        "reason": f"Batch exceeds the {batch_max_mb} MB total limit",
                    })
                    continue
                batch_bytes += file_bytes

                upload_title = stem

                # Check for duplicate title in the library (same parent).
                # The check and the insert share one immediate write
                # transaction, so a concurrent same-title upload serializes
                # here and sees the committed row instead of racing past
                # the check into a duplicate title.
                async with UnitOfWork(project_id, immediate=True) as uow:
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

                from app.services.background_task_service import background_task_service
                await background_task_service.enqueue_document_process(project_id, doc.id)

            except Exception as e:
                logger.error("Failed to upload file %s: %s", upload_file.filename, e, exc_info=True)
                errors.append({"file": upload_file.filename or "unknown", "reason": str(e)})

        if results:
            project_service.touch_project(project_id)
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
            # Get-or-create inside one immediate write transaction: two
            # concurrent uploads of the same folder path serialize here, so
            # the loser sees the winner's committed folder instead of
            # creating a duplicate.
            async with UnitOfWork(project_id, immediate=True) as uow:
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

    # Upper bound on ``stem_N`` candidates tried when every preferred name
    # is already claimed by another upload.
    MAX_NAME_CLASH_ATTEMPTS = 1000

    # Uploads land in this subdirectory of the library directory; orphan
    # cleanup only removes stale temps there, never an in-flight upload.
    UPLOADING_DIRNAME = ".uploading"

    async def _write_upload_unique(self, path: Path, upload_file: Any) -> Path:
        """Stream an UploadFile onto ``path`` (or ``path_1``, ``path_2``, ...)
        with a size cap, claiming the final name atomically.

        The stream lands in a temp file under the library's ``.uploading``
        subdirectory — never scanned by orphan cleanup — and is then moved
        onto a target claimed with an exclusive create, so concurrent uploads
        of the same name land on distinct files instead of overwriting each
        other, and no reader ever sees a partial file. Raises FileSystemError
        once the stream exceeds the configured upload limit; in that case no
        target was claimed and the temp file is removed.
        """
        max_bytes = settings.LIBRARY_UPLOAD_MAX_MB * 1024 * 1024
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        uploading_dir = path.parent / self.UPLOADING_DIRNAME
        uploading_dir.mkdir(exist_ok=True)
        fd, tmp_name = await asyncio.to_thread(
            tempfile.mkstemp,
            dir=str(uploading_dir),
            prefix=".upload_",
            suffix=path.suffix or ".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            written = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await upload_file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise FileSystemError(
                            f"File exceeds the {settings.LIBRARY_UPLOAD_MAX_MB} MB upload limit"
                        )
                    await asyncio.to_thread(out.write, chunk)
                await asyncio.to_thread(out.flush)
                await asyncio.to_thread(os.fsync, out.fileno())
            return await self._claim_target_path(path, tmp_path)
        except BaseException:
            try:
                await asyncio.to_thread(tmp_path.unlink)
            except OSError:
                pass
            raise

    async def _claim_target_path(self, path: Path, tmp_path: Path) -> Path:
        """Claim ``path`` (or the next free ``stem_N`` variant) exclusively.

        The exclusive create is the claim: the first upload to succeed owns
        the name, the loser moves on to the next candidate. On any failure
        after claiming, the empty target is removed so no dead placeholder
        blocks later uploads.
        """
        stem = path.stem
        suffix = path.suffix
        for attempt in range(self.MAX_NAME_CLASH_ATTEMPTS):
            target = path if attempt == 0 else path.parent / f"{stem}_{attempt}{suffix}"
            try:
                fd = await asyncio.to_thread(
                    os.open, str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666,
                )
            except FileExistsError:
                continue
            try:
                await asyncio.to_thread(os.close, fd)
                await asyncio.to_thread(os.replace, tmp_path, target)
            except BaseException:
                try:
                    await asyncio.to_thread(target.unlink)
                except OSError:
                    pass
                raise
            return target
        raise FileSystemError(f"Could not find a unique name for '{path.name}'")

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------
    async def _process_document_in_background(self, project_id: str, doc_id: str,
                                               expected_revision: int | None = None,
                                               task_context=None):
        """Run document processing with a per-document timeout.

        Fatal errors intentionally propagate to the durable task runner. The
        runner owns retry accounting and the final document failure state.
        """
        try:
            await asyncio.wait_for(
                self._run_processing_logic(
                    project_id, doc_id,
                    expected_revision=expected_revision,
                    task_context=task_context,
                ),
                timeout=settings.LIBRARY_MAX_PROCESSING_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Processing timed out (exceeded "
                f"{settings.LIBRARY_MAX_PROCESSING_SECONDS} seconds)"
            ) from exc

    async def _run_processing_logic(self, project_id: str, doc_id: str,
                                    expected_revision: int | None = None,
                                    task_context=None):
        """Main processing: convert file if needed, extract AI fields, index for RAG."""
        from app.services.library_service import library_service

        # Check cancel before marking processing
        if await self._should_stop(project_id, doc_id, task_context, expected_revision):
            return

        # 1. Mark as processing; a stale task (document edited since it was
        #    enqueued) exits instead of overwriting the newer state.
        if not await library_service.mark_document_processing(
            project_id, doc_id, expected_revision=expected_revision,
        ):
            return
        if task_context:
            await task_context.heartbeat()

        if await self._should_stop(project_id, doc_id, task_context, expected_revision):
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
                    max_bytes = settings.LIBRARY_UPLOAD_MAX_MB * 1024 * 1024
                    if file_path_obj.stat().st_size > max_bytes:
                        raise ServiceException(
                            f"Text file exceeds the {settings.LIBRARY_UPLOAD_MAX_MB} MB size limit"
                        )
                    # Large files would block the event loop on a synchronous read.
                    content = await asyncio.to_thread(
                        file_path_obj.read_text, encoding="utf-8", errors="replace",
                    )
                    if await self._should_stop(
                        project_id, doc_id, task_context, expected_revision
                    ):
                        return
                    await library_service.update_document_content(
                        project_id, doc_id, content, expected_revision=expected_revision,
                    )
                    await library_service.append_processing_log(
                        project_id, doc_id, f"Read {len(content)} characters.")
                    if task_context:
                        await task_context.heartbeat()
                doc = await library_service.get_document(project_id, doc_id)
            else:
                await library_service.append_processing_log(
                    project_id, doc_id, f"Non-text file ({ext}), converting with Docling...")
                content = await self._convert_with_docling(
                    str(file_path_obj), project_id, doc_id,
                    task_context=task_context,
                    expected_revision=expected_revision,
                )
                if content is None:
                    return  # Cancelled during conversion
                if task_context:
                    await task_context.heartbeat()
                if not content.strip():
                    raise DocumentConversionError(str(file_path_obj), doc_id=doc_id)
                await library_service.update_document_content(
                    project_id, doc_id, content, expected_revision=expected_revision,
                )
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
            if not await library_service.mark_document_indexing(
                project_id, doc_id,
                log_append="Empty document. Queued for indexing.",
                expected_revision=expected_revision,
            ):
                return
            try:
                from app.services.background_task_service import background_task_service
                await background_task_service.enqueue_rag_index(project_id, doc_id)
            except Exception as e:
                logger.warning("Failed to enqueue empty document for RAG indexing: %s", e, exc_info=True)
                await library_service.append_processing_log(
                    project_id, doc_id, f"RAG indexing queue warning: {e}")
            return

        if await self._should_stop(project_id, doc_id, task_context, expected_revision):
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
                    expected_revision=expected_revision,
                    task_context=task_context,
                )
                if ai_fields is None:
                    return  # Cancelled during AI extraction
                if ai_fields:
                    if await self._should_stop(
                        project_id, doc_id, task_context, expected_revision
                    ):
                        return
                    title = ai_fields.get("title") or doc.get("title", "")
                    description = ai_fields.get("description", "")
                    keywords = ai_fields.get("keywords", [])
                    await library_service.update_document_fields(
                        project_id, doc_id,
                        title=title, description=description,
                        keywords=keywords if isinstance(keywords, list) else [],
                        expected_revision=expected_revision,
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

        if await self._should_stop(project_id, doc_id, task_context, expected_revision):
            return

        # 5. Mark processing as done — ready for RAG indexing. A stale task
        #    exits instead of overwriting a newer user-driven transition.
        if not await library_service.mark_document_indexing(
            project_id, doc_id, expected_revision=expected_revision,
        ):
            return
        if task_context:
            await task_context.heartbeat()

        if await self._should_stop(project_id, doc_id, task_context, expected_revision):
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
    async def _convert_with_docling(self, file_path: str, project_id: str, doc_id: str,
                                    task_context=None,
                                    expected_revision: int | None = None) -> str | None:
        """Convert a file to markdown in an isolated worker process.

        Each conversion gets its own process and its own timeout: a cancelled
        conversion is terminated instead of occupying a worker thread, a
        runaway conversion cannot exhaust the web process's memory, and a
        child stuck in native code cannot starve the library queue for the
        whole document budget. Returns None when the document was cancelled,
        deleted, or superseded.
        """
        ctx = multiprocessing.get_context("spawn")
        recv_conn, send_conn = ctx.Pipe(duplex=False)
        try:
            process = ctx.Process(
                target=_docling_convert_worker,
                args=(file_path, send_conn),
                daemon=True,
            )
            process.start()
        except BaseException:
            recv_conn.close()
            send_conn.close()
            raise
        send_conn.close()

        recv_task = None

        async def _await_result():
            nonlocal recv_task
            while not recv_conn.poll(0):
                if await self._should_stop(
                    project_id, doc_id,
                    task_context=task_context,
                    expected_revision=expected_revision,
                ):
                    logger.info("Docling conversion cancelled for doc %s", doc_id)
                    return None
                await asyncio.sleep(_CONVERSION_POLL_SECONDS)
            # poll() returns as soon as any bytes are readable, but a large
            # result still needs a blocking recv() to transfer and unpickle;
            # run it in a thread so the event loop stays responsive. The task
            # reference lets the cleanup below drain it before the pipe is
            # closed.
            recv_task = asyncio.ensure_future(asyncio.to_thread(recv_conn.recv))
            # Shield the await: a wait_for timeout (or outer cancellation)
            # must not cancel the recv task itself — its thread would keep
            # running while the task looks dead, and the cleanup below could
            # no longer drain it by awaiting.
            return await asyncio.shield(recv_task)

        def _reap_process():
            if process.is_alive():
                process.terminate()
                process.join(timeout=_REAP_JOIN_SECONDS)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=_REAP_JOIN_SECONDS)

        timeout = settings.LIBRARY_CONVERSION_TIMEOUT_SECONDS
        try:
            result = await asyncio.wait_for(_await_result(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Docling conversion timed out after %s seconds for %s",
                timeout, file_path,
            )
            raise DocumentConversionError(file_path, doc_id=doc_id)
        except (EOFError, OSError):
            # Worker died without sending a result (e.g. killed by the OOM
            # killer); surface as a retryable conversion failure.
            raise DocumentConversionError(file_path, doc_id=doc_id)
        finally:
            # terminate/join park their caller for up to the reap grace, so
            # they run in a thread: a timed-out conversion must not freeze
            # the event loop. Reaping first closes the child's pipe end,
            # which unblocks a recv still waiting for a result; only then is
            # the abandoned recv task drained (its exception retrieved) and
            # recv_conn closed — closing it under a thread still blocked in
            # recv would risk fd reuse.
            try:
                await asyncio.to_thread(_reap_process)
                if recv_task is not None:
                    try:
                        await recv_task
                    except Exception:
                        # Drain only: the conversion's outcome is already
                        # decided, so whatever the abandoned recv surfaced
                        # (EOF after the child died, corrupt partial frame)
                        # must not mask it.
                        pass
            finally:
                recv_conn.close()

        if result is None:
            return None  # Cancelled during conversion
        ok, payload = result
        if not ok:
            logger.warning("Docling conversion failed for %s: %s", file_path, payload)
            raise DocumentConversionError(file_path, doc_id=doc_id)
        return payload

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
        expected_revision: int | None = None,
        task_context=None,
    ) -> Dict | None:
        """Extract title/description/keywords using the RA model.

        Args:
            content: Document text to analyze.
            current_title: Existing title. The prompt tells the model to keep
                it unchanged unless it is clearly meaningless or unrelated.
            extract_fields: If provided, only return these fields from the AI result.
                           None = return all fields.
            project_id: If provided, enables DB fallback cancellation checks.
            doc_id: If provided, enables DB fallback cancellation checks.
            expected_revision: Abort when the document was edited during extraction.
            task_context: Background task context for heartbeat-based cancellation.

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

        cancellable = bool(task_context or (project_id and doc_id))

        max_attempts = 1 if cancellable else 3
        timeout = settings.AI_METADATA_TIMEOUT_SECONDS
        for attempt in range(1, max_attempts + 1):
            # Check cancellation between retries
            if cancellable and await self._should_stop(
                project_id, doc_id, task_context, expected_revision
            ):
                logger.info("AI extraction cancelled for doc %s at attempt %d", doc_id, attempt)
                return None

            try:
                from app.services.llm_service import llm_service

                coro = llm_service.call_json(
                    prompt=prompt,
                    system="You are a document metadata extractor. Return ONLY valid JSON, no markdown, no explanation, no code fences.",
                    model_role="ra",
                    timeout=timeout,
                    max_tokens=settings.AI_METADATA_OUTPUT_TOKENS,
                )

                if cancellable:
                    # Cancellable: task-heartbeat checks plus 5s DB checks
                    result = await self._cancellable_llm_call(
                        project_id, doc_id, coro,
                        expected_revision=expected_revision,
                        task_context=task_context,
                    )
                    if result is None:
                        return None
                else:
                    # Not cancellable: the sync route and manual extraction
                    # run without a cancellation context.
                    result = await asyncio.wait_for(coro, timeout=timeout)

                result = self._normalize_ai_metadata_result(result)

                # Filter to requested fields if specified
                if extract_fields is not None:
                    result = {k: v for k, v in result.items() if k in extract_fields}
                return result
            except asyncio.TimeoutError:
                logger.warning(f"AI extraction attempt {attempt} timed out ({timeout}s limit)")
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
            tokens = encoding.encode(content, disallowed_special=())
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
                current = await uow.library.get_by_id(doc_id)
                if current:
                    await uow.library.update_processing_status(
                        doc_id, STATUS_INDEXING,
                        expected_revision=current.revision,
                    )
            from app.services.background_task_service import background_task_service
            await background_task_service.enqueue_rag_index(project_id, doc_id)
            return {
                "success": True,
                "message": "AI field extraction completed",
                "fields": {"title": title, "description": description, "keywords": keywords},
            }
        raise LLMResponseError("AI extraction returned empty result")

    async def reprocess_failed(self, project_id: str, doc_id: str) -> Dict:
        """Re-run processing for a single failed document.

        Only failed documents are accepted: resetting an active document
        would clobber in-flight work and erase its failure context.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        if not doc:
            raise DocumentNotFoundError(doc_id)
        if doc.processing_status != STATUS_FAILED:
            raise ServiceException(
                f"Document '{doc.title}' is {doc.processing_status}, not failed; "
                "only failed documents can be reprocessed",
                code="DOCUMENT_NOT_FAILED", status_code=409,
            )
        if not doc.file_path or not Path(doc.file_path).exists():
            raise FileMissingError(doc.file_path)

        async with UnitOfWork(project_id) as uow:
            new_revision = await uow.library.reset_processing(doc_id)

        from app.services.background_task_service import background_task_service
        await background_task_service.enqueue_document_process(project_id, doc_id)

        return {"success": True, "message": "Reprocessing started", "revision": new_revision}

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
