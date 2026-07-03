"""Settings configuration checker.

Validates settings structure and tests model connectivity before saving.
Yields SSE events so the frontend can display real-time progress.
"""

import asyncio
import base64
import json
import struct
import time
import zlib
from typing import Any, AsyncGenerator

from app.core.config import Settings, validate_settings_yaml
from app.core.logging import get_logger
from app.core.model_config import ModelEndpoint

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Check result helpers
# ---------------------------------------------------------------------------

_CHECK_ITEMS = [
    ("structure", "Config Structure"),
    ("supervisor", "Supervisor Model"),
    ("ra", "RA Model"),
    ("vision", "Vision Model"),
    ("draw", "Draw Model"),
    ("embedding", "Embedding Model"),
    ("rerank", "Rerank Model"),
]


def _sse(event: str, data: dict) -> str:
    """Format one SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Map an exception to (error_type, message)."""
    name = type(exc).__name__.lower()
    msg = str(exc)

    if "timeout" in name or "timeout" in msg.lower():
        return "timeout", f"Request timed out: {_brief(msg)}"
    if "connect" in name or "connectionerror" in name:
        return "unreachable", f"Cannot reach endpoint: {_brief(msg)}"
    if "authentication" in name or "401" in msg or "403" in msg:
        return "auth_error", f"Authentication failed: {_brief(msg)}"
    if "notfound" in name or "404" in msg:
        return "model_not_found", f"Model not found: {_brief(msg)}"
    if "context" in name or "context" in msg.lower():
        return "bad_response", f"Context error: {_brief(msg)}"

    return "bad_response", _brief(msg)


def _brief(text: str, limit: int = 200) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


# ---------------------------------------------------------------------------
# Tiny red PNG (stdlib only, no Pillow dependency)
# ---------------------------------------------------------------------------

_RED_PNG_B64_CACHE: str | None = None


def _tiny_red_png_b64() -> str:
    """Return a base64-encoded 16×16 solid red PNG."""
    global _RED_PNG_B64_CACHE
    if _RED_PNG_B64_CACHE is not None:
        return _RED_PNG_B64_CACHE

    width, height = 16, 16
    raw = b""
    for _ in range(height):
        raw += b"\x00"          # filter: none
        raw += b"\xff\x00\x00" * width  # RGB red

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        body = ctype + data
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + body + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", idat)
    png += _chunk(b"IEND", b"")

    _RED_PNG_B64_CACHE = base64.b64encode(png).decode()
    return _RED_PNG_B64_CACHE


# ---------------------------------------------------------------------------
# Endpoint construction from a Settings object (not global settings)
# ---------------------------------------------------------------------------

def _endpoint_from_role(cfg: Settings, role: str) -> ModelEndpoint:
    """Build a ModelEndpoint by resolving the reuse chain on *cfg*."""
    ms = cfg.model_settings_for_role(role)
    return ModelEndpoint(
        role=role,
        model=ms.model,
        provider=ms.provider,
        api_key=ms.api_key,
        api_base=ms.base_url,
        extra=ms.extra,
        source=ms.source,
        hf_endpoint=ms.hf_endpoint,
    )


def _text_endpoint_key(ep: ModelEndpoint) -> str:
    """Unique key for text-only endpoints (used for dedup)."""
    return f"{ep.provider}|{ep.model}|{ep.api_key}|{ep.api_base}"


# ---------------------------------------------------------------------------
# Individual check implementations
# ---------------------------------------------------------------------------

async def _check_chat_model(endpoint: ModelEndpoint, prompt: str) -> None:
    """Test a chat model with a simple text prompt via LiteLLM."""
    import litellm

    if endpoint.is_local:
        raise RuntimeError("Chat model is configured as local (not supported)")

    await litellm.acompletion(
        model=endpoint.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=50,
        timeout=60,
        stream=False,
        drop_params=True,
        **endpoint.litellm_kwargs(),
    )


async def _check_vision_model(endpoint: ModelEndpoint, prompt: str) -> None:
    """Test a vision model with a base64 image via LiteLLM."""
    import litellm

    if endpoint.is_local:
        raise RuntimeError("Vision model is configured as local (not supported)")

    png_b64 = _tiny_red_png_b64()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{png_b64}",
            }},
        ],
    }]

    await litellm.acompletion(
        model=endpoint.litellm_model,
        messages=messages,
        max_tokens=50,
        timeout=60,
        stream=False,
        drop_params=True,
        **endpoint.litellm_kwargs(),
    )


async def _check_draw_model(endpoint: ModelEndpoint, prompt: str) -> None:
    """Test an image generation model via LiteLLM."""
    import litellm

    if endpoint.is_local:
        raise RuntimeError("Draw model is configured as local (not supported)")

    kwargs: dict[str, Any] = {
        "model": endpoint.litellm_model,
        "prompt": prompt,
        "n": 1,
        "size": "256x256",
        "timeout": 60,
        **endpoint.litellm_kwargs(),
    }

    if hasattr(litellm, "aimage_generation"):
        await litellm.aimage_generation(**kwargs)
    else:
        await asyncio.to_thread(litellm.image_generation, **kwargs)


async def _check_embedding_model(endpoint: ModelEndpoint) -> None:
    """Test embedding: cloud via LiteLLM, local via HuggingFaceEmbedding."""
    if endpoint.is_local:
        await _check_local_embedding(endpoint)
    else:
        await _check_cloud_embedding(endpoint)


async def _check_cloud_embedding(endpoint: ModelEndpoint) -> None:
    import litellm

    response = await asyncio.to_thread(
        litellm.embedding,
        model=endpoint.litellm_model,
        input=["SiGMA is a local AI research and writing platform."],
        timeout=30,
        **endpoint.litellm_kwargs(),
    )
    data = response.data if hasattr(response, "data") else response.get("data", [])
    if not data or not data[0].get("embedding"):
        raise RuntimeError("Embedding provider returned empty result")


async def _check_local_embedding(endpoint: ModelEndpoint) -> None:
    from app.services.rag_service import _resolve_model_path

    def _run():
        from llama_index.embeddings.huggingface import HuggingFaceEmbedding

        model_path = _resolve_model_path(
            endpoint.model, endpoint.source, endpoint.hf_endpoint,
        )
        model = HuggingFaceEmbedding(model_name=model_path)
        result = model.get_text_embedding("SiGMA is a local AI research and writing platform.")
        if result is None or len(result) == 0:
            raise RuntimeError("Local embedding model returned empty vector")

    await asyncio.to_thread(_run)


async def _check_rerank_model(endpoint: ModelEndpoint) -> None:
    """Test rerank: cloud via LiteLLM, local via CrossEncoder."""
    if endpoint.is_local:
        await _check_local_rerank(endpoint)
    else:
        await _check_cloud_rerank(endpoint)


async def _check_cloud_rerank(endpoint: ModelEndpoint) -> None:
    import litellm

    await asyncio.to_thread(
        litellm.rerank,
        model=endpoint.litellm_model,
        query="What is SiGMA?",
        documents=[
            "SiGMA is a local AI research platform.",
            "The cat sat on the mat.",
        ],
        top_n=2,
        **endpoint.litellm_kwargs(),
    )


async def _check_local_rerank(endpoint: ModelEndpoint) -> None:
    from app.services.rag_service import _resolve_model_path

    def _run():
        from sentence_transformers import CrossEncoder

        model_path = _resolve_model_path(
            endpoint.model, endpoint.source, endpoint.hf_endpoint,
        )
        model = CrossEncoder(model_path)
        scores = model.predict([
            ["What is SiGMA?", "SiGMA is a local AI research platform."],
            ["What is SiGMA?", "The cat sat on the mat."],
        ])
        if scores is None or len(scores) == 0:
            raise RuntimeError("Local reranker returned empty scores")

    await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class SettingsCheckService:
    """Validates settings config and checks model connectivity.

    Yields SSE events so the frontend can display real-time progress.
    """

    async def check(
        self,
        content: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> AsyncGenerator[str, None]:
        """Run all checks and yield SSE event strings.

        Parameters mirror the existing PUT /system/settings body:
        either a raw YAML string (content) or a structured dict (config).
        """
        total = len(_CHECK_ITEMS)
        yield _sse("check_start", {"total": total})

        # 1. Parse and validate config structure
        yield _sse("check_progress", {"role": "structure", "label": "Config Structure"})

        try:
            if content is not None:
                parsed = validate_settings_yaml(content)
            elif config is not None:
                parsed = Settings.model_validate(config)
            else:
                raise ValueError("Either content or config is required")
        except Exception as exc:
            logger.debug("Settings structure validation failed", exc_info=True)
            yield _sse("check_result", {
                "role": "structure", "label": "Config Structure",
                "status": "fail", "error_type": "config_error",
                "message": _brief(str(exc)),
            })
            yield _sse("check_done", {"passed": 0, "failed": 1, "skipped": total - 1})
            return

        # Additional required-field checks
        missing = self._check_required_fields(parsed)
        if missing:
            yield _sse("check_result", {
                "role": "structure", "label": "Config Structure",
                "status": "fail", "error_type": "config_error",
                "message": f"Missing required fields: {', '.join(missing)}",
            })
            yield _sse("check_done", {"passed": 0, "failed": 1, "skipped": total - 1})
            return

        yield _sse("check_result", {
            "role": "structure", "label": "Config Structure", "status": "pass",
        })

        # 2. Model connectivity checks
        passed = 1
        failed = 0
        skipped = 0
        tested_text_keys: set[str] = set()

        for role, label in _CHECK_ITEMS[1:]:
            yield _sse("check_progress", {"role": role, "label": label})

            skip_reason = self._should_skip(parsed, role)
            if skip_reason:
                skipped += 1
                yield _sse("check_result", {
                    "role": role, "label": label,
                    "status": "skip", "reason": skip_reason,
                })
                # Still record the text endpoint key for dedup
                self._record_key(parsed, role, tested_text_keys)
                continue

            result = await self._run_model_check(parsed, role)
            yield _sse("check_result", result)

            if result["status"] == "pass":
                passed += 1
                self._record_key(parsed, role, tested_text_keys)
            else:
                failed += 1

        yield _sse("check_done", {"passed": passed, "failed": failed, "skipped": skipped})

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _check_required_fields(self, cfg: Settings) -> list[str]:
        """Return a list of human-readable missing-field descriptions."""
        missing = []
        supervisor = cfg.model_settings_for_role("supervisor")
        if not supervisor.model:
            missing.append("supervisor model")

        # Draw, embedding, rerank are optional — only flag if provider is set
        # but model is empty
        for role in ("draw", "embedding", "rerank"):
            ms = getattr(cfg.models, role, None)
            if ms and ms.provider and not ms.model and not ms.reuse:
                missing.append(f"{role} model")

        return missing

    def _should_skip(self, cfg: Settings, role: str) -> str | None:
        """Return a skip reason string, or None if this check should run."""
        ms = getattr(cfg.models, role, None)
        if not ms:
            return "Not configured"

        # No model name and not reusing → skip (optional feature)
        if not ms.model and not ms.reuse:
            return "Not configured"

        # RA reuses supervisor → skip text check (same endpoint)
        if role == "ra" and ms.reuse:
            return f"Reuses {ms.reuse}"

        # Vision always runs (multimodal test), even when reusing

        # Rerank disabled → skip
        if role == "rerank" and not cfg.library.reranker_enabled:
            return "Reranker disabled"

        return None

    def _record_key(
        self, cfg: Settings, role: str, tested_text_keys: set[str],
    ) -> None:
        """Record the text-endpoint key for dedup after a check."""
        # Vision uses a different test (multimodal), so its key doesn't
        # count as a "text-only" test for dedup purposes
        if role in ("vision", "embedding", "rerank", "structure"):
            return
        try:
            ep = _endpoint_from_role(cfg, role)
            if ep.model:
                tested_text_keys.add(_text_endpoint_key(ep))
        except (ValueError, AttributeError):
            pass

    async def _run_model_check(self, cfg: Settings, role: str) -> dict:
        """Execute a single model connectivity check, return result dict."""
        try:
            endpoint = _endpoint_from_role(cfg, role)
        except (ValueError, AttributeError) as exc:
            return {
                "role": role, "label": dict(_CHECK_ITEMS).get(role, role),
                "status": "fail", "error_type": "config_error",
                "message": _brief(str(exc)),
            }

        label = dict(_CHECK_ITEMS).get(role, role)
        start = time.monotonic()

        try:
            if role in ("supervisor", "ra"):
                await asyncio.wait_for(
                    _check_chat_model(endpoint, "Who are you? Reply in under 10 words."),
                    timeout=60,
                )
            elif role == "vision":
                await asyncio.wait_for(
                    _check_vision_model(
                        endpoint,
                        "What color is this image? Reply in under 10 words.",
                    ),
                    timeout=60,
                )
            elif role == "draw":
                await asyncio.wait_for(
                    _check_draw_model(endpoint, "A simple landscape painting"),
                    timeout=60,
                )
            elif role == "embedding":
                await asyncio.wait_for(
                    _check_embedding_model(endpoint),
                    timeout=120,  # local model loading can be slow
                )
            elif role == "rerank":
                await asyncio.wait_for(
                    _check_rerank_model(endpoint),
                    timeout=120,
                )
            else:
                return {
                    "role": role, "label": label, "status": "skip",
                    "reason": "Unknown check type",
                }

            duration_ms = int((time.monotonic() - start) * 1000)
            return {"role": role, "label": label, "status": "pass", "duration_ms": duration_ms}

        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "role": role, "label": label,
                "status": "fail", "error_type": "timeout",
                "message": "Check timed out after 60 seconds",
                "duration_ms": duration_ms,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Model connectivity check failed for role %s", role, exc_info=True)
            duration_ms = int((time.monotonic() - start) * 1000)
            error_type, message = _classify_error(exc)
            return {
                "role": role, "label": label,
                "status": "fail", "error_type": error_type,
                "message": message, "duration_ms": duration_ms,
            }
