"""Tests for the sleep tool — input validation and messaging."""

import pytest

from app.agents.tools.sleep import _sleep, _MAX_DURATION


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Replace ``asyncio.sleep`` with an instant no-op for every test here.

    Deliberately global: the tool body calls ``asyncio.sleep`` directly (not
    an injectable seam), so the module-global patch is the only way to keep
    duration-based tests instant. monkeypatch restores the real sleep after
    each test; nothing else in these tests' event loops depends on real
    time.
    """
    async def _instant(_):
        return None
    monkeypatch.setattr("asyncio.sleep", _instant)


@pytest.mark.asyncio
async def test_sleep_negative_returns_error():
    result = await _sleep(-1)
    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "non-negative" in result
    assert "-1" in result


@pytest.mark.asyncio
async def test_sleep_zero_returns_immediately():
    # Zero is valid (no actual wait beyond asyncio overhead)
    result = await _sleep(0)
    assert result == "Slept for 0 seconds."


@pytest.mark.asyncio
async def test_sleep_normal_duration():
    result = await _sleep(1)
    assert result == "Slept for 1 seconds."


@pytest.mark.asyncio
async def test_sleep_caps_at_max_and_notes_capping():
    result = await _sleep(_MAX_DURATION + 100)
    assert f"Slept for {_MAX_DURATION} seconds" in result
    assert "capped" in result
    assert str(_MAX_DURATION + 100) in result  # requested value echoed


@pytest.mark.asyncio
async def test_sleep_exactly_max_no_cap_message():
    result = await _sleep(_MAX_DURATION)
    assert result == f"Slept for {_MAX_DURATION} seconds."
    assert "capped" not in result
