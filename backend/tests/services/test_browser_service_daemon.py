"""Tests for ``BrowserService._chrome_daemon`` crash-loop bounding.

The daemon relaunches Chrome when it exits, but only up to
``_CHROME_MAX_RELAUNCH`` times; after that it sets ``_chrome_failed`` and
stops, so a corrupted profile cannot trigger unbounded relaunches.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import browser_service as bs_module
from app.services.browser_service import BrowserService


def _make_service() -> BrowserService:
    return BrowserService(base_dir="/tmp/sigma-test-daemon")


def _dead_proc(returncode: int = 1) -> SimpleNamespace:
    """Stand-in for an asyncio subprocess that has already exited."""
    return SimpleNamespace(returncode=returncode)


def _patch_sleep(monkeypatch):
    """Replace asyncio.sleep in the browser_service module with a no-op.

    The daemon has no real subprocess to wait on in these tests, so the
    poll/interval sleeps only slow things down. Patching the module-level
    reference the daemon uses keeps each test under a few milliseconds.
    monkeypatch restores the original on teardown.
    """
    async def _fast(_delay, *_args, **_kwargs):
        return None
    monkeypatch.setattr(bs_module.asyncio, "sleep", _fast)


@pytest.mark.asyncio
async def test_daemon_gives_up_after_max_relaunches(monkeypatch):
    svc = _make_service()
    svc._running = True
    monkeypatch.setattr(svc, "_port_alive", lambda _port: True)
    _patch_sleep(monkeypatch)

    relaunch = AsyncMock(side_effect=lambda: _dead_proc(1))
    monkeypatch.setattr(svc, "_relaunch_chrome", relaunch)

    svc._procs = {"chrome": _dead_proc(1)}

    await svc._chrome_daemon()

    assert svc._chrome_failed is True
    assert relaunch.await_count == BrowserService._CHROME_MAX_RELAUNCH
    assert svc._chrome_relaunch_count == BrowserService._CHROME_MAX_RELAUNCH + 1


@pytest.mark.asyncio
async def test_daemon_resets_counter_when_chrome_is_alive(monkeypatch):
    """A live Chrome process at poll time clears the failure counter."""
    svc = _make_service()
    svc._running = True
    svc._chrome_relaunch_count = 3
    monkeypatch.setattr(svc, "_port_alive", lambda _port: True)

    # Live proc (returncode is None). The relaunch mock fails the test if
    # the daemon ever calls it.
    svc._procs = {"chrome": SimpleNamespace(returncode=None)}
    relaunch = AsyncMock()
    monkeypatch.setattr(svc, "_relaunch_chrome", relaunch)

    # Flip _running from inside the daemon's first poll sleep so the loop
    # exits deterministically after one cycle. The counter must have been
    # reset before that sleep fired.
    original_sleep = bs_module.asyncio.sleep

    async def _sleep_then_stop(_delay, *_a, **_kw):
        svc._running = False
        await original_sleep(0)
    monkeypatch.setattr(bs_module.asyncio, "sleep", _sleep_then_stop)

    await svc._chrome_daemon()

    assert svc._chrome_failed is False
    assert svc._chrome_relaunch_count == 0
    assert relaunch.await_count == 0


@pytest.mark.asyncio
async def test_daemon_stops_without_relaunch_when_shutdown_during_interval(monkeypatch):
    """_running=False observed during the post-crash interval exits cleanly."""
    svc = _make_service()
    svc._running = True
    monkeypatch.setattr(svc, "_port_alive", lambda _port: True)

    # The interval sleep is the window where shutdown can race. Flip
    # _running from inside that sleep; the subsequent `if not self._running`
    # check must break the loop without another relaunch.
    original_sleep = bs_module.asyncio.sleep

    async def _flip_running(_delay, *_a, **_kw):
        svc._running = False
        await original_sleep(0)
    monkeypatch.setattr(bs_module.asyncio, "sleep", _flip_running)

    relaunch = AsyncMock(side_effect=lambda: _dead_proc(1))
    monkeypatch.setattr(svc, "_relaunch_chrome", relaunch)
    svc._procs = {"chrome": _dead_proc(1)}

    await svc._chrome_daemon()

    assert svc._chrome_failed is False
    assert relaunch.await_count == 0
