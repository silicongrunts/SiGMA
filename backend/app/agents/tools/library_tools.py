"""
Library tools — search, browse, and manage the project knowledge base.

8 tools: library_search, library_ls, library_new, library_mkdir,
         library_mv, library_update, library_get, library_rm.
"""

import json
import os
import shutil
import tempfile
from html import escape as _html_escape
from pathlib import Path
from collections import OrderedDict

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import (
    PROMPT_LIBRARY_SEARCH, PROMPT_LIBRARY_LS, PROMPT_LIBRARY_NEW,
    PROMPT_LIBRARY_MKDIR, PROMPT_LIBRARY_MV, PROMPT_LIBRARY_UPDATE,
    PROMPT_LIBRARY_GET, PROMPT_LIBRARY_RM,
)
from app.core.config import settings
from app.core.document_status import STATUS_PENDING, STATUS_COMPLETED
from app.core.exceptions import FileSystemError, RAGIndexModelMismatchError
from app.core.logging import get_logger
from app.core.utils import is_within, sanitize_filename
from app.core.atomic_file import ProjectFileLock
from app.services.library_service import library_service

logger = get_logger(__name__)


# ── Pure helpers (ID parsing, content formatting, search-result rendering) ──

def short_id(doc_id: str) -> str:
    """Truncate a UUID to 8 chars for display."""
    return doc_id[:8]


def parse_ids(value) -> list:
    """Normalize a string-or-array IDs parameter to a list.

    Handles: native list, JSON array string, comma-separated string, single string.
    """
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        if value.strip().startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        return [v.strip() for v in value.split(",") if v.strip()]
    try:
        return [str(v).strip() for v in value if str(v).strip()]
    except TypeError:
        return []


def parse_fields(value) -> list:
    """Normalize a string-or-array field parameter to a list.

    Handles: native list, JSON array string, comma-separated string, single string.
    Defaults to ``["content"]``.
    """
    if not value or value == "" or value == "content":
        return ["content"]
    if isinstance(value, list):
        result = [str(v).strip() for v in value if str(v).strip()]
        return result or ["content"]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    result = [str(v).strip() for v in parsed if str(v).strip()]
                    return result or ["content"]
            except (json.JSONDecodeError, ValueError):
                pass
        if "," in stripped:
            result = [v.strip() for v in stripped.split(",") if v.strip()]
            return result or ["content"]
        return [stripped]
    return ["content"]


def content_preview(content: str, max_len: int = 50) -> str:
    """Build a start...middle...end preview of content."""
    if len(content) <= max_len * 3:
        return content
    start = content[:max_len].strip()
    mid_start = len(content) // 2 - max_len // 2
    middle = content[mid_start:mid_start + max_len].strip()
    end = content[-max_len:].strip()
    return f"{start}...{middle}...{end}"


def xml_escape(text: str) -> str:
    """Escape text for safe inclusion in XML output."""
    return _html_escape(text, quote=True)


def format_search_results(doc_entries: list, total: int,
                          start: int = None, end: int = None) -> str:
    """Format search result entries as XML.

    Each entry may contain one or more matches.  Example output::

        <doc>
        <id>abc12345</id>
        <title>...</title>
        <match line="42" score="0.85">text</match>
        <match line="108">text</match>
        </doc>

    ``total`` is the real match count (independent of pagination).
    ``start`` / ``end`` (1-indexed inclusive) describe the current page
    window; pass ``None`` to omit window info (e.g. semantic mode, which
    is not paginated).
    """
    if start is not None and end is not None:
        header = f"Found {total} result(s), showing {start}-{end}:"
    else:
        header = f"Found {total} result(s):"
    parts = [header, ""]
    for e in doc_entries:
        parts.append("<doc>")
        parts.append(f"<id>{short_id(e['id'])}</id>")
        if e["title"]:
            parts.append(f"<title>{xml_escape(e['title'])}</title>")
        if e["description"]:
            description = e["description"]
            if len(description) > 300:
                description = description[:300] + "..."
            parts.append(f"<description>{xml_escape(description)}</description>")
        kws = e["keywords"]
        if isinstance(kws, list) and kws:
            parts.append(f"<keyword>{xml_escape(', '.join(str(k) for k in kws))}</keyword>")
        for m in e.get("matches", []):
            attrs = f' line="{m["line"]}"'
            if m.get("field"):
                attrs += f' field="{xml_escape(m["field"])}"'
            if m.get("score") is not None:
                attrs += f' score="{m["score"]}"'
            text = m["text"]
            if len(text) > 400:
                text = text[:400] + "..."
            parts.append(f"<match{attrs}>{xml_escape(text)}</match>")
        parts.append("</doc>")
        parts.append("")
    return "\n".join(parts)


def format_directory_listing(docs: list, label: str) -> str:
    """Format a directory listing."""
    if not docs:
        return f'Directory "{label}": (empty)'

    lines = [f'Directory "{label}":']
    for d in docs:
        sid = short_id(d["id"])
        if d.get("is_folder"):
            lines.append(f"  [{sid}] {d['title']}/")
        else:
            title = d.get("title", "Untitled")
            kws = d.get("keywords", [])
            kw_str = ", ".join(kws) if isinstance(kws, list) and kws else ""
            suffix = f" — {kw_str}" if kw_str else ""
            lines.append(f"  [{sid}] {title}{suffix}")

    return "\n".join(lines)


def format_document_content(doc, fields: list,
                             offset: int = 0, limit: int = 200) -> str:
    """Format document fields (keywords, description, content) for display."""
    result_parts = []

    status = doc.processing_status
    if status != STATUS_COMPLETED:
        result_parts.append(f"[Document not yet processed (status: {status}), temporarily unavailable]")

    for f in fields:
        if f == "keywords":
            kws = json.loads(doc.keywords) if doc.keywords else []
            result_parts.append(f"Keywords: {', '.join(kws) if kws else '(none)'}")
        elif f == "description":
            result_parts.append(f"Description: {doc.description or '(none)'}")
        elif f == "content":
            content = doc.content or ""
            lines = content.split("\n")
            if limit < 0:
                start_idx = max(0, len(lines) + limit)
                end_idx = len(lines)
            else:
                if offset < 0:
                    result_parts.append(f"Error: offset must be >= 0, got {offset}")
                    continue
                start_idx = max(0, offset)
                line_limit = 200 if limit == 0 else limit
                end_idx = min(len(lines), start_idx + line_limit)
            numbered = [f"{i + 1}\t{lines[i]}" for i in range(start_idx, end_idx)]
            content_str = "\n".join(numbered)
            if start_idx > 0:
                content_str = f"... (skipped first {start_idx} lines)\n" + content_str
            if end_idx < len(lines):
                content_str += f"\n\n... ({len(lines) - end_idx} more lines not shown)"
            result_parts.append(content_str)

    return "\n\n".join(result_parts)


# ── Helpers ──────────────────────────────────────────────────────────

async def _resolve_id(project_id: str, short_id_val: str):
    """Resolve a short/full ID via library_service. Returns (doc, error_str)."""
    return await library_service.resolve_by_prefix(project_id, short_id_val)


def _copy_unique_file(source: Path, target: Path) -> Path:
    """Copy source to a unique target path without loading it into memory.

    Appends ``_1``, ``_2``, ... suffixes to deduplicate against existing files.
    Bounded by ``max_attempts`` to prevent runaway loops on pathological
    filesystems; raises ``FileSystemError`` if no unique name is found.
    """
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stem = target.stem
    suffix = target.suffix
    max_attempts = 100
    for attempt in range(max_attempts):
        candidate = target if attempt == 0 else target.parent / f"{stem}_{attempt}{suffix}"
        with ProjectFileLock(candidate):
            if candidate.exists():
                continue
            fd, tmp_name = tempfile.mkstemp(
                dir=str(candidate.parent),
                prefix=".upload_",
                suffix=candidate.suffix or ".tmp",
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as out, source.open("rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp_path, candidate)
                return candidate
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    raise FileSystemError(
        f"Could not find a unique filename after {max_attempts} attempts near {target}"
    )


# ── Tool implementations ────────────────────────────────────────────

# Fixed page size for keyword-search pagination. The LLM navigates via the
# 1-indexed `page` parameter; there is no upper bound on page, so the LLM
# can page through the full match set. Semantic mode is not paginated — it
# always returns the configured top-k chunks and ignores `page`.
KEYWORD_SEARCH_PAGE_SIZE = 50


async def _library_search(project_id: str, query: str, mode: str = "keyword",
                          parent_id: str = "", page: int = 1) -> str:
    """Search library documents by keyword or semantic similarity. Returns XML."""
    if not query.strip():
        return "Error: query cannot be empty"
    if mode not in ("keyword", "semantic"):
        return f"Error: unknown mode '{mode}'. Use 'keyword' or 'semantic'."
    if mode == "keyword" and page < 1:
        # Semantic mode is not paginated and ignores `page`; only keyword
        # mode enforces the 1-indexed lower bound.
        return "Error: page must be >= 1"

    # Resolve parent_id
    resolved_parent_id = None
    if parent_id:
        parent_doc, err = await _resolve_id(project_id, parent_id)
        if err:
            return f"Error: {err}"
        if not parent_doc.is_folder:
            return f"Error: ID '{parent_id}' is not a folder"
        resolved_parent_id = parent_doc.id

    # Run search
    if mode == "keyword":
        offset = (page - 1) * KEYWORD_SEARCH_PAGE_SIZE
        payload = await library_service.search_documents_paged(
            project_id, query, parent_id=resolved_parent_id,
            limit=KEYWORD_SEARCH_PAGE_SIZE, offset=offset,
        )
        raw_results = payload["results"]
        real_total = payload["total"]

        # Distinguish "no matches at all" from "page is out of range". The
        # latter must surface the real count so the LLM does not abandon the
        # query thinking nothing matched.
        if not raw_results:
            if real_total > 0:
                last_page = (real_total + KEYWORD_SEARCH_PAGE_SIZE - 1) // KEYWORD_SEARCH_PAGE_SIZE
                return (
                    f"Found {real_total} result(s) for '{query}', "
                    f"page {page} is out of range (last page: {last_page})."
                )
            return f"No results for '{query}'"
    else:
        try:
            raw_results = await library_service.rag_search(
                project_id, query, parent_id=resolved_parent_id,
            )
        except RAGIndexModelMismatchError:
            return (
                "Error: Embedding model changed since this library was indexed. "
                "Ask the user to rebuild the Library RAG index before running "
                "semantic search or indexing more documents."
            )
        real_total = len(raw_results)

        if not raw_results:
            return f"No results for '{query}'"

    # Build per-doc entries
    if mode == "keyword":
        doc_entries = []
        for r in raw_results:
            doc_entries.append({
                "id": r["id"],
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "keywords": r.get("keywords", []),
                "matches": r.get("search_matches", []),
            })
    else:
        # Group by doc_id — keep all chunks (not just the best)
        grouped = OrderedDict()
        for r in raw_results:
            did = r["id"]
            if did not in grouped:
                grouped[did] = {
                    "id": did,
                    "title": r.get("title", ""),
                    "description": r.get("description", ""),
                    "keywords": r.get("keywords", []),
                    "matches": [],
                }
            chunk_text = r.get("chunk_text", "")
            if len(chunk_text) > 300:
                chunk_text = chunk_text[:300] + "..."
            grouped[did]["matches"].append({
                "text": chunk_text,
                "line": r.get("chunk_line_start", 0),
                "score": r.get("relevance_score"),
            })
        doc_entries = list(grouped.values())

    # Filter out entries with no matches
    doc_entries = [e for e in doc_entries if e["matches"]]

    if not doc_entries:
        return f"No results for '{query}'"

    # Header: keyword mode shows the current page window; semantic mode is
    # not paginated so it gets a plain count header.
    if mode == "keyword":
        start = (page - 1) * KEYWORD_SEARCH_PAGE_SIZE + 1
        end = start - 1 + len(doc_entries)
        return format_search_results(
            doc_entries, total=real_total, start=start, end=end,
        )
    return format_search_results(
        doc_entries, total=real_total, start=None, end=None,
    )


async def _library_ls(project_id: str, parent_id=None) -> str:
    """List directory contents."""
    ids = parse_ids(parent_id)

    if not ids:
        return await _ls_directory(project_id, None, "Root")

    parts = []
    for pid in ids:
        doc, err = await _resolve_id(project_id, pid)
        if err:
            parts.append(f"Error: {err}\n")
            continue
        if not doc.is_folder:
            parts.append(f"ID '{pid}' is not a folder (it's '{doc.title}'), skipping.\n")
            continue
        parts.append(await _ls_directory(project_id, doc.id, doc.title))

    return "\n".join(parts) if parts else "(empty)"


async def _ls_directory(project_id: str, parent_id, label: str) -> str:
    """Format a single directory listing."""
    docs = await library_service.list_documents(project_id, parent_id=parent_id)
    return format_directory_listing(docs, label)


async def _library_new(project_id: str, content_type: str, content: str,
                       title: str, parent_id: str = "") -> str:
    """Add a new document to the library."""
    if not content.strip():
        return "Error: content cannot be empty"
    if not title or not title.strip():
        return "Error: title is required"

    # Resolve parent folder
    resolved_parent = None
    parent_doc = None
    if parent_id:
        parent_doc, err = await _resolve_id(project_id, parent_id)
        if err:
            return f"Error: {err}"
        if not parent_doc.is_folder:
            return f"Error: ID '{parent_id}' is not a folder"
        resolved_parent = parent_doc.id

    doc_title = title.strip()
    doc_content = ""
    doc_source = ""
    doc_type = "text"
    file_path_to_save = None  # tracked for rollback on DB failure

    if content_type == "text":
        doc_content = content
        doc_source = "SiGMA generate"
        doc_type = "text"

    elif content_type == "file":
        file_path = Path(content)

        # project_root is needed both to resolve relative paths and to decide
        # whether the imported file lives inside the project (source = relative)
        # or outside (source = absolute). Reads outside the sandbox are allowed
        # by design — only writes are sandbox-restricted.
        try:
            project_root = settings.get_project_path(project_id)
        except Exception:
            logger.debug("Library tool project lookup failed for %s", project_id, exc_info=True)
            return f"Error: project not found: {project_id}"

        file_path = file_path.resolve() if file_path.is_absolute() else (project_root / content).resolve()

        if not file_path.exists():
            return f"Error: file not found: {content}"
        if not file_path.is_file():
            return f"Error: not a file: {content}"

        ext = file_path.suffix.lower()
        from app.services.document_processing_service import UPLOADABLE_EXTENSIONS
        if ext not in UPLOADABLE_EXTENSIONS:
            return f"Error: unsupported file type: {ext or '(no extension)'}"

        # Source records where the file was imported from: relative when inside
        # the project (portable), absolute otherwise (external read).
        if is_within(file_path, project_root):
            try:
                doc_source = str(file_path.relative_to(project_root))
            except ValueError:
                doc_source = str(file_path)
        else:
            doc_source = str(file_path)
        doc_type = ext.lstrip(".")

        sigma_dir = settings.get_sigma_path(project_id)
        library_dir = sigma_dir / "library"
        library_dir.mkdir(parents=True, exist_ok=True)

        stem = file_path.stem
        target_path = _copy_unique_file(file_path, library_dir / f"{stem}{file_path.suffix}")
        file_path_to_save = str(target_path)

    elif content_type == "tab":
        try:
            markdown, url = await _extract_tab_content(content)
            if not markdown.strip():
                return f"Error: tab '{content}' returned empty content"
            doc_content = markdown
            doc_source = url
            doc_type = "markdown"
        except Exception as e:
            logger.exception("Failed to extract browser tab content for library import")
            return f"Error: failed to extract tab content: {e}"

    else:
        return f"Error: unknown content_type '{content_type}'. Use 'text', 'file', or 'tab'."

    try:
        # Sanitize the display filename for file-type documents
        safe_file_name = None
        if content_type == "file":
            try:
                safe_file_name = sanitize_filename(Path(content).name)
            except FileSystemError:
                return f"Error: invalid filename '{Path(content).name}'"

        doc = await library_service.create_library_document(
            project_id,
            title=doc_title,
            content=doc_content,
            source=doc_source,
            doc_type=doc_type,
            file_name=safe_file_name,
            file_path=file_path_to_save,
            processing_status=STATUS_PENDING,
            parent_id=resolved_parent,
        )

        from app.services.background_task_service import background_task_service
        await background_task_service.enqueue_document_process(project_id, doc.id)

        parent_label = "root" if not resolved_parent else parent_doc.title
        parent_sid = short_id(resolved_parent) if resolved_parent else "root"
        if doc_content:
            preview = content_preview(doc_content)
            return (
                f"Successfully added {content_type} to {parent_label} (ID: {parent_sid}), "
                f"document ID is {short_id(doc.id)}, content: {preview}"
            )
        else:
            return (
                f"Successfully added file to {parent_label} (ID: {parent_sid}), "
                f"document ID is {short_id(doc.id)}, processing..."
            )

    except Exception as e:
        logger.exception("Failed to add library document")
        # Rollback the copied file so DB failures don't leave orphans in the
        # library directory. Only the file we just copied is removed; existing
        # files (e.g. a `_1`-suffixed collision from a prior run) stay intact.
        if file_path_to_save is not None:
            try:
                orphan = Path(file_path_to_save)
                if orphan.exists():
                    orphan.unlink()
            except OSError:
                logger.warning(
                    "Failed to clean up orphaned file %s after DB failure",
                    file_path_to_save, exc_info=True,
                )
        return f"Failed to add document: {e}"


async def _extract_tab_content(tab_id: str) -> tuple:
    """Extract markdown content from a browser tab. Returns (markdown, url)."""
    from app.agents.tools.browser_manager import get_browser_manager
    from app.agents.tools.dom_service import get_dom_service
    from app.agents.tools.browser_tools import _html_to_markdown
    from app.agents.tools.browser_thread import dispatch

    mgr = get_browser_manager()
    page = await dispatch(mgr.get_page(tab_id))
    dom = get_dom_service()
    cleaned_html = await dispatch(dom.build_clean_html(page))
    markdown = _html_to_markdown(cleaned_html)
    return markdown, page.url


async def _library_mkdir(project_id: str, title: str, parent_id: str = "") -> str:
    """Create a new folder."""
    resolved_parent = None
    parent_doc = None
    if parent_id:
        parent_doc, err = await _resolve_id(project_id, parent_id)
        if err:
            return err
        if not parent_doc.is_folder:
            return f"ID '{parent_id}' is not a folder"
        resolved_parent = parent_doc.id

    try:
        folder = await library_service.create_folder(project_id, title, resolved_parent)
        parent_label = "root" if not resolved_parent else f"{parent_doc.title} (ID: {short_id(resolved_parent)})"
        return f"Successfully created folder \"{title}\" (ID: {short_id(folder['id'])}) in {parent_label}"
    except ValueError as e:
        return f"Failed to create folder: {e}"


async def _library_mv(project_id: str, src_id, dst_id: str = "") -> str:
    """Move items to a target folder."""
    src_list = parse_ids(src_id)
    if not src_list:
        return "Error: no source IDs provided"

    target_id = None
    if dst_id:
        dst_doc, err = await _resolve_id(project_id, dst_id)
        if err:
            return err
        if not dst_doc.is_folder:
            return f"Target ID '{dst_id}' is not a folder"
        target_id = dst_doc.id

    resolved_ids = []
    errors = []
    for sid in src_list:
        doc, err = await _resolve_id(project_id, sid)
        if err:
            errors.append(f"  {sid}: {err}")
        else:
            resolved_ids.append(doc.id)

    if errors and not resolved_ids:
        return "Move failed:\n" + "\n".join(errors)

    try:
        result = await library_service.move_items(project_id, resolved_ids, target_id)
        parts = [f"Successfully moved {result['moved']} item(s)"]
        if errors:
            parts.append("Skipped:\n" + "\n".join(errors))
        return "\n".join(parts)
    except Exception as e:
        logger.exception("Failed to move library document")
        return f"Move failed: {e}"


async def _library_update(project_id: str, id: str, title: str = "",
                          description: str = "", old_string: str = "",
                          new_string: str = "") -> str:
    """Update a document's title, description, or content.

    Folders only support title updates. Content replacement (old_string/new_string)
    is forwarded to the service, which performs the read-count-replace-write
    cycle atomically inside a single transaction (closes the TOCTOU window).
    """
    has_old = bool(old_string)
    has_new = bool(new_string)
    if has_old != has_new:
        return "Error: old_string and new_string must both be provided"

    doc, err = await _resolve_id(project_id, id)
    if err:
        return f"Error: {err}"

    # Pre-screen folder edits: only title allowed. Deeper validation (e.g.
    # duplicate name) runs in the service inside the transaction.
    if doc.is_folder and (description or has_old or has_new):
        return ("Error: folders only support title updates; "
                "description and content edits are not supported on folders")

    updates: dict = {}
    if title:
        updates["title"] = title
    if description:
        updates["description"] = description
    if has_old and has_new:
        if old_string == new_string:
            return "Error: old_string and new_string are identical, nothing to change"
        updates["old_string"] = old_string
        updates["new_string"] = new_string

    if not updates:
        return "No changes specified"

    try:
        result = await library_service.update_document(project_id, doc.id, updates)
        if not result:
            return "Update failed: document not found"
        changed = ", ".join(k for k in updates.keys() if k != "old_string" and k != "new_string")
        if "old_string" in updates:
            changed = ("content" if not changed else f"{changed}, content")
        return f"Successfully updated \"{result['title']}\" (ID: {short_id(result['id'])}), changed: {changed}"
    except Exception as e:
        logger.exception("Failed to update library document")
        return f"Update failed: {e}"


async def _library_get(project_id: str, id: str, field=None,
                       offset: int = 0, limit: int = 200) -> str:
    """Read document fields from the library."""
    fields = parse_fields(field)

    doc, err = await _resolve_id(project_id, id)
    if err:
        return err
    if doc.is_folder:
        return "Cannot read folder content, use library_ls to browse directories"

    return format_document_content(doc, fields, offset, limit)


async def _library_rm(project_id: str, id) -> str:
    """Delete documents or folders from the library."""
    id_list = parse_ids(id)
    if not id_list:
        return "Error: no IDs provided"

    resolved_ids = []
    skipped = []
    for short_id_val in id_list:
        doc, err = await _resolve_id(project_id, short_id_val)
        if err:
            skipped.append(f"  {short_id_val}: {err}")
        else:
            resolved_ids.append(doc.id)

    if resolved_ids:
        try:
            result = await library_service.batch_delete(project_id, resolved_ids)
            parts = [f"Successfully deleted {result['deleted']} item(s)"]
            if skipped:
                parts.append("Skipped:\n" + "\n".join(skipped))
            return "\n".join(parts)
        except Exception as e:
            logger.exception("Failed to delete library document")
            return f"Delete failed: {e}"

    return "No documents found to delete\n" + "\n".join(skipped)


# ── Register tools ───────────────────────────────────────────────────

tool_registry.register(ToolDefinition(
    name="library_search",
    description="Search the project library for documents by keyword or semantic similarity.",
    prompt=PROMPT_LIBRARY_SEARCH,
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search content. For keyword mode, provide exact terms. For semantic mode, provide a natural language description."},
            "mode": {"type": "string", "description": "Search mode: 'keyword' or 'semantic'", "default": "keyword"},
            "parent_id": {"type": "string", "description": "Folder ID to restrict search scope (including subdirectories). Omit this field to search the entire library.", "default": ""},
            "page": {"type": "integer", "description": "Page number for keyword mode (50 results per page, 1-indexed). Ignored by semantic mode.", "default": 1},
        },
        "required": ["query"],
    },
    call=lambda query, mode="keyword", parent_id="", page=1, project_id="": _library_search(
        project_id, query, mode, parent_id, page,
    ),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="library_ls",
    description="List contents of library directories.",
    prompt=PROMPT_LIBRARY_LS,
    input_schema={
        "type": "object",
        "properties": {
            "parent_id": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Folder ID(s) to list (provide at least first 8 chars for prefix matching). Omit this field to list the root directory.",
                "default": "",
            },
        },
        "required": [],
    },
    call=lambda parent_id=None, project_id="": _library_ls(project_id, parent_id),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="library_new",
    description="Add a new document to the library from text, file, or browser tab.",
    prompt=PROMPT_LIBRARY_NEW,
    input_schema={
        "type": "object",
        "properties": {
            "content_type": {"type": "string", "description": "Source type: 'text', 'file', or 'tab'"},
            "content": {"type": "string", "description": "Text, absolute/project-relative file path, or tab ID"},
            "title": {"type": "string", "description": "Document title (required — caller must provide; AI extraction never overwrites it)"},
            "parent_id": {"type": "string", "description": "Folder ID to place the document in. Omit this field for root.", "default": ""},
        },
        "required": ["content_type", "content", "title"],
    },
    call=lambda content_type, content, title, parent_id="", project_id="": _library_new(
        project_id, content_type, content, title, parent_id,
    ),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="library_mkdir",
    description="Create a new folder in the library.",
    prompt=PROMPT_LIBRARY_MKDIR,
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Folder name"},
            "parent_id": {"type": "string", "description": "Parent folder ID. Omit this field to create in root.", "default": ""},
        },
        "required": ["title"],
    },
    call=lambda title, parent_id="", project_id="": _library_mkdir(project_id, title, parent_id),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="library_mv",
    description="Move documents or folders to a target folder.",
    prompt=PROMPT_LIBRARY_MV,
    input_schema={
        "type": "object",
        "properties": {
            "src_id": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "A single ID or array of IDs to move (provide at least first 8 chars each for prefix matching)",
            },
            "dst_id": {"type": "string", "description": "Target folder ID. Omit this field to move to root.", "default": ""},
        },
        "required": ["src_id"],
    },
    call=lambda src_id, dst_id="", project_id="": _library_mv(project_id, src_id, dst_id),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="library_update",
    description="Update a document's or folder's title, description, or content. Folders only support title updates.",
    prompt=PROMPT_LIBRARY_UPDATE,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Document or folder ID (provide at least first 8 chars for prefix matching)"},
            "title": {"type": "string", "description": "New title (folders allow only this)", "default": ""},
            "description": {"type": "string", "description": "New description (documents only)", "default": ""},
            "old_string": {"type": "string", "description": "Text to find in the content. Must be unique in the document and must pair with new_string. Documents only.", "default": ""},
            "new_string": {"type": "string", "description": "Replacement text (must pair with old_string, documents only)", "default": ""},
        },
        "required": ["id"],
    },
    call=lambda id, title="", description="", old_string="", new_string="", project_id="": _library_update(
        project_id, id, title, description, old_string, new_string,
    ),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="library_get",
    description="Read a document's fields from the library.",
    prompt=PROMPT_LIBRARY_GET,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Document ID (provide at least first 8 chars for prefix matching). Must be a document, not a folder."},
            "field": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Field(s) to read: \"keywords\", \"description\", or \"content\". Default is [\"content\"].",
                "default": "",
            },
            "offset": {"type": "integer", "description": "0-indexed content start line; must be >=0", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "description": "Maximum lines to read. Omit or pass 0 for 200; negative returns last abs(limit) lines. Only applies to content.", "default": 200},
        },
        "required": ["id"],
    },
    call=lambda id, field=None, offset=0, limit=200, project_id="": _library_get(
        project_id, id, field, offset, limit,
    ),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="library_rm",
    description="Delete one or more documents or folders from the library.",
    prompt=PROMPT_LIBRARY_RM,
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Library item ID(s) (provide at least first 8 chars for prefix match)",
            },
        },
        "required": ["id"],
    },
    call=lambda id, project_id="": _library_rm(project_id, id),
    requires_project_id=True,
    is_read_only=False,
))
