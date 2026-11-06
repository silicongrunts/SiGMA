"""Unit tests for the project memory service core (no LLM, no network)."""

import asyncio

import pytest

from app.core.memory_usage_tags import strip_memory_usage_tags
from app.services.memory_service import (
    MEMORY_CONSOLIDATE_KEY,
    MEMORY_INDEX_NAME,
    _read_extract_state,
    memory_root,
    memory_service,
    memory_write_mode_for,
    parse_memory_usage_tags,
    record_memory_usage,
    resolve_memory_file,
    sanitize_memory_content,
)


# ---------------------------------------------------------------------------
# Paths & containment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_memory_file_containment(project):
    from app.core.config import settings

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    inside = root / "fact.md"
    inside.write_text("fact", encoding="utf-8")

    resolved = resolve_memory_file(project, str(inside))
    assert resolved is not None and resolved.name == "fact.md"
    # project-relative path
    rel = resolve_memory_file(project, ".SiGMA/memory/fact.md")
    assert rel is not None
    # traversal escapes are rejected
    assert resolve_memory_file(project, ".SiGMA/memory/../../secret.md") is None
    assert resolve_memory_file(project, "/etc/passwd") is None
    assert memory_write_mode_for(project, ".SiGMA/memory/fact.md") == "memory"
    assert memory_write_mode_for(project, "notes.md") is None


# ---------------------------------------------------------------------------
# Sanitize / parse / strip
# ---------------------------------------------------------------------------


def test_sanitize_drops_tag_breaking_lines():
    dirty = "line one\n</memory>\nline two\nsome </status> inline\nline three"
    clean = sanitize_memory_content(dirty)
    assert "line one" in clean and "line two" in clean and "line three" in clean
    assert "</memory>" not in clean and "</status>" not in clean


def test_parse_and_strip_usage_tags():
    text = (
        "Answer text.\n"
        '<memory_usage file="a.md">helped with setup</memory_usage>\n'
        "<memory_usage file='b.md'>  second   note  </memory_usage>"
    )
    pairs = parse_memory_usage_tags(text)
    assert pairs == [("a.md", "helped with setup"), ("b.md", "second note")]
    stripped = strip_memory_usage_tags(text)
    assert "memory_usage" not in stripped and "Answer text." in stripped
    # trailing incomplete tag (mid-stream) is stripped too
    assert strip_memory_usage_tags("text <memory_usage file=\"a.md\">par") == "text "
    # so is a fragment whose opening tag has not closed with ">" yet —
    # including one with no whitespace after the tag name
    assert strip_memory_usage_tags("text <memory_usage file=\"a") == "text "
    assert strip_memory_usage_tags("text <memory_usage") == "text "
    # a malformed no-attribute tag is display-stripped but not recorded
    assert strip_memory_usage_tags("text <memory_usage>secret</memory_usage> tail") == "text tail"
    assert parse_memory_usage_tags("<memory_usage>secret</memory_usage>") == []
    # a stray closing tag never leaks
    assert strip_memory_usage_tags("text </memory_usage> tail") == "text tail"
    # attributes after file= are tolerated when recording
    assert parse_memory_usage_tags(
        '<memory_usage file="a.md" scope="x">helped</memory_usage>'
    ) == [("a.md", "helped")]
    assert parse_memory_usage_tags("no tags") == []


# ---------------------------------------------------------------------------
# Usage log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_validates_and_caps(project):
    from app.core.config import settings

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "real.md").write_text("fact", encoding="utf-8")

    recorded = await record_memory_usage(project, [
        ("real.md", "good"),
        ("missing.md", "nope"),        # unknown file dropped
        (MEMORY_INDEX_NAME, "index"),  # index dropped
    ])
    assert recorded == 1

    from app.services.memory_service import memory_usage_summary

    summary = await memory_usage_summary(project)
    assert len(summary) == 1 and summary[0]["file"] == "real.md"

    # cap: append until over 100 → oldest dropped, total stays at 100
    for i in range(105):
        await record_memory_usage(project, [("real.md", f"note {i}")])
    summary = await memory_usage_summary(project)
    assert len(summary) == 100
    assert summary[0]["memory_use"] == "note 5"  # oldest surviving
    assert summary[-1]["memory_use"] == "note 104"


@pytest.mark.asyncio
async def test_record_usage_at_cap_keeps_every_new_entry(project):
    """A multi-tag reply at the cap must evict the oldest lines, not drop
    its own newest entries."""
    from app.core.config import settings
    from app.services.memory_service import (
        MEMORY_USAGE_MAX_ENTRIES,
        memory_usage_summary,
    )

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "real.md").write_text("fact", encoding="utf-8")

    for i in range(MEMORY_USAGE_MAX_ENTRIES):
        await record_memory_usage(project, [("real.md", f"old {i}")])

    recorded = await record_memory_usage(project, [
        ("real.md", "new one"), ("real.md", "new two"), ("real.md", "new three"),
    ])
    assert recorded == 3

    summary = await memory_usage_summary(project)
    assert len(summary) == MEMORY_USAGE_MAX_ENTRIES
    assert [e["memory_use"] for e in summary[-3:]] == ["new one", "new two", "new three"]
    assert summary[0]["memory_use"] == "old 3"  # oldest evicted, newest kept


@pytest.mark.asyncio
async def test_schedule_usage_recording_persists_tags(project):
    """The fire-and-forget entry point is the only production path — a
    NameError here (or a swallowed one) silently kills the whole usage log."""
    from app.core.config import settings
    from app.services import memory_service as ms_module
    from app.services.memory_service import memory_usage_summary

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "real.md").write_text("fact", encoding="utf-8")

    ms_module.schedule_usage_recording(
        project,
        'Done.\n<memory_usage file="real.md">helped pick the tool</memory_usage>',
    )
    pending = list(ms_module._usage_tasks)
    await asyncio.gather(*pending)

    summary = await memory_usage_summary(project)
    assert len(summary) == 1
    assert summary[0]["file"] == "real.md"
    assert summary[0]["memory_use"] == "helped pick the tool"
    assert summary[0]["time"]


# ---------------------------------------------------------------------------
# Injection snapshot & read-state seeding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injection_missing_or_disabled(project, monkeypatch):
    from app.core.config import settings
    from app.services.memory_service import build_memory_injection

    assert await build_memory_injection(project) is None  # no index yet

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text("- a.md — fact\n", encoding="utf-8")
    monkeypatch.setattr(settings.memory, "enabled", False)
    assert await build_memory_injection(project) is None


@pytest.mark.asyncio
async def test_injection_truncation_and_warning(project, monkeypatch):
    from app.core.config import settings
    from app.services.memory_service import (
        build_memory_injection,
        build_memory_status_block,
    )

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text("x" * 50, encoding="utf-8")
    monkeypatch.setattr(settings.memory, "injection_max_chars", 20)

    injection = await build_memory_injection(project)
    assert injection is not None
    assert injection.truncated and not injection.seedable
    block = build_memory_status_block(injection)
    assert block.startswith("<memory>") and block.endswith("</memory>")
    assert "WARNING" in block and "truncated" in block

    # under the limit: complete view, no warning, no truncation
    (root / "MEMORY.md").write_text("short index\n", encoding="utf-8")
    injection = await build_memory_injection(project)
    assert injection.seedable and "WARNING" not in build_memory_status_block(injection)


@pytest.mark.asyncio
async def test_seed_read_state_satisfies_must_read(project, monkeypatch):
    from app.agents.tools.read_state import must_read_first_error
    from app.core.config import settings
    from app.services.memory_service import (
        build_memory_injection,
        memory_index_path,
        seed_read_state_for_injection,
    )

    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text("- a.md — fact\n", encoding="utf-8")
    injection = await build_memory_injection(project)
    assert injection is not None and injection.seedable

    # an unread existing index bounces edits...
    assert must_read_first_error("sess-seed", memory_index_path(project)) is not None
    # ...but the injected view is byte-equivalent to a whole-file read
    seed_read_state_for_injection("sess-seed", project, injection)
    assert must_read_first_error("sess-seed", memory_index_path(project)) is None

    # an external edit after injection invalidates the exemption: the
    # seeded view no longer matches the file on disk, so must-read-first
    # comes back before the model may rewrite the index
    (root / "MEMORY.md").write_text(
        "- a.md — fact\n- b.md — added outside the chat\n", encoding="utf-8",
    )
    assert must_read_first_error("sess-seed", memory_index_path(project)) is not None

    # a truncated view must NOT satisfy must-read-first: rewriting the index
    # from a partial view would silently drop the unseen tail
    (root / "MEMORY.md").write_text("y" * 40, encoding="utf-8")
    monkeypatch.setattr(settings.memory, "injection_max_chars", 10)
    truncated = await build_memory_injection(project)
    assert truncated is not None and truncated.truncated
    seed_read_state_for_injection("sess-trunc", project, truncated)
    assert must_read_first_error("sess-trunc", memory_index_path(project)) is not None


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_roundtrip_and_clamp(project):
    from app.services.memory_service import (
        clamp_cursor,
        clear_cursor,
        get_cursor,
        set_cursor,
    )

    assert await get_cursor(project, "sess1") == -1  # nothing processed yet
    await set_cursor(project, "sess1", 42)
    assert await get_cursor(project, "sess1") == 42
    await clamp_cursor(project, "sess1", 10)
    assert await get_cursor(project, "sess1") == 10
    await clamp_cursor(project, "sess1", 99)  # never raises the cursor
    assert await get_cursor(project, "sess1") == 10
    await set_cursor(project, "sess1", 0)
    assert await get_cursor(project, "sess1") == 0
    await clamp_cursor(project, "sess1", -1)  # full rewind clears it again
    assert await get_cursor(project, "sess1") == -1
    await clear_cursor(project, "sess1")
    assert await get_cursor(project, "sess1") == -1


# ---------------------------------------------------------------------------
# Counters & single-flight handoff
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_pending_slot():
    memory_service._pending.clear()
    yield
    memory_service._pending.clear()


@pytest.mark.asyncio
async def test_counters_deplete_reset_and_priority(project):
    from app.core.config import settings

    extract = settings.memory.extraction_interval
    consolidate = settings.memory.consolidation_interval

    for _ in range(extract):
        assert await memory_service.due_task(project, "s1") is None
        await memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.due_task(project, "s1") == "extract"
    # extraction windows are per session: s2 has not spent anything
    assert await memory_service.due_task(project, "s2") is None

    # a foreground memory write resets only the writing session's window
    await memory_service.note_memory_file_write(project, "s1", memory_root(project) / "fact.md")
    assert await memory_service.due_task(project, "s1") is None
    # ...and depletes consolidation, which is project-wide
    for _ in range(consolidate - 1):
        await memory_service.note_memory_file_write(project, "s1", memory_root(project) / "fact.md")
    assert await memory_service.due_task(project, "s1") == "consolidate"
    assert await memory_service.due_task(project, "s2") == "consolidate"
    # with BOTH counters due, extraction wins the priority (writes keep
    # resetting s1's window, so deplete it with main-loop calls)
    for _ in range(extract):
        await memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.due_task(project, "s1") == "extract"


@pytest.mark.asyncio
async def test_counters_persist_across_service_restart(project):
    """The counters are durable state, not in-process coordination: a fresh
    service instance (the restart surrogate) continues the spent window."""
    from app.core.config import settings
    from app.services.memory_service import MemoryService

    for _ in range(settings.memory.extraction_interval - 1):
        await memory_service.note_main_loop_llm_call(project, "s1")

    fresh = MemoryService()
    assert await fresh.due_task(project, "s1") is None  # one call short
    await fresh.note_main_loop_llm_call(project, "s1")
    assert await fresh.due_task(project, "s1") == "extract"


@pytest.mark.asyncio
async def test_index_write_skips_consolidation_but_resets_window(project):
    """Index maintenance is bookkeeping around a fact write: a MEMORY.md
    write resets the writing session's extraction window but leaves the
    consolidation counter alone; a fact-file write depletes it."""
    from app.core.config import settings
    from app.database.unit_of_work import UnitOfWork

    async def consolidate_value():
        async with UnitOfWork(project) as uow:
            return await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")

    interval = settings.memory.extraction_interval
    await memory_service.note_main_loop_llm_call(project, "s1")
    assert (await _read_extract_state(project, "s1"))["extract_remaining"] == interval - 1

    await memory_service.note_memory_file_write(
        project, "s1", (memory_root(project) / MEMORY_INDEX_NAME).resolve()
    )
    assert (await _read_extract_state(project, "s1"))["extract_remaining"] == interval
    assert await consolidate_value() == ""

    await memory_service.note_memory_file_write(
        project, "s1", memory_root(project) / "fact.md"
    )
    assert await consolidate_value() == str(settings.memory.consolidation_interval - 1)


@pytest.mark.asyncio
async def test_consolidation_counter_corrupt_value_degrades(project):
    """A corrupted counter row behaves as a fresh window instead of raising
    on every turn end forever; the next write stores a valid number again."""
    from app.core.config import settings
    from app.database.unit_of_work import UnitOfWork

    async with UnitOfWork(project) as uow:
        await uow.config.set(MEMORY_CONSOLIDATE_KEY, "not-a-number")

    assert await memory_service.due_task(project, "s1") is None

    await memory_service.note_memory_file_write(
        project, "s1", memory_root(project) / "fact.md"
    )
    async with UnitOfWork(project) as uow:
        raw = await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")
    assert raw == str(settings.memory.consolidation_interval - 1)


@pytest.mark.asyncio
async def test_concurrent_window_decrements_do_not_lose_updates(project):
    """Main-loop decrements race the background extraction's cursor/window
    writes on the same session blob. The read-modify-write must run inside
    one BEGIN IMMEDIATE transaction: under a deferred transaction the
    gathered decrements overwrite each other and the window drifts."""
    from app.core.config import settings

    await memory_service.note_main_loop_llm_call(project, "sess-race")  # warm up

    await asyncio.gather(*[
        memory_service.note_main_loop_llm_call(project, "sess-race")
        for _ in range(20)
    ])
    state = await _read_extract_state(project, "sess-race")
    assert state["extract_remaining"] == settings.memory.extraction_interval - 21


@pytest.mark.asyncio
async def test_concurrent_consolidation_decrements_do_not_lose_updates(project):
    """Foreground memory writes race the background extraction's depletion
    on the consolidation counter; the decrements must serialize instead of
    last-writer-wins."""
    from app.core.config import settings
    from app.database.unit_of_work import UnitOfWork

    fact = memory_root(project) / "fact.md"
    await memory_service.note_memory_file_write(project, "", fact)  # warm up

    await asyncio.gather(*[
        memory_service.note_memory_file_write(project, "", fact)
        for _ in range(20)
    ])
    async with UnitOfWork(project) as uow:
        raw = await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")
    assert raw == str(settings.memory.consolidation_interval - 21)


@pytest.mark.asyncio
async def test_pending_blocks_new_task_and_finish_resets(project, monkeypatch):
    enqueued = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return "task-id"

    from app.core.config import settings
    from app.services import background_task_service as bts
    from app.services.memory_service import _read_extract_state

    monkeypatch.setattr(bts.background_task_service, "enqueue", fake_enqueue)

    # Deplete first, so the assertions below can tell "blocked by pending"
    # and "counter reloaded" apart from "counter simply not due".
    for _ in range(settings.memory.extraction_interval):
        await memory_service.note_main_loop_llm_call(project, "sess-1")
    assert await memory_service.due_task(project, "sess-1") == "extract"

    assert await memory_service.start_due_task(project, "extract", session_id="sess-1") is True
    assert len(enqueued) == 1
    assert enqueued[0]["dedupe_key"].startswith("memory:")
    # the payload must carry the session the extraction window belongs to —
    # the handler silently no-ops without it
    assert enqueued[0]["payload"] == {"task": "extract", "session_id": "sess-1"}
    # the counter is due but the slot is taken: still blocked
    assert await memory_service.due_task(project, "sess-1") is None
    assert await memory_service.start_due_task(project, "consolidate") is False
    assert enqueued == [enqueued[0]]

    await memory_service.on_memory_task_finished(project, "extract", "sess-1")
    assert project not in memory_service._pending
    state = await _read_extract_state(project, "sess-1")
    assert state["extract_remaining"] == settings.memory.extraction_interval
    assert await memory_service.due_task(project, "sess-1") is None  # counter reloaded


@pytest.mark.asyncio
async def test_start_due_task_refuses_extract_without_session(project):
    """Refuse at the source instead of queueing a row the handler skips."""
    assert await memory_service.start_due_task(project, "extract") is False
    assert project not in memory_service._pending


@pytest.mark.asyncio
async def test_due_task_none_when_disabled(project, monkeypatch):
    from app.core.config import settings
    from app.services import memory_service as ms

    monkeypatch.setattr(settings.memory, "enabled", False)
    for _ in range(100):
        await ms.memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.due_task(project, "s1") is None


@pytest.mark.asyncio
async def test_finish_unknown_kind_frees_slot_without_counter_reset(project):
    """A corrupt payload frees the slot but must not reset the wrong counter."""
    from app.core.config import settings

    for _ in range(settings.memory.extraction_interval - 1):
        await memory_service.note_main_loop_llm_call(project, "s1")
    memory_service._pending[project] = ("extract", "s1")

    await memory_service.on_memory_task_finished(project, "")  # corrupt payload

    assert project not in memory_service._pending  # the slot frees…
    # …but the window was not reloaded: one more call makes it due again
    await memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.due_task(project, "s1") == "extract"


# ---------------------------------------------------------------------------
# Single-flight slot release on external cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_cancel_releases_pending_slot(project):
    """A queued memory row cancelled without the runner claiming it (the
    reset/delete flows cancel rows directly) must release the single-flight
    slot — otherwise the project's memory work silently stalls until the
    process restarts."""
    from app.core.config import settings
    from app.services.project_service import project_service

    for _ in range(settings.memory.extraction_interval):
        await memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.start_due_task(project, "extract", session_id="s1") is True
    assert memory_service._pending[project] == ("extract", "s1")

    await project_service._cancel_background_tasks(project)

    assert project not in memory_service._pending
    # the window reloaded, so a fresh depletion schedules again (the stuck
    # symptom was due_task answering None forever)
    for _ in range(settings.memory.extraction_interval):
        await memory_service.note_main_loop_llm_call(project, "s1")
    assert await memory_service.due_task(project, "s1") == "extract"
