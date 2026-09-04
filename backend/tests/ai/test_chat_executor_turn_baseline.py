"""chat_executor._load_turn_usage_baseline — seeding the whole-turn stats.

Every chat launch builds a fresh in-memory tracker; the launch reassembles
the whole-turn numbers from the two durable pieces the turn's earlier
tasks left behind: the message rows persisted after the user message
(``base``) and the subagent carry stamped into the pending interaction
checkpoint by the pause that split the turn (``carry``).
"""

import pytest

from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.services.chat_executor import _load_turn_usage_baseline


@pytest.mark.asyncio
async def test_baseline_reads_persisted_window_and_carry(project):
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
        session_id = session.id
        messages = uow.messages
        await messages.create(session_id, role="user", content="turn 1")
        await messages.create(
            session_id, role="assistant", content="a",
            token_count=10, input_tokens=100,
        )
        await messages.create(session_id, role="user", content="turn 2")
        await messages.create(
            session_id, role="assistant", content="",
            token_count=5, input_tokens=30, cached_tokens=7,
        )
        task_id = generate_id()
        await uow.task_state.set_queued(task_id, session_id=session_id)
        await uow.task_state.mark_running(task_id)
        await uow.task_state.mark_awaiting_input(task_id, {
            "checkpoint": {
                "interaction_id": "i1", "interaction_type": "permission",
            },
            "agent_usage_carry": {"input": 400, "output": 30, "cached": 100},
        })

    base, carry = await _load_turn_usage_baseline(project, session_id)

    # Only the rows after the LAST user message count — the first turn's
    # assistant row belongs to the previous turn's stats.
    assert base.to_dict() == {"input": 30, "output": 5, "cached": 7}
    assert carry.to_dict() == {"input": 400, "output": 30, "cached": 100}


@pytest.mark.asyncio
async def test_fresh_turn_baseline_is_zero(project):
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
        session_id = session.id
        await uow.messages.create(session_id, role="user", content="hi")

    base, carry = await _load_turn_usage_baseline(project, session_id)

    assert base.to_dict() == {"input": 0, "output": 0, "cached": 0}
    assert carry.to_dict() == {"input": 0, "output": 0, "cached": 0}
