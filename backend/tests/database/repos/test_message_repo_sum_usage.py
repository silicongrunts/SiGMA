"""MessageRepository.sum_usage_after_last_user — the per-turn usage window.

The whole-turn token stats are seeded at chat-task launch from the rows
persisted after the turn's user message: assistant rows (per-LLM-call
usage), tool rows (subagent deltas), and compaction boundary rows (the
summarization call's own spend).
"""

import pytest

from app.database.repos.message_repo import MessageRepository
from app.database.repos.session_repo import SessionRepository


async def _seed(db, rows):
    # messages.session_id has a production-enforced FK to sessions.id,
    # so the parent session row must exist before the messages.
    await SessionRepository(db).stage_create(
        session_id="session-1", title="Usage window",
    )
    await db.commit()
    messages = MessageRepository(db)
    for role, content, *rest in rows:
        await messages.create(
            "session-1", role=role, content=content, **(rest[0] if rest else {}),
        )
    await db.commit()


@pytest.mark.database
@pytest.mark.asyncio
async def test_sums_only_rows_after_last_user_message(db_session_factory):
    async with db_session_factory() as db:
        await _seed(db, [
            ("user", "turn 1"),
            ("assistant", "a", {"token_count": 10, "input_tokens": 100}),
            ("user", "turn 2"),
            ("assistant", "b", {"token_count": 5, "input_tokens": 30, "cached_tokens": 7}),
        ])

        sums = await MessageRepository(db).sum_usage_after_last_user("session-1")

    assert sums == {"output": 5, "input": 30, "cached": 7}


@pytest.mark.database
@pytest.mark.asyncio
async def test_tool_and_boundary_rows_count_into_the_window(db_session_factory):
    async with db_session_factory() as db:
        await _seed(db, [
            ("user", "go"),
            ("assistant", "", {
                "tool_calls": "[]", "token_count": 20, "input_tokens": 200,
            }),
            ("tool", "result", {
                "tool_call_id": "t1", "token_count": 9, "input_tokens": 11,
            }),
            ("system", "boundary body", {
                "is_boundary": True, "token_count": 7, "input_tokens": 50,
                "cached_tokens": 3,
            }),
        ])

        sums = await MessageRepository(db).sum_usage_after_last_user("session-1")

    assert sums == {"output": 36, "input": 261, "cached": 3}


@pytest.mark.database
@pytest.mark.asyncio
async def test_no_user_row_yields_zeros(db_session_factory):
    async with db_session_factory() as db:
        await _seed(db, [("assistant", "orphan reply")])

        sums = await MessageRepository(db).sum_usage_after_last_user("session-1")

    assert sums == {"output": 0, "input": 0, "cached": 0}


@pytest.mark.database
@pytest.mark.asyncio
async def test_empty_session_yields_zeros(db_session_factory):
    async with db_session_factory() as db:
        sums = await MessageRepository(db).sum_usage_after_last_user("session-1")

    assert sums == {"output": 0, "input": 0, "cached": 0}
