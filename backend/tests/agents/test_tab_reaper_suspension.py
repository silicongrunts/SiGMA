"""Unit tests for TabReaper suspension (ChatGPT OAuth login support).

The login window keeps an OAuth tab open far longer than the idle timeout,
so the reaper must be suspendable; after resumption every tab gets a fresh
idle window (time spent inside the suspension must not count as idle).
"""

import httpx
import pytest

from app.agents.tools.browser_tab_reaper import TabReaper


@pytest.fixture
def reaper() -> TabReaper:
    return TabReaper(cdp_port=1, idle_timeout=0)  # never started; no loop


@pytest.mark.unit
def test_suspended_scan_skips_cdp_and_resets_idle_clock(reaper, monkeypatch):
    reaper._state = {
        "tid-1": {"key": ("https://example.com", "Example"), "first_seen": 0.0},
    }
    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: calls.append(a))

    reaper.suspend()
    reaper._scan()

    assert calls == []                     # CDP never touched while suspended
    assert reaper._state == {}             # idle baseline dropped


@pytest.mark.unit
def test_resume_restarts_scans(reaper, monkeypatch):
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return []

    seen = []
    monkeypatch.setattr(httpx, "get", lambda url, timeout: seen.append(url) or _Resp())

    reaper.suspend()
    reaper._scan()
    assert seen == []

    reaper.resume()
    reaper._scan()
    assert len(seen) == 1                  # scanning again after the last owner


@pytest.mark.unit
def test_refcount_floors_at_zero(reaper):
    reaper.resume()                        # unbalanced resume must not go negative
    reaper.suspend()
    reaper.suspend()
    reaper.resume()
    reaper._scan()                         # still suspended (count == 1)
    reaper.resume()
    assert reaper._suspend_count == 0
