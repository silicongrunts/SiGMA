"""
Annotation Repository — CRUD operations for Annotation model.

Thread replies are stored as Message rows (annotation_id FK).
Only this file (and other files in database/) may import Annotation directly.
"""

from app.core.utils import generate_id
from typing import List, Optional, Dict, Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Annotation, AnnotationFileState, AnnotationFileTransaction
from app.database.repos.message_repo import MessageRepository
from app.database.repos.file_deletion_repo import FileDeletionRepository


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

    async def get_file_state(self, file_path: str) -> Optional[AnnotationFileState]:
        result = await self._session.execute(
            select(AnnotationFileState).where(AnnotationFileState.file_path == file_path)
        )
        return result.scalar_one_or_none()

    async def ensure_file_state(self, file_path: str, file_hash: str) -> AnnotationFileState:
        await FileDeletionRepository(self._session).assert_available(file_path)
        state = await self.get_file_state(file_path)
        if state is None:
            state = AnnotationFileState(file_path=file_path, revision=0, file_hash=file_hash)
            self._session.add(state)
        return state

    async def apply_mutation_cas(
        self, file_path: str, upserts: List[Dict[str, Any]], delete_ids: List[str],
        expected_revision: int, expected_file_hash: str,
        resulting_file_hash: Optional[str] = None,
    ) -> AnnotationFileState:
        """Apply explicit upserts/deletes with a conditional state update."""
        await FileDeletionRepository(self._session).assert_available(file_path)
        state = await self.get_file_state(file_path)
        if state is None:
            if expected_revision != 0:
                raise ValueError("annotation CAS mismatch")
            state = AnnotationFileState(
                file_path=file_path, revision=0, file_hash=expected_file_hash,
            )
            self._session.add(state)
            await self._session.flush()
        if state.revision != expected_revision or state.file_hash != expected_file_hash:
            raise ValueError("annotation CAS mismatch")

        result = await self._session.execute(
            select(Annotation).where(Annotation.file_path == file_path)
        )
        existing_by_id = {annotation.id: annotation for annotation in result.scalars().all()}
        for data in upserts:
            annotation_id = data.get("id") or generate_id()
            from_pos, to_pos = data.get("from", 0), data.get("to", 0)
            original_text = data.get("originalText") or ""
            if to_pos <= from_pos or not original_text.strip():
                continue
            existing = existing_by_id.get(annotation_id)
            if existing is None:
                self._session.add(Annotation(
                    id=annotation_id, file_path=file_path, from_pos=from_pos,
                    to_pos=to_pos, original_text=original_text,
                ))
                for reply in data.get("thread", []):
                    content = reply.get("content", "")
                    if content.strip():
                        await MessageRepository(self._session).stage_create_for_annotation(
                            annotation_id, reply.get("role", "user"), content,
                        )
            else:
                existing.from_pos, existing.to_pos = from_pos, to_pos
                existing.original_text = original_text
        if delete_ids:
            await self._session.execute(
                delete(Annotation).where(
                    Annotation.file_path == file_path,
                    Annotation.id.in_(delete_ids),
                )
            )
        next_revision = expected_revision + 1
        result = await self._session.execute(
            update(AnnotationFileState)
            .where(
                AnnotationFileState.file_path == file_path,
                AnnotationFileState.revision == expected_revision,
                AnnotationFileState.file_hash == expected_file_hash,
            )
            .values(
                revision=next_revision,
                file_hash=resulting_file_hash or expected_file_hash,
            )
        )
        if result.rowcount != 1:
            raise ValueError("annotation CAS mismatch")
        await self._session.commit()
        await self._session.refresh(state)
        return state

    async def get_transactions(self) -> List[AnnotationFileTransaction]:
        result = await self._session.execute(
            select(AnnotationFileTransaction).order_by(AnnotationFileTransaction.created_at)
        )
        return list(result.scalars().all())

    async def get_transaction(self, transaction_id: str) -> Optional[AnnotationFileTransaction]:
        return await self._session.get(AnnotationFileTransaction, transaction_id)

    async def create_transaction(self, **values) -> AnnotationFileTransaction:
        await FileDeletionRepository(self._session).assert_available(values["file_path"])
        transaction = AnnotationFileTransaction(**values)
        self._session.add(transaction)
        await self._session.commit()
        return transaction

    async def delete_transaction(self, transaction_id: str) -> None:
        await self._session.execute(
            delete(AnnotationFileTransaction).where(AnnotationFileTransaction.id == transaction_id)
        )
        await self._session.commit()

    async def resolve(self, annotation_id: str) -> tuple[Optional[Annotation], Optional[str]]:
        """Resolve an annotation by exact ID.

        Returns (annotation, error_message). One of them is None.
        """
        anno = await self.get_by_id(annotation_id)
        if anno:
            return anno, None
        return None, f"No annotation found with ID '{annotation_id}'"

    async def get_by_id(self, annotation_id: str) -> Optional[Annotation]:
        result = await self._session.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .options(selectinload(Annotation.messages))
        )
        return result.scalar_one_or_none()

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
