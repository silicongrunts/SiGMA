"""
LLM Loop Runner — shared loop kernel for QueryLoop, AnnotationLoop, and subagents.

Shared while-loop logic (LLM streaming + tool execution + SSE emission) used by
all chat-style loops.  Callers provide a LoopContext that configures which tools
are available, how to persist messages, and optional hooks for compaction,
permission checks, and response validation.
"""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Awaitable, Any

from app.core.chat_attachments import render_image_refs_tag, strip_image_refs_tag
from app.core.logging import get_logger
from app.core.task_status import SSE_CANCELLED, SSE_DONE, SSE_ERROR
from app.agents.tools.registry import tool_registry
from app.services.token_budget import TokenBudgetTracker, TokenBudgetExceeded, extract_llm_usage

logger = get_logger(__name__)

MAX_TOOL_OUTPUT_CHARS = 50_000
MAX_CONSECUTIVE_TOOL_ERRORS = 3
AGENT_TOOL_ERROR_PREFIX = "Agent error:"
# SSE event types (shared by all loop consumers). The terminal event names
# (SSE_CANCELLED/SSE_DONE/SSE_ERROR) are imported at the top of this module
# from app.core.task_status — they are the wire contract with the frontend and
# must not drift from the stream relay's emission/matching.
SSE_DELTA = "delta"
SSE_TOOL_START = "tool_start"
SSE_TOOL_END = "tool_end"
SSE_THOUGHT = "thought"
SSE_FILE_CHANGED = "file_changed"
SSE_ANNOTATION_CHANGED = "annotation_changed"
SSE_TASK_LIST = "task_list"
SSE_AWAITING_INPUT = "awaiting_input"
SSE_AGENT_EVENT = "agent_event"
SSE_CONTEXT_STATS = "context_stats"
SSE_COMPACT_START = "compact_start"
SSE_COMPACT_DONE = "compact_done"


class InteractiveToolPause(Exception):
    """Raised when a subagent encounters an interactive tool and cannot pause.

    Propagates to the parent loop, which saves its own checkpoint and
    pauses on behalf of the subagent.  Carries all state needed for resume.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        interaction_data: dict,
        agent_session_id: str = "",
        agent_type: str = "",
        parent_tool_call_id: str = "",
        agent_usage_baseline: dict | None = None,
    ):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self.interaction_data = interaction_data
        self.agent_session_id = agent_session_id
        self.agent_type = agent_type
        self.parent_tool_call_id = parent_tool_call_id
        self.agent_usage_baseline = agent_usage_baseline
        super().__init__(
            f"Interactive tool '{tool_name}' requires user input in subagent"
        )


@dataclass
class LoopContext:
    """Configuration for one run of the LLM loop."""

    project_id: str
    session_id: str | None = None          # None for non-persisted runs
    model_role: str = "supervisor"          # "supervisor" or "ra"
    response_max_tokens: int = 32_000
    tool_schemas: list[dict] = field(default_factory=list)
    allowed_tools: frozenset[str] | None = None    # None = all allowed
    forbidden_tools: frozenset[str] = field(default_factory=frozenset)
    forbidden_tool_context: str = ""
    cancel_event: asyncio.Event | None = None
    is_agent_tool: bool = False  # True when running a subagent via agent tool

    # Semantic label for the loop's position in the agent topology.
    #   "main"       - top-level user-facing loop (query_loop)
    #   "annotation" - annotation-thread reply loop
    #   "plan"       - plan-approval subagent loop
    # Callers set this explicitly so the kernel does not need to infer it
    # by comparing ``allowed_tools`` against specific toolset constants.
    context_kind: str = "main"

    # Task ID for checkpoint persistence (interactive tools).
    # Empty string means no checkpoint will be saved — the runner will
    # raise InteractiveToolPause instead.
    task_id: str = ""

    # ── Hooks (optional callbacks) ──

    # execute_tool: if set, replaces default tool execution.
    #   Signature: async (tool_name, tool_args) -> str
    execute_tool: Callable[..., Awaitable[str]] | None = None

    # persist_messages: save new messages. Signature: async (messages) -> None
    persist_messages: Callable[..., Awaitable[None]] | None = None

    # prepare_messages: called before each LLM call and may replace the
    # in-memory context, for example after compaction.  It returns
    # (messages, events_to_emit).
    prepare_messages: Callable[..., Awaitable[tuple[list[dict], list[dict]]]] | None = None

    # validate_final_response: check LLM text before finalizing (e.g. diff validation).
    #   Returns an error string to feed back, or None if OK.
    validate_final_response: Callable[[str], Awaitable[str | None]] | None = None

    # on_interactive_pause: called when an interactive tool is about to pause.
    #   The hook is responsible for saving the interaction checkpoint
    #   (typically via uow.task_state.mark_awaiting_input).  If not provided,
    #   the runner raises InteractiveToolPause (used by subagents).
    #   Signature: async (tool_name, tool_args, tool_call_id, interaction_data) -> None
    on_interactive_pause: Callable[..., Awaitable[None]] | None = None

    # on_permission_pause: called when a write/bash/notebook tool needs user
    #   approval. Same checkpoint semantics as on_interactive_pause. If not
    #   provided, the runner raises PermissionRequestPause (used by subagents).
    #   Signature: async (tool_name, tool_args, tool_call_id, interaction_data) -> None
    on_permission_pause: Callable[..., Awaitable[None]] | None = None

    # Callbacks for fetching data after tool execution
    get_active_tasks: Callable[[str], Awaitable[list]] | None = None

    # Shared across a main loop and all nested agents for this user turn.
    token_budget_tracker: TokenBudgetTracker | None = None

    # Real input tokens from the last LLM call.  When non-zero, the
    # prepare_messages callback can use incremental estimation instead of
    # full tiktoken.  Reset to 0 after compaction replaces the message list.
    last_real_input_tokens: int = 0
    last_real_count_at_index: int = 0


class LLMLoopRunner:
    """Shared LLM loop kernel.  One instance can be reused for multiple runs."""

    async def run(self, ctx: LoopContext, messages: list[dict]) -> AsyncIterator[dict]:
        """Run the LLM ↔ tool loop.  Yields SSE event dicts.

        Modifies `messages` in place (appends assistant + tool messages).
        On completion, the last assistant message's content is available as
        LoopResult.final_text.
        """
        accumulated_input = 0
        accumulated_output = 0
        accumulated_cached = 0
        aborted = False
        exit_error = None
        error_message_persisted = False
        consecutive_tool_errors = 0
        tool_error_stop_message = ""

        async def _emit_cancelled() -> AsyncIterator[dict]:
            turn_usage = self._build_turn_usage(
                ctx, accumulated_input, accumulated_output, accumulated_cached,
            )
            if ctx.persist_messages:
                await ctx.persist_messages(messages)
            cancel_data = {"message": "Task cancelled by user"}
            if turn_usage:
                cancel_data["usage"] = self._public_usage(turn_usage)
            yield self.sse(SSE_CANCELLED, cancel_data)

        while not aborted:
            # Check cancellation — distinguish budget exceeded from user cancel.
            if ctx.cancel_event and ctx.cancel_event.is_set():
                if ctx.token_budget_tracker and ctx.token_budget_tracker.exceeded:
                    # Budget exceeded (possibly by a subagent): emit as final
                    # message content so the user sees the budget summary, then
                    # fall through to the done event and normal persistence.
                    budget_msg = ctx.token_budget_tracker.status_message()
                    yield self.sse(SSE_DELTA, {"content": "\n\n" + budget_msg})
                    messages.append(self.msg("assistant", budget_msg))
                    break
                async for evt in _emit_cancelled():
                    yield evt
                return

            if ctx.prepare_messages:
                prepared, events = await ctx.prepare_messages(messages)
                messages[:] = prepared
                for event in events:
                    yield event

            # Stream LLM response
            delta_queue: asyncio.Queue = asyncio.Queue()

            async def _stream_to_queue():
                try:
                    text, reasoning, tc, usage = await self._stream_llm(
                        ctx, messages, delta_queue,
                    )
                    await delta_queue.put(("__result__", (text, reasoning, tc, usage)))
                except Exception as exc:
                    logger.exception("LLM stream task failed")
                    await delta_queue.put(("__error__", exc))

            llm_task = asyncio.create_task(_stream_to_queue())

            text_content = ""
            reasoning_content = ""
            tool_calls = []
            llm_usage = None

            while True:
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    llm_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await llm_task
                    async for evt in _emit_cancelled():
                        yield evt
                    return

                try:
                    item_type, item_data = await asyncio.wait_for(
                        delta_queue.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                if item_type == "__result__":
                    text_content, reasoning_content, tool_calls, llm_usage = item_data
                    break
                elif item_type == "__error__":
                    await llm_task
                    exit_error = item_data
                    break
                elif item_type == "delta":
                    evt = self.sse(SSE_DELTA, {"content": item_data})
                    yield evt
                elif item_type == "reasoning_delta":
                    evt = self.sse(SSE_THOUGHT, {"content": item_data})
                    yield evt
                elif item_type == "stream_status":
                    evt = self.sse("stream_status", item_data)
                    yield evt

            await llm_task

            # LLM error during streaming — break outer loop for finalization.
            if exit_error:
                break

            # Accumulate usage
            if llm_usage:
                tu = extract_llm_usage(llm_usage)
                accumulated_input += tu.input
                accumulated_output += tu.output
                accumulated_cached += tu.cached
                if ctx.token_budget_tracker:
                    ctx.token_budget_tracker.add_llm_usage(llm_usage)
                # Store real input tokens for incremental context estimation.
                # messages[] has not been mutated yet (assistant append is below),
                # so len(messages) matches what was actually sent to the LLM.
                if tu.input:
                    ctx.last_real_input_tokens = tu.input
                    ctx.last_real_count_at_index = len(messages)

            # Emit progressive usage after each LLM call so the UI can
            # display running totals during multi-step tool loops.
            progressive_usage = self._build_turn_usage(
                ctx, accumulated_input, accumulated_output, accumulated_cached,
                for_realtime=True,
            )
            if progressive_usage:
                yield self.sse("turn_usage", {"usage": self._public_usage(progressive_usage)})

            budget_exceeded = False
            if ctx.token_budget_tracker:
                try:
                    ctx.token_budget_tracker.ensure_within_budget()
                except TokenBudgetExceeded:
                    budget_exceeded = True
                    if ctx.cancel_event:
                        ctx.cancel_event.set()
                    text_content = ctx.token_budget_tracker.status_message()
                    tool_calls = []
                    yield self.sse(SSE_DELTA, {"content": text_content})

            # Append assistant message
            if text_content or tool_calls or reasoning_content:
                extra = {}
                if llm_usage:
                    tu = extract_llm_usage(llm_usage)
                    extra["_input_tokens"] = tu.input
                    extra["_completion_tokens"] = tu.output
                    extra["_cached_tokens"] = tu.cached
                messages.append(self.msg(
                    "assistant", text_content or "", tool_calls,
                    reasoning_content=reasoning_content, **extra,
                ))

            if budget_exceeded:
                break

            # No tool calls → validate and finalize
            if not tool_calls:
                if ctx.validate_final_response:
                    validation_error = await ctx.validate_final_response(text_content)
                    if validation_error:
                        # Inject synthetic tool call for retry
                        synthetic_call = {
                            "id": "_diff_validate",
                            "type": "function",
                            "function": {"name": "_diff_validate", "arguments": "{}"},
                        }
                        messages[-1]["tool_calls"] = [synthetic_call]
                        evt_start = self.sse(SSE_TOOL_START, {
                            "tool": "_diff_validate", "params": "{}"
                        })
                        yield evt_start
                        evt_end = self.sse(SSE_TOOL_END, {
                            "tool": "_diff_validate",
                            "result_summary": validation_error[:200],
                        })
                        yield evt_end
                        messages.append(self.msg(
                            "tool", validation_error, tool_call_id="_diff_validate"
                        ))
                        if ctx.persist_messages:
                            await ctx.persist_messages(messages)
                        continue  # let LLM retry
                break

            # Execute tool calls
            pending_image_messages: list[dict] = []
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("params", {})
                tool_call_id = tc.get("id", "")

                tool_def = tool_registry.get(tool_name)

                # Inject project_id / session_id for tools that need them
                if tool_def:
                    if tool_def.requires_project_id:
                        tool_args["project_id"] = ctx.project_id
                    if tool_def.requires_session_id and ctx.session_id:
                        tool_args["session_id"] = ctx.session_id
                    if tool_def.requires_model_role:
                        tool_args["model_role"] = ctx.model_role

                # Runtime whitelist enforcement
                if ctx.allowed_tools is not None and tool_name not in ctx.allowed_tools:
                    tool_result = (
                        f"Error: Tool '{tool_name}' is not available in this context"
                    )
                    evt = self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result[:200],
                        "tool_call_id": tool_call_id,
                    })
                    yield evt
                    messages.append(self.msg("tool", tool_result, tool_call_id=tool_call_id))
                    continue

                # Runtime forbidden list enforcement
                if tool_name in ctx.forbidden_tools:
                    tool_result = self._forbidden_tool_result(tool_name, ctx)
                    evt = self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result[:200],
                        "tool_call_id": tool_call_id,
                    })
                    yield evt
                    messages.append(self.msg("tool", tool_result, tool_call_id=tool_call_id))
                    continue

                # Emit tool_start with tool_call_id
                evt_start = self.sse(SSE_TOOL_START, {
                    "tool": tool_name,
                    "params": self._safe_params(tool_args),
                    "tool_call_id": tool_call_id,
                })
                yield evt_start

                # Interactive tool — build interaction data. On success it
                # pauses for user input; on a validation/call error the error
                # is fed back to the LLM as a tool result (like normal tools)
                # so the LLM can retry — invalid input never reaches the user
                # as a broken modal.
                if tool_def and tool_def.requires_user_interaction:
                    tool_error = None
                    try:
                        interaction_data = await self.call_tool(tool_def, tool_args)
                    except Exception as exc:
                        tool_error = f"Tool '{tool_name}' error: {exc}"
                        interaction_data = None

                    # A valid pause request is a dict carrying interaction_type.
                    # Anything else (error string, call exception, or a dict
                    # without interaction_type) is a tool error → feed it back
                    # to the LLM as a tool result, do not pause.
                    if tool_error or not (
                        isinstance(interaction_data, dict)
                        and interaction_data.get("interaction_type")
                    ):
                        if tool_error is None:
                            if isinstance(interaction_data, dict):
                                tool_error = (
                                    "Error: interactive tool returned no "
                                    f"interaction_type. Payload: "
                                    f"{str(interaction_data)[:200]}"
                                )
                            else:
                                tool_error = (
                                    str(interaction_data).strip()
                                    or "Error: empty tool result"
                                )
                        yield self.sse(SSE_TOOL_END, {
                            "tool": tool_name,
                            "result_summary": tool_error[:200],
                            "tool_call_id": tool_call_id,
                        })
                        messages.append(self.msg(
                            "tool", tool_error, tool_call_id=tool_call_id,
                        ))
                        continue  # round-persist + next LLM turn handle the rest
                    turn_usage = self._build_turn_usage(
                        ctx, accumulated_input, accumulated_output, accumulated_cached,
                    )
                    # If cancel landed while the interactive tool was preparing
                    # its prompt, honor it: record a cancelled tool result (this
                    # keeps tool_call/result pairing intact) and let the outer
                    # loop emit the cancelled event instead of parking for input.
                    if ctx.cancel_event and ctx.cancel_event.is_set():
                        messages.append(self.msg(
                            "tool", "Tool cancelled by user.",
                            tool_call_id=tool_call_id,
                        ))
                        break
                    if ctx.persist_messages:
                        await ctx.persist_messages(messages)

                    # Persist interaction checkpoint or raise to parent.
                    # Messages already carry per-message token accounting, so
                    # the checkpoint callback needs no usage payload.
                    if ctx.on_interactive_pause:
                        await ctx.on_interactive_pause(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            tool_call_id=tool_call_id,
                            interaction_data=interaction_data,
                        )
                    elif not ctx.task_id:
                        # Subagent without on_interactive_pause — propagate up
                        raise InteractiveToolPause(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            tool_call_id=tool_call_id,
                            interaction_data=interaction_data,
                        )
                    else:
                        # Fallback: persist checkpoint directly via UnitOfWork
                        from app.database.unit_of_work import UnitOfWork
                        async with UnitOfWork(ctx.project_id) as uow:
                            await uow.task_state.mark_awaiting_input(
                                ctx.task_id, {
                                    "tool_name": tool_name,
                                    "tool_args": tool_args,
                                    "tool_call_id": tool_call_id,
                                    "interaction_data": interaction_data,
                                }
                            )

                    evt_input = self.sse(SSE_AWAITING_INPUT, interaction_data)
                    yield evt_input
                    done_data = {}
                    if turn_usage:
                        done_data["usage"] = self._public_usage(turn_usage)
                    yield self.sse(SSE_DONE, done_data)
                    return  # loop pauses

                # agent tool — run via queue-based event forwarding
                if tool_def and getattr(tool_def, 'is_agent_tool', False):
                    try:
                        async for evt in self._run_agent_tool(
                            ctx, tool_name, tool_args, tool_call_id, messages
                        ):
                            yield evt
                    except InteractiveToolPause:
                        raise
                    except Exception as exc:
                        # PermissionRequestPause propagates up so the parent
                        # loop (or QueryLoop) can checkpoint on behalf of the
                        # subagent. All other exceptions take the error path.
                        if (
                            type(exc).__name__ == "PermissionRequestPause"
                            and hasattr(exc, "tool")
                        ):
                            raise
                        exit_error = exc
                        aborted = True
                        break
                    agent_tool_message = self._last_tool_message(
                        messages, tool_call_id,
                    )
                    if agent_tool_message.startswith(AGENT_TOOL_ERROR_PREFIX):
                        consecutive_tool_errors += 1
                        if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                            tool_error_stop_message = (
                                "Model produced invalid tool calls "
                                f"{consecutive_tool_errors} consecutive times. "
                                f"Last tool error: {agent_tool_message}"
                            )
                    else:
                        consecutive_tool_errors = 0
                        tool_error_stop_message = ""
                    continue

                # Execute the tool (via hook or default)
                try:
                    if ctx.execute_tool:
                        tool_result = await ctx.execute_tool(tool_name, tool_args)
                    else:
                        tool_result = await self.execute_tool_default(tool_name, tool_args)
                except Exception as exc:
                    # PermissionRequestPause is raised by execute_with_permission
                    # when a write/bash/notebook tool needs user approval. Handle
                    # it like an interactive pause: persist a checkpoint, emit
                    # awaiting_input, and end this run. The user's response arrives
                    # via the resume path.
                    _is_perm_pause = (
                        type(exc).__name__ == "PermissionRequestPause"
                        and hasattr(exc, "tool")
                    )
                    if _is_perm_pause:
                        pause = exc  # PermissionRequestPause instance
                        # Stamp the full tool args / call id so the checkpoint
                        # can re-execute the tool on resume.
                        pause.tool_args = tool_args
                        pause.inner_tool_call_id = tool_call_id
                        turn_usage = self._build_turn_usage(
                            ctx, accumulated_input, accumulated_output,
                            accumulated_cached,
                        )
                        # If cancel landed before we could pause, record a
                        # cancelled tool result and let the outer loop emit the
                        # cancelled event instead of parking.
                        if ctx.cancel_event and ctx.cancel_event.is_set():
                            messages.append(self.msg(
                                "tool", "Tool cancelled by user.",
                                tool_call_id=tool_call_id,
                            ))
                            break
                        if ctx.persist_messages:
                            await ctx.persist_messages(messages)

                        interaction_data = {
                            "interaction_type": "permission",
                            "tool": pause.tool,
                            "tool_name": pause.tool_name,
                            "path": pause.path,
                            "operation": pause.operation,
                            "content": pause.content,
                            "description": pause.description,
                        }
                        if ctx.on_permission_pause:
                            await ctx.on_permission_pause(
                                tool_name=tool_name,
                                tool_args=tool_args,
                                tool_call_id=tool_call_id,
                                interaction_data=interaction_data,
                            )
                        elif not ctx.task_id:
                            # Subagent without on_permission_pause — propagate up
                            # to the parent loop, which will checkpoint on behalf.
                            raise
                        else:
                            # Fallback: persist checkpoint directly
                            from app.database.unit_of_work import UnitOfWork
                            async with UnitOfWork(ctx.project_id) as uow:
                                await uow.task_state.mark_awaiting_input(
                                    ctx.task_id, {
                                        "tool_name": tool_name,
                                        "tool_args": tool_args,
                                        "tool_call_id": tool_call_id,
                                        "interaction_data": interaction_data,
                                    }
                                )

                        yield self.sse(SSE_AWAITING_INPUT, interaction_data)
                        done_data = {}
                        if turn_usage:
                            done_data["usage"] = self._public_usage(turn_usage)
                        yield self.sse(SSE_DONE, done_data)
                        return  # loop pauses

                    tool_result = f"Tool '{tool_name}' error: {exc}"
                    yield self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result[:200],
                        "tool_call_id": tool_call_id,
                    })
                    messages.append(self.msg(
                        "tool", tool_result, tool_call_id=tool_call_id,
                    ))
                    consecutive_tool_errors += 1
                    if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                        tool_error_stop_message = (
                            "Model produced invalid tool calls "
                            f"{consecutive_tool_errors} consecutive times. "
                            f"Last tool error: {tool_result}"
                        )
                    continue

                consecutive_tool_errors = 0
                tool_error_stop_message = ""

                tool_result, image_messages = self._normalize_tool_result(tool_result)

                # Truncate very long outputs
                if len(tool_result) > MAX_TOOL_OUTPUT_CHARS:
                    tool_result = tool_result[:MAX_TOOL_OUTPUT_CHARS] + "\n... [truncated]"

                # Emit tool_end with tool_call_id
                evt_end = self.sse(SSE_TOOL_END, {
                    "tool": tool_name,
                    "result_summary": strip_image_refs_tag(tool_result)[:200],
                    "tool_call_id": tool_call_id,
                })
                yield evt_end

                # Side-effect events (file_changed, annotation_changed, task_list)
                fc_evt = self._emit_file_changed(tool_name, tool_args, tool_result)
                if fc_evt:
                    yield fc_evt

                ac_evt = self._emit_annotation_changed(tool_name, tool_args)
                if ac_evt:
                    yield ac_evt

                if tool_name in {"task_create", "task_update", "task_list", "task_get", "task_write"}:
                    tl_evt = await self._fetch_task_list(ctx)
                    if tl_evt:
                        yield tl_evt

                # Append tool result to messages
                messages.append(self.msg("tool", tool_result, tool_call_id=tool_call_id))
                pending_image_messages.extend(image_messages)

            messages.extend(pending_image_messages)

            # Persist every completed tool round
            if ctx.persist_messages:
                await ctx.persist_messages(messages)

            if tool_error_stop_message:
                exit_error = RuntimeError(tool_error_stop_message)
                aborted = True

            if aborted:
                break

        # Final persistence. Persistors store per-message provider usage; the
        # turn usage is kept for SSE compatibility and cancellation reporting.
        turn_usage = self._build_turn_usage(
            ctx, accumulated_input, accumulated_output, accumulated_cached,
        )

        if exit_error:
            error_text = self._format_error_message(exit_error)
            messages.append(self.msg("assistant", error_text))
            error_message_persisted = True

        if ctx.persist_messages:
            await ctx.persist_messages(messages)

        done_data = {}
        if turn_usage:
            done_data["usage"] = self._public_usage(turn_usage)

        if exit_error:
            error_data = {
                "error": str(exit_error),
                "content": messages[-1].get("content", "") if error_message_persisted else "",
            }
            if turn_usage:
                error_data["usage"] = self._public_usage(turn_usage)
            yield self.sse(SSE_ERROR, error_data)
            return

        yield self.sse(SSE_DONE, done_data)

    @staticmethod
    def _build_turn_usage(
        ctx: LoopContext,
        accumulated_input: int,
        accumulated_output: int,
        accumulated_cached: int,
        for_realtime: bool = False,
    ) -> dict | None:
        if ctx.token_budget_tracker and (for_realtime or not ctx.is_agent_tool):
            tracked = ctx.token_budget_tracker.usage
            usage = {
                "input": tracked.input,
                "output": tracked.output,
                "cached": tracked.cached,
            }
        elif accumulated_input or accumulated_output or accumulated_cached:
            usage = {
                "input": accumulated_input,
                "output": accumulated_output,
                "cached": accumulated_cached,
            }
        else:
            return None
        return usage

    @staticmethod
    def _public_usage(turn_usage: dict) -> dict:
        return {
            "input": turn_usage.get("input", 0),
            "output": turn_usage.get("output", 0),
            "cached": turn_usage.get("cached", 0),
        }

    @staticmethod
    def tracker_usage_snapshot(ctx: LoopContext) -> dict[str, int]:
        if not ctx.token_budget_tracker:
            return {"input": 0, "output": 0, "cached": 0}
        usage = ctx.token_budget_tracker.usage
        return {"input": usage.input, "output": usage.output, "cached": usage.cached}

    @staticmethod
    def _tracker_usage_delta(ctx: LoopContext, before: dict[str, int]) -> dict[str, int]:
        after = LLMLoopRunner.tracker_usage_snapshot(ctx)
        return {
            "input": max(0, after["input"] - int(before.get("input") or 0)),
            "output": max(0, after["output"] - int(before.get("output") or 0)),
            "cached": max(0, after["cached"] - int(before.get("cached") or 0)),
        }

    @staticmethod
    def usage_extra(usage: dict[str, int] | None) -> dict:
        if not usage:
            return {}
        return {
            "_input_tokens": int(usage.get("input") or 0),
            "_completion_tokens": int(usage.get("output") or 0),
            "_cached_tokens": int(usage.get("cached") or 0),
        }

    # ------------------------------------------------------------------
    # agent tool execution with queue-based event forwarding
    # ------------------------------------------------------------------

    async def _run_agent_tool(
        self,
        ctx: LoopContext,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        messages: list[dict],
    ) -> AsyncIterator[dict]:
        """Run an agent tool, forwarding subagent SSE events via a queue."""
        from app.agents.tools.agent_tool import agent_exec_context

        # Create a queue for the agent tool's subagent events
        event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        usage_before = self.tracker_usage_snapshot(ctx)

        # Context kind is set explicitly by whoever constructs the LoopContext.
        context_kind = ctx.context_kind

        async def _emit_event(event: dict) -> None:
            """Forward subagent events into our queue."""
            await event_queue.put(event)

        inherited_messages = self._messages_before_tool_call(messages, tool_call_id)

        # Set the agent execution context (used by agent_tool.py).  The
        # background task inherits this contextvars snapshot; the current task
        # resets it in finally below.
        context_token = agent_exec_context.set({
            "tool_call_id": tool_call_id,
            "messages": inherited_messages,  # clean snapshot for fork mode
            "emit_event": _emit_event,
            "cancel_event": ctx.cancel_event,
            "context_kind": context_kind,
            "token_budget_tracker": ctx.token_budget_tracker,
            "model_role": ctx.model_role,
            "response_max_tokens": ctx.response_max_tokens,
            "tool_schemas": ctx.tool_schemas,
        })

        # Execute the agent tool in a background task
        async def _execute():
            try:
                if ctx.execute_tool:
                    result = await ctx.execute_tool(tool_name, tool_args)
                else:
                    result = await self.execute_tool_default(tool_name, tool_args)
                await event_queue.put({"__agent_result__": result})
            except InteractiveToolPause as e:
                await event_queue.put({"__interactive_pause__": e})
            except Exception as e:
                if type(e).__name__ == "PermissionRequestPause" and hasattr(e, "tool"):
                    await event_queue.put({"__interactive_pause__": e})
                else:
                    logger.exception("agent tool execution failed for %s", tool_name)
                    await event_queue.put({"__agent_error__": str(e)})

        agent_task = asyncio.create_task(_execute())

        # Consume events from the queue and yield as agent_event SSE
        try:
            while True:
                if ctx.cancel_event and ctx.cancel_event.is_set():
                    agent_task.cancel()
                    is_budget = ctx.token_budget_tracker and ctx.token_budget_tracker.exceeded
                    tool_result = (
                        "Agent stopped: token budget exceeded."
                        if is_budget
                        else "Agent cancelled by user."
                    )
                    yield self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result,
                        "tool_call_id": tool_call_id,
                    })
                    messages.append(
                        self.msg(
                            "tool", tool_result, tool_call_id=tool_call_id,
                            **self.usage_extra(self._tracker_usage_delta(ctx, usage_before)),
                        )
                    )
                    return

                try:
                    item = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if "__agent_result__" in item:
                    # Agent finished — emit tool_end and append result
                    tool_result = item["__agent_result__"]
                    if len(tool_result) > MAX_TOOL_OUTPUT_CHARS:
                        tool_result = tool_result[:MAX_TOOL_OUTPUT_CHARS] + "\n... [truncated]"

                    evt_end = self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result[:200],
                        "tool_call_id": tool_call_id,
                    })
                    yield evt_end

                    # Side-effect events for agent tool (mainly file_changed)
                    fc_evt = self._emit_file_changed(tool_name, tool_args, tool_result)
                    if fc_evt:
                        yield fc_evt

                    messages.append(
                        self.msg(
                            "tool", tool_result, tool_call_id=tool_call_id,
                            **self.usage_extra(self._tracker_usage_delta(ctx, usage_before)),
                        )
                    )
                    break

                elif "__interactive_pause__" in item:
                    # Subagent hit an interactive tool or permission gate —
                    # enrich with parent tool_call_id and re-raise so the
                    # parent loop can checkpoint on behalf of the subagent.
                    e = item["__interactive_pause__"]
                    e.parent_tool_call_id = tool_call_id
                    await agent_task
                    raise e

                elif "__agent_error__" in item:
                    tool_result = f"{AGENT_TOOL_ERROR_PREFIX} {item['__agent_error__']}"
                    evt_end = self.sse(SSE_TOOL_END, {
                        "tool": tool_name,
                        "result_summary": tool_result[:200],
                        "tool_call_id": tool_call_id,
                    })
                    yield evt_end
                    messages.append(
                        self.msg(
                            "tool", tool_result, tool_call_id=tool_call_id,
                            **self.usage_extra(self._tracker_usage_delta(ctx, usage_before)),
                        )
                    )
                    return

                else:
                    # Regular subagent SSE event — wrap in agent_event envelope
                    inner_type = item.get("type", "")
                    inner_data = item.get("data", {})

                    # Forward inner awaiting_input as top-level awaiting_input
                    # so the frontend shows the interaction dialog
                    if inner_type == SSE_AWAITING_INPUT:
                        yield self.sse(SSE_AGENT_EVENT, {
                            "parent_tool_call_id": tool_call_id,
                            "agent_type": tool_args.get("agent_type", ""),
                            "inner_type": inner_type,
                            "inner_data": inner_data,
                        })
                        yield self.sse(SSE_AWAITING_INPUT, inner_data)
                    elif inner_type == SSE_DONE:
                        # Don't forward subagent's done event — it's internal
                        pass
                    elif inner_type == "turn_usage":
                        # Forward progressive usage as top-level event so the
                        # frontend's existing handler updates the message in
                        # real time during subagent execution.
                        yield self.sse(SSE_AGENT_EVENT, {
                            "parent_tool_call_id": tool_call_id,
                            "agent_type": tool_args.get("agent_type", ""),
                            "inner_type": inner_type,
                            "inner_data": inner_data,
                        })
                        yield self.sse("turn_usage", inner_data)
                    else:
                        yield self.sse(SSE_AGENT_EVENT, {
                            "parent_tool_call_id": tool_call_id,
                            "agent_type": tool_args.get("agent_type", ""),
                            "inner_type": inner_type,
                            "inner_data": inner_data,
                        })
        finally:
            agent_exec_context.reset(context_token)
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except asyncio.CancelledError:
                    pass  # cleanup: agent task cancellation
                except Exception:
                    logger.debug("Agent task cleanup raised non-cancelled error", exc_info=True)

    @staticmethod
    def _messages_before_tool_call(
        messages: list[dict], tool_call_id: str,
    ) -> list[dict]:
        """Return a valid prefix before the assistant turn owning tool_call_id.

        Fork agents inherit the parent conversation, but the agent tool is
        invoked while its owning assistant message is still incomplete from the
        chat API's perspective: that assistant message has tool_calls and the
        current agent tool result has not been appended yet.  Sending that
        partial turn to a forked subagent produces a 400 from OpenAI-compatible
        APIs.  The clean prefix is everything before the assistant message that
        requested the current agent tool.
        """
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                if tc.get("id") == tool_call_id:
                    return list(messages[:idx])
        return list(messages)

    @staticmethod
    def _forbidden_tool_result(tool_name: str, ctx: LoopContext) -> str:
        if ctx.forbidden_tool_context == "fork":
            return (
                f"Error: Tool '{tool_name}' cannot be used in fork mode. "
                "You are running a bounded fork subtask; do not launch "
                "subagents or modify parent task state from here. Complete the "
                "assigned subtask with the other available tools, then return "
                "a complete final assistant message for the parent agent."
            )
        return f"Error: Tool '{tool_name}' is forbidden in this context"

    @staticmethod
    def _last_tool_message(messages: list[dict], tool_call_id: str) -> str:
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            if message.get("tool_call_id") == tool_call_id:
                return str(message.get("content") or "")
        return ""

    # ------------------------------------------------------------------
    # Side-effect event helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _emit_file_changed(tool_name: str, tool_args: dict, tool_result: str = "") -> dict | None:
        """Return file_changed SSE event if applicable."""
        if tool_result.startswith("Error:"):
            return None
        if tool_name == "notebook_run_cell" and tool_args.get("interrupt"):
            return None
        if tool_name in {"write", "edit", "bash", "notebook_edit", "notebook_run_cell", "agent"}:
            paths = []
            if tool_name in ("write", "edit"):
                p = tool_args.get("file_path") or tool_args.get("path") or ""
                if p:
                    paths.append(p)
            elif tool_name in ("notebook_edit", "notebook_run_cell"):
                p = tool_args.get("notebook_path") or ""
                if p:
                    paths.append(p)
            return LLMLoopRunner.sse(SSE_FILE_CHANGED, {"paths": paths})
        return None

    def _emit_annotation_changed(self, tool_name: str, tool_args: dict) -> dict | None:
        """Return annotation_changed SSE event if applicable."""
        if tool_name in {"annotation_new", "annotation_rm", "annotation_reply"}:
            return self.sse(SSE_ANNOTATION_CHANGED, {
                "file_path": tool_args.get("file_path", "")
            })
        return None

    async def _fetch_task_list(self, ctx: LoopContext) -> dict | None:
        """Fetch active tasks and return task_list SSE event."""
        try:
            tasks = await ctx.get_active_tasks(ctx.session_id)
            return self.sse(SSE_TASK_LIST, {"tasks": tasks})
        except Exception:
            logger.debug("Failed to fetch active task list", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # LLM streaming
    # ------------------------------------------------------------------

    @staticmethod
    async def _stream_llm(
        ctx: LoopContext,
        messages: list[dict],
        delta_queue: asyncio.Queue,
    ) -> tuple[str, str, list[dict], dict | None]:
        """Stream LLM response. Delegates to llm_service.stream_chat()."""
        from app.services.llm_service import llm_service

        return await llm_service.stream_chat(
            messages=messages,
            model_role=ctx.model_role,
            tools=ctx.tool_schemas,
            delta_queue=delta_queue,
            max_tokens=ctx.response_max_tokens,
            session_id=ctx.session_id,
        )

    # ------------------------------------------------------------------
    # Default tool execution
    # ------------------------------------------------------------------

    @staticmethod
    async def execute_tool_default(tool_name: str, tool_args: dict) -> str:
        """Execute a tool by name using the global registry. Returns result string."""
        tool_def = tool_registry.get(tool_name)
        if tool_def is None:
            return f"Error: Unknown tool '{tool_name}'"
        if tool_def.call is None:
            return f"Error: Tool '{tool_name}' has no call implementation"
        return await LLMLoopRunner.call_tool(tool_def, tool_args)

    @staticmethod
    def _normalize_tool_result(result: Any) -> tuple[str, list[dict]]:
        if not isinstance(result, dict) or result.get("type") != "image":
            return str(result), []

        media_type = str(result.get("media_type") or "image/png")
        image_base64 = str(result.get("image_base64") or "")
        text = str(result.get("text") or "Image captured for direct visual inspection.")
        if not image_base64:
            return text, []

        image_ref = result.get("image_ref")
        persisted_text = text
        if isinstance(image_ref, dict):
            persisted_text += render_image_refs_tag([image_ref])

        image_message = LLMLoopRunner.msg(
            "user",
            [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
                },
            ],
            _ephemeral=True,
        )
        return persisted_text, [image_message]

    @staticmethod
    async def call_tool(tool_def, tool_args: dict) -> Any:
        """Call a tool with kwargs filtered to its declared parameters.

        The runner injects context params (``project_id``/``session_id``/
        ``model_role``) based on the tool's ``requires_*`` flags. These are
        intentionally absent from ``input_schema`` so the LLM cannot see or
        emit them, but they must still be forwarded to the tool's ``call``.

        Spurious params produced by the LLM that are not in the schema and
        not in the context-injection set are dropped before invocation,
        avoiding ``TypeError: unexpected keyword argument`` from tools whose
        ``call`` signature does not accept ``**kwargs``.
        """
        schema_props = set(tool_def.input_schema.get("properties", {}).keys())
        context_params = {"project_id", "session_id", "model_role"}
        clean = {
            k: v for k, v in tool_args.items()
            if k in schema_props or k in context_params
        }
        return await tool_def.call(**clean)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def msg(role: str, content: str, tool_calls: list[dict] | None = None,
             tool_call_id: str = "", reasoning_content: str = "", **extra) -> dict:
        msg: dict = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = [
                {"id": tc.get("id", ""), "type": "function",
                 "function": {"name": tc["name"],
                              "arguments": json.dumps(tc.get("params", {}), ensure_ascii=False)}}
                for tc in tool_calls
            ]
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        msg.update(extra)
        return msg

    @staticmethod
    def entry_from_history(msg, content) -> dict:
        """Convert a persisted message row to an in-memory LLM message dict.

        Shared by QueryLoop._build_messages and AnnotationLoop._build_messages:
        both walk the DB history and need the same tool_calls / tool_call_id /
        reasoning_content handling. ``content`` is the already-prepared text
        (the caller is responsible for image/attachment rewriting).
        """
        entry = LLMLoopRunner.msg(msg.role, content)
        if msg.tool_calls:
            try:
                entry["tool_calls"] = json.loads(msg.tool_calls)
            except (json.JSONDecodeError, TypeError):
                logger.debug("Skipping malformed tool_calls for message %s", msg.id, exc_info=True)
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        if getattr(msg, 'reasoning_content', None):
            entry["reasoning_content"] = msg.reasoning_content
        return entry

    @staticmethod
    def apply_cache_control(messages: list[dict], *, target_offset: int) -> None:
        """Idempotently mark exactly one user message with a cache breakpoint.

        Prompt caching only caches content *before* the ``cache_control`` marker.
        To grow the cache incrementally across turns, the marker must move with
        the latest stable user message each turn. This helper:

        1. Clears ``cache_control`` from every user message (so the previous
           turn's marker never lingers — ``messages`` is reused in place across
           turns).
        2. Selects the target user message by scanning backwards. ``tool`` and
           ``_ephemeral`` (in-memory image) messages are skipped because they are
           not real user turns.
        3. Sets a single ``cache_control: {"type": "ephemeral"}`` at message
           level. LiteLLM's OpenRouter handler moves it into the content block
           automatically; the ZAI/GLM handler passes it through as-is. Either
           way no more than one breakpoint exists per request.

        ``target_offset``: 0 = newest user message (main loop), 1 = second-to-newest
        (annotation loop, where the annotated span migrates into the newest user
        message each turn, so the second-to-newest is the stable cacheable prefix).
        No-op when there are not enough user messages (e.g. the first turn).
        """
        user_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user" and not m.get("_ephemeral")
        ]
        for i in user_indices:
            messages[i].pop("cache_control", None)
        if len(user_indices) > target_offset:
            target = user_indices[-(target_offset + 1)]
            messages[target]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

    @staticmethod
    def sse(event_type: str, data: dict) -> dict:
        return {"type": event_type, "data": data}

    @staticmethod
    def _safe_params(params: dict) -> str:
        s = json.dumps(params, ensure_ascii=False)
        if len(s) > 200:
            s = s[:200] + "..."
        return s

    @staticmethod
    def _format_error_message(error: Exception) -> str:
        message = str(error) or error.__class__.__name__
        return f"Error: {message}"
