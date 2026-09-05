"""
Unit of Work — provides a DB session and repositories for a single project.

Usage::

    async with UnitOfWork(project_id) as uow:
        messages = await uow.messages.get_messages(session_id)
        # Services must explicitly call commit when using non-self-commit methods:
        await uow.commit()

**Important — commit is NOT automatic.**  ``__aexit__`` only closes the
session; it does **not** commit.  Either call ``await uow.commit()`` explicitly
or rely on a repository method that self-commits (see below).

Self-commit methods
-------------------
Some repository mutation methods call ``session.commit()`` internally. New code
that needs cross-repository atomicity should use staged repository methods
inside ``UnitOfWork.execute_atomic()``.

Methods that self-commit (**every mutation except those listed below**):

* ``SessionRepository``: ``create``, ``update``, ``delete``
* ``MessageRepository``: ``create``,
  ``delete_by_session``, ``create_for_annotation``
* ``AnnotationRepository``: ``apply_mutation_cas``,
  ``create_transaction``, ``delete_transaction``
* ``LibraryRepository``: ``create``, ``update``, ``delete``, ``move_items``,
  ``update_processing_status``, ``update_processing_log``, ``update_content``,
  ``update_fields``, ``mark_failed``, ``reset_processing``
* ``TaskRepository``: ``create``, ``replace_all``, ``delete_by_session``
* ``TaskStateRepository``: ``set_queued``, ``mark_running``, ``mark_completed``,
  ``mark_failed``, ``mark_cancelled``, ``request_cancel``,
  ``mark_awaiting_input``, ``claim_interaction``,
  ``prune_terminal_by_session``, ``prune_terminal_by_owner``,
  ``delete_by_owner``, ``fail_all_runnable``
* ``ProjectConfigRepository``: ``set``
* ``BackgroundTaskRepository``: all mutation methods

Methods that do **NOT** self-commit (caller must ``uow.commit()``):

* ``SessionRepository``: ``stage_touch``, ``stage_create``
* ``MessageRepository``: ``stage_create``, ``stage_truncate_from``,
  ``stage_copy_messages``, ``stage_create_for_annotation``,
  ``stage_boundary_for_annotation``
* ``TaskRepository.update`` — modifies ORM attributes only
* ``FileDeletionRepository``: ``stage_begin``, ``stage_finish``

Atomic bulk methods (self-commit as a single transaction):

* ``TaskRepository.replace_all`` — deletes all + inserts in one commit

Do NOT mix self-commit methods with ``uow.commit()`` expecting a single
atomic transaction — the self-commit breaks the transaction boundary.

Preferred atomic pattern::

    async def _op(uow):
        await uow.messages.stage_create(...)
        await uow.sessions.stage_touch(...)

    await UnitOfWork.execute_atomic(project_id, _op)
"""

import asyncio

from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.logging import get_logger
from app.database.manager import get_db_manager
from app.database.repos.session_repo import SessionRepository
from app.database.repos.message_repo import MessageRepository
from app.database.repos.annotation_repo import AnnotationRepository
from app.database.repos.file_deletion_repo import FileDeletionRepository
from app.database.repos.library_repo import LibraryRepository
from app.database.repos.task_state_repo import TaskStateRepository
from app.database.repos.task_repo import TaskRepository
from app.database.repos.config_repo import ProjectConfigRepository
from app.database.repos.background_task_repo import BackgroundTaskRepository
from app.database.seq_utils import MAX_RETRIES, RETRY_DELAY

logger = get_logger(__name__)


class UnitOfWork:
    """Scoped database unit of work for a single project.

    Provides a DB session and repository instances for a single project.
    Normal usage::

        async with UnitOfWork(project_id) as uow:
            messages = await uow.messages.get_messages(session_id)

    **Commit is NOT automatic on clean exit.**  Either call
    ``await uow.commit()`` explicitly, or rely on repository methods that
    self-commit (see module docstring for the full catalogue).

    **Self-commit exception:** Most repository mutation methods commit
    internally (e.g. ``TaskRepository.create()``, ``MessageRepository.create()``).
    Seq-allocating methods use
    self-commit to make uniqueness conflicts visible across concurrent
    connections; other self-commit methods preserve the repository API's
    transaction boundary. Do NOT wrap them in a transaction that expects a
    single ``uow.commit()`` at the end — the self-commit will release the lock
    early.

    For atomic bulk operations, use dedicated methods like
    ``TaskRepository.replace_all()`` which handles delete + insert in a
    single commit.
    """

    def __init__(self, project_id: str, *, allow_inactive: bool = False,
                 immediate: bool = False):
        self.project_id = project_id
        self.allow_inactive = allow_inactive
        self._immediate = immediate
        # Repos (initialized on enter)
        self.sessions: SessionRepository = None  # type: ignore
        self.messages: MessageRepository = None  # type: ignore
        self.annotations: AnnotationRepository = None  # type: ignore
        self.file_deletions = None
        self.library: LibraryRepository = None  # type: ignore
        self.task_state: TaskStateRepository = None  # type: ignore
        self.tasks: TaskRepository = None  # type: ignore
        self.config: ProjectConfigRepository = None  # type: ignore
        self.background_tasks: BackgroundTaskRepository = None  # type: ignore
        self._db_manager = None
        self._session = None

    async def __aenter__(self):
        self._db_manager = await get_db_manager()
        await self._db_manager.ensure_db_exists(
            self.project_id,
            allow_inactive=self.allow_inactive,
        )

        self._session = await self._db_manager.get_session(
            self.project_id,
            allow_inactive=self.allow_inactive,
        )
        try:
            # Initialize repositories with the active session
            self.sessions = SessionRepository(self._session)
            self.messages = MessageRepository(self._session)
            self.annotations = AnnotationRepository(self._session)
            self.file_deletions = FileDeletionRepository(self._session)
            self.library = LibraryRepository(self._session)
            self.task_state = TaskStateRepository(self._session)
            self.tasks = TaskRepository(self._session)
            self.config = ProjectConfigRepository(self._session)
            self.background_tasks = BackgroundTaskRepository(self._session)

            if self._immediate:
                # Begin the transaction as a writer immediately: every statement
                # below — including guard reads — runs inside one write
                # transaction, so a concurrent writer either committed before
                # this read (and is visible to the guard) or serializes after
                # this commit. A deferred transaction would instead read without
                # a snapshot and let the later write proceed on fresh data.
                await self._session.execute(sa_text("BEGIN IMMEDIATE"))
            return self
        except BaseException:
            await self._session.close()
            await self._db_manager.release_session(self.project_id)
            raise

    async def __aexit__(self, exc_type, _exc_val, _exc_tb):
        if exc_type:
            try:
                await self._session.rollback()
            except Exception:
                logger.debug("UnitOfWork rollback failed", exc_info=True)
        try:
            await self._session.close()
        except Exception:
            logger.debug("UnitOfWork session close failed", exc_info=True)
        finally:
            await self._db_manager.release_session(self.project_id)

    async def commit(self):
        await self._session.commit()

    async def rollback(self):
        await self._session.rollback()

    @classmethod
    async def execute_atomic(
        cls,
        project_id: str,
        operation,
        *,
        immediate: bool = False,
        max_retries: int = MAX_RETRIES,
    ):
        """Run staged repository operations in one commit with seq-conflict retry.

        ``operation`` receives a UnitOfWork and must use non-self-commit methods.
        If a concurrent writer wins the same unique seq value, the whole
        operation is retried from a fresh session so all related writes remain
        atomic. With ``immediate=True`` the transaction is begun as a writer
        (BEGIN IMMEDIATE) before the operation runs, so a guard read inside
        ``operation`` is serialized against concurrent claims.
        """
        for attempt in range(max_retries):
            async with cls(project_id, immediate=immediate) as uow:
                try:
                    result = await operation(uow)
                    await uow.commit()
                    return result
                except (IntegrityError, OperationalError):
                    await uow.rollback()
                    if attempt >= max_retries - 1:
                        raise
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    @property
    def session(self):
        """Public accessor for the underlying database session."""
        return self._session
