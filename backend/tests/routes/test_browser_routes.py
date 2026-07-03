from types import SimpleNamespace

import pytest

from app.routes import browser


@pytest.mark.route
@pytest.mark.asyncio
async def test_start_browser_validates_project_before_start(monkeypatch):
    calls = []

    async def start():
        calls.append("start")
        return {"status": "running"}

    monkeypatch.setattr(
        browser,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: calls.append(("project", project_id))),
    )
    monkeypatch.setattr(browser, "get_browser_service", lambda: SimpleNamespace(start=start))

    result = await browser.start_browser("project-1")

    assert result["success"] is True
    assert calls == [("project", "project-1"), "start"]


@pytest.mark.route
@pytest.mark.asyncio
async def test_stop_browser_returns_stopped_status(monkeypatch):
    calls = []

    async def stop():
        calls.append("stop")

    monkeypatch.setattr(
        browser,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: calls.append(("project", project_id))),
    )
    monkeypatch.setattr(browser, "get_browser_service", lambda: SimpleNamespace(stop=stop))

    result = await browser.stop_browser("project-1")

    assert result["data"] == {"status": "stopped"}
    assert calls == [("project", "project-1"), "stop"]
