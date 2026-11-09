"""Bounded tool-using loop for background memory tasks.

A deliberately small agent kernel, separate from ``LLMLoopRunner``: memory
extraction and consolidation run headless in a background task, so they need
no SSE, no persistence, no permission pauses. What they DO need — and what
this loop provides — is:

* a hard turn budget (``max_turns``; each turn = one LLM call + its tools),
* execution-boundary tool policy: the model may see the full tool catalog
  (byte-identical requests for extraction keep the provider prefix cache
  hot), but only the allowed subset executes and everything else comes back
  as a tool error — the tool directory stays broad while the execution
  boundary stays narrow,
* memory-root containment for the mutating tools,
* cooperative cancellation via the queue's cancel event.

Errors propagate to the caller: the background queue owns retry/backoff and
terminal-failure handling; the loop must not swallow provider failures.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.tools.registry import tool_registry
from app.core.logging import get_logger
from app.core.message_format import apply_cache_control
from app.services.llm_loop_runner import LLMLoopRunner

logger = get_logger(__name__)

# Tools the extraction agent may execute: reads everywhere in the project,
# writes only inside the memory root (enforced below).
EXTRACTION_ALLOWED_TOOLS = frozenset({"read", "ls", "glob", "grep", "write", "edit"})
# The consolidation agent's surface is exactly the four tools its prompt and
# schema catalog declare (see memory_file_tools for rm).
CONSOLIDATION_ALLOWED_TOOLS = frozenset({"read", "edit", "write", "rm"})

_MEMORY_MUTATING_TOOLS = frozenset({"write", "edit", "rm"})

# Fixed no-op replies. Matched on a stripped whole-text basis so a model that
# adds surrounding whitespace still counts as a no-op, while any explanation
# around the sentinel does not.
NOTHING_TO_SAVE = "Nothing to save."
NOTHING_TO_CHANGE = "Nothing to change."


@dataclass
class MemoryAgentResult:
    turns: int
    final_text: str
    tool_error_count: int = 0
    # Resolved memory-root paths of mutating tool calls that actually landed
    # (write/edit/rm), in call order. The extraction handler feeds them to
    # the consolidation counter; consolidation's own run ignores them.
    memory_mutations: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.final_text.strip() in (NOTHING_TO_SAVE, NOTHING_TO_CHANGE)


def _mutating_target(project_id: str, tool_name: str, tool_args: dict) -> Path | None:
    """Resolved memory-root path a mutating tool targets, or ``None``."""
    if tool_name not in _MEMORY_MUTATING_TOOLS:
        return None
    from app.services.memory_service import resolve_memory_file

    raw = tool_args.get("file_path") or tool_args.get("path") or ""
    return resolve_memory_file(project_id, raw) if raw else None


def _policy_error(
    tool_name: str,
    tool_args: dict,
    *,
    allowed_tools: frozenset[str],
    project_id: str,
) -> str | None:
    """Execution-boundary policy. ``None`` = allowed."""
    if tool_name not in allowed_tools:
        return (
            f"Error: tool '{tool_name}' is denied for the memory agent "
            f"(allowed: {', '.join(sorted(allowed_tools))})."
        )
    if tool_name in _MEMORY_MUTATING_TOOLS:
        raw = tool_args.get("file_path") or tool_args.get("path") or ""
        if _mutating_target(project_id, tool_name, tool_args) is None:
            return (
                f"Error: '{raw or tool_name}' must target a path inside the "
                "project memory directory (.SiGMA/memory)."
            )
    return None


async def _call_llm(
    *,
    session_id: str,
    messages: list[dict],
    tool_schemas: list[dict],
    max_tokens: int,
    cancel_event: asyncio.Event | None,
) -> tuple[str, list[dict]]:
    """One headless LLM call via the shared streaming service.

    Stream deltas are discarded — only the final (text, tool_calls)
    matters, and a mid-stream failure surfaces as an exception here (the
    background queue owns retry policy). ``session_id`` is the sticky-routing
    key: extraction passes the chat session it replays so the byte-identical
    prefix lands on the instance holding the main loop's cache. Cancellation
    is cooperative at call boundaries — checked before the call, and outer
    cancellation propagates through the await.
    """
    from app.services.llm_service import llm_service

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError()
    # No prepare hook runs inside this loop, so this call owns the cache
    # markers: re-place them before every LLM call so the loop's growing
    # tail joins the cached prefix incrementally. target_offset=1 keeps the
    # anchor off the newest candidate (the one-off extraction prompt, later
    # the in-flight tail) — extraction's anchor then rests on the last
    # history message, the byte position the main loop's request cached.
    apply_cache_control(messages, target_offset=1)
    text, _reasoning, tool_calls, _usage = await llm_service.stream_chat(
        messages=messages,
        model_role="supervisor",
        tools=tool_schemas,
        delta_queue=asyncio.Queue(),
        max_tokens=max_tokens,
        session_id=session_id,
    )
    return text, tool_calls


async def run_memory_agent_loop(
    *,
    project_id: str,
    messages: list[dict],
    tool_schemas: list[dict],
    allowed_tools: frozenset[str],
    max_turns: int,
    max_tokens: int,
    cancel_event: asyncio.Event | None = None,
    task_label: str = "memory",
    routing_session_id: str | None = None,
) -> MemoryAgentResult:
    """Run the bounded loop; returns the final text (no-op sentinel possible).

    ``routing_session_id`` keys the LLM calls' sticky routing — extraction
    passes the replayed chat session for provider-cache hits; ``None`` falls
    back to a per-task synthetic id (consolidation's fresh session). The
    synthetic session also stays the tools' session id either way, because
    read-state (must-read-first) is tracked per background run.
    """
    synthetic_session = f"{task_label}:{project_id}"
    routing_session = routing_session_id or synthetic_session
    working = [dict(m) for m in messages]
    tool_error_count = 0
    mutations: list[str] = []

    for turn in range(1, max_turns + 1):
        text, tool_calls = await _call_llm(
            session_id=routing_session,
            messages=working,
            tool_schemas=tool_schemas,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
        )
        if not tool_calls:
            working.append(LLMLoopRunner.msg("assistant", text))
            logger.info(
                "Memory agent (%s/%s) finished after %d turn(s)", task_label, project_id, turn,
            )
            return MemoryAgentResult(
                turns=turn, final_text=text,
                tool_error_count=tool_error_count, memory_mutations=mutations,
            )

        working.append(LLMLoopRunner.msg("assistant", text, tool_calls=tool_calls))
        for tc in tool_calls:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError()
            tool_name = tc.get("name", "")
            try:
                tool_args = tc.get("params") or {}
                if isinstance(tool_args, str):
                    tool_args = json.loads(tool_args or "{}")
            except ValueError:
                tool_args = {}
            tool_call_id = tc.get("id", "")

            error = _policy_error(
                tool_name, tool_args,
                allowed_tools=allowed_tools,
                project_id=project_id,
            )
            if error:
                tool_result = error
                tool_error_count += 1
            else:
                tool_def = tool_registry.get(tool_name)
                if tool_def is None:
                    tool_result = f"Error: Unknown tool '{tool_name}'"
                    tool_error_count += 1
                else:
                    try:
                        injected = {
                            **tool_args,
                            "project_id": project_id,
                            "session_id": synthetic_session,
                        }
                        tool_result = str(await LLMLoopRunner.call_tool(tool_def, injected))
                        if (
                            tool_name in _MEMORY_MUTATING_TOOLS
                            and not tool_result.startswith("Error")
                        ):
                            target = _mutating_target(project_id, tool_name, tool_args)
                            if target is not None:
                                mutations.append(str(target))
                    except Exception as exc:
                        tool_result = f"Error: {exc}"
                        tool_error_count += 1
            logger.debug(
                "Memory agent (%s/%s) tool %s -> %s",
                task_label, project_id, tool_name, str(tool_result)[:120],
            )
            working.append(LLMLoopRunner.msg("tool", tool_result, tool_call_id=tool_call_id))

    # Turn budget exhausted: whatever was written so far stands (writes are
    # already durable); the final partial text is returned as-is.
    logger.warning(
        "Memory agent (%s/%s) hit the %d-turn budget", task_label, project_id, max_turns,
    )
    return MemoryAgentResult(
        turns=max_turns, final_text=text,
        tool_error_count=tool_error_count, memory_mutations=mutations,
    )
