"""Background memory handlers: cursor skip, extraction end-to-end, consolidation.

The LLM is scripted (``llm_service.stream_chat`` replaced); everything else —
system prompt rebuild, history rebuild, cursor persistence, real tool
execution — runs for real against the temporary project.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.database.unit_of_work import UnitOfWork
from app.services import llm_service as llm_service_module
from app.services.memory_service import get_cursor, set_cursor

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _ctx(project_id):
    return SimpleNamespace(project_id=project_id, cancel_event=asyncio.Event())


def _scripted_llm(monkeypatch, responses):
    async def fake_stream_chat(**kwargs):
        text, tool_calls = responses.pop(0)
        return text, "", tool_calls, None

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", fake_stream_chat)


def _recording_llm(monkeypatch, reply, calls):
    """Script a single no-tool reply and snapshot each request's messages.

    The loop mutates its working list in place, so a live reference would
    show the final state, not what each call saw.
    """

    async def fake_stream_chat(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return reply, "", [], None

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", fake_stream_chat)


async def _add_messages(project_id, turns):
    """Create a session (messages.session_id is an FK) and fill it."""
    async with UnitOfWork(project_id) as uow:
        session = await uow.sessions.create()
        for role, content in turns:
            await uow.messages.create(session.id, role, content)
        return session.id


@pytest.fixture
def memory_tree(project):
    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def test_extract_skips_without_new_messages(project, monkeypatch):
    async def must_not_be_called(**kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called when the window is empty")

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", must_not_be_called)

    # no messages at all, and cursor already at max seq for the second check
    await handle_extract(project, "sess-empty")
    assert await get_cursor(project, "sess-empty") == -1

    caught = await _add_messages(project, [("user", "hi"), ("assistant", "hello")])
    await set_cursor(project, caught, 2)  # window fully extracted already
    await handle_extract(project, caught)
    assert await get_cursor(project, caught) == 2


async def handle_extract(project, session_id):
    from app.services.memory_task_handlers import handle_memory_extract

    await handle_memory_extract(_ctx(project), {"session_id": session_id})


async def test_extract_end_to_end_writes_memory_and_advances_cursor(
    project, memory_tree, monkeypatch,
):
    sid = await _add_messages(project, [
        ("user", "My name is 张三, please remember it."),
        ("assistant", "Got it."),
    ])
    calls: list = []

    async def scripted(**kwargs):
        # the loop mutates its working list in place, so snapshot per call
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if len(calls) == 1:
            return ("Saving.", "", [{
                "id": "c1", "name": "write",
                "params": {
                    # absolute: write's body resolves relative paths via the
                    # project registry, which the test fixture does not rebind
                    "file_path": str(memory_tree / "user_name.md"),
                    "content": "---\ndescription: user name\n---\n\nThe user's name is 张三.",
                },
            }], None)
        return ("Nothing to save.", "", [], None)

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", scripted)

    await handle_extract(project, sid)

    assert (memory_tree / "user_name.md").exists()
    assert await get_cursor(project, sid) == 1
    # seq 0 is inside the window (the fresh-session cursor starts at -1):
    # the request carries the full history between system prompt and
    # extraction prompt, so the opening user message is actually analyzed
    history = calls[0]["messages"][1:-1]
    assert len(history) == 2
    assert "张三" in history[0]["content"]


async def test_extract_noop_still_advances_cursor(project, memory_tree, monkeypatch):
    sid = await _add_messages(project, [("user", "hello"), ("assistant", "hi")])
    _scripted_llm(monkeypatch, [("Nothing to save.", [])])

    await handle_extract(project, sid)
    # seq is 0-based: two messages cover seq 0..1
    assert await get_cursor(project, sid) == 1
    assert not (memory_tree / "MEMORY.md").exists()


async def test_consolidate_noop_completes(project, memory_tree, monkeypatch):
    (memory_tree / "MEMORY.md").write_text("- a.md — fact\n", encoding="utf-8")
    (memory_tree / "a.md").write_text("fact\n", encoding="utf-8")
    calls: list = []
    _recording_llm(monkeypatch, "Nothing to change.", calls)

    await handle_consolidate(project)
    assert len(calls) == 1
    # consolidation requests carry only the consolidation system prompt + brief
    roles = [m["role"] for m in calls[0]["messages"]]
    assert roles == ["system", "user"]
    assert "MEMORY.md" in calls[0]["messages"][1]["content"]


async def handle_consolidate(project):
    from app.services.memory_task_handlers import handle_memory_consolidate

    await handle_memory_consolidate(_ctx(project), {})


async def _consolidate_value(project_id):
    from app.services.memory_service import MEMORY_CONSOLIDATE_KEY

    async with UnitOfWork(project_id) as uow:
        return await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")


async def test_extract_writes_deplete_consolidation_noop_does_not(
    project, memory_tree, monkeypatch,
):
    """Fact files an extraction actually wrote deplete the project-wide
    consolidation window — distinct files, index edits excluded, and a run
    that writes nothing (no-op) leaves the counter untouched."""
    sid = await _add_messages(project, [
        ("user", "My name is 张三, please remember it."),
        ("assistant", "Got it."),
    ])
    fact = str(memory_tree / "user_name.md")
    interval = settings.memory.consolidation_interval

    async def writes_once(**kwargs):
        return ("Saving.", "", [{
            "id": "c1", "name": "write",
            "params": {
                "file_path": fact,
                "content": "---\ndescription: user name\n---\n\nThe user's name is 张三.",
            },
        }, {
            "id": "c2", "name": "edit",
            "params": {"file_path": fact, "old_string": "张三", "new_string": "张三（Zhang San）"},
        }, {
            "id": "c3", "name": "write",
            "params": {"file_path": str(memory_tree / "MEMORY.md"), "content": "- a.md — x\n"},
        }], None)

    async def noop(**kwargs):
        return ("Nothing to save.", "", [], None)

    state = {"calls": 0}

    async def scripted(**kwargs):
        state["calls"] += 1
        return await (writes_once if state["calls"] == 1 else noop)(**kwargs)

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", scripted)

    # pre-deplete the session's window: a background write must not reset
    # it — only task completion does, and this direct handler test skips it
    from app.services.memory_service import memory_service

    for _ in range(3):
        await memory_service.note_main_loop_llm_call(project, sid)

    # one fact file written (write + edit) plus the index: -1, not -2 or -3
    await handle_extract(project, sid)
    assert await _consolidate_value(project) == str(interval - 1)
    async with UnitOfWork(project) as uow:
        window = json.loads(await uow.config.get(f"memory.{sid}", "{}"))
    assert window["cursor"] == 1
    assert window["extract_remaining"] == settings.memory.extraction_interval - 3

    # a no-op run over fresh messages writes nothing: counter unchanged
    sid2 = await _add_messages(project, [("user", "bye"), ("assistant", "bye")])
    await handle_extract(project, sid2)
    assert await _consolidate_value(project) == str(interval - 1)


def test_manifest_counts_characters_not_bytes(project, memory_tree):
    """The manifest feeds a prompt whose size rule is in characters; CJK
    byte sizes run ~3x that count, so a byte figure would send compliant
    files to condensing on every consolidation round."""
    from app.services.memory_task_handlers import _memory_manifest

    content = "---\ndescription: 用户偏好\n---\n\n" + "用户偏好安静的环境。" * 40
    assert len(content.encode("utf-8")) != len(content)
    (memory_tree / "preference.md").write_text(content, encoding="utf-8")

    entries = {e["file"]: e for e in _memory_manifest(project)}
    assert entries["preference.md"]["chars"] == len(content)
    assert entries["preference.md"]["description"] == "用户偏好"


async def test_extract_failure_leaves_cursor_frozen(project, memory_tree, monkeypatch):
    sid = await _add_messages(project, [("user", "hi"), ("assistant", "hello")])

    async def broken_stream(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", broken_stream)

    with pytest.raises(RuntimeError):
        await handle_extract(project, sid)
    # the queue owns retry: the cursor must not advance past unprocessed turns
    assert await get_cursor(project, sid) == -1


async def test_start_due_task_payload_feeds_extraction(project, memory_tree, monkeypatch):
    """The production enqueue path must build a payload the handler accepts:
    an extraction row without session_id silently no-ops and still reports
    success, so the memory_service → handler seam needs an end-to-end check."""
    enqueued = {}

    async def fake_enqueue(**kwargs):
        enqueued.update(kwargs)
        return "task-id"

    from app.services import background_task_service as bts
    from app.services.memory_service import memory_service

    monkeypatch.setattr(bts.background_task_service, "enqueue", fake_enqueue)

    sid = await _add_messages(project, [
        ("user", "My name is 张三, please remember it."),
        ("assistant", "Got it."),
    ])
    assert await memory_service.start_due_task(project, "extract", session_id=sid) is True
    assert enqueued["payload"]["session_id"] == sid

    calls = []

    async def scripted(**kwargs):
        calls.append(kwargs)
        return "Nothing to save.", "", [], None

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", scripted)

    # feed the captured payload straight to the handler: the chain works
    from app.services.memory_task_handlers import handle_memory_extract

    await handle_memory_extract(_ctx(project), enqueued["payload"])
    assert await get_cursor(project, sid) == 1
    # the LLM call routes through the replayed chat session, so the
    # byte-identical prefix can hit the main loop's provider cache
    assert calls[0]["session_id"] == sid


def test_startup_handler_registration_covers_memory_kinds():
    """Registration is import-time; the startup function must import the module.

    A missing import is invisible to in-process tests (some other test always
    imports the handlers), so this runs a clean interpreter through the real
    startup seam — without it, every background memory task dies with
    "unknown task kind".
    """
    code = (
        "from app.core.lifecycle import register_background_task_handlers\n"
        "register_background_task_handlers()\n"
        "from app.services.library_task_protocol import (\n"
        "    KIND_MEMORY_CONSOLIDATE,\n"
        "    KIND_MEMORY_EXTRACT,\n"
        "    get_task_handler,\n"
        ")\n"
        "assert get_task_handler(KIND_MEMORY_EXTRACT) is not None\n"
        "assert get_task_handler(KIND_MEMORY_CONSOLIDATE) is not None\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(BACKEND_ROOT)},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
