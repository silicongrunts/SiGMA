"""
Library Repository — CRUD operations for LibraryDocument model.

Only this file (and other files in database/) may import LibraryDocument directly.
All services that need library document data MUST use this repository.
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy import select, or_, asc, desc, func, text, bindparam
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LibraryDocument, parse_keywords
from app.core.document_status import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING
from app.core.logging import get_logger
from app.core.utils import generate_id, to_iso, utcnow

logger = get_logger(__name__)

KEYWORD_TRIGRAM_MIN_CHARS = 3
KEYWORD_MATCH_FIELDS = (
    ("title", "title"),
    ("description", "description"),
    ("content", "content"),
)


def _casefold_span(text_value: str, query: str) -> Optional[tuple[int, int]]:
    """Return the original-text span for a case-insensitive substring match."""
    if not text_value or not query:
        return None

    folded_parts = []
    original_offsets = []
    for index, char in enumerate(text_value):
        folded = char.casefold()
        folded_parts.append(folded)
        original_offsets.extend([index] * len(folded))

    folded_text = "".join(folded_parts)
    folded_query = query.casefold()
    start = folded_text.find(folded_query)
    if start < 0 or not folded_query:
        return None

    end = start + len(folded_query)
    if end > len(original_offsets):
        return None
    return original_offsets[start], original_offsets[end - 1] + 1


def _keyword_match(field_name: str, text_value: str, query: str,
                   context_chars: int = 80) -> Optional[Dict[str, Any]]:
    span = _casefold_span(text_value or "", query)
    if not span:
        return None

    match_start, match_end = span
    snippet_start = max(0, match_start - context_chars)
    snippet_end = min(len(text_value), match_end + context_chars)
    snippet = text_value[snippet_start:snippet_end]
    if snippet_start > 0:
        snippet = "..." + snippet
    if snippet_end < len(text_value):
        snippet += "..."

    return {
        "field": field_name,
        "text": snippet,
        "line": text_value[:match_start].count("\n") + 1,
    }


def _keyword_matches(document: LibraryDocument, query: str) -> List[Dict[str, Any]]:
    matches = []
    for field_name, attr_name in KEYWORD_MATCH_FIELDS:
        match = _keyword_match(field_name, getattr(document, attr_name) or "", query)
        if match:
            matches.append(match)
    return matches


class LibraryRepository:
    """Repository for LibraryDocument table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(
        self,
        title: str,
        content: str = "",
        description: str = "",
        source: str = "",
        doc_type: str = "text",
        keywords: Optional[List[str]] = None,
        file_name: Optional[str] = None,
        file_path: Optional[str] = None,
        is_folder: bool = False,
        parent_id: Optional[str] = None,
        processing_status: str = STATUS_COMPLETED,
    ) -> LibraryDocument:
        doc = LibraryDocument(
            id=generate_id(),
            title=title,
            description=description,
            content=content,
            source=source,
            doc_type=doc_type,
            keywords=json.dumps(keywords or []),
            file_name=file_name,
            file_path=file_path,
            is_folder=is_folder,
            parent_id=parent_id,
            processing_status=processing_status,
            processing_log="",
            processing_started_at=utcnow() if processing_status != STATUS_COMPLETED else None,
        )
        self._session.add(doc)
        await self._session.commit()
        await self._session.refresh(doc)
        return doc

    async def get_by_id(self, doc_id: str) -> Optional[LibraryDocument]:
        result = await self._session.execute(
            select(LibraryDocument).where(LibraryDocument.id == doc_id)
        )
        return result.scalar_one_or_none()

    async def get_child_by_title(
        self,
        title: str,
        parent_id: Optional[str] = None,
    ) -> Optional[LibraryDocument]:
        result = await self._session.execute(
            select(LibraryDocument).where(
                LibraryDocument.title == title,
                LibraryDocument.parent_id == parent_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_summary_by_id(self, doc_id: str, preview_chars: int = 5000) -> Optional[Dict[str, Any]]:
        """Fetch one document without loading the full content column."""
        result = await self._session.execute(
            select(
                LibraryDocument.id.label("id"),
                LibraryDocument.title.label("title"),
                LibraryDocument.description.label("description"),
                LibraryDocument.source.label("source"),
                LibraryDocument.doc_type.label("doc_type"),
                LibraryDocument.keywords.label("keywords"),
                LibraryDocument.revision.label("revision"),
                LibraryDocument.processing_status.label("processing_status"),
                LibraryDocument.processing_started_at.label("processing_started_at"),
                LibraryDocument.processing_completed_at.label("processing_completed_at"),
                LibraryDocument.file_name.label("file_name"),
                LibraryDocument.file_path.label("file_path"),
                LibraryDocument.updated_at.label("updated_at"),
                LibraryDocument.is_folder.label("is_folder"),
                LibraryDocument.parent_id.label("parent_id"),
                func.substr(LibraryDocument.content, 1, preview_chars).label("content_preview"),
                func.length(LibraryDocument.content).label("content_length"),
            ).where(LibraryDocument.id == doc_id)
        )
        row = result.mappings().one_or_none()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "content": "",
            "content_preview": row["content_preview"] or "",
            "content_truncated": (row["content_length"] or 0) > preview_chars,
            "source": row["source"],
            "doc_type": row["doc_type"],
            "keywords": parse_keywords(row["keywords"]),
            "revision": row["revision"],
            "processing_status": row["processing_status"],
            "processing_started_at": to_iso(row["processing_started_at"]),
            "processing_completed_at": to_iso(row["processing_completed_at"]),
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "updated_at": to_iso(row["updated_at"]),
            "is_folder": row["is_folder"],
            "parent_id": row["parent_id"],
        }

    async def get_file_info(self, doc_id: str) -> Optional[Dict]:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return None
        return {"file_path": doc.file_path, "file_name": doc.file_name}

    async def update(self, doc_id: str, data: Dict[str, Any]) -> Optional[LibraryDocument]:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return None

        should_bump_revision = False
        for field in ("title", "description", "content", "source", "doc_type", "keywords"):
            if field in data and data[field] is not None:
                value = data[field]
                # keywords arrives as a list from the API layer; serialize at the
                # repository boundary so the ORM row always stores a JSON string.
                if field == "keywords" and isinstance(value, list):
                    value = json.dumps(value)
                if field in {"title", "description", "content"} and getattr(doc, field) != value:
                    should_bump_revision = True
                setattr(doc, field, value)
        if should_bump_revision:
            doc.revision += 1

        await self._session.commit()
        await self._session.refresh(doc)
        return doc

    async def delete(self, doc_id: str) -> bool:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return False
        await self._session.delete(doc)
        await self._session.commit()
        return True

    # ------------------------------------------------------------------
    # Listing & Pagination
    # ------------------------------------------------------------------

    async def list_all(
        self,
        parent_id: Optional[str] = None,
        sort: str = "updated_at",
        order: str = "desc",
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[LibraryDocument]:
        query = (
            select(LibraryDocument)
            .where(LibraryDocument.parent_id == parent_id)
        )

        sort_col = LibraryDocument.title if sort == "title" else LibraryDocument.updated_at
        sort_fn = asc if order == "asc" else desc
        query = query.order_by(desc(LibraryDocument.is_folder), sort_fn(sort_col))

        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def count_all(
        self, parent_id: Optional[str] = None
    ) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(LibraryDocument)
            .where(LibraryDocument.parent_id == parent_id)
        )
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search_keyword(
        self,
        query: str,
        allowed_ids: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        query = query.strip()
        docs = None
        if len(query) >= KEYWORD_TRIGRAM_MIN_CHARS:
            docs = await self._search_keyword_fts(query, allowed_ids, limit, offset)

        if docs is None:
            docs = await self._search_keyword_like(query, allowed_ids, limit, offset)

        matches = []
        for doc in docs:
            doc_matches = _keyword_matches(doc, query)
            if doc_matches:
                matches.append({"document": doc, "matches": doc_matches})
        return matches

    async def count_search_keyword(
        self,
        query: str,
        allowed_ids: Optional[List[str]] = None,
    ) -> int:
        """Count documents matching the keyword query.

        Mirrors the FTS/LIKE branching of ``search_keyword`` so the count
        is consistent with what search would return absent limit/offset.
        FTS count returns None on DB error so the caller can fall back to LIKE.
        """
        query = query.strip()
        if len(query) >= KEYWORD_TRIGRAM_MIN_CHARS:
            count = await self._count_keyword_fts(query, allowed_ids)
            if count is not None:
                return count
        return await self._count_keyword_like(query, allowed_ids)

    async def _count_keyword_like(
        self,
        query: str,
        allowed_ids: Optional[List[str]],
    ) -> int:
        q = select(func.count()).select_from(LibraryDocument).where(
            or_(
                LibraryDocument.title.contains(query),
                LibraryDocument.description.contains(query),
                LibraryDocument.content.contains(query),
            ),
        )
        if allowed_ids is not None:
            if not allowed_ids:
                return 0
            q = q.where(LibraryDocument.id.in_(allowed_ids))
        result = await self._session.execute(q)
        return result.scalar_one()

    async def _count_keyword_fts(
        self,
        query: str,
        allowed_ids: Optional[List[str]],
    ) -> Optional[int]:
        fts_query = '"' + query.replace('"', '""') + '"'
        sql = """
            SELECT COUNT(*)
            FROM library_documents_fts f
            JOIN library_documents ld ON ld.rowid = f.rowid
            WHERE library_documents_fts MATCH :query
        """
        params: dict[str, Any] = {"query": fts_query}
        if allowed_ids is not None:
            if not allowed_ids:
                return 0
            placeholders = []
            for idx, doc_id in enumerate(allowed_ids):
                key = f"id_{idx}"
                placeholders.append(f":{key}")
                params[key] = doc_id
            sql += f" AND ld.id IN ({', '.join(placeholders)})"
        try:
            result = await self._session.execute(text(sql), params)
        except Exception:
            logger.debug("Library FTS count failed; falling back to caller", exc_info=True)
            return None
        return result.scalar_one()

    async def _search_keyword_like(
        self,
        query: str,
        allowed_ids: Optional[List[str]],
        limit: int,
        offset: int,
    ) -> List[LibraryDocument]:
        q = select(LibraryDocument).where(
            or_(
                LibraryDocument.title.contains(query),
                LibraryDocument.description.contains(query),
                LibraryDocument.content.contains(query),
            ),
        )
        if allowed_ids is not None:
            if not allowed_ids:
                return []
            q = q.where(LibraryDocument.id.in_(allowed_ids))
        q = q.order_by(LibraryDocument.updated_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def _search_keyword_fts(
        self,
        query: str,
        allowed_ids: Optional[List[str]],
        limit: int,
        offset: int,
    ) -> Optional[List[LibraryDocument]]:
        fts_query = '"' + query.replace('"', '""') + '"'
        sql = """
            SELECT ld.id
            FROM library_documents_fts f
            JOIN library_documents ld ON ld.rowid = f.rowid
            WHERE library_documents_fts MATCH :query
        """
        params: dict[str, Any] = {"query": fts_query, "limit": limit, "offset": offset}
        if allowed_ids is not None:
            if not allowed_ids:
                return []
            placeholders = []
            for idx, doc_id in enumerate(allowed_ids):
                key = f"id_{idx}"
                placeholders.append(f":{key}")
                params[key] = doc_id
            sql += f" AND ld.id IN ({', '.join(placeholders)})"
        sql += " ORDER BY rank LIMIT :limit OFFSET :offset"
        try:
            result = await self._session.execute(text(sql), params)
        except Exception:
            logger.debug("Library FTS query failed; falling back to caller", exc_info=True)
            return None
        ids = [row[0] for row in result.all()]
        if not ids:
            return []
        docs = await self.get_by_ids(ids)
        doc_map = {doc.id: doc for doc in docs}
        return [doc_map[doc_id] for doc_id in ids if doc_id in doc_map]

    # ------------------------------------------------------------------
    # Folder Operations
    # ------------------------------------------------------------------

    async def get_descendants(self, folder_id: str) -> List[str]:
        """Recursively collect all descendant IDs of a folder."""
        result = await self._session.execute(
            text("""
                WITH RECURSIVE descendants(id, is_folder) AS (
                    SELECT id, is_folder
                    FROM library_documents
                    WHERE parent_id = :folder_id
                    UNION ALL
                    SELECT child.id, child.is_folder
                    FROM library_documents child
                    JOIN descendants parent ON child.parent_id = parent.id
                    WHERE parent.is_folder = 1
                )
                SELECT id FROM descendants
            """),
            {"folder_id": folder_id},
        )
        return [row[0] for row in result.all()]

    async def get_ancestor_chain(self, doc_id: str) -> List[Dict[str, Any]]:
        """Return the folder chain from the library root down to ``doc_id``'s
        parent folder, as ``[{id, title}, ...]`` (root first).

        ``doc_id`` itself is excluded: the chain describes *where* the document
        lives, so it can be used to rebuild breadcrumbs up to its container.
        A top-level document has an empty chain. A missing ``doc_id`` also
        yields an empty chain rather than raising, so callers can treat
        "not found" uniformly with "no ancestors".
        """
        result = await self._session.execute(
            text("""
                WITH RECURSIVE chain(id, title, parent_id, depth) AS (
                    SELECT id, title, parent_id, 0
                    FROM library_documents
                    WHERE id = :doc_id
                    UNION ALL
                    SELECT parent.id, parent.title, parent.parent_id, child.depth + 1
                    FROM library_documents parent
                    JOIN chain child ON parent.id = child.parent_id
                )
                SELECT id, title FROM chain
                WHERE id != :doc_id
                ORDER BY depth DESC
            """),
            {"doc_id": doc_id},
        )
        return [{"id": row[0], "title": row[1]} for row in result.all()]

    async def get_folder_paths(self, doc_ids: List[str]) -> Dict[str, str]:
        """Build the virtual folder path for each document in one query.

        Batch counterpart of :meth:`get_ancestor_chain`: walks the
        ``parent_id`` chain upward for every ``doc_id`` via a single recursive
        CTE, then joins each ancestor folder's ``title`` with ``" / "`` (root
        to leaf). The library root itself is not part of the path.

        Returns ``{doc_id: path}``; a document at the library root maps to
        ``""``. One extra query per search result page — no N+1.
        """
        if not doc_ids:
            return {}
        result = await self._session.execute(
            text("""
                WITH RECURSIVE chain(doc_id, ancestor_id, title, depth) AS (
                    SELECT id, parent_id, NULL, 0
                    FROM library_documents
                    WHERE id IN :doc_ids
                    UNION ALL
                    SELECT c.doc_id, parent.parent_id, parent.title, c.depth + 1
                    FROM chain c
                    JOIN library_documents parent ON parent.id = c.ancestor_id
                    WHERE c.ancestor_id IS NOT NULL AND parent.is_folder = 1
                )
                SELECT doc_id, title FROM chain
                WHERE title IS NOT NULL
                ORDER BY doc_id, depth DESC
            """).bindparams(bindparam("doc_ids", expanding=True)),
            {"doc_ids": list(doc_ids)},
        )
        paths = {doc_id: "" for doc_id in doc_ids}
        current: Optional[str] = None
        parts: List[str] = []
        for doc_id, title in result.all():
            if doc_id != current:
                if current is not None:
                    paths[current] = " / ".join(parts)
                current = doc_id
                parts = []
            parts.append(title)
        if current is not None:
            paths[current] = " / ".join(parts)
        return paths

    async def move_items(
        self, ids: List[str], target_folder_id: Optional[str]
    ) -> int:
        moved = 0
        for item_id in ids:
            doc = await self.get_by_id(item_id)
            if doc:
                doc.parent_id = target_folder_id
                moved += 1
        await self._session.commit()
        return moved

    async def check_duplicate_title(
        self,
        title: str,
        parent_id: Optional[str] = None,
        exclude_id: Optional[str] = None,
    ) -> bool:
        """Check if a document/folder with the same title exists in the same directory."""
        query = select(LibraryDocument).where(
            LibraryDocument.title == title,
            LibraryDocument.parent_id == parent_id,
        )
        if exclude_id:
            query = query.where(LibraryDocument.id != exclude_id)
        result = await self._session.execute(query)
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Processing Status
    # ------------------------------------------------------------------

    async def update_processing_status(
        self,
        doc_id: str,
        status: str,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        log_append: str = "",
    ) -> None:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return
        if log_append:
            old_log = doc.processing_log or ""
            doc.processing_log = (old_log + "\n" + log_append).strip()
        doc.processing_status = status
        if started_at:
            doc.processing_started_at = started_at
        if completed_at:
            doc.processing_completed_at = completed_at
        self._session.add(doc)
        await self._session.commit()

    async def update_processing_log(self, doc_id: str, log_append: str) -> None:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return
        old_log = doc.processing_log or ""
        timestamp = utcnow().strftime("%H:%M:%S")
        doc.processing_log = (old_log + "\n" + f"[{timestamp}] {log_append}").strip()
        self._session.add(doc)
        await self._session.commit()

    async def update_content(self, doc_id: str, content: str) -> Optional[LibraryDocument]:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return None
        doc.content = content
        self._session.add(doc)
        await self._session.commit()
        await self._session.refresh(doc)
        return doc

    async def update_fields(
        self,
        doc_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        keywords: Optional[list] = None,
        source: Optional[str] = None,
        bump_revision: bool = False,
    ) -> None:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return
        should_bump_revision = False
        if title is not None:
            should_bump_revision = should_bump_revision or doc.title != title
            doc.title = title
        if description is not None:
            should_bump_revision = should_bump_revision or doc.description != description
            doc.description = description
        if keywords is not None:
            doc.keywords = json.dumps(keywords)
        if source is not None:
            doc.source = source
        if bump_revision and should_bump_revision:
            doc.revision += 1
        self._session.add(doc)
        await self._session.commit()

    async def mark_failed(self, doc_id: str, log_append: str = "") -> None:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return
        doc.processing_status = STATUS_FAILED
        old_log = doc.processing_log or ""
        timestamp = utcnow().strftime("%H:%M:%S")
        doc.processing_log = (old_log + "\n" + f"[{timestamp}] {log_append}").strip() if (old_log or log_append) else ""
        doc.processing_completed_at = utcnow()
        self._session.add(doc)
        await self._session.commit()

    async def reset_processing(self, doc_id: str, status: str = STATUS_PENDING) -> None:
        doc = await self.get_by_id(doc_id)
        if not doc:
            return
        doc.processing_status = status
        doc.processing_started_at = utcnow()
        doc.processing_completed_at = None
        doc.processing_log = "Reprocessing started..."
        self._session.add(doc)
        await self._session.commit()

    # ------------------------------------------------------------------
    # Bulk queries
    # ------------------------------------------------------------------

    async def list_by_status(self, status: str) -> List[LibraryDocument]:
        result = await self._session.execute(
            select(LibraryDocument)
            .where(LibraryDocument.processing_status == status)
            .order_by(LibraryDocument.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_ids(self, doc_ids: List[str]) -> List[LibraryDocument]:
        """Fetch multiple documents by their IDs."""
        if not doc_ids:
            return []
        result = await self._session.execute(
            select(LibraryDocument).where(LibraryDocument.id.in_(doc_ids))
        )
        return list(result.scalars().all())

    async def get_all(self) -> List[LibraryDocument]:
        result = await self._session.execute(
            select(LibraryDocument)
        )
        return list(result.scalars().all())

    async def get_doc_status_summary(self) -> List[Dict]:
        """Return lightweight status info for all non-folder documents."""
        result = await self._session.execute(
            select(
                LibraryDocument.id,
                LibraryDocument.title,
                LibraryDocument.processing_status,
                LibraryDocument.parent_id,
            )
            .where(
                LibraryDocument.is_folder == False,  # noqa: E712
            )
        )
        return [
            {"id": r[0], "title": r[1], "processing_status": r[2], "parent_id": r[3]}
            for r in result.all()
        ]
