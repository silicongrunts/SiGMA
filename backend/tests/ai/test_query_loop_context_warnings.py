import pytest

from app.services.compaction_service import ContextStats
from app.services.query_loop import QueryLoop


@pytest.mark.asyncio
async def test_context_warning_is_injected_once_at_sixty_percent(monkeypatch):
    import app.services.query_loop as query_loop_module

    monkeypatch.setattr(query_loop_module, "tool_schemas_for_model_role", lambda role: [])
    monkeypatch.setattr(
        query_loop_module.compaction_service,
        "stats_for_messages_incremental",
        lambda *args, **kwargs: ContextStats(
            current_tokens=60,
            compact_threshold=100,
            max_context_length=200,
        ),
    )

    loop = QueryLoop(project_id="project-a", session_id="session-1")
    messages = [{"role": "system", "content": "system"}]

    prepared, _events = await loop._prepare_messages(messages)
    prepared, _events = await loop._prepare_messages(prepared)

    warnings = [
        msg for msg in prepared
        if msg.get("role") == "system" and "60% of the configured" in msg.get("content", "")
    ]
    assert len(warnings) == 1
    assert warnings[0]["_ephemeral"] is True


@pytest.mark.asyncio
async def test_context_critical_skips_warning_when_ninety_percent_hits_first(monkeypatch):
    import app.services.query_loop as query_loop_module

    monkeypatch.setattr(query_loop_module, "tool_schemas_for_model_role", lambda role: [])
    monkeypatch.setattr(
        query_loop_module.compaction_service,
        "stats_for_messages_incremental",
        lambda *args, **kwargs: ContextStats(
            current_tokens=90,
            compact_threshold=100,
            max_context_length=200,
        ),
    )

    loop = QueryLoop(project_id="project-a", session_id="session-1")
    prepared, _events = await loop._prepare_messages([
        {"role": "system", "content": "system"},
    ])

    contents = [msg.get("content", "") for msg in prepared]
    assert any("CRITICAL" in content and "90% of the configured" in content for content in contents)
    assert not any("WARNING" in content for content in contents)
