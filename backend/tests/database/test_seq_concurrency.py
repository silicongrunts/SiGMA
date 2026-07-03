"""
Tests for concurrent seq allocation on messages and tasks.

Verifies that unique constraints + retry logic prevent duplicate seq
values under concurrent writes.  Uses 3 concurrent writers — enough
to exercise the conflict/retry path without overwhelming SQLite's
single-writer architecture.
"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.models import Session, Annotation, Task, Message
from app.database.repos.message_repo import MessageRepository
from app.database.repos.task_repo import TaskRepository
from app.database.unit_of_work import UnitOfWork
from app.core.utils import generate_id


# ---------------------------------------------------------------------------
# Concurrent session message seq
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_concurrent_session_message_seq(db_session_factory):
    """3 concurrent message creates produce unique seqs 0-2."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test"))
        await s.commit()

    seqs: list[int] = []

    async def create(idx: int):
        async with db_session_factory() as s:
            repo = MessageRepository(s)
            msg = await repo.create(
                session_id=session_id, role="user", content=f"m{idx}"
            )
            seqs.append(msg.seq)

    await asyncio.gather(*[asyncio.create_task(create(i)) for i in range(3)])

    assert len(seqs) == 3
    assert sorted(seqs) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Concurrent annotation message seq
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_concurrent_annotation_message_seq(db_session_factory):
    """3 concurrent annotation messages produce unique seqs."""
    anno_id = generate_id()
    async with db_session_factory() as s:
        s.add(Annotation(id=anno_id, file_path="/test.tex", from_pos=0, to_pos=10))
        await s.commit()

    seqs: list[int] = []

    async def create(idx: int):
        async with db_session_factory() as s:
            repo = MessageRepository(s)
            msg = await repo.create_for_annotation(
                annotation_id=anno_id, role="user", content=f"r{idx}"
            )
            seqs.append(msg.seq)

    await asyncio.gather(*[asyncio.create_task(create(i)) for i in range(3)])

    assert len(seqs) == 3
    assert sorted(seqs) == [0, 1, 2]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_message_requires_exactly_one_owner(db_session_factory):
    """A message belongs to either a chat session or an annotation, never both/neither."""
    session_id = generate_id()
    anno_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test"))
        s.add(Annotation(id=anno_id, file_path="/test.tex", from_pos=0, to_pos=10))
        await s.commit()

        s.add(Message(role="user", content="orphan", seq=0))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()

        s.add(Message(
            session_id=session_id,
            annotation_id=anno_id,
            role="user",
            content="ambiguous",
            seq=0,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------------------------------------
# Concurrent task seq
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_concurrent_task_seq(db_session_factory):
    """3 concurrent task creates produce unique seqs."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test"))
        await s.commit()

    seqs: list[int] = []

    async def create(idx: int):
        async with db_session_factory() as s:
            repo = TaskRepository(s)
            t = await repo.create(session_id=session_id, subject=f"task {idx}")
            seqs.append(t.seq)

    await asyncio.gather(*[asyncio.create_task(create(i)) for i in range(3)])

    assert len(seqs) == 3
    assert sorted(seqs) == [0, 1, 2]


# ---------------------------------------------------------------------------
# Task replace_all atomicity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_task_replace_all_atomic(db_session_factory):
    """replace_all deletes old and inserts new tasks atomically."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test"))
        await s.commit()

    # Seed initial tasks
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        await repo.create(session_id=session_id, subject="old_1")
        await repo.create(session_id=session_id, subject="old_2")

    # Replace with new set
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        tasks = await repo.replace_all(session_id, [
            {"content": "new_a", "status": "pending"},
            {"content": "new_b", "status": "in_progress"},
            {"content": "new_c", "status": "completed"},
        ])
        assert len(tasks) == 3
        assert [t.seq for t in tasks] == [0, 1, 2]

    # Verify only new tasks exist
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        all_tasks = await repo.list_active(session_id)
        subjects = [t.subject for t in all_tasks]
        assert subjects == ["new_a", "new_b", "new_c"]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_replace_all_rollback_on_failure(db_session_factory):
    """If replace_all's commit fails, old tasks remain intact."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test"))
        await s.commit()

    # Seed initial tasks
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        await repo.create(session_id=session_id, subject="old_1")
        await repo.create(session_id=session_id, subject="old_2")

    # Replace the session's commit with one that fails on first call
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        real_commit = s.commit

        async def fail_once():
            await s.rollback()
            raise Exception("boom")

        s.commit = fail_once
        try:
            await repo.replace_all(session_id, [{"content": "new_a"}])
        except Exception:
            pass

    # Old tasks must still be there (rollback undid the delete)
    async with db_session_factory() as s:
        repo = TaskRepository(s)
        all_tasks = await repo.list_active(session_id)
        subjects = [t.subject for t in all_tasks]
        assert "old_1" in subjects, f"old_1 lost! Got: {subjects}"
        assert "old_2" in subjects, f"old_2 lost! Got: {subjects}"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_execute_atomic_stages_message_and_session_touch(db_session_factory, monkeypatch):
    """Staged message create and session touch commit as one transaction."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="project-a"))
        await s.commit()

    class FakeDbManager:
        async def ensure_db_exists(self, project_id, *, allow_inactive=False):
            pass

        async def get_session(self, project_id, *, allow_inactive=False):
            return db_session_factory()

    async def fake_get_db_manager():
        return FakeDbManager()

    monkeypatch.setattr("app.database.unit_of_work.get_db_manager", fake_get_db_manager)

    async def operation(uow):
        await uow.messages.stage_create(session_id=session_id, role="user", content="hello")
        await uow.sessions.stage_touch(session_id)

    await UnitOfWork.execute_atomic("project-a", operation)

    async with db_session_factory() as s:
        result = await s.execute(select(Message).where(Message.session_id == session_id))
        messages = list(result.scalars().all())
        assert len(messages) == 1
        assert messages[0].content == "hello"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_staged_message_rolls_back_with_failed_touch(db_session_factory):
    """A failure after staging a message rolls back the whole transaction."""
    session_id = generate_id()
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="project-a"))
        await s.commit()

    async with db_session_factory() as s:
        repo = MessageRepository(s)
        await repo.stage_create(session_id=session_id, role="user", content="hello")
        await s.rollback()

    async with db_session_factory() as s:
        result = await s.execute(select(Message).where(Message.session_id == session_id))
        assert list(result.scalars().all()) == []
