"""
Database models for SiGMA session storage.
"""

import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import (
    String, Integer, Text, ForeignKey, DateTime, Boolean, UniqueConstraint,
    CheckConstraint, MetaData,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs
from app.core.utils import generate_id, to_iso, utcnow


# Stable naming convention for all constraints so future migrations can
# reference them by name regardless of how the DB was originally created.
# NOTE: ``ck`` is intentionally omitted — CHECK constraints always carry an
# explicit ``name=`` in the model, and the SQLAlchemy ``ck`` convention would
# wrap that name a second time (e.g. ``ck_messages_ck_message_one_owner``).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def parse_keywords(raw: Optional[str]) -> List[str]:
    """Deserialize the ``LibraryDocument.keywords`` TEXT column to a list.

    The column stores a JSON-encoded array. Centralized here so every consumer
    that goes through ``to_dict`` / ``to_summary_dict`` sees a real list,
    regardless of how the row was written.

    Returns an empty list for NULL/empty/malformed values — callers can
    iterate without re-checking the storage format.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all database models."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Session(Base):
    """
    Chat session for a project.

    Multiple sessions per project. No channel constraint —
    all tabs (Explore, Library, Synthesis) share the same sessions.
    """
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    # Retained for NOT NULL only. This DB file is already project-scoped
    # (one file per project under ``userdata/<id>/.SiGMA/``), so the column
    # is not used for filtering or identity. Writers still populate it with
    # the current project id to satisfy the constraint.
    project_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    # Session kind: "chat" for user sessions, "agent" for hidden agent sessions
    session_kind: Mapped[str] = mapped_column(String(20), default="chat", nullable=False)
    # Agent type for agent sessions: "general", "explore", "plan"
    agent_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Parent session that spawned this agent session
    parent_session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    parent_tool_call_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan",
        order_by="Message.seq"
    )
    tasks: Mapped[List["Task"]] = relationship(
        "Task", back_populates="session", cascade="all, delete-orphan",
        order_by="Task.seq"
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
            "is_archived": self.is_archived,
            "session_kind": self.session_kind,
            "agent_type": self.agent_type,
        }


class Message(Base):
    """
    Self-contained chat message.

    Dual-purpose: chat messages are linked to a session (session_id),
    annotation replies are linked to an annotation (annotation_id).
    Exactly one of session_id / annotation_id is set per row.

    Tool calls are stored as JSON in the message row itself.
    No separate Parts table — one message is one row.
    """
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_message_session_seq"),
        UniqueConstraint("annotation_id", "seq", name="uq_message_annotation_seq"),
        CheckConstraint(
            """
            (session_id IS NOT NULL AND annotation_id IS NULL)
            OR (session_id IS NULL AND annotation_id IS NOT NULL)
            """,
            name="ck_message_one_owner",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    session_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    annotation_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("annotations.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    role: Mapped[str] = mapped_column(String(50), nullable=False)  # "user", "assistant", "system", "tool"
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_calls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of tool calls
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # For role="tool"
    reasoning_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Thinking-model chain-of-thought

    # Token tracking (for compaction + cost control + UI display)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Compaction: boundary marker — start of a compacted segment
    is_boundary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Sequential order within the session/annotation (for ordering and truncation)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Relationships
    session: Mapped[Optional["Session"]] = relationship("Session", back_populates="messages")
    annotation: Mapped[Optional["Annotation"]] = relationship("Annotation", back_populates="messages")


class Annotation(Base):
    """File annotations (comments on specific text ranges).

    Thread replies are stored as Message rows with annotation_id set.
    """
    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)

    file_path: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    from_pos: Mapped[int] = mapped_column(Integer, nullable=False)
    to_pos: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="annotation", cascade="all, delete-orphan",
        order_by="Message.seq",
    )

    def to_dict(self) -> Dict[str, Any]:
        """Field-level serialization only (no thread construction).

        For the full UI dict including the merged thread, use
        ``serialize_annotation(annotation)`` from ``app.services.annotation_service``.
        """
        return {
            "id": self.id,
            "from": self.from_pos,
            "to": self.to_pos,
            "originalText": self.original_text,
        }


class LibraryDocument(Base):
    """Documents in the Library tab for a project."""
    __tablename__ = "library_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(50), default="text")
    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    embedding_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(20), default="completed", nullable=False)
    processing_log: Mapped[Optional[str]] = mapped_column(Text, default="", nullable=False)
    processing_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    processing_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    file_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    parent_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("library_documents.id", ondelete="CASCADE"), nullable=True, index=True)
    is_folder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "content": self.content,
            "source": self.source,
            "doc_type": self.doc_type,
            "keywords": parse_keywords(self.keywords),
            "revision": self.revision,
            "processing_status": self.processing_status,
            "processing_started_at": to_iso(self.processing_started_at),
            "processing_completed_at": to_iso(self.processing_completed_at),
            "file_name": self.file_name,
            "file_path": self.file_path,
            "updated_at": to_iso(self.updated_at),
            "is_folder": self.is_folder,
            "parent_id": self.parent_id,
        }

    def to_summary_dict(self) -> Dict[str, Any]:
        """Lightweight serialization for list views (excludes content)."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "doc_type": self.doc_type,
            "keywords": parse_keywords(self.keywords),
            "revision": self.revision,
            "processing_status": self.processing_status,
            "processing_log": self.processing_log,
            "processing_started_at": to_iso(self.processing_started_at),
            "processing_completed_at": to_iso(self.processing_completed_at),
            "file_name": self.file_name,
            "updated_at": to_iso(self.updated_at),
            "is_folder": self.is_folder,
            "parent_id": self.parent_id,
        }


class Task(Base):
    """Task/todo item for a session."""
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_task_session_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_id)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)

    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending, in_progress, completed, deleted
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    session: Mapped["Session"] = relationship("Session", back_populates="tasks")


class TaskState(Base):
    """Task heartbeat and status tracking per project."""
    __tablename__ = "task_state"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    owner_type: Mapped[str] = mapped_column(String(50), default="chat_session", nullable=False, index=True)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    task_type: Mapped[str] = mapped_column(String, default="llm")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    heartbeat_at: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    interaction_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON for pending user interaction
    created_at: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[str] = mapped_column(String, default="")


class BackgroundTask(Base):
    """Durable background task queue entry scoped to a project.

    Huey is used only to wake worker loops.  This table is the source of truth
    for library/background task state, retry, leasing, and crash recovery.
    """
    __tablename__ = "background_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Retained for NOT NULL only. See Session.project_id for the rationale:
    # this DB is already project-scoped, and the worker passes the project_id
    # explicitly through its claim/run pipeline rather than reading this column.
    project_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    queue: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(200), index=True, nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "queue": self.queue,
            "status": self.status,
            "priority": self.priority,
            "payload_json": self.payload_json,
            "dedupe_key": self.dedupe_key,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "lease_owner": self.lease_owner,
            "lease_expires_at": to_iso(self.lease_expires_at),
            "heartbeat_at": to_iso(self.heartbeat_at),
            "error": self.error,
            "created_at": to_iso(self.created_at),
            "updated_at": to_iso(self.updated_at),
            "started_at": to_iso(self.started_at),
            "completed_at": to_iso(self.completed_at),
        }


class ProjectConfig(Base):
    """Key-value configuration for a project (auto-snapshot settings, etc.)."""
    __tablename__ = "project_config"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
