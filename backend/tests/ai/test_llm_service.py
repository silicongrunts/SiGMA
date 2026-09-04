"""Unit tests for llm_service with a fake litellm backend: stream delta
parsing, retry/timeout and iterator-cleanup contracts, litellm error
classification, and outbound payload shaping (system folding, tool-result
pairing)."""

import asyncio

import pytest

from app.core.config import settings
from app.core.config import ModelSettings
from app.core.exceptions import LLMException
from app.core.exceptions import LLMTimeoutError
from app.services.llm_service import LLMService
from app.services.llm_service import INTERRUPTED_TOOL_RESULT
from app.services.llm_service import _with_single_leading_system
from app.services.llm_service import with_complete_tool_results


class FakeLiteLLM:
    async def acompletion(self, **kwargs):
        async def _stream():
            yield {
                "choices": [{
                    "delta": {
                        "content": "hello ",
                        "reasoning_content": "thinking ",
                    }
                }]
            }
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_1",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":',
                            },
                        }]
                    }
                }]
            }
            yield {
                "choices": [{
                    "delta": {
                        "content": "world",
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": '"paper.md"}'},
                        }],
                    }
                }]
            }
            yield {
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "prompt_tokens_details": {"cached_tokens": 3},
                }
            }

        return _stream()


class FlakyLiteLLM:
    def __init__(self):
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("provider connection dropped")

        async def _stream():
            yield {"choices": [{"delta": {"content": "recovered"}}]}

        return _stream()


class TrackedStream:
    def __init__(self, chunks=None, error=None, wait_for_next=False):
        self.chunks = list(chunks or [])
        self.error = error
        self.wait_for_next = wait_for_next
        self.closed = 0
        self.next_started = asyncio.Event()
        self.release_next = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.next_started.set()
        if self.wait_for_next and not self.chunks:
            await self.release_next.wait()
        if self.chunks:
            return self.chunks.pop(0)
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self.closed += 1


class StreamLiteLLM:
    def __init__(self, stream):
        self.stream = stream
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        return self.stream


class AttrObject:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def to_dict(self):
        return self.__dict__


@pytest.mark.asyncio
async def test_stream_chat_preserves_text_reasoning_tool_calls_and_usage(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: FakeLiteLLM()))

    delta_queue = asyncio.Queue()
    service = LLMService()

    text, reasoning, tool_calls, usage = await service.stream_chat(
        messages=[{"role": "user", "content": "hi"}],
        model_role="supervisor",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        delta_queue=delta_queue,
    )

    assert text == "hello world"
    assert reasoning == "thinking "
    assert tool_calls == [{
        "id": "call_1",
        "name": "read_file",
        "params": {"path": "paper.md"},
    }]
    assert usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 3},
    }

    queued = []
    while not delta_queue.empty():
        queued.append(await delta_queue.get())
    assert queued == [("delta", "hello "), ("reasoning_delta", "thinking "), ("delta", "world")]


def test_to_dict_serializes_attr_objects():
    chunk = AttrObject(
        choices=[
            AttrObject(
                delta=AttrObject(content="hello"),
                usage=None,
            )
        ],
        usage=AttrObject(prompt_tokens=1),
    )

    assert LLMService._to_dict(chunk) == {
        "choices": [{"delta": {"content": "hello"}, "usage": None}],
        "usage": {"prompt_tokens": 1},
    }


@pytest.mark.asyncio
async def test_stream_chat_passes_max_tokens(monkeypatch):
    captured = {}

    class CaptureLiteLLM(FakeLiteLLM):
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return await super().acompletion(**kwargs)

    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    await service.stream_chat(
        messages=[{"role": "user", "content": "hi"}],
        model_role="supervisor",
        max_tokens=32_000,
    )

    assert captured["max_tokens"] == 32_000


@pytest.mark.asyncio
async def test_stream_chat_retries_provider_disconnect(monkeypatch):
    flaky = FlakyLiteLLM()

    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(settings.retry, "max_retries", 2)
    monkeypatch.setattr(settings.retry, "delay", 0)
    monkeypatch.setattr(settings.retry, "backoff", 1)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: flaky))

    delta_queue = asyncio.Queue()
    service = LLMService()

    text, reasoning, tool_calls, usage = await service.stream_chat(
        messages=[{"role": "user", "content": "hi"}],
        model_role="supervisor",
        delta_queue=delta_queue,
    )

    assert text == "recovered"
    assert reasoning == ""
    assert tool_calls == []
    assert usage is None
    assert flaky.calls == 2

    queued = []
    while not delta_queue.empty():
        queued.append(await delta_queue.get())
    assert queued[0][0] == "stream_status"
    assert queued[0][1]["status"] == "retrying"
    assert queued[-1] == ("delta", "recovered")


def configure_supervisor(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))


@pytest.mark.asyncio
async def test_stream_chat_closes_provider_iterator_on_normal_completion(monkeypatch):
    configure_supervisor(monkeypatch)
    stream = TrackedStream([{"choices": [{"delta": {"content": "ok"}}]}])
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))

    await LLMService().stream_chat([{"role": "user", "content": "hi"}])

    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_timeout_cancels_provider_stream_creation(monkeypatch):
    configure_supervisor(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class DelayedLiteLLM:
        async def acompletion(self, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(settings.retry, "max_retries", 1)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: DelayedLiteLLM()))

    with pytest.raises(LLMTimeoutError):
        await LLMService().stream_chat(
            [{"role": "user", "content": "hi"}], timeout=0.01,
        )

    assert started.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    asyncio.TimeoutError,
    ConnectionError,
    RuntimeError,
])
async def test_stream_chat_closes_iterator_on_failure(monkeypatch, failure):
    configure_supervisor(monkeypatch)
    monkeypatch.setattr(settings.retry, "max_retries", 1)
    stream = TrackedStream(error=failure("provider failed"))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))

    with pytest.raises((LLMTimeoutError, LLMException)):
        await LLMService().stream_chat(
            [{"role": "user", "content": "hi"}], timeout=0.1,
        )

    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_closes_iterator_on_idle_timeout(monkeypatch):
    configure_supervisor(monkeypatch)
    monkeypatch.setattr(settings.retry, "max_retries", 1)
    stream = TrackedStream(
        [{"choices": [{"delta": {"content": "first"}}]}],
        wait_for_next=True,
    )
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))
    monkeypatch.setattr("app.services.llm_service.STREAM_IDLE_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(LLMTimeoutError):
        await LLMService().stream_chat([{"role": "user", "content": "hi"}], timeout=1)

    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_explicit_timeout_is_whole_call_deadline(monkeypatch):
    configure_supervisor(monkeypatch)
    monkeypatch.setattr(settings.retry, "max_retries", 1)
    stream = TrackedStream(wait_for_next=True)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))
    monkeypatch.setattr("app.services.llm_service.STREAM_FIRST_CHUNK_TIMEOUT_SECONDS", 10)

    with pytest.raises(LLMTimeoutError):
        await LLMService().stream_chat([{"role": "user", "content": "hi"}], timeout=0.01)

    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_closes_iterator_when_caller_is_cancelled(monkeypatch):
    configure_supervisor(monkeypatch)
    stream = TrackedStream(wait_for_next=True)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))

    task = asyncio.create_task(
        LLMService().stream_chat([{"role": "user", "content": "hi"}], timeout=10)
    )
    await stream.next_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_queue_failure_closes_iterator(monkeypatch):
    configure_supervisor(monkeypatch)
    monkeypatch.setattr(settings.retry, "max_retries", 1)
    stream = TrackedStream([{"choices": [{"delta": {"content": "ok"}}]}])
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: StreamLiteLLM(stream)))

    class BrokenQueue:
        async def put(self, item):
            raise RuntimeError("queue closed")

    with pytest.raises(LLMException):
        await LLMService().stream_chat(
            [{"role": "user", "content": "hi"}], delta_queue=BrokenQueue(),
        )
    assert stream.closed == 1


@pytest.mark.asyncio
async def test_stream_chat_retry_closes_failed_attempt_before_next(monkeypatch):
    configure_supervisor(monkeypatch)
    first = TrackedStream(error=ConnectionError("dropped"))
    second = TrackedStream([{"choices": [{"delta": {"content": "recovered"}}]}])

    class RetryingLiteLLM:
        def __init__(self):
            self.streams = [first, second]

        async def acompletion(self, **kwargs):
            return self.streams.pop(0)

    monkeypatch.setattr(settings.retry, "max_retries", 2)
    monkeypatch.setattr(settings.retry, "delay", 0)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: RetryingLiteLLM()))

    result = await LLMService().stream_chat(
        [{"role": "user", "content": "hi"}], timeout=1,
    )

    assert result[0] == "recovered"
    assert first.closed == 1
    assert second.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_chunk", [
    {"content": "partial"},
    {"reasoning_content": "thinking"},
])
@pytest.mark.parametrize("failure", [ConnectionError, asyncio.TimeoutError])
async def test_stream_chat_does_not_retry_after_visible_output(
    monkeypatch, visible_chunk, failure,
):
    configure_supervisor(monkeypatch)
    first = TrackedStream(
        [{"choices": [{"delta": visible_chunk}]}],
        error=failure("failed after visible output"),
    )
    second = TrackedStream([{"choices": [{"delta": {"content": "duplicate"}}]}])

    class RetryingLiteLLM:
        def __init__(self):
            self.streams = [first, second]
            self.calls = 0

        async def acompletion(self, **kwargs):
            self.calls += 1
            return self.streams.pop(0)

    provider = RetryingLiteLLM()
    monkeypatch.setattr(settings.retry, "max_retries", 2)
    monkeypatch.setattr(settings.retry, "delay", 0)
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: provider))
    delta_queue = asyncio.Queue()

    with pytest.raises(LLMException):
        await LLMService().stream_chat(
            [{"role": "user", "content": "hi"}],
            delta_queue=delta_queue,
            timeout=1,
        )

    assert provider.calls == 1
    assert first.closed == 1
    assert second.closed == 0
    queued = []
    while not delta_queue.empty():
        queued.append(await delta_queue.get())
    assert queued == [
        ("delta", "partial") if "content" in visible_chunk
        else ("reasoning_delta", "thinking")
    ]


def test_retry_delay_uses_configured_exponential_cap(monkeypatch):
    monkeypatch.setattr(settings.retry, "delay", 2.0)
    monkeypatch.setattr(settings.retry, "backoff", 2.0)
    monkeypatch.setattr(settings.retry, "max_delay", 64.0)

    assert LLMService._retry_delay(1) == 2.0
    assert LLMService._retry_delay(2) == 4.0
    assert LLMService._retry_delay(6) == 64.0
    assert LLMService._retry_delay(10) == 64.0


def test_litellm_error_classification():
    class AuthenticationError(Exception):
        pass

    class ContextWindowExceededError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class ServiceUnavailableError(Exception):
        status_code = 503

    class UnknownHTTPError(Exception):
        # A litellm error carrying only an HTTP status: the class name matches
        # no known pattern, so classification must ride on status_code alone.
        status_code = 504

    class PaymentRequiredError(Exception):
        status_code = 402

    auth = LLMService._map_litellm_error(AuthenticationError("bad key"))
    context = LLMService._map_litellm_error(ContextWindowExceededError("too large"))
    connection = LLMService._map_litellm_error(APIConnectionError("network down"))
    unavailable = LLMService._map_litellm_error(ServiceUnavailableError("overloaded"))
    unknown = LLMService._map_litellm_error(UnknownHTTPError("gateway glitch"))
    payment = LLMService._map_litellm_error(PaymentRequiredError("no credit"))

    assert not LLMService._is_retryable_error(auth)
    assert not LLMService._is_retryable_error(context)
    assert LLMService._is_retryable_error(connection)
    assert LLMService._is_retryable_error(unavailable)
    # status_code-only classification: retryable gateway status...
    assert unknown.code == "LLM_ERROR"
    assert unknown.status_code == 504
    assert LLMService._is_retryable_error(unknown)
    # ...and a non-retryable one stays surfaced immediately.
    assert payment.code == "LLM_ERROR"
    assert not LLMService._is_retryable_error(payment)


@pytest.mark.asyncio
async def test_call_chat_text_passes_max_tokens(monkeypatch):
    captured = {}

    class CaptureLiteLLM:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    text, usage = await service.call_chat_text(
        messages=[{"role": "user", "content": "compact"}],
        model_role="ra",
        max_tokens=20_000,
    )

    assert text == "ok"
    assert usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert captured["max_tokens"] == 20_000


@pytest.mark.asyncio
async def test_call_chat_text_passes_tools_and_tool_choice(monkeypatch):
    captured = {}

    class CaptureLiteLLM:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "summary text"}}]}

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    tools = [{"type": "function", "function": {"name": "read_file"}}]
    service = LLMService()
    text, usage = await service.call_chat_text(
        messages=[{"role": "user", "content": "compact"}],
        model_role="ra",
        tools=tools,
        tool_choice="none",
    )

    assert text == "summary text"
    assert usage is None
    assert captured["tools"] == tools
    assert captured["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_call_chat_text_omits_tools_when_none(monkeypatch):
    captured = {}

    class CaptureLiteLLM:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    text, usage = await service.call_chat_text(
        messages=[{"role": "user", "content": "hi"}],
        model_role="ra",
    )

    assert text == "ok"
    assert usage is None
    assert "tools" not in captured
    assert "tool_choice" not in captured


# ---------------------------------------------------------------------------
# System-message folding at the outbound boundary
# ---------------------------------------------------------------------------

def test_with_single_leading_system_merges_boundary_summary_into_prompt():
    folded = _with_single_leading_system([
        {"role": "system", "content": "system prompt"},
        {"role": "system", "content": "[passive] summary"},
        {"role": "user", "content": "hi"},
    ])
    assert [m["role"] for m in folded] == ["system", "user"]
    assert folded[0]["content"] == "system prompt\n\n[passive] summary"
    assert folded[1] == {"role": "user", "content": "hi"}


def test_with_single_leading_system_passes_compliant_lists_through():
    leading_only = [{"role": "system", "content": "prompt"}, {"role": "user", "content": "hi"}]
    assert _with_single_leading_system(leading_only) is leading_only
    no_system = [{"role": "user", "content": "hi"}]
    assert _with_single_leading_system(no_system) is no_system


def test_with_single_leading_system_promotes_lone_mid_list_system():
    folded = _with_single_leading_system([
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "note"},
    ])
    assert [m["role"] for m in folded] == ["system", "user"]
    assert folded[0]["content"] == "note"


@pytest.mark.asyncio
async def test_stream_chat_folds_extra_system_messages(monkeypatch):
    captured = {}

    class CaptureLiteLLM(FakeLiteLLM):
        async def acompletion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return await super().acompletion(**kwargs)

    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    await service.stream_chat(
        messages=[
            {"role": "system", "content": "prompt"},
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "hi"},
        ],
        model_role="supervisor",
    )

    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user"]
    assert "summary" in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_call_chat_text_folds_extra_system_messages(monkeypatch):
    captured = {}

    class CaptureLiteLLM:
        async def acompletion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return {"choices": [{"message": {"content": "summary text"}}]}

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    await service.call_chat_text(
        messages=[
            {"role": "system", "content": "prompt"},
            {"role": "system", "content": "old summary"},
            {"role": "user", "content": "compact"},
        ],
        model_role="ra",
    )

    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user"]
    assert "old summary" in captured["messages"][0]["content"]


# ---------------------------------------------------------------------------
# Tool-call pairing at the outbound boundary
#
# A permission pause checkpoints the assistant message before any sibling of
# a parallel batch ran, so stored history can hold tool calls with no result.
# Providers reject such requests outright, so the payload builders must pair
# them before sending — the wire-level tests below pin the pause-mid-batch
# shape (5 parallel calls, only the first answered, resume after approval).
# ---------------------------------------------------------------------------

def captured_history():
    """A parallel batch paused on its first gated call; the resume answers
    only that one."""
    calls = [
        {"id": f"call_{i}", "type": "function",
         "function": {"name": "notebook_run_cell", "arguments": "{}"}}
        for i in range(5)
    ]
    return [
        {"role": "user", "content": "test the notebook tools"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_0",
         "content": "Error: Jupyter server is not running."},
    ]


def test_with_complete_tool_results_passes_paired_lists_through():
    paired = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "body"},
    ]
    assert with_complete_tool_results(paired) is paired

    plain = [{"role": "user", "content": "hi"}]
    assert with_complete_tool_results(plain) is plain


def test_with_complete_tool_results_answers_unpaired_siblings():
    history = captured_history()

    repaired = with_complete_tool_results(history)

    roles = [(m["role"], m.get("tool_call_id")) for m in repaired]
    assert roles == [
        ("user", None),
        ("assistant", None),
        ("tool", "call_0"),
        ("tool", "call_1"),
        ("tool", "call_2"),
        ("tool", "call_3"),
        ("tool", "call_4"),
    ]
    for message in repaired[3:]:
        assert message["content"] == INTERRUPTED_TOOL_RESULT
    # Stored history stays untouched — the repair is request-local.
    assert len(history) == 3


def test_with_complete_tool_results_is_idempotent():
    once = with_complete_tool_results(captured_history())
    twice = with_complete_tool_results(once)
    assert twice == once


def test_with_complete_tool_results_repairs_each_batch_independently():
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}},
            {"id": "call_b", "type": "function",
             "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_a", "content": "ok"},
        {"role": "assistant", "content": "mid turn"},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_c", "type": "function",
             "function": {"name": "read", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_c", "content": "body"},
    ]

    repaired = with_complete_tool_results(history)

    answered = [m.get("tool_call_id") for m in repaired if m["role"] == "tool"]
    assert answered == ["call_a", "call_b", "call_c"]
    assert repaired[2]["content"] == INTERRUPTED_TOOL_RESULT
    assert repaired[2]["role"] == "tool"
    # The later user turn sits after the inserted result, order preserved.
    assert [m["role"] for m in repaired] == [
        "assistant", "tool", "tool", "assistant", "user",
        "assistant", "tool",
    ]


@pytest.mark.asyncio
async def test_stream_chat_repairs_dangling_tool_calls_before_sending(monkeypatch):
    captured = {}

    class CaptureLiteLLM(FakeLiteLLM):
        async def acompletion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return await super().acompletion(**kwargs)

    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    await service.stream_chat(
        messages=captured_history(),
        model_role="supervisor",
    )

    sent_ids = [
        m.get("tool_call_id") for m in captured["messages"] if m["role"] == "tool"
    ]
    assert sent_ids == [f"call_{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_call_chat_text_repairs_dangling_tool_calls_before_sending(monkeypatch):
    captured = {}

    class CaptureLiteLLM:
        async def acompletion(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-test",
        provider="openai",
        api_key="sk-test",
    ))
    monkeypatch.setattr(LLMService, "_litellm", staticmethod(lambda: CaptureLiteLLM()))

    service = LLMService()
    await service.call_chat_text(
        messages=captured_history(),
        model_role="ra",
    )

    sent_ids = [
        m.get("tool_call_id") for m in captured["messages"] if m["role"] == "tool"
    ]
    assert sent_ids == [f"call_{i}" for i in range(5)]
