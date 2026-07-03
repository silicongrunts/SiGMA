"""
LLM Service - unified abstraction for all cloud model calls.

Application code calls logical SiGMA roles (supervisor, ra, vision, draw).
Provider details are resolved at this boundary and passed to LiteLLM SDK.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict

from app.core.config import settings
from app.core.exceptions import (
    ConfigurationError,
    LLMException,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.core.logging import get_logger
from app.core.model_config import ModelEndpoint, get_model_endpoint

logger = get_logger(__name__)

STREAM_FIRST_CHUNK_TIMEOUT_SECONDS = 900.0
STREAM_IDLE_TIMEOUT_SECONDS = 300.0
STREAM_ABSOLUTE_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class GeneratedImage:
    """Provider-neutral image generation result."""

    url: str = ""
    b64_json: str = ""
    mime_type: str = "image/png"
    metadata: dict[str, Any] | None = None


class LLMService:
    """Unified LLM service backed by LiteLLM SDK."""

    async def call_json(
        self,
        prompt: str,
        system: str = "",
        model_role: str = "ra",
        timeout: float = 3600.0,
        max_tokens: int | None = None,
    ) -> Dict:
        """Call a chat model and parse the response content as JSON."""
        for attempt in range(1, settings.MAX_RETRIES + 1):
            raw = await self.call_text(
                prompt=prompt,
                system=system,
                model_role=model_role,
                timeout=timeout,
                max_tokens=max_tokens,
            )
            raw = self._strip_json_fences(raw)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                if attempt >= settings.MAX_RETRIES:
                    raise LLMResponseError(f"Invalid JSON response: {exc}") from exc
                logger.warning("LLM JSON call attempt %d returned invalid JSON", attempt)
                continue
            if not isinstance(parsed, dict):
                if attempt >= settings.MAX_RETRIES:
                    raise LLMResponseError("JSON response must be an object")
                logger.warning("LLM JSON call attempt %d returned non-object JSON", attempt)
                continue
            return parsed
        raise LLMResponseError("Invalid JSON response")

    async def call_text(
        self,
        prompt: str,
        system: str = "",
        model_role: str = "ra",
        timeout: float = 120.0,
        max_tokens: int | None = None,
    ) -> str:
        """Call a chat model and return text content."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        text, _ = await self.call_chat_text(
            messages=messages,
            model_role=model_role,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        return text

    async def call_chat_text(
        self,
        messages: list[dict],
        model_role: str = "ra",
        timeout: float = 120.0,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> tuple[str, dict | None]:
        """Call a chat model with caller-supplied messages.

        Returns (text_content, usage_dict_or_None).
        """
        if max_tokens is None:
            max_tokens = settings.NORMAL_RESPONSE_MAX_TOKENS
        response = await self._completion_with_retries(
            role=model_role,
            messages=messages,
            timeout=timeout,
            stream=False,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
        )
        message = self._first_message(response)
        text = str(message.get("content") or "").strip()
        return text, response.get("usage")

    async def call_vision(
        self,
        prompt: str,
        image_base64: str,
        image_media_type: str = "image/png",
        timeout: float = 120.0,
    ) -> str:
        """Analyze an image using the configured vision role."""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{image_media_type};base64,{image_base64}",
                    },
                },
            ],
        }]

        response = await self._completion_with_retries(
            role="vision",
            messages=messages,
            timeout=timeout,
            stream=False,
            max_tokens=None,
        )
        message = self._first_message(response)
        return str(message.get("content") or "")

    async def generate_image(
        self,
        prompt: str,
        timeout: float = 300.0,
        **kwargs: Any,
    ) -> GeneratedImage:
        """Generate an image using the configured draw role."""
        endpoint = self._require_endpoint("draw")

        async def _call():
            litellm = self._litellm()
            call_kwargs = {
                "model": endpoint.litellm_model,
                "prompt": prompt,
                **endpoint.litellm_kwargs(),
                **kwargs,
            }
            if hasattr(litellm, "aimage_generation"):
                return await litellm.aimage_generation(**call_kwargs)
            return await asyncio.to_thread(litellm.image_generation, **call_kwargs)

        response = await self._retry_role_call("draw", timeout, _call)
        payload = self._to_dict(response)
        data = payload.get("data") or []
        if not data:
            raise LLMResponseError("Image model returned no image data")
        first = self._to_dict(data[0])
        return GeneratedImage(
            url=str(first.get("url") or ""),
            b64_json=str(first.get("b64_json") or ""),
            mime_type=str(first.get("mime_type") or first.get("mimeType") or "image/png"),
            metadata={k: v for k, v in first.items() if k not in {"url", "b64_json"}},
        )

    async def stream_chat(
        self,
        messages: list[dict],
        model_role: str = "supervisor",
        tools: list[dict] | None = None,
        timeout: float = 300.0,
        delta_queue: "asyncio.Queue | None" = None,
        max_tokens: int | None = None,
    ) -> tuple[str, str, list[dict], dict | None]:
        """Stream chat while preserving the existing SiGMA return contract.

        Returns:
            (text_content, reasoning_content, parsed_tool_calls, usage_dict_or_None)
        """
        endpoint = self._require_endpoint(model_role)
        litellm = self._litellm()
        if max_tokens is None:
            max_tokens = settings.NORMAL_RESPONSE_MAX_TOKENS

        payload = {
            "model": endpoint.litellm_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "drop_params": True,
            **endpoint.litellm_kwargs(),
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools

        max_attempts = max(1, settings.MAX_RETRIES)
        last_error: LLMException | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._stream_chat_once(
                    litellm=litellm,
                    payload=payload,
                    timeout=timeout,
                    delta_queue=delta_queue,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                last_error = LLMTimeoutError(timeout)
                mapped_message = last_error.message
                if attempt >= max_attempts:
                    raise last_error from exc
            except Exception as exc:
                mapped = self._map_litellm_error(exc)
                last_error = mapped
                retryable = self._is_retryable_error(mapped)
                mapped_message = mapped.message
                if not retryable or attempt >= max_attempts:
                    raise mapped from exc

            logger.warning(
                "LLM %s stream attempt %d/%d failed: %s",
                model_role, attempt, max_attempts, mapped_message,
            )
            if delta_queue is not None:
                await delta_queue.put((
                    "stream_status",
                    {
                        "status": "retrying",
                        "message": "LLM stream interrupted; retrying.",
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "error": mapped_message,
                    },
                ))
            await asyncio.sleep(self._retry_delay(attempt))

        raise last_error or LLMException("LLM stream failed after retries")

    async def _stream_chat_once(
        self,
        *,
        litellm,
        payload: dict,
        timeout: float,
        delta_queue: "asyncio.Queue | None",
    ) -> tuple[str, str, list[dict], dict | None]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_tool_calls: dict[int, dict[str, Any]] = {}
        usage = None

        first_chunk_timeout = max(float(timeout), STREAM_FIRST_CHUNK_TIMEOUT_SECONDS)
        stream = await asyncio.wait_for(
            litellm.acompletion(**payload), timeout=first_chunk_timeout,
        )
        stream_iter = stream.__aiter__()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + STREAM_ABSOLUTE_TIMEOUT_SECONDS
        seen_chunk = False

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LLMTimeoutError(STREAM_ABSOLUTE_TIMEOUT_SECONDS)

            chunk_timeout = (
                STREAM_IDLE_TIMEOUT_SECONDS if seen_chunk else first_chunk_timeout
            )
            chunk_timeout = min(chunk_timeout, remaining)
            try:
                chunk = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=chunk_timeout,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise LLMTimeoutError(chunk_timeout) from exc

            seen_chunk = True
            chunk_dict = self._to_dict(chunk)
            if chunk_dict.get("usage"):
                usage = chunk_dict["usage"]

            choices = chunk_dict.get("choices") or []
            if not choices:
                continue
            choice = self._to_dict(choices[0])
            if choice.get("usage"):
                usage = choice["usage"]
            delta = self._to_dict(choice.get("delta") or {})

            text = self._extract_text_delta(delta)
            if text:
                text_parts.append(text)
                if delta_queue is not None:
                    await delta_queue.put(("delta", text))

            reasoning = self._extract_reasoning_delta(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
                if delta_queue is not None:
                    await delta_queue.put(("reasoning_delta", reasoning))

            self._merge_tool_call_deltas(delta.get("tool_calls") or [], raw_tool_calls)

        parsed_tool_calls = self._parse_tool_calls(raw_tool_calls)
        return "".join(text_parts), "".join(reasoning_parts), parsed_tool_calls, usage

    async def _completion_with_retries(
        self,
        *,
        role: str,
        messages: list[dict],
        timeout: float,
        stream: bool,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> dict:
        endpoint = self._require_endpoint(role)

        async def _call():
            litellm = self._litellm()
            payload = {
                "model": endpoint.litellm_model,
                "messages": messages,
                "stream": stream,
                "drop_params": True,
                **endpoint.litellm_kwargs(),
            }
            if max_tokens:
                payload["max_tokens"] = max_tokens
            if tools:
                payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
            return await litellm.acompletion(**payload)

        response = await self._retry_role_call(role, timeout, _call)
        return self._to_dict(response)

    async def _retry_role_call(self, role: str, timeout: float, call):
        max_attempts = max(1, settings.MAX_RETRIES)
        for attempt in range(1, max_attempts + 1):
            should_retry = False
            mapped_message = ""
            try:
                return await asyncio.wait_for(call(), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                mapped = LLMTimeoutError(timeout)
                mapped_message = mapped.message
                logger.warning("LLM %s call attempt %d timed out", role, attempt)
                if attempt >= max_attempts:
                    raise LLMTimeoutError(timeout)
                should_retry = True
            except ConfigurationError:
                raise
            except Exception as exc:
                mapped = self._map_litellm_error(exc)
                mapped_message = mapped.message
                logger.warning("LLM %s call attempt %d failed: %s", role, attempt, mapped.message)
                # Non-retryable errors (401/403/400/context-window) surface
                # immediately instead of burning the retry budget.
                if not self._is_retryable_error(mapped) or attempt >= max_attempts:
                    raise mapped
                should_retry = True
            if should_retry:
                logger.warning(
                    "LLM %s call retrying after attempt %d/%d: %s",
                    role, attempt, max_attempts, mapped_message,
                )
                await asyncio.sleep(self._retry_delay(attempt))
        raise LLMException("LLM call failed after retries")

    def _require_endpoint(self, role: str) -> ModelEndpoint:
        try:
            endpoint = get_model_endpoint(role)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        if endpoint.is_local:
            raise ConfigurationError(f"{role.upper()} is configured as local but requires a cloud model")
        if not endpoint.model:
            raise ConfigurationError(f"{role.upper()}_MODEL is not configured")
        return endpoint

    @staticmethod
    def _litellm():
        try:
            import litellm
        except ImportError as exc:
            raise ConfigurationError("LiteLLM is not installed. Install backend requirements.") from exc
        return litellm

    @staticmethod
    def _strip_json_fences(raw: str) -> str:
        text = raw.strip()
        if not text.startswith("```"):
            return text
        lines = text.split("\n")
        if lines and "json" in lines[0].lower():
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    @classmethod
    def _to_dict(cls, value: Any) -> dict:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            return cls._plain_value(converted) if isinstance(converted, dict) else {}
        if hasattr(value, "model_dump"):
            converted = value.model_dump()
            return cls._plain_value(converted) if isinstance(converted, dict) else {}
        if hasattr(value, "__dict__"):
            return cls._plain_attrs(value)
        return {}

    @classmethod
    def _plain_attrs(cls, value: Any) -> dict:
        result = {}
        for key, attr in vars(value).items():
            if key.startswith("_"):
                continue
            result[key] = cls._plain_value(attr)
        return result

    @classmethod
    def _plain_value(cls, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {k: cls._plain_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._plain_value(item) for item in value]
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            return cls._plain_value(converted)
        if hasattr(value, "model_dump"):
            converted = value.model_dump()
            return cls._plain_value(converted)
        if hasattr(value, "__dict__"):
            return cls._plain_attrs(value)
        return value

    @classmethod
    def _first_message(cls, response: dict) -> dict:
        choices = response.get("choices") or []
        if not choices:
            raise LLMResponseError("LLM response contained no choices")
        choice = cls._to_dict(choices[0])
        message = cls._to_dict(choice.get("message") or {})
        if not message:
            raise LLMResponseError("LLM response contained no message")
        return message

    @staticmethod
    def _extract_text_delta(delta: dict) -> str:
        content = delta.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                item_dict = LLMService._to_dict(item)
                text = item_dict.get("text")
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def _extract_reasoning_delta(delta: dict) -> str:
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
        return ""

    @classmethod
    def _merge_tool_call_deltas(
        cls,
        tool_call_deltas: list[Any],
        raw_tool_calls: dict[int, dict[str, Any]],
    ) -> None:
        for tool_call_delta in tool_call_deltas:
            tool_call = cls._to_dict(tool_call_delta)
            index = tool_call.get("index")
            if index is None:
                index = len(raw_tool_calls)
            if index not in raw_tool_calls:
                raw_tool_calls[index] = {
                    "id": tool_call.get("id") or "",
                    "name": "",
                    "params": "",
                }
            current = raw_tool_calls[index]
            if tool_call.get("id"):
                current["id"] = tool_call["id"]
            function = cls._to_dict(tool_call.get("function") or {})
            if function.get("name"):
                current["name"] = function["name"]
            if function.get("arguments"):
                current["params"] += function["arguments"]

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: dict[int, dict[str, Any]]) -> list[dict]:
        parsed_tool_calls = []
        for tool_call in raw_tool_calls.values():
            if not tool_call["name"]:
                continue
            try:
                params = json.loads(tool_call["params"]) if tool_call["params"] else {}
            except json.JSONDecodeError:
                params = {}
            parsed_tool_calls.append({
                "id": tool_call["id"],
                "name": tool_call["name"],
                "params": params,
            })
        return parsed_tool_calls

    @staticmethod
    def _is_retryable_error(exc: LLMException) -> bool:
        """Whether an LLM exception is worth retrying.

        Shared by both the streaming and non-streaming retry paths so that
        non-retryable failures (401/403/400/context-window-exceeded) surface
        to the user immediately instead of burning the retry budget.
        """
        if isinstance(exc, ConfigurationError):
            return False
        if exc.code in {"LLM_TIMEOUT", "LLM_RATE_LIMIT"}:
            return True
        if exc.code in {"LLM_API_CONNECTION", "LLM_API_ERROR", "LLM_BUDGET_EXCEEDED"}:
            return True
        if exc.code in {
            "LLM_AUTHENTICATION",
            "LLM_PERMISSION_DENIED",
            "LLM_CONTEXT_WINDOW",
            "LLM_BAD_REQUEST",
            "LLM_INVALID_REQUEST",
            "LLM_NOT_FOUND",
            "LLM_UNSUPPORTED_PARAMS",
            "LLM_CONTENT_POLICY",
            "LLM_UNPROCESSABLE",
        }:
            return False
        if exc.status_code in {408, 429, 500, 502, 503, 504}:
            return True
        return False

    @staticmethod
    def _map_litellm_error(exc: Exception) -> LLMException:
        if isinstance(exc, LLMException):
            return exc
        name = exc.__class__.__name__.lower()
        message = str(exc) or exc.__class__.__name__
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)

        if "timeout" in name or status_code == 408:
            return LLMTimeoutError()
        if "ratelimit" in name or "rate_limit" in name or status_code == 429 or "429" in message:
            return LLMRateLimitError()
        if "contextwindow" in name:
            return LLMException(message, "LLM_CONTEXT_WINDOW", 400)
        if "authentication" in name:
            return LLMException(message, "LLM_AUTHENTICATION", 401)
        if "permissiondenied" in name:
            return LLMException(message, "LLM_PERMISSION_DENIED", 403)
        if "badrequest" in name:
            return LLMException(message, "LLM_BAD_REQUEST", 400)
        if "invalidrequest" in name:
            return LLMException(message, "LLM_INVALID_REQUEST", 400)
        if "notfound" in name:
            return LLMException(message, "LLM_NOT_FOUND", 404)
        if "unsupportedparams" in name:
            return LLMException(message, "LLM_UNSUPPORTED_PARAMS", 400)
        if "contentpolicyviolation" in name or "blockedpiientity" in name:
            return LLMException(message, "LLM_CONTENT_POLICY", 400)
        if "unprocessableentity" in name:
            return LLMException(message, "LLM_UNPROCESSABLE", 422)
        if "budgetexceeded" in name:
            return LLMException(message, "LLM_BUDGET_EXCEEDED", 429)
        if "apiconnection" in name or isinstance(exc, (ConnectionError, OSError)):
            return LLMException(message, "LLM_API_CONNECTION", 502)
        if "internalserver" in name:
            return LLMException(message, "LLM_API_ERROR", 500)
        if "badgateway" in name:
            return LLMException(message, "LLM_API_ERROR", 502)
        if "serviceunavailable" in name:
            return LLMException(message, "LLM_API_ERROR", 503)
        if "apiresponsevalidation" in name:
            return LLMException(message, "LLM_RESPONSE_ERROR", 502)
        if "apierror" in name:
            return LLMException(message, "LLM_API_ERROR", int(status_code or 502))
        if status_code:
            return LLMException(message, "LLM_ERROR", int(status_code))
        return LLMException(f"LLM call failed: {message}")

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        base = float(settings.RETRY_DELAY)
        backoff = float(settings.RETRY_BACKOFF)
        max_delay = float(settings.RETRY_MAX_DELAY)
        return min(base * (backoff ** max(0, attempt - 1)), max_delay)


llm_service = LLMService()
