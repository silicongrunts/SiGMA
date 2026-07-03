"""
Annotation Repository — CRUD operations for Annotation model.

Thread replies are stored as Message rows (annotation_id FK).
Only this file (and other files in database/) may import Annotation directly.
"""

from app.core.utils import generate_id
from typing import List, Optional, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Annotation


class AnnotationRepository:
    """Repository for Annotation table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_file(self, file_path: str) -> List[Annotation]:
        result = await self._session.execute(
            select(Annotation)
            .where(Annotation.file_path == file_path)
            .options(selectinload(Annotation.messages))
            .order_by(Annotation.created_at)
        )
        return list(result.scalars().all())

    async def resolve_by_prefix(self, short_id: str) -> tuple[Optional[Annotation], Optional[str]]:
        """Resolve an annotation by full or prefix ID (min 8 chars).

        Returns (annotation, error_message). One of them is None.
        """
        # Exact match first
        anno = await self.get_by_id(short_id)
        if anno:
            return anno, None
        # Prefix match (requires >= 8 chars)
        if len(short_id) < 8:
            return None, f"ID '{short_id}' is too short, please provide at least 8 characters"
        result = await self._session.execute(
            select(Annotation)
            .where(Annotation.id.startswith(short_id))
            .options(selectinload(Annotation.messages))
        )
        matches = list(result.scalars().all())
        if len(matches) == 0:
            return None, f"No annotation found with ID starting with '{short_id}'"
        if len(matches) > 1:
            return None, f"ID '{short_id}' matched {len(matches)} annotations, please provide more characters"
        return matches[0], None

    async def delete_by_prefix(self, short_id: str) -> tuple[bool, Optional[str]]:
        """Delete an annotation by full or prefix ID.

        Returns (success, error_message).
        """
        anno, err = await self.resolve_by_prefix(short_id)
        if err:
            return False, err
        await self._session.delete(anno)
        await self._session.commit()
        return True, None

    async def get_by_id(self, annotation_id: str) -> Optional[Annotation]:
        result = await self._session.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .options(selectinload(Annotation.messages))
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        file_path: str,
        from_pos: int,
        to_pos: int,
        original_text: Optional[str] = None,
        annotation_id: Optional[str] = None,
    ) -> Annotation:
        anno = Annotation(
            id=annotation_id or generate_id(),
            file_path=file_path,
            from_pos=from_pos,
            to_pos=to_pos,
            original_text=original_text,
        )
        self._session.add(anno)
        await self._session.commit()
        await self._session.refresh(anno, attribute_names=["messages"])
        return anno

    async def get_all(self) -> List[Annotation]:
        result = await self._session.execute(
            select(Annotation)
            .options(selectinload(Annotation.messages))
            .order_by(Annotation.created_at)
        )
        return list(result.scalars().all())

    async def save_all(
        self, file_path: str, annotations: List[Dict[str, Any]]
    ) -> None:
        """Synchronize annotation rows for a file with the given list.

        Existing annotation rows are updated in place so their thread messages
        are preserved. Removed annotations are deleted, which cascades their
        messages. New annotation rows are inserted without thread data.
        """
        result = await self._session.execute(
            select(Annotation).where(Annotation.file_path == file_path)
        )
        existing_by_id = {annotation.id: annotation for annotation in result.scalars().all()}
        incoming_ids = set()

        for anno_data in annotations:
            from_pos = anno_data.get("from", 0)
            to_pos = anno_data.get("to", 0)
            original_text = anno_data.get("originalText") or ""
            # Skip annotations with invalid (empty/collapsed) ranges
            if to_pos <= from_pos or not original_text.strip():
                continue
            anno_id = anno_data.get("id") or generate_id()
            incoming_ids.add(anno_id)
            existing = existing_by_id.get(anno_id)
            if existing:
                existing.from_pos = from_pos
                existing.to_pos = to_pos
                existing.original_text = original_text
            else:
                self._session.add(Annotation(
                    id=anno_id,
                    file_path=file_path,
                    from_pos=from_pos,
                    to_pos=to_pos,
                    original_text=original_text,
                ))

        for anno_id, annotation in existing_by_id.items():
            if anno_id not in incoming_ids:
                await self._session.delete(annotation)

        await self._session.commit()

    async def get_annotation(
        self, file_path: str, annotation_id: str
    ) -> Optional[Annotation]:
        """Get a single annotation ORM row (with messages eagerly loaded).

        Returns ``None`` if no annotation matches both *file_path* and
        *annotation_id*. UI serialization is the caller's responsibility
        (see ``services.annotation_service.serialize_annotation``).
        """
        result = await self._session.execute(
            select(Annotation)
            .where(
                Annotation.file_path == file_path,
                Annotation.id == annotation_id,
            )
            .options(selectinload(Annotation.messages))
        )
        return result.scalar_one_or_none()
