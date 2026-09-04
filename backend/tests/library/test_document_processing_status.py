"""Document processing status constants vs the migrated schema.

The constants in ``app/core/document_status.py`` are the single source of
truth for the lifecycle; these tests verify they actually fit the migrated
``library_documents.processing_status`` column and the repository default,
not just that they match themselves.
"""

import pytest

from app.core.document_status import ACTIVE_STATUSES, ALL_STATUSES, STATUS_COMPLETED
from app.database.repos.library_repo import LibraryRepository


def test_active_statuses_are_a_subset_of_all_statuses():
    """Every status the sweeps treat as 'in flight' must be a member of the
    full status set — a typo in one set silently breaks lifecycle queries."""
    assert ACTIVE_STATUSES <= ALL_STATUSES


@pytest.mark.asyncio
async def test_every_status_constant_is_writable_to_the_migrated_column(
    db_session_factory,
):
    """Each named constant round-trips through the real migrated schema:
    the value fits the column and reads back unchanged via the repository."""
    async with db_session_factory() as db:
        repo = LibraryRepository(db)
        created = [
            (status,
             (await repo.create(
                 title=f"doc-{status}", content="body",
                 processing_status=status,
             )).id)
            for status in sorted(ALL_STATUSES)
        ]

        for status, doc_id in created:
            row = await repo.get_by_id(doc_id)
            assert row is not None
            assert row.processing_status == status


@pytest.mark.asyncio
async def test_repository_default_status_is_a_legal_status(db_session_factory):
    """The repository's implicit default must stay inside ALL_STATUSES —
    a renamed constant here would create rows no lifecycle query matches."""
    async with db_session_factory() as db:
        repo = LibraryRepository(db)
        doc = await repo.create(title="default status", content="body")

        assert STATUS_COMPLETED in ALL_STATUSES
        assert doc.processing_status == STATUS_COMPLETED
