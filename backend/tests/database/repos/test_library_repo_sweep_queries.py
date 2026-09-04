"""Lightweight sweep projections of ``LibraryRepository``.

The maintenance sweep runs every 60 seconds and must not load full
document rows (the content column included); these tests pin the
projections it uses.
"""

import pytest

from app.core.document_status import STATUS_COMPLETED, STATUS_INDEXING, STATUS_PENDING
from app.database.repos.library_repo import LibraryRepository


@pytest.mark.asyncio
async def test_list_status_projection_returns_lightweight_rows(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        pending = await repo.create(
            title="a", content="x" * 10_000, processing_status=STATUS_PENDING,
        )
        indexing = await repo.create(
            title="b", content="body", processing_status=STATUS_INDEXING,
        )
        await repo.create(title="c", content="done", processing_status=STATUS_COMPLETED)

        rows = await repo.list_status_projection((STATUS_PENDING, STATUS_INDEXING))

        assert {row.id for row in rows} == {pending.id, indexing.id}
        assert {row.processing_status for row in rows} == {
            STATUS_PENDING, STATUS_INDEXING,
        }
        assert all(row.processing_started_at is not None for row in rows)
        # Column projection: the content field is never fetched.
        assert all(not hasattr(row, "content") for row in rows)


@pytest.mark.asyncio
async def test_list_ids_returns_all_ids_without_full_rows(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        doc_a = await repo.create(title="a", content="body")
        doc_b = await repo.create(title="b", content="", is_folder=True)

        ids = await repo.list_ids()

        assert set(ids) == {doc_a.id, doc_b.id}


@pytest.mark.asyncio
async def test_list_ids_with_content_filters_blank_content_and_folders(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        keep = await repo.create(title="a", content="body", processing_status=STATUS_COMPLETED)
        await repo.create(title="b", content="   ", processing_status=STATUS_COMPLETED)
        await repo.create(title="c", content="", processing_status=STATUS_COMPLETED)
        await repo.create(
            title="folder", content="", is_folder=True,
            processing_status=STATUS_COMPLETED,
        )

        ids = await repo.list_ids_with_content(STATUS_COMPLETED)

        assert ids == [keep.id]
