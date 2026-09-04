"""Service assembly for keyword search pagination.

The candidate-count semantics (total is an upper bound of the post-filtered
results) are covered at the repository level in
``tests/database/repos/test_library_keyword_search.py``; this file only keeps
the assembly smoke: ``search_documents_paged`` must wire the repository's
count into ``total`` and enrich the page results with their matches.
"""

import pytest

from app.database.unit_of_work import UnitOfWork
from app.services.library_service import library_service


async def _create_doc(project_id, **overrides):
    kwargs = dict(title="notes", content="")
    kwargs.update(overrides)
    async with UnitOfWork(project_id) as uow:
        return await uow.library.create(**kwargs)


@pytest.mark.asyncio
async def test_search_documents_paged_assembles_total_and_enriched_results(project):
    """End-to-end assembly through a real project DB: the response pairs the
    SQL candidate count with the enriched page results (match fields attached
    per document), so callers get pagination metadata and snippets in one
    call."""
    await _create_doc(project, title="alpha report", content="body")
    await _create_doc(project, title="beta report", content="contains alpha here")

    result = await library_service.search_documents_paged(project, "alpha")

    assert result["total"] == len(result["results"]) == 2
    for doc in result["results"]:
        assert doc["search_matches"], "each result must carry its match fields"
        assert doc["search_snippets"]
