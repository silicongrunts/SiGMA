"""LibraryRepository folder-hierarchy queries: ancestor chains and the
batched root-to-leaf folder paths used for search result context."""

import pytest

from app.database.repos.library_repo import LibraryRepository


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_ancestor_chain_returns_root_to_parent_for_nested_doc(db_session_factory):
    """The chain is root→parent of the doc, excluding the doc itself, root first."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        root_folder = await repo.create(title="Root", content="", is_folder=True)
        mid_folder = await repo.create(
            title="Mid", content="", is_folder=True, parent_id=root_folder.id
        )
        doc = await repo.create(title="Doc", content="body", parent_id=mid_folder.id)

        chain = await repo.get_ancestor_chain(doc.id)

    assert [c["id"] for c in chain] == [root_folder.id, mid_folder.id]
    assert [c["title"] for c in chain] == ["Root", "Mid"]


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_ancestor_chain_empty_for_top_level_doc(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        doc = await repo.create(title="Top", content="body")

        chain = await repo.get_ancestor_chain(doc.id)

    assert chain == []


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_ancestor_chain_empty_for_missing_doc(db_session_factory):
    """A non-existent id yields an empty chain rather than raising."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)

        chain = await repo.get_ancestor_chain("does-not-exist")

    assert chain == []


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_folder_paths_builds_root_to_leaf_path(db_session_factory):
    """A nested document maps to 'Root / Mid', root first, doc itself excluded."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        root_folder = await repo.create(title="Root", content="", is_folder=True)
        mid_folder = await repo.create(
            title="Mid", content="", is_folder=True, parent_id=root_folder.id
        )
        doc = await repo.create(title="Doc", content="body", parent_id=mid_folder.id)

        paths = await repo.get_folder_paths([doc.id])

    assert paths == {doc.id: "Root / Mid"}


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_folder_paths_empty_for_top_level_doc(db_session_factory):
    """A document at the library root maps to an empty string."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        doc = await repo.create(title="Top", content="body")

        paths = await repo.get_folder_paths([doc.id])

    assert paths == {doc.id: ""}


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_folder_paths_batches_multiple_docs(db_session_factory):
    """One query resolves different-depth paths for several docs at once."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        outer = await repo.create(title="Outer", content="", is_folder=True)
        inner = await repo.create(
            title="Inner", content="", is_folder=True, parent_id=outer.id
        )
        deep_doc = await repo.create(title="Deep", content="body", parent_id=inner.id)
        shallow_doc = await repo.create(title="Shallow", content="body", parent_id=outer.id)
        root_doc = await repo.create(title="RootLevel", content="body")

        paths = await repo.get_folder_paths([deep_doc.id, shallow_doc.id, root_doc.id])

    assert paths == {
        deep_doc.id: "Outer / Inner",
        shallow_doc.id: "Outer",
        root_doc.id: "",
    }


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_folder_paths_missing_id_is_empty(db_session_factory):
    """Unknown ids are returned as empty paths rather than raising."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        doc = await repo.create(title="Real", content="body")

        paths = await repo.get_folder_paths([doc.id, "does-not-exist"])

    assert paths == {doc.id: "", "does-not-exist": ""}


@pytest.mark.database
@pytest.mark.asyncio
async def test_get_folder_paths_empty_input_returns_empty(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)

        paths = await repo.get_folder_paths([])

    assert paths == {}
