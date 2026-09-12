"""LLM loop runner message building and persistence.

_build_messages rehydration (input-token baseline, tool image refs) and the
query/agent _save_messages contracts: what reaches the provider, what lands
in history, and which rows carry per-message usage.
"""

from importlib import import_module
from types import SimpleNamespace

import pytest

import_module("app.agents.tools")
import app.services.agent_service as agent_service_module
import app.services.query_loop as query_loop_module
from app.core.chat_attachments import render_image_refs_tag
from app.core.config import ModelSettings, settings
from app.services.agent_service import AgentService
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.query_loop import QueryLoop
from tests.ai.conftest import (
    FakeConfigRepo,
    RecordingMessagesRepo,
    RecordingSessionsRepo,
    make_fake_uow,
)


@pytest.mark.asyncio
async def test_build_messages_context_baseline_uses_latest_assistant_input_only(monkeypatch, tmp_path):
    history = [
        SimpleNamespace(
            role="user", content="question", tool_calls=None, tool_call_id=None,
            reasoning_content=None, input_tokens=0,
        ),
        SimpleNamespace(
            role="assistant", content="call agent", tool_calls=None, tool_call_id=None,
            reasoning_content=None, input_tokens=28_000,
        ),
        SimpleNamespace(
            role="tool", content="agent result", tool_calls=None, tool_call_id="call_agent",
            reasoning_content=None, input_tokens=450_000,
        ),
    ]

    monkeypatch.setattr(
        query_loop_module, "UnitOfWork",
        make_fake_uow(messages=RecordingMessagesRepo(history), config=FakeConfigRepo()),
    )
    monkeypatch.setattr(
        query_loop_module.session_temp_service,
        "session_dir_for_prompt",
        lambda project_id, session_id: str(tmp_path / ".SiGMA" / "sessions" / session_id),
    )
    monkeypatch.setattr(
        query_loop_module.prompt_service,
        "build_system_prompt",
        lambda **kwargs: "system",
    )

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    messages = await loop._build_messages()

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert loop._persisted_real_input_tokens == 28_000
    assert loop._persisted_real_count_at_index == 2


@pytest.mark.asyncio
async def test_build_messages_rehydrates_tool_image_refs_for_multimodal_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(model="supervisor-model"))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="supervisor"))

    image_tag = render_image_refs_tag([{
        "path": "/tmp/read-image.png",
        "mime_type": "image/png",
        "source": "read",
        "text": "Image file: /tmp/read-image.png (10x10)",
    }])
    history = [
        SimpleNamespace(
            role="tool",
            content=f"Image file: /tmp/read-image.png (10x10){image_tag}",
            tool_calls=None,
            tool_call_id="call_read",
            reasoning_content=None,
            input_tokens=0,
        ),
    ]

    async def fake_read_image_path_base64(project_id, path):
        assert path == "/tmp/read-image.png"
        return "aW1hZ2U=", "image/png"

    monkeypatch.setattr(
        query_loop_module, "UnitOfWork",
        make_fake_uow(messages=RecordingMessagesRepo(history), config=FakeConfigRepo()),
    )
    monkeypatch.setattr(
        query_loop_module.session_temp_service,
        "session_dir_for_prompt",
        lambda project_id, session_id: str(tmp_path / ".SiGMA" / "sessions" / session_id),
    )
    monkeypatch.setattr(query_loop_module, "read_image_path_base64", fake_read_image_path_base64)
    monkeypatch.setattr(
        query_loop_module.prompt_service,
        "build_system_prompt",
        lambda **kwargs: "system",
    )

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    messages = await loop._build_messages()

    assert [m["role"] for m in messages] == ["system", "tool", "user"]
    assert "<image_refs>" not in messages[1]["content"]
    assert messages[2]["_ephemeral"] is True
    assert messages[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="


@pytest.mark.asyncio
async def test_query_persist_skips_rehydrated_ephemeral_images_without_duplicate_history(monkeypatch):
    fake_messages = RecordingMessagesRepo(history=[
        SimpleNamespace(role="user"),
        SimpleNamespace(role="assistant"),
    ])
    fake_sessions = RecordingSessionsRepo()
    monkeypatch.setattr(
        query_loop_module, "UnitOfWork",
        make_fake_uow(messages=fake_messages, sessions=fake_sessions),
    )

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    await loop._save_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {"role": "user", "content": [{"type": "text", "text": "old image"}], "_ephemeral": True},
        {"role": "assistant", "content": "old assistant"},
        {"role": "assistant", "content": "new assistant"},
    ])

    assert [m["content"] for m in fake_messages.created] == ["new assistant"]
    assert fake_messages.updated == []
    assert fake_sessions.touched == ["session-1"]


@pytest.mark.asyncio
async def test_query_persist_writes_zero_for_messages_without_real_usage(monkeypatch):
    fake_messages = RecordingMessagesRepo()
    fake_sessions = RecordingSessionsRepo()
    monkeypatch.setattr(
        query_loop_module, "UnitOfWork",
        make_fake_uow(messages=fake_messages, sessions=fake_sessions),
    )

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    await loop._save_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "fallback text without provider usage"},
    ])

    assert fake_messages.created[0]["role"] == "user"
    assert fake_messages.created[0]["token_count"] == 0
    assert fake_messages.created[0]["input_tokens"] == 0
    assert fake_messages.created[0]["cached_tokens"] == 0
    assert fake_messages.created[1]["role"] == "assistant"
    assert fake_messages.created[1]["token_count"] == 0
    assert fake_messages.created[1]["input_tokens"] == 0
    assert fake_messages.created[1]["cached_tokens"] == 0
    assert fake_messages.updated == []


@pytest.mark.asyncio
async def test_agent_persist_does_not_update_prior_assistant_when_final_save_has_no_new_messages(monkeypatch):
    fake_messages = RecordingMessagesRepo(history=[
        SimpleNamespace(role="user"),
        SimpleNamespace(role="assistant"),
        SimpleNamespace(role="tool"),
    ])
    fake_sessions = RecordingSessionsRepo()
    monkeypatch.setattr(
        agent_service_module, "UnitOfWork",
        make_fake_uow(messages=fake_messages, sessions=fake_sessions),
    )

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "tool call"},
            {"role": "tool", "content": "tool result"},
        ],
    )

    assert fake_messages.created == []
    assert fake_sessions.touched == ["agent-session"]


@pytest.mark.asyncio
async def test_agent_persist_writes_per_message_usage(monkeypatch):
    fake_messages = RecordingMessagesRepo()
    monkeypatch.setattr(
        agent_service_module, "UnitOfWork",
        make_fake_uow(messages=fake_messages, sessions=RecordingSessionsRepo()),
    )

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
            {
                "role": "assistant",
                "content": "tool call",
                "_input_tokens": 100,
                "_completion_tokens": 20,
                "_cached_tokens": 40,
            },
            {
                "role": "tool",
                "content": "agent result",
                "tool_call_id": "call_agent",
                "_input_tokens": 300,
                "_completion_tokens": 60,
                "_cached_tokens": 120,
            },
        ],
    )

    assert fake_messages.created[0]["role"] == "user"
    assert fake_messages.created[0]["input_tokens"] == 0
    assert fake_messages.created[0]["token_count"] == 0
    assert fake_messages.created[0]["cached_tokens"] == 0
    assert fake_messages.created[1]["role"] == "assistant"
    assert fake_messages.created[1]["input_tokens"] == 100
    assert fake_messages.created[1]["token_count"] == 20
    assert fake_messages.created[1]["cached_tokens"] == 40
    assert fake_messages.created[2]["role"] == "tool"
    assert fake_messages.created[2]["input_tokens"] == 300
    assert fake_messages.created[2]["token_count"] == 60
    assert fake_messages.created[2]["cached_tokens"] == 120


@pytest.mark.asyncio
async def test_agent_persist_does_not_update_old_assistant_without_persisted_marker(monkeypatch):
    fake_messages = RecordingMessagesRepo(history=[SimpleNamespace(role="tool")])
    monkeypatch.setattr(
        agent_service_module, "UnitOfWork",
        make_fake_uow(messages=fake_messages, sessions=RecordingSessionsRepo()),
    )

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [{"role": "system", "content": "system"}],
    )

    # Only a system message was passed; nothing should be persisted.
    assert fake_messages.created == []


# ---------------------------------------------------------------------------
# Compaction boundary rows are presented to the LLM as user messages
# ---------------------------------------------------------------------------

def _history_row(role: str, is_boundary: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        role=role, content="[passive] summary", tool_calls=None,
        tool_call_id=None, reasoning_content=None, input_tokens=0,
        is_boundary=is_boundary,
    )


def test_entry_from_history_maps_boundary_row_to_user():
    entry = LLMLoopRunner.entry_from_history(
        _history_row("system", is_boundary=True), "[passive] summary",
    )
    assert entry == {"role": "user", "content": "[passive] summary"}


def test_entry_from_history_keeps_regular_system_row():
    entry = LLMLoopRunner.entry_from_history(
        _history_row("system", is_boundary=False), "system prompt",
    )
    assert entry == {"role": "system", "content": "system prompt"}


def test_messages_from_history_maps_boundary_row_to_user():
    messages, baseline = AgentService._messages_from_history(
        "system prompt", [_history_row("system", is_boundary=True)],
    )
    assert messages == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "[passive] summary"},
    ]
    assert baseline == {"input": 0, "index": 0}
