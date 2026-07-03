import asyncio

import pytest

from app.core.config import settings
from app.core.config import ModelSettings
from app.services.llm_service import LLMService


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

    auth = LLMService._map_litellm_error(AuthenticationError("bad key"))
    context = LLMService._map_litellm_error(ContextWindowExceededError("too large"))
    connection = LLMService._map_litellm_error(APIConnectionError("network down"))
    unavailable = LLMService._map_litellm_error(ServiceUnavailableError("overloaded"))

    assert not LLMService._is_retryable_error(auth)
    assert not LLMService._is_retryable_error(context)
    assert LLMService._is_retryable_error(connection)
    assert LLMService._is_retryable_error(unavailable)


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
