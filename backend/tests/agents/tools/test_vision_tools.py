"""Tests for the vision_analyze tool — focus on the P0 regression:
the tool must never let an exception escape to the loop runner.
"""

import pytest

from app.agents.tools.vision_tools import _vision
from app.core.config import settings, ModelSettings


# ---------------------------------------------------------------------------
# Preflight checks — must return friendly strings, never raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vision_no_model_configured(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings())
    result = await _vision(question="what is this", image_path="x.png", project_id="proj")
    assert result == "Error: vision model is not configured."


@pytest.mark.asyncio
async def test_vision_empty_inputs(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))
    assert await _vision(question="", image_path="x.png", project_id="proj") == \
        "Error: question is required."
    assert await _vision(question="what", image_path="", project_id="proj") == \
        "Error: image_path is required."


# ---------------------------------------------------------------------------
# Image-read failure — must surface as Error string, not exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vision_read_failure(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import vision_tools
    from app.core.exceptions import FileSystemError

    async def read_boom(*args, **kwargs):
        raise FileSystemError("not found", code="NOT_FOUND", status_code=404)

    monkeypatch.setattr(vision_tools, "read_image_path_base64", read_boom)

    result = await _vision(question="what", image_path="missing.png", project_id="proj")
    assert result.startswith("Error: vision analysis failed:")
    assert "not found" in result


# ---------------------------------------------------------------------------
# LLM-call failure — the load-bearing regression test for the P0 fix.
# Previously, only read_image_path_base64 was wrapped in try/except and any
# exception from call_vision would abort the entire conversation turn.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vision_llm_failure_does_not_propagate(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import vision_tools

    async def fake_read(*args, **kwargs):
        return ("ZmFrZQ==", "image/png")

    async def vision_boom(*args, **kwargs):
        raise RuntimeError("upstream provider exploded")

    monkeypatch.setattr(vision_tools, "read_image_path_base64", fake_read)
    monkeypatch.setattr(vision_tools.llm_service, "call_vision", vision_boom)

    result = await _vision(question="what", image_path="x.png", project_id="proj")

    assert result.startswith("Error: vision analysis failed:")
    assert "upstream provider exploded" in result


# ---------------------------------------------------------------------------
# Empty-analysis fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vision_empty_analysis_fallback(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import vision_tools

    async def fake_read(*args, **kwargs):
        return ("ZmFrZQ==", "image/png")

    async def fake_call(*args, **kwargs):
        return ""

    monkeypatch.setattr(vision_tools, "read_image_path_base64", fake_read)
    monkeypatch.setattr(vision_tools.llm_service, "call_vision", fake_call)

    result = await _vision(question="what", image_path="x.png", project_id="proj")
    assert result == "(vision model returned no analysis)"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vision_success(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import vision_tools

    async def fake_read(*args, **kwargs):
        return ("ZmFrZQ==", "image/png")

    async def fake_call(*args, **kwargs):
        return "It's a cat."

    monkeypatch.setattr(vision_tools, "read_image_path_base64", fake_read)
    monkeypatch.setattr(vision_tools.llm_service, "call_vision", fake_call)

    result = await _vision(question="what", image_path="x.png", project_id="proj")
    assert result == "It's a cat."
