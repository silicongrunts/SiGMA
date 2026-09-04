"""MessageRepository.stage_update_content — in-place refresh used by the
partial-assistant checkpoint."""

import pytest

from app.database.repos.message_repo import MessageRepository
from app.database.repos.session_repo import SessionRepository


@pytest.mark.database
@pytest.mark.asyncio
async def test_stage_update_content_updates_row_in_place(db_session_factory):
    async with db_session_factory() as db:
        messages = MessageRepository(db)
        # messages.session_id has a production-enforced FK to sessions.id.
        await SessionRepository(db).stage_create(
            session_id="session-1", title="Checkpoint",
        )
        await db.commit()
        created = await messages.create("session-1", role="assistant", content="partial")

        updated = await messages.stage_update_content(
            created.id,
            content="partial + more",
            token_count=12,
        )
        await db.commit()

        assert updated is True
        row = await messages.get_by_id(created.id)
        assert row.content == "partial + more"
        assert row.token_count == 12
        # The row identity (and thus its seq position) is untouched.
        assert row.id == created.id
        assert row.seq == created.seq


@pytest.mark.database
@pytest.mark.asyncio
async def test_stage_update_content_reports_missing_row(db_session_factory):
    async with db_session_factory() as db:
        messages = MessageRepository(db)
        updated = await messages.stage_update_content(
            "no-such-row", content="x",
        )
        assert updated is False
