import pytest

from app.services.compaction_service import CompactionResult
from app.services.compaction_service import ContextStats
from app.services.query_loop import QueryLoop


@pytest.mark.asyncio
async def test_compaction_done_event_carries_summary(monkeypatch):
    """The compact_done payload includes the summary so the live timeline can
    render the same expandable card the history view serves after refresh."""
    import app.services.query_loop as query_loop_module

    monkeypatch.setattr(query_loop_module, "tool_schemas_for_model_role", lambda role: [])
    monkeypatch.setattr(
        query_loop_module.compaction_service,
        "stats_for_messages_incremental",
        lambda *args, **kwargs: ContextStats(
            current_tokens=150,
            compact_threshold=100,
            max_context_length=200,
        ),
    )

    compacted = [{"role": "system", "content": "system"}]
    result = CompactionResult(
        summary="summarized conversation",
        boundary_content="[passive] summarized conversation",
        messages=compacted,
        stats=ContextStats(current_tokens=30, compact_threshold=100, max_context_length=200),
        usage=None,
    )

    async def _fake_compact(messages, **kwargs):
        return result

    monkeypatch.setattr(query_loop_module.compaction_service, "compact_messages", _fake_compact)

    class _FakeUnitOfWork:
        @staticmethod
        async def execute_atomic(project_id, operation):
            return None

    monkeypatch.setattr(query_loop_module, "UnitOfWork", _FakeUnitOfWork)

    loop = QueryLoop(project_id="project-a", session_id="session-1")
    prepared, events = await loop._prepare_messages([
        {"role": "system", "content": "system"},
    ])

    done_events = [e for e in events if e.get("type") == "compact_done"]
    assert len(done_events) == 1
    assert done_events[0]["data"]["summary"] == "summarized conversation"
    assert done_events[0]["data"]["current_tokens"] == 30
    assert prepared == compacted
