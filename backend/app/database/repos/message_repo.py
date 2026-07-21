"""
Message Repository — CRUD operations for Message model.

Only this file (and other files in database/) may import Message directly.
"""

from typing import List, Optional, Dict, Any, Tuple

from sqlalchemy import select, delete as sql_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import Message
from app.database.seq_utils import allocate_seq_with_retry, stage_seq_object

logger = get_logger(__name__)


def _normalize_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized == "sigma":
        raise ValueError("Persisted message role must be 'assistant', not UI role 'SiGMA'")
    return normalized or "user"


class MessageRepository:
    """Repository for Message table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _do_create_message(
        self,
        *,
        group_field: str,
        group_value: str,
        staged: bool,
        role: str,
        content: str = "",
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        token_count: int = 0,
        cached_tokens: int = 0,
        input_tokens: int = 0,
        is_boundary: bool = False,
    ) -> Message:
        """Build and persist a Message row under the given FK column.

        ``group_field`` is the column name ("session_id" or "annotation_id").
        It is resolved to the ORM column at call time so the helper stays a
        plain string parameter — easier to read than passing a column object
        and unpacking ``.key`` later. ``staged`` selects between the
        self-committing and UnitOfWork-owned allocation paths.
        """
        group_col = getattr(Message, group_field)
        allocate = stage_seq_object if staged else allocate_seq_with_retry
        return await allocate(
            self._session, Message, group_col, group_value,
            lambda seq: Message(
                **{group_field: group_value},
                role=_normalize_role(role), content=content, tool_calls=tool_calls,
                tool_call_id=tool_call_id, reasoning_content=reasoning_content,
                token_count=token_count, cached_tokens=cached_tokens,
                input_tokens=input_tokens, is_boundary=is_boundary, seq=seq,
            ),
        )

    async def create(
        self,
        session_id: str,
        role: str,
        content: str = "",
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        token_count: int = 0,
        cached_tokens: int = 0,
        input_tokens: int = 0,
        is_boundary: bool = False,
    ) -> Message:
        """Create a message. seq is auto-assigned with retry on conflict.

        Self-commits so that the unique constraint check is visible across
        concurrent connections.
        """
        return await self._do_create_message(
            group_field="session_id", group_value=session_id, staged=False,
            role=role, content=content, tool_calls=tool_calls,
            tool_call_id=tool_call_id, reasoning_content=reasoning_content,
            token_count=token_count, cached_tokens=cached_tokens,
            input_tokens=input_tokens, is_boundary=is_boundary,
        )

    async def stage_create(
        self,
        session_id: str,
        role: str,
        content: str = "",
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        token_count: int = 0,
        cached_tokens: int = 0,
        input_tokens: int = 0,
        is_boundary: bool = False,
    ) -> Message:
        """Stage a session message without committing.

        Use inside ``UnitOfWork.execute_atomic()`` when the message must commit
        together with other repository mutations.
        """
        return await self._do_create_message(
            group_field="session_id", group_value=session_id, staged=True,
            role=role, content=content, tool_calls=tool_calls,
            tool_call_id=tool_call_id, reasoning_content=reasoning_content,
            token_count=token_count, cached_tokens=cached_tokens,
            input_tokens=input_tokens, is_boundary=is_boundary,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_messages(self, session_id: str) -> List[Message]:
        """Get all messages for a session, ordered by seq."""
        query = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq)
        )
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_by_id(self, message_id: str) -> Optional[Message]:
        result = await self._session.execute(
            select(Message).where(Message.id == message_id)
        )
        return result.scalar_one_or_none()

    async def get_last_boundary_seq(self, session_id: str) -> int | None:
        result = await self._session.execute(
            select(func.max(Message.seq))
            .where(Message.session_id == session_id, Message.is_boundary == True)  # noqa: E712
        )
        return result.scalar_one()

    async def get_messages_for_llm(self, session_id: str) -> List[Message]:
        """Get messages that should be sent to the LLM.

        Returns messages from the last is_boundary marker onward.
        If no boundary exists, returns all messages.
        """
        return await self._get_messages_from_last_boundary("session_id", session_id)

    async def get_count(self, session_id: str) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )
        return result.scalar_one()

    async def get_total_tokens(self, session_id: str) -> int:
        """Return cumulative real input+output usage, not context length."""
        result = await self._session.execute(
            select(func.coalesce(func.sum(Message.input_tokens + Message.token_count), 0))
            .where(Message.session_id == session_id)
        )
        return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Delete / Truncate
    # ------------------------------------------------------------------

    async def delete_by_session(self, session_id: str) -> int:
        result = await self._session.execute(
            sql_delete(Message).where(Message.session_id == session_id)
        )
        await self._session.commit()
        return result.rowcount

    async def stage_truncate_from(self, session_id: str, seq: int) -> int:
        """Stage deletion of all messages with seq >= given seq."""
        result = await self._session.execute(
            sql_delete(Message).where(
                Message.session_id == session_id, Message.seq >= seq
            )
        )
        return result.rowcount

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    async def stage_boundary_for_annotation(
        self, annotation_id: str, summary: str,
    ) -> Message:
        """Stage a compaction boundary message for an annotation thread.

        Use inside ``UnitOfWork.execute_atomic()`` so the boundary commits
        together with the surrounding mutation.
        """
        return await self.stage_create_for_annotation(
            annotation_id=annotation_id,
            role="system",
            content=summary,
            is_boundary=True,
        )

    # ------------------------------------------------------------------
    # Chat-history fetch for UI shaping
    # ------------------------------------------------------------------

    async def get_messages_with_boundary(
        self,
        session_id: str,
    ) -> Tuple[List[Message], Optional[int]]:
        """Return raw session messages plus the last compaction boundary seq.

        Callers (typically ``services.ai_service``) shape this tuple into
        UI entries via ``core.message_format.shape_messages_for_ui``.
        Keeping the repo free of UI concerns lets it return plain rows.
        """
        boundary_seq = await self.get_last_boundary_seq(session_id)
        messages = await self.get_messages(session_id)
        return messages, boundary_seq

    # ------------------------------------------------------------------
    # Annotation-specific methods
    # ------------------------------------------------------------------

    async def create_for_annotation(
        self,
        annotation_id: str,
        role: str,
        content: str = "",
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        token_count: int = 0,
        cached_tokens: int = 0,
        input_tokens: int = 0,
        is_boundary: bool = False,
    ) -> Message:
        """Create a message linked to an annotation. seq is auto-assigned with retry."""
        return await self._do_create_message(
            group_field="annotation_id", group_value=annotation_id, staged=False,
            role=role, content=content, tool_calls=tool_calls,
            tool_call_id=tool_call_id, reasoning_content=reasoning_content,
            token_count=token_count, cached_tokens=cached_tokens,
            input_tokens=input_tokens, is_boundary=is_boundary,
        )

    async def stage_create_for_annotation(
        self,
        annotation_id: str,
        role: str,
        content: str = "",
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        reasoning_content: str | None = None,
        token_count: int = 0,
        cached_tokens: int = 0,
        input_tokens: int = 0,
        is_boundary: bool = False,
    ) -> Message:
        """Stage a message linked to an annotation without committing.

        Use inside ``UnitOfWork.execute_atomic()`` when the message must commit
        together with other repository mutations (e.g. AnnotationLoop saving a
        batch of new messages atomically, mirroring QueryLoop's pattern).
        """
        return await self._do_create_message(
            group_field="annotation_id", group_value=annotation_id, staged=True,
            role=role, content=content, tool_calls=tool_calls,
            tool_call_id=tool_call_id, reasoning_content=reasoning_content,
            token_count=token_count, cached_tokens=cached_tokens,
            input_tokens=input_tokens, is_boundary=is_boundary,
        )

    async def get_messages_for_annotation_llm(self, annotation_id: str) -> List[Message]:
        """Get annotation messages from the last compaction boundary onward."""
        return await self._get_messages_from_last_boundary("annotation_id", annotation_id)

    async def _get_messages_from_last_boundary(
        self, group_field: str, group_value: str
    ) -> List[Message]:
        group_col = getattr(Message, group_field)
        boundary_result = await self._session.execute(
            select(func.max(Message.seq))
            .where(group_col == group_value, Message.is_boundary == True)  # noqa: E712
        )
        boundary_seq = boundary_result.scalar_one()

        if boundary_seq is not None:
            query = (
                select(Message)
                .where(group_col == group_value, Message.seq >= boundary_seq)
                .order_by(Message.seq)
            )
        else:
            query = (
                select(Message)
                .where(group_col == group_value)
                .order_by(Message.seq)
            )

        result = await self._session.execute(query)
        return list(result.scalars().all())
