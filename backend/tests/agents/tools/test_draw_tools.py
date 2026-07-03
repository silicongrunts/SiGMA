"""Tests for the draw_image tool — focus on the P0 regression:
the tool must never let an exception escape to the loop runner.
"""

import pytest

from app.agents.tools.draw_tools import _draw_image
from app.core.config import settings, ModelSettings


# ---------------------------------------------------------------------------
# Preflight checks — must return friendly strings, never raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draw_image_no_model_configured(monkeypatch):
    monkeypatch.setattr(settings.models, "draw", ModelSettings())
    result = await _draw_image("a cat", project_id="proj")
    assert result == "Error: draw model is not configured."


@pytest.mark.asyncio
async def test_draw_image_empty_prompt(monkeypatch):
    monkeypatch.setattr(settings.models, "draw", ModelSettings(
        model="dall-e-test", provider="openai", api_key="sk-test",
    ))
    assert await _draw_image("", project_id="proj") == "Error: prompt is required."
    assert await _draw_image("   ", project_id="proj") == "Error: prompt is required."


# ---------------------------------------------------------------------------
# LLM-call failure — the load-bearing regression test for the P0 fix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draw_image_llm_failure_does_not_propagate(monkeypatch):
    monkeypatch.setattr(settings.models, "draw", ModelSettings(
        model="dall-e-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import draw_tools

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream provider exploded")

    monkeypatch.setattr(draw_tools.draw_service, "draw_image", boom)

    result = await _draw_image("a cat", project_id="proj")

    # No exception escaped; LLM receives a structured error string it can react to.
    assert result.startswith("Error: failed to generate image:")
    assert "upstream provider exploded" in result


@pytest.mark.asyncio
async def test_draw_image_service_dict_error_passes_through(monkeypatch):
    """When the service returns ``{"error": ...}`` (e.g. empty prompt slip-through),
    the tool surfaces it as an Error: string."""
    monkeypatch.setattr(settings.models, "draw", ModelSettings(
        model="dall-e-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import draw_tools

    async def returns_dict_error(*args, **kwargs):
        return {"error": "rate limited"}

    monkeypatch.setattr(draw_tools.draw_service, "draw_image", returns_dict_error)

    result = await _draw_image("a cat", project_id="proj")
    assert result == "Error: rate limited"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_draw_image_success(monkeypatch):
    monkeypatch.setattr(settings.models, "draw", ModelSettings(
        model="dall-e-test", provider="openai", api_key="sk-test",
    ))

    from app.agents.tools import draw_tools

    async def fake_draw(project_id, prompt):
        return {
            "path": ".SiGMA/draw/test.png",
            "prompt": prompt,
            "mime_type": "image/png",
        }

    monkeypatch.setattr(draw_tools.draw_service, "draw_image", fake_draw)

    result = await _draw_image("a cat", project_id="proj")
    assert "Generated image saved at `.SiGMA/draw/test.png`." in result
    assert "![](.SiGMA/draw/test.png)" in result
