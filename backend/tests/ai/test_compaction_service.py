"""Unit tests for compaction_service: cancel-safe summarization, compaction
budgets and boundary views, token estimation (images, incremental stats,
special tokens), and the boundary row's summarization-spend persistence."""

import asyncio
import base64
import struct

import pytest

from app.core.config import ModelSettings, settings
from app.services.compaction_service import compaction_service
from tests.ai.conftest import RecordingMessagesRepo, make_fake_uow


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["cancel", "provider", "both"])
async def test_summarize_with_cancel_drains_racing_tasks(monkeypatch, winner):
    started = asyncio.Event()
    release = asyncio.Event()
    created_tasks = []

    async def fake_call_chat_text(**kwargs):
        created_tasks.append(asyncio.current_task())
        started.set()
        if winner == "provider":
            return "summary", None
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise
        return "summary", None

    monkeypatch.setattr(
        "app.services.compaction_service.llm_service.call_chat_text",
        fake_call_chat_text,
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(compaction_service._summarize_with_cancel(
        [{"role": "user", "content": "content"}],
        model_role="supervisor",
        tools=None,
        session_id=None,
        cancel_event=cancel_event,
    ))
    await started.wait()
    if winner in {"cancel", "both"}:
        cancel_event.set()
    if winner in {"provider", "both"}:
        release.set()

    if winner in {"cancel", "both"}:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == ("summary", None)
    assert created_tasks and all(created_task.done() for created_task in created_tasks)


@pytest.mark.asyncio
async def test_summarize_with_cancel_drains_tasks_on_outer_cancellation(monkeypatch):
    started = asyncio.Event()
    created_tasks = []

    async def fake_call_chat_text(**kwargs):
        created_tasks.append(asyncio.current_task())
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.services.compaction_service.llm_service.call_chat_text",
        fake_call_chat_text,
    )
    task = asyncio.create_task(compaction_service._summarize_with_cancel(
        [{"role": "user", "content": "content"}],
        model_role="supervisor",
        tools=None,
        session_id=None,
        cancel_event=asyncio.Event(),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert created_tasks and all(created_task.done() for created_task in created_tasks)


def test_compaction_budget_defaults_and_formula(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
        max_context_length=100_000,
    ))

    budget = compaction_service.budget_for_role("supervisor")

    assert budget.max_context_length == 100_000
    assert budget.compact_threshold == 80_000
    assert budget.response_max_tokens == 32_000
    assert budget.compact_response_max_tokens == 20_000


def test_compaction_budget_uses_configured_threshold(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
        max_context_length=120_000,
        compress_threshold=70_000,
    ))

    assert compaction_service.budget_for_role("supervisor").compact_threshold == 70_000


@pytest.mark.asyncio
async def test_compact_messages_builds_boundary_view(monkeypatch):
    async def fake_call_chat_text(**kwargs):
        assert kwargs["model_role"] == "supervisor"
        assert kwargs["max_tokens"] == 20_000
        assert kwargs["messages"][-1]["role"] == "user"
        assert kwargs["tools"] == [{"type": "function", "name": "test"}]
        return (
            "Current goal: finish the task.\nDone: read files.\nNext: continue.",
            {"prompt_tokens": 100, "completion_tokens": 50,
             "prompt_tokens_details": {"cached_tokens": 30}},
        )

    monkeypatch.setattr(
        "app.services.compaction_service.llm_service.call_chat_text",
        fake_call_chat_text,
    )

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "Please do the work"},
        {"role": "assistant", "content": "I started"},
    ]

    result = await compaction_service.compact_messages(
        messages,
        model_role="supervisor",
        mode="passive",
        tools=[{"type": "function", "name": "test"}],
    )

    assert result.messages == [
        {"role": "system", "content": "system prompt"},
        {"role": "system", "content": result.boundary_content},
    ]
    assert "Continue the user's latest request" in result.boundary_content
    assert "Current goal: finish the task." in result.boundary_content
    assert result.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 30},
    }


@pytest.mark.asyncio
async def test_compact_messages_adds_usage_to_tracker(monkeypatch):
    async def fake_call_chat_text(**kwargs):
        return (
            "Goal: complete the integration test.\nDone: setup environment.\nNext: run all assertions.",
            {"prompt_tokens": 200, "completion_tokens": 80,
             "prompt_tokens_details": {"cached_tokens": 100}},
        )

    monkeypatch.setattr(
        "app.services.compaction_service.llm_service.call_chat_text",
        fake_call_chat_text,
    )

    from app.services.token_budget import TokenBudgetTracker

    tracker = TokenBudgetTracker(budget=None)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    await compaction_service.compact_messages(
        messages,
        model_role="supervisor",
        mode="passive",
        tools=[],
        token_budget_tracker=tracker,
    )

    assert tracker.usage.input == 200
    assert tracker.usage.output == 80
    assert tracker.usage.cached == 100


@pytest.mark.asyncio
async def test_compact_messages_strips_task_reminders_from_summary_input(monkeypatch):
    from app.services.task_service import render_task_reminder_tag

    captured = {}

    async def fake_call_chat_text(**kwargs):
        captured["messages"] = kwargs["messages"]
        return (
            "Current goal: finish the work.\nDone: step one.\nNext: step two.",
            {"prompt_tokens": 10, "completion_tokens": 5,
             "prompt_tokens_details": {"cached_tokens": 0}},
        )

    monkeypatch.setattr(
        "app.services.compaction_service.llm_service.call_chat_text",
        fake_call_chat_text,
    )

    reminder = render_task_reminder_tag([
        {"id": "3", "subject": "Translate chapter 2", "status": "pending"},
    ])
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "please continue" + reminder},
        {"role": "assistant", "content": "ok"},
    ]

    await compaction_service.compact_messages(
        messages, model_role="supervisor", mode="passive", tools=[],
    )

    summarized_users = [
        m["content"] for m in captured["messages"][:-1] if m["role"] == "user"
    ]
    assert summarized_users == ["please continue"]
    # The caller's live list keeps the reminder — stripping is copy-on-write.
    assert "please continue" in messages[1]["content"]
    assert "unfinished tasks" in messages[1]["content"]


def test_strip_user_reminder_handles_multipart_content():
    from app.services.compaction_service import _strip_user_reminder
    from app.services.task_service import render_task_reminder_tag

    reminder = render_task_reminder_tag([
        {"id": "1", "subject": "Caption figures", "status": "pending"},
    ])
    entry = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this" + reminder},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
        ],
    }

    stripped = _strip_user_reminder(entry)

    assert stripped["content"][0]["text"] == "what is this"
    assert stripped["content"][1] == entry["content"][1]
    # Original entry is untouched.
    assert "unfinished tasks" in entry["content"][0]["text"]


# ---------------------------------------------------------------------------
# Image token estimation
# (minimal PNG/JPEG builders; parsing itself is pinned by
# tests/core/test_image_dimensions.py)
# ---------------------------------------------------------------------------

def _png_header(width: int, height: int) -> bytes:
    """Build a minimal PNG header (signature + IHDR chunk) for testing."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height)
    return sig + b"\x00\x00\x00\r" + b"IHDR" + ihdr_data


def _jpeg_header(width: int, height: int) -> bytes:
    """Build a minimal JPEG header (SOI + SOF0) for testing."""
    return (
        b"\xff\xd8"                         # SOI
        b"\xff\xc0"                         # SOF0 marker
        b"\x00\x0b"                         # segment length (11 bytes)
        b"\x08"                             # precision
        + struct.pack(">HH", height, width)
        + b"\x01\x11\x00"                   # channels
    )


def _jpeg_with_app_segment(width: int, height: int, app_payload_size: int) -> bytes:
    app_len = app_payload_size + 2
    return (
        b"\xff\xd8"
        b"\xff\xe1"
        + struct.pack(">H", app_len)
        + b"x" * app_payload_size
        + _jpeg_header(width, height)[2:]
    )


# PNG/JPEG header parsing (including GIF, pinned over there) is pinned by
# tests/core/test_image_dimensions.py; the builders stay here because the
# image-token estimation tests below still need synthetic image payloads.


def test_estimate_image_url_tokens_megapixel_formula():
    """1920×1080 = 2.0736 MP → int(2.0736 * 1000) = 2073."""
    header = _png_header(1920, 1080)
    b64 = base64.b64encode(header).decode()
    url = f"data:image/png;base64,{b64}"
    assert compaction_service._estimate_image_url_tokens(url) == 2073


def test_estimate_image_url_tokens_small_image():
    """100×100 = 0.01 MP → 10 tokens."""
    header = _png_header(100, 100)
    b64 = base64.b64encode(header).decode()
    url = f"data:image/png;base64,{b64}"
    assert compaction_service._estimate_image_url_tokens(url) == 10


def test_estimate_image_url_tokens_jpeg_delayed_sof():
    header = _jpeg_with_app_segment(1024, 768, 512)
    b64 = base64.b64encode(header).decode()
    url = f"data:image/jpeg;base64,{b64}"
    assert compaction_service._estimate_image_url_tokens(url) == 786


def test_estimate_image_url_tokens_http_url_returns_zero():
    assert compaction_service._estimate_image_url_tokens("https://example.com/img.png") == 0


def test_estimate_image_url_tokens_invalid_base64_returns_zero():
    assert compaction_service._estimate_image_url_tokens("data:image/png;base64,!!!notb64!!!") == 0


def test_estimate_messages_tokens_mixed_text_and_image():
    """Multimodal content: text uses tiktoken, image uses megapixel formula."""
    header = _png_header(1000, 500)  # 0.5 MP → 500 tokens
    b64 = base64.b64encode(header).decode()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]
    total = compaction_service.estimate_messages_tokens(messages)
    # Must include image tokens (500) + text tokens for all roles
    assert total >= 500
    # A real screenshot (1920x1080 = ~2073 image tokens) with a large base64
    # payload would previously be massively over-estimated.  Verify that the
    # image portion is bounded by the megapixel formula, not the base64 length.
    big_header = _png_header(1920, 1080)
    big_b64 = base64.b64encode(big_header + b"\x00" * 100_000).decode()
    big_messages = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}},
        ]},
    ]
    big_total = compaction_service.estimate_messages_tokens(big_messages)
    # 1920*1080/1e6 * 1000 = 2073 image tokens + small overhead
    assert 2070 <= big_total <= 2100


def test_estimate_messages_tokens_string_content_unchanged():
    """Plain string content still uses tiktoken (no regression)."""
    messages = [{"role": "user", "content": "Hello world"}]
    tokens = compaction_service.estimate_messages_tokens(messages)
    assert tokens > 0
    # Rough sanity: "Hello world" is 2-3 tokens + role overhead
    assert tokens < 20


def test_estimate_messages_tokens_dict_content_is_counted():
    messages = [{"role": "user", "content": {"type": "text", "value": "Hello world"}}]
    tokens = compaction_service.estimate_messages_tokens(messages)
    assert tokens >= compaction_service.count_tokens_fallback("Hello world")


def test_estimate_messages_tokens_malformed_multimodal_part_is_counted():
    messages = [{"role": "user", "content": ["unexpected text part"]}]
    tokens = compaction_service.estimate_messages_tokens(messages)
    assert tokens >= compaction_service.count_tokens_fallback("unexpected text part")


# ---------------------------------------------------------------------------
# Incremental token estimation (stats_for_messages_incremental)
# ---------------------------------------------------------------------------

def test_incremental_falls_back_to_full_when_no_real_tokens():
    """No real tokens available → full tiktoken estimation."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    stats = compaction_service.stats_for_messages_incremental(
        messages,
        model_role="supervisor",
        last_real_input_tokens=0,
        last_real_count_at_index=0,
    )
    expected = compaction_service.stats_for_messages(
        messages, model_role="supervisor",
    )
    assert stats.current_tokens == expected.current_tokens


def test_incremental_uses_real_tokens_plus_new_messages():
    """Real tokens + tiktoken for messages added since last LLM call."""
    messages_before = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    # Simulate: LLM was called with messages_before, returned prompt_tokens=500
    messages_after = messages_before + [
        {"role": "assistant", "content": "hi there"},
        {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        {"role": "user", "content": "follow-up question"},
    ]
    stats = compaction_service.stats_for_messages_incremental(
        messages_after,
        model_role="supervisor",
        last_real_input_tokens=500,
        last_real_count_at_index=2,  # len(messages_before)
    )
    # Should be 500 + tiktoken for the 3 new messages only
    new_only = [
        {"role": "assistant", "content": "hi there"},
        {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        {"role": "user", "content": "follow-up question"},
    ]
    incremental_tokens = compaction_service.estimate_messages_tokens(new_only, tools=None)
    assert stats.current_tokens == 500 + incremental_tokens


def test_incremental_falls_back_when_messages_truncated():
    """Compaction replaces messages → last_real_count_at_index > len(messages)."""
    new_messages = [
        {"role": "system", "content": "sys"},
        {"role": "system", "content": "compacted summary"},
    ]
    stats = compaction_service.stats_for_messages_incremental(
        new_messages,
        model_role="supervisor",
        last_real_input_tokens=5000,
        last_real_count_at_index=50,  # old list was 50 messages, new is 2
    )
    expected = compaction_service.stats_for_messages(
        new_messages, model_role="supervisor",
    )
    assert stats.current_tokens == expected.current_tokens


def test_incremental_falls_back_when_real_tokens_negative():
    """Negative real tokens treated as unavailable."""
    messages = [{"role": "user", "content": "test"}]
    stats = compaction_service.stats_for_messages_incremental(
        messages,
        model_role="supervisor",
        last_real_input_tokens=-1,
        last_real_count_at_index=0,
    )
    expected = compaction_service.stats_for_messages(
        messages, model_role="supervisor",
    )
    assert stats.current_tokens == expected.current_tokens


def test_incremental_falls_back_when_index_negative():
    messages = [{"role": "user", "content": "test"}]
    stats = compaction_service.stats_for_messages_incremental(
        messages,
        model_role="supervisor",
        last_real_input_tokens=100,
        last_real_count_at_index=-1,
    )
    expected = compaction_service.stats_for_messages(
        messages, model_role="supervisor",
    )
    assert stats.current_tokens == expected.current_tokens


def test_incremental_no_new_messages():
    """No messages added since last call → real tokens unchanged."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    stats = compaction_service.stats_for_messages_incremental(
        messages,
        model_role="supervisor",
        last_real_input_tokens=1000,
        last_real_count_at_index=2,  # same length as messages
    )
    assert stats.current_tokens == 1000


def test_incremental_stats_has_correct_budget_fields(monkeypatch):
    """Returned ContextStats includes correct threshold/max from budget."""
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
        max_context_length=100_000,
    ))
    messages = [{"role": "user", "content": "test"}]
    stats = compaction_service.stats_for_messages_incremental(
        messages,
        model_role="supervisor",
        last_real_input_tokens=100,
        last_real_count_at_index=0,
    )
    assert stats.max_context_length == 100_000
    assert stats.compact_threshold == 80_000
    d = stats.to_dict()
    assert d["max_context_length"] == 100_000
    assert d["compact_threshold"] == 80_000
    assert d["current_tokens"] > 0


# ---------------------------------------------------------------------------
# Special-token safety (regression: literal "<|endoftext|>" in model/tool
# output crashed token estimation and killed running chat tasks)
# ---------------------------------------------------------------------------

@pytest.mark.regression
def test_count_tokens_fallback_encodes_special_tokens_as_text():
    """Literal special tokens must be counted, not rejected by tiktoken."""
    assert compaction_service.count_tokens_fallback("done<|endoftext|>next") > 0
    assert compaction_service.count_tokens_fallback("<|endofprompt|>") > 0
    assert compaction_service.count_tokens_fallback("") == 0


@pytest.mark.regression
def test_estimate_messages_tokens_survives_special_tokens():
    """Full estimation path over tool output containing special tokens."""
    messages = [
        {"role": "tool", "content": "experiment output <|endoftext|> tail"},
        {"role": "assistant", "content": "summary <|endofprompt|> done"},
    ]
    assert compaction_service.estimate_messages_tokens(messages) > 0


@pytest.mark.asyncio
async def test_stage_session_boundary_persists_summarization_spend():
    """The boundary row carries the summarization call's own spend so a
    post-compaction resume seeds it into the turn stats instead of the
    display dropping after the split."""
    messages_repo = RecordingMessagesRepo()
    uow = make_fake_uow(messages=messages_repo)()

    await compaction_service.stage_session_boundary(
        uow, "session-1", "boundary body",
        usage={"input": 1234, "output": 56, "cached": 1200},
    )

    staged = messages_repo.created[0]
    assert staged["session_id"] == "session-1"
    assert staged["role"] == "system"
    assert staged["is_boundary"] is True
    assert staged["token_count"] == 56
    assert staged["input_tokens"] == 1234
    assert staged["cached_tokens"] == 1200


@pytest.mark.asyncio
async def test_stage_session_boundary_without_usage_stays_zero():
    messages_repo = RecordingMessagesRepo()
    uow = make_fake_uow(messages=messages_repo)()

    await compaction_service.stage_session_boundary(uow, "session-1", "boundary body")

    staged = messages_repo.created[0]
    assert (staged["token_count"], staged["input_tokens"], staged["cached_tokens"]) == (0, 0, 0)
