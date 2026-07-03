import pytest

from app.database.repos.library_repo import LibraryRepository


@pytest.mark.asyncio
async def test_keyword_search_matches_title_description_and_content(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        title_doc = await repo.create(title="Quantum Atlas", content="body")
        description_doc = await repo.create(
            title="notes",
            description="Contains Graphene references",
            content="body",
        )
        content_doc = await repo.create(
            title="notes",
            description="",
            content="The appendix mentions vector databases.",
        )

        title_results = await repo.search_keyword("Quantum")
        description_results = await repo.search_keyword("graphene")
        content_results = await repo.search_keyword("VECTOR")

    assert [r["document"].id for r in title_results] == [title_doc.id]
    assert title_results[0]["matches"][0]["field"] == "title"
    assert [r["document"].id for r in description_results] == [description_doc.id]
    assert description_results[0]["matches"][0]["field"] == "description"
    assert [r["document"].id for r in content_results] == [content_doc.id]
    assert content_results[0]["matches"][0]["field"] == "content"


@pytest.mark.asyncio
async def test_keyword_search_uses_substring_matching_for_trigram_queries(db_session_factory):
    cjk_content = "\u7f51\u9875\u5feb\u7167\u5305\u542b\u4e2d\u6587\u6d4b\u8bd5\u6750\u6599"
    cjk_query = "\u4e2d\u6587\u6d4b"
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        english_doc = await repo.create(title="notes", content="alpha helloWorld omega")
        cjk_doc = await repo.create(title="notes", content=cjk_content)

        english_results = await repo.search_keyword("loW")
        cjk_results = await repo.search_keyword(cjk_query)

    assert [r["document"].id for r in english_results] == [english_doc.id]
    assert english_results[0]["matches"][0]["field"] == "content"
    assert [r["document"].id for r in cjk_results] == [cjk_doc.id]
    assert cjk_query in cjk_results[0]["matches"][0]["text"]


@pytest.mark.asyncio
async def test_keyword_search_falls_back_for_short_queries(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        doc = await repo.create(title="AI report", content="body")

        results = await repo.search_keyword("AI")

    assert [r["document"].id for r in results] == [doc.id]
    assert results[0]["matches"][0]["field"] == "title"


@pytest.mark.asyncio
async def test_keyword_search_respects_allowed_ids_after_verification(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        allowed_doc = await repo.create(title="matching document", content="target phrase")
        await repo.create(title="matching document", content="target phrase")

        results = await repo.search_keyword("target", allowed_ids=[allowed_doc.id])

    assert [r["document"].id for r in results] == [allowed_doc.id]


@pytest.mark.asyncio
async def test_count_search_keyword_matches_search_result_count(db_session_factory):
    """count_search_keyword must return the same total that search_keyword
    would return absent limit/offset. This is the contract the tool-layer
    pagination header relies on."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        await repo.create(title="alpha match", content="body")
        await repo.create(title="notes", description="alpha too", content="body")
        await repo.create(title="notes", content="contains alpha here")
        await repo.create(title="no hit here", content="nothing")

        full = await repo.search_keyword("alpha", limit=100, offset=0)
        count = await repo.count_search_keyword("alpha")

    assert count == len(full) == 3


@pytest.mark.asyncio
async def test_count_search_keyword_respects_allowed_ids(db_session_factory):
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        allowed = await repo.create(title="alpha match", content="body")
        await repo.create(title="alpha other", content="body")

        full_count = await repo.count_search_keyword("alpha")
        scoped_count = await repo.count_search_keyword(
            "alpha", allowed_ids=[allowed.id],
        )

    assert full_count == 2
    assert scoped_count == 1


@pytest.mark.asyncio
async def test_count_search_keyword_empty_allowed_ids_is_zero(db_session_factory):
    """Empty allowed_ids list should short-circuit to 0 (no rows match)."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        await repo.create(title="alpha", content="body")
        count = await repo.count_search_keyword("alpha", allowed_ids=[])
    assert count == 0


@pytest.mark.asyncio
async def test_count_search_keyword_short_query_uses_like_path(db_session_factory):
    """Short queries (< KEYWORD_TRIGRAM_MIN_CHARS) bypass FTS; count must
    still work via the LIKE path."""
    async with db_session_factory() as session:
        repo = LibraryRepository(session)
        await repo.create(title="AI report", content="body")
        await repo.create(title="AI notes", content="body")

        count = await repo.count_search_keyword("AI")
    assert count == 2
