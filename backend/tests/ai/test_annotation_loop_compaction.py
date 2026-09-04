"""Passive compaction inside AnnotationLoop must honour the cancel signal.

Drives ``AnnotationLoop._prepare_messages`` with the real
``compaction_service.compact_messages`` (only the summary LLM call is
scripted): a stop that fires while the compaction call is in flight must
abort that call quickly, leave no orphaned provider request, and must not
persist a compaction boundary for a turn nobody asked to continue.
"""

import asyncio
import contextlib
from importlib import import_module

import pytest

import_module("app.agents.tools")
import app.services.annotation_loop as annotation_loop_module
import app.services.compaction_service as compaction_service_module
from app.services.annotation_loop import AnnotationLoop
from app.services.compaction_service import ContextStats
from tests.ai.conftest import make_fake_uow


def _over_threshold_stats():
    return ContextStats(
        current_tokens=150,
        compact_threshold=100,
        max_context_length=200,
    )


def _make_uow():
    """A UnitOfWork stand-in recording boundary-write attempts without
    running the atomic operation (the cancelled path must never reach it)."""
    return make_fake_uow(run_atomic_operation=False, atomic_calls=[])


def _make_loop(cancel_event):
    return AnnotationLoop(
        project_id="project-a",
        file_path="notes.md",
        annotation_id="ann-1",
        cancel_event=cancel_event,
    )


def _script_slow_summary(monkeypatch, state, summary_delay=30.0):
    """Replace the compaction summary LLM call with a slow, cancellable call."""

    async def slow_call_chat_text(*args, **kwargs):
        try:
            await asyncio.sleep(summary_delay)
        except asyncio.CancelledError:
            state["llm_cancelled"] = True
            raise
        state["llm_finished"] = True
        return "x" * 80

    monkeypatch.setattr(
        compaction_service_module.llm_service,
        "call_chat_text",
        slow_call_chat_text,
    )


@pytest.mark.asyncio
async def test_cancel_during_compaction_aborts_call_and_skips_boundary(monkeypatch):
    """A stop that fires mid-compaction cancels the summary LLM call within
    the polling cadence (not after its natural timeout) and no boundary row
    is written."""
    monkeypatch.setattr(
        annotation_loop_module.compaction_service,
        "stats_for_messages_incremental",
        lambda *args, **kwargs: _over_threshold_stats(),
    )
    uow_cls = _make_uow()
    monkeypatch.setattr(annotation_loop_module, "UnitOfWork", uow_cls)

    state = {"llm_cancelled": False, "llm_finished": False}
    _script_slow_summary(monkeypatch, state)

    cancel_event = asyncio.Event()
    loop = _make_loop(cancel_event)

    async def stop_soon():
        await asyncio.sleep(0.15)
        cancel_event.set()

    stop_task = asyncio.create_task(stop_soon())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                loop._prepare_messages([{"role": "system", "content": "s"}]),
                timeout=5.0,
            )
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task

    assert state["llm_cancelled"]
    assert not state["llm_finished"]
    assert uow_cls.atomic_calls == []


@pytest.mark.asyncio
async def test_cancelled_turn_never_starts_compaction(monkeypatch):
    """A cancel signal already set before the compaction phase must skip the
    summary LLM call entirely."""
    monkeypatch.setattr(
        annotation_loop_module.compaction_service,
        "stats_for_messages_incremental",
        lambda *args, **kwargs: _over_threshold_stats(),
    )
    uow_cls = _make_uow()
    monkeypatch.setattr(annotation_loop_module, "UnitOfWork", uow_cls)

    state = {"llm_cancelled": False, "llm_finished": False}
    _script_slow_summary(monkeypatch, state)

    cancel_event = asyncio.Event()
    cancel_event.set()
    loop = _make_loop(cancel_event)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            loop._prepare_messages([{"role": "system", "content": "s"}]),
            timeout=5.0,
        )

    assert not state["llm_finished"]
    assert uow_cls.atomic_calls == []
