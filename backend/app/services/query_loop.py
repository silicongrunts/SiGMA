"""
QueryLoop — the main AI interaction loop.

Delegates the LLM ↔ tool loop to LLMLoopRunner and handles:
- Message building (system prompt + history)
- Permission gates for write/exec tools
- Resume from interaction checkpoint (direct + subagent)
- Agent descriptions in system prompt
"""

import asyncio
from functools import partial
from typing import AsyncIterator, Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.chat_attachments import (
    extract_attachments,
    extract_image_refs,
    format_attachment_status,
    strip_image_refs_tag,
    strip_internal_image_tags,
)
from app.core.model_config import get_model_endpoint, model_role_accepts_images
from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.agents.prompt_service import prompt_service
from app.agents.tool_schema_service import tool_schemas_for_model_role
from app.agents.tools import tool_registry
from app.agents.tools.read_state import read_state_cache
from app.services.compaction_service import compaction_service
from app.services.chat_attachments import read_attachment_base64, read_image_path_base64
from app.services.session_temp_service import session_temp_service
from app.services.task_service import task_to_dict
from app.core.chat_events import (
    SSE_AWAITING_INPUT, SSE_CONTEXT_STATS, SSE_COMPACT_START, SSE_COMPACT_DONE,
)
from app.services.llm_loop_runner import (
    LLMLoopRunner, LoopContext,
    SSE_ERROR, SSE_DONE, SSE_TOOL_END, SSE_AGENT_EVENT,
    MAX_TOOL_OUTPUT_CHARS,
)
from app.services.pauses import InteractiveToolPause, is_permission_pause
from app.services.permission_executor import (
    approved_notebook_drift_error,
    approved_target_drift_error,
    restore_approved_read_state,
)
from app.services.token_budget import extract_llm_usage
from app.services.message_persist import stage_new_messages, CHECKPOINT_ROW_ID
from app.core.text_diff import CONTENT_SOFT_LIMIT

logger = get_logger(__name__)


class QueryLoop:
    """Main AI interaction loop for a single user turn."""

    def __init__(
        self,
        project_id: str,
        session_id: str,
        model: str = "supervisor",
        task_id: str = "",
        interaction_response: dict | None = None,
        cancel_event: "asyncio.Event | None" = None,
        token_budget_tracker=None,
    ):
        self.project_id = project_id
        self.session_id = session_id
        self.model_role = model
        self._model_name = self._resolve_model(model)
        self._task_id = task_id
        self._interaction_response = interaction_response
        self._cancel_event = cancel_event
        self._token_budget_tracker = token_budget_tracker
        self._persisted_real_input_tokens = 0
        self._persisted_real_count_at_index = 0
        self._context_warning_level = 0

    def _checkpoint(self, interaction_data: dict) -> dict:
        """Attach the immutable identity shared by UI, DB, and resume."""
        interaction_id = interaction_data.get("interaction_id") or generate_id()
        interaction_type = interaction_data.get("interaction_type")
        if not interaction_type:
            raise ValueError("Interactive checkpoint is missing interaction_type")
        interaction_data.update({
            "task_id": self._task_id,
            "interaction_id": interaction_id,
        })
        return {
            "interaction_id": interaction_id,
            "interaction_type": interaction_type,
        }

    async def _finish_checkpoint(self, success: bool, error: str = "") -> None:
        response = self._interaction_response or {}
        async with UnitOfWork(self.project_id) as uow:
            if success:
                await uow.task_state.complete_interaction(response.get("task_id", ""))
            else:
                await uow.task_state.fail_interaction(response.get("task_id", ""), error)

    @staticmethod
    def _resolve_model(role: str) -> str:
        return get_model_endpoint(role).litellm_model

    def _require_not_cancelled(self) -> None:
        """Raise CancelledError when the user has stopped this turn.

        Boundary check for phases that have no per-chunk cancel signal of
        their own (message building, the compaction call); the compaction LLM
        call itself is raced against the cancel event inside
        ``compaction_service.compact_messages``. The raised CancelledError
        propagates to the task runner, which finalizes the row as cancelled.
        """
        if self._cancel_event and self._cancel_event.is_set():
            raise asyncio.CancelledError()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        """Run one turn of the query loop. Yields SSE event dicts."""
        messages = []
        try:
            # ── Resume from interaction checkpoint ──
            if self._interaction_response is not None:
                async for event in self._resume_from_interaction():
                    yield event
                return

            # Build messages
            messages = await self._build_messages()
            if not messages:
                yield await self._error_event("Failed to build messages")
                return

            # Build loop context
            ctx = self._build_loop_context()
            async for event in self._run_loop_guarded(ctx, messages):
                yield event

        except Exception as e:
            logger.error("QueryLoop error: %s", e, exc_info=True)
            yield await self._error_event(e)

    async def _run_loop_guarded(
        self, ctx, messages: list[dict],
    ) -> AsyncIterator[dict]:
        """THE single pause boundary for every main-loop entry path.

        ``LLMLoopRunner.run()`` deliberately re-raises subagent pauses so the
        QueryLoop layer can checkpoint them. Every entry that re-runs the main
        loop (new message, permission resume, interaction resume, subagent
        interaction resume) must iterate through this method: a pause
        escaping the loop is converted into the matching checkpoint +
        awaiting_input / done events HERE, so it can never leak into the
        task runner and kill it.

        ``_save_messages`` persists only the not-yet-persisted tail, so
        catching a pause after a partially persisted round is safe.
        """
        try:
            async for event in LLMLoopRunner().run(ctx, messages):
                yield event
        except InteractiveToolPause as e:
            # Subagent hit an interactive tool — persist main messages first
            # (includes the assistant→agent tool_call), then save checkpoint.
            await self._save_messages(messages)
            async for event in self._emit_subagent_pause(e):
                yield event
        except Exception as e:
            # Subagent hit a permission gate — same checkpoint pattern, but
            # the interaction_data is a permission payload so resume routes
            # to _resume_from_permission (or _resume_subagent_interaction
            # when the paused subagent has a persistent session).
            if is_permission_pause(e):
                await self._save_messages(messages)
                async for event in self._emit_subagent_permission_pause(e):
                    yield event
            else:
                logger.error("QueryLoop error: %s", e, exc_info=True)
                yield await self._error_event(e)

    async def compact_active(self) -> AsyncIterator[dict]:
        """Run an explicit user-requested session compaction and stop."""
        self._require_not_cancelled()
        try:
            messages = await self._build_messages()
            if not messages:
                yield await self._error_event("Failed to build messages")
                return

            tools = tool_schemas_for_model_role(self.model_role)
            stats = compaction_service.stats_for_messages(
                messages, model_role=self.model_role, tools=tools,
            )
            yield LLMLoopRunner.sse(SSE_CONTEXT_STATS, stats.to_dict())
            yield LLMLoopRunner.sse(SSE_COMPACT_START, {
                "message": "Session Compacting...",
                **stats.to_dict(),
            })
            self._require_not_cancelled()
            result = await compaction_service.compact_messages(
                messages,
                model_role=self.model_role,
                mode="active",
                tools=tools,
                token_budget_tracker=self._token_budget_tracker,
                session_id=self.session_id,
                cancel_event=self._cancel_event,
            )
            async def _operation(uow):
                await compaction_service.stage_session_boundary(
                    uow, self.session_id, result.boundary_content,
                    usage=extract_llm_usage(result.usage).to_dict(),
                )
                await uow.sessions.stage_touch(self.session_id)

            await UnitOfWork.execute_atomic(self.project_id, _operation)

            # Compaction collapses the conversation — must-read-first cache must
            # reset so the LLM is forced to re-read files before further edits.
            read_state_cache.clear(self.session_id)

            done_data = {}
            if result.usage:
                done_data["usage"] = extract_llm_usage(result.usage).to_dict()
            yield LLMLoopRunner.sse(SSE_COMPACT_DONE, {
                "summary": result.summary,
                **result.stats.to_dict(),
            })
            yield LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict())
            yield LLMLoopRunner.sse(SSE_DONE, done_data)
        except Exception as e:
            logger.error("Active compact failed: %s", e, exc_info=True)
            yield await self._error_event(
                f"Unable to compact this session: {e}. "
                "Please create a new session and continue from there."
            )

    async def context_stats(self) -> dict:
        """Return current estimated LLM context stats for this session."""
        messages = await self._build_messages()
        stats = compaction_service.stats_for_messages_incremental(
            messages,
            model_role=self.model_role,
            tools=tool_schemas_for_model_role(self.model_role),
            last_real_input_tokens=self._persisted_real_input_tokens,
            last_real_count_at_index=self._persisted_real_count_at_index,
        )
        return stats.to_dict()

    # ------------------------------------------------------------------
    # Loop context construction
    # ------------------------------------------------------------------

    def _build_loop_context(self) -> LoopContext:
        ctx = LoopContext(
            project_id=self.project_id,
            session_id=self.session_id,
            model_role=self.model_role,
            context_kind="main",
            tool_schemas=tool_schemas_for_model_role(self.model_role),
            response_max_tokens=compaction_service.budget_for_role(self.model_role).response_max_tokens,
            allowed_tools=None,
            forbidden_tools=frozenset(),
            cancel_event=self._cancel_event,
            task_id=self._task_id,
            execute_tool=self._execute_tool_with_permissions,
            persist_messages=self._save_messages,
            # The main loop's persister implements the partial-assistant
            # checkpoint protocol, so long streamed turns are durably
            # persisted while they stream.
            persist_partial_assistant=True,
            prepare_messages=self._prepare_messages,
            get_active_tasks=self._get_active_tasks,
            on_pause=self._on_pause,
            token_budget_tracker=self._token_budget_tracker,
        )
        self._loop_ctx = ctx
        return ctx

    # ------------------------------------------------------------------
    # Pause checkpoint hook (interactive tools AND permission gates)
    # ------------------------------------------------------------------

    async def _on_pause(
        self, *, tool_name, tool_args, tool_call_id, interaction_data,
    ) -> None:
        """Save the awaiting_input checkpoint for a paused tool.

        An interactive tool asking for input and a gated tool waiting for
        approval park the task identically — same checkpoint payload, same
        resume dispatch (``_resume_from_interaction`` routes on the payload's
        ``interaction_type``) — so one hook serves both pause sites.

        The task id is stamped into the payload so the awaiting_input SSE
        frame and the persisted checkpoint both carry the unique interaction
        id the frontend needs for dialog remount keys (and so a restore from
        the DB yields the same shape).
        """
        checkpoint = self._checkpoint(interaction_data)
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.mark_awaiting_input(
                self._task_id, {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call_id,
                    "interaction_data": interaction_data,
                    "checkpoint": checkpoint,
                }
            )

    @staticmethod
    def _checkpoint_carry(pause) -> dict[str, int]:
        """Subagent spend to persist with a pause checkpoint.

        Seeded back into the next task's tracker (see
        ``chat_executor._load_turn_usage_baseline``) so the turn token
        stats keep counting the parked subagent's spend across the
        pause/resume split. Zero when the pause carries no stamp
        (direct tool pauses, and checkpoints saved before the field
        existed).
        """
        carry = getattr(pause, "agent_usage_carry", None) or {}
        return {
            "input": int(carry.get("input") or 0),
            "output": int(carry.get("output") or 0),
            "cached": int(carry.get("cached") or 0),
        }

    # ------------------------------------------------------------------
    # Subagent checkpoint (interactive tool paused inside a subagent)
    # ------------------------------------------------------------------

    async def _save_subagent_checkpoint(self, pause: InteractiveToolPause) -> None:
        """Save the checkpoint for a subagent interaction pause.

        Subagents with a persistent session (general/resume/plan) checkpoint
        as a rich subagent interaction: the resume dispatch routes on the
        ``is_subagent_interaction`` sentinel and continues the subagent
        mid-loop. Top-level interaction_type/interaction_data are mirrored
        for the frontend restore path (getActive →
        active.interaction.interaction_type); the dispatch checks the
        sentinel first, so the redundant fields never cause a wrong branch.

        Fork subagents have no persistent session to resume — they take the
        same fallback as the permission pause: a direct-style checkpoint
        carrying the outer agent tool call plus the inner interactive tool's
        identity, so resume executes the inner tool with the user's answers
        and injects the result as the agent tool's result.
        """
        interaction = pause.interaction_data or {}
        checkpoint = self._checkpoint(interaction)
        carry = self._checkpoint_carry(pause)
        if not pause.agent_session_id:
            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.mark_awaiting_input(
                    self._task_id, {
                        "tool_name": "agent",
                        "tool_args": {},
                        "tool_call_id": pause.parent_tool_call_id,
                        "interaction_data": interaction,
                        "checkpoint": checkpoint,
                        "inner_tool_name": pause.tool_name,
                        "inner_tool_args": pause.tool_args,
                        "agent_usage_carry": carry,
                    }
                )
            return
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.mark_awaiting_input(
                self._task_id, {
                    # Sentinel for resume dispatch
                    "is_subagent_interaction": True,
                    # Top-level interaction fields (for frontend restore only)
                    "interaction_type": interaction.get("interaction_type"),
                    "interaction_data": interaction,
                    "checkpoint": checkpoint,
                    # Outer agent tool context
                    "parent_tool_call_id": pause.parent_tool_call_id,
                    # Subagent session
                    "agent_session_id": pause.agent_session_id,
                    "agent_type": pause.agent_type,
                    "agent_usage_baseline": pause.agent_usage_baseline or {
                        "input": 0, "output": 0, "cached": 0,
                    },
                    # Inner interactive tool context
                    "inner_tool_name": pause.tool_name,
                    "inner_tool_args": pause.tool_args,
                    "inner_tool_call_id": pause.tool_call_id,
                    "inner_interaction_data": pause.interaction_data,
                    # Subagent spend parked with the pause (turn stats)
                    "agent_usage_carry": carry,
                }
            )

    async def _emit_subagent_pause(self, pause: InteractiveToolPause) -> AsyncIterator[dict]:
        """Persist a subagent pause and emit the matching UI events.

        No done frame here: parking is not a loop-computed terminal event —
        the task runner synthesizes done when it sees the awaiting_input
        status.
        """
        # Stamp the owning task id so the emitted dialog payload and the
        # persisted checkpoint carry the frontend's interaction id.
        pause.interaction_data = {
            **(pause.interaction_data or {}),
            "task_id": self._task_id,
        }
        await self._save_subagent_checkpoint(pause)
        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": pause.parent_tool_call_id,
            "agent_type": pause.agent_type,
            "inner_type": "awaiting_input",
            "inner_data": pause.interaction_data,
        })
        yield LLMLoopRunner.sse(SSE_AWAITING_INPUT, pause.interaction_data)

    async def _emit_subagent_permission_pause(self, pause) -> AsyncIterator[dict]:
        """Persist a subagent permission pause and emit matching UI events.

        A subagent's tool hit a permission gate. If the subagent has a
        persistent session (general/resume agents), the checkpoint is saved as
        a subagent interaction — preserving the agent_session_id so the
        subagent can be resumed mid-loop after the user responds. The approved
        operation is executed on resume and its result injected into the
        subagent's message history, then the subagent loop continues.

        Fork agents have no persistent session; they fall back to the direct
        checkpoint path (the tool is executed on resume and the main loop
        re-runs).
        """
        interaction_data = {
            "interaction_type": "permission",
            "task_id": self._task_id,
            "tool": pause.tool,
            "tool_name": pause.tool_name,
            "path": pause.path,
            "resolved_path": pause.resolved_path,
            "operation": pause.operation,
            "content": pause.content,
            "content_truncated": pause.content_truncated,
            "content_sha256": pause.content_sha256,
            "description": pause.description,
            "diff_lines": pause.diff_lines,
            "diff_truncated": pause.diff_truncated,
        }
        checkpoint = self._checkpoint(interaction_data)
        carry = self._checkpoint_carry(pause)

        has_session = bool(getattr(pause, "agent_session_id", ""))

        if has_session:
            # Subagent with a persistent session — checkpoint as a subagent
            # interaction so _resume_subagent_interaction resumes it mid-loop.
            # interaction_type/interaction_data are mirrored to the top level so
            # the frontend restore path (getActive) can rebuild the modal
            # uniformly. Resume dispatch checks is_subagent_interaction first,
            # so the redundant fields never cause a wrong branch.
            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.mark_awaiting_input(
                    self._task_id, {
                        "is_subagent_interaction": True,
                        # Top-level interaction fields (for frontend restore only)
                        "interaction_type": interaction_data.get("interaction_type"),
                        "interaction_data": interaction_data,
                        "checkpoint": checkpoint,
                        "parent_tool_call_id": pause.parent_tool_call_id,
                        "agent_session_id": pause.agent_session_id,
                        "agent_type": pause.agent_type,
                        "agent_usage_baseline": pause.agent_usage_baseline or {
                            "input": 0, "output": 0, "cached": 0,
                        },
                        "inner_tool_name": pause.tool_name,
                        "inner_tool_args": getattr(pause, "tool_args", {}),
                        "inner_tool_call_id": getattr(pause, "inner_tool_call_id", ""),
                        "inner_interaction_data": interaction_data,
                        # Subagent spend parked with the pause (turn stats)
                        "agent_usage_carry": carry,
                    }
                )
        else:
            # Fork agent (no persistent session) — save the inner tool details
            # so _resume_from_permission can execute it directly on approval
            # and inject the result as the agent tool's result.
            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.mark_awaiting_input(
                    self._task_id, {
                        "tool_name": "agent",
                        "tool_args": {},
                        "tool_call_id": pause.parent_tool_call_id,
                        "interaction_data": interaction_data,
                        "checkpoint": checkpoint,
                        "inner_tool_name": pause.tool_name,
                        "inner_tool_args": getattr(pause, "tool_args", {}),
                        "agent_usage_carry": carry,
                    }
                )

        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": pause.parent_tool_call_id,
            "agent_type": getattr(pause, "agent_type", ""),
            "inner_type": "awaiting_input",
            "inner_data": interaction_data,
        })
        yield LLMLoopRunner.sse(SSE_AWAITING_INPUT, interaction_data)

    async def _prepare_messages(self, messages: list[dict]) -> tuple[list[dict], list[dict]]:
        loop_ctx = getattr(self, "_loop_ctx", None)
        last_real_input_tokens, last_real_count_at_index = self._real_token_baseline(
            loop_ctx,
        )
        stats = compaction_service.stats_for_messages_incremental(
            messages,
            model_role=self.model_role,
            tools=tool_schemas_for_model_role(self.model_role),
            last_real_input_tokens=last_real_input_tokens,
            last_real_count_at_index=last_real_count_at_index,
        )
        events = [LLMLoopRunner.sse(SSE_CONTEXT_STATS, stats.to_dict())]
        if stats.current_tokens <= stats.compact_threshold:
            warning_message = self._context_threshold_warning(stats)
            if warning_message:
                # User role on purpose: serving-side chat templates reject a
                # system message outside the first position, and this warning
                # must reach the model at the current end of the conversation.
                messages.append(LLMLoopRunner.msg(
                    "user",
                    f"<status>{warning_message}</status>",
                    _ephemeral=True,
                ))
            LLMLoopRunner.apply_cache_control(messages, target_offset=0)
            return messages, events

        events.append(LLMLoopRunner.sse(SSE_COMPACT_START, {
            "message": "Session Compacting...",
            **stats.to_dict(),
        }))
        # Boundary check: a turn the user already cancelled must not start a
        # new compaction.
        self._require_not_cancelled()
        try:
            result = await compaction_service.compact_messages(
                messages,
                model_role=self.model_role,
                mode="passive",
                tools=tool_schemas_for_model_role(self.model_role),
                token_budget_tracker=self._token_budget_tracker,
                session_id=self.session_id,
                cancel_event=self._cancel_event,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Unable to compact this session: {exc}. "
                "Please create a new session and continue from there."
            ) from exc
        async def _operation(uow):
            await compaction_service.stage_session_boundary(
                uow, self.session_id, result.boundary_content,
                usage=extract_llm_usage(result.usage).to_dict(),
            )
            await uow.sessions.stage_touch(self.session_id)

        await UnitOfWork.execute_atomic(self.project_id, _operation)

        # Compaction collapses the conversation — must-read-first cache must
        # reset so the LLM is forced to re-read files before further edits.
        read_state_cache.clear(self.session_id)

        # Compaction replaced the message list — invalidate cached real tokens.
        if loop_ctx:
            loop_ctx.last_real_input_tokens = 0
            loop_ctx.last_real_count_at_index = 0
        self._persisted_real_input_tokens = 0
        self._persisted_real_count_at_index = 0

        events.append(LLMLoopRunner.sse(SSE_COMPACT_DONE, {
            "summary": result.summary,
            **result.stats.to_dict(),
        }))
        events.append(LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict()))
        LLMLoopRunner.apply_cache_control(result.messages, target_offset=0)
        return result.messages, events

    async def _get_active_tasks(self, session_id: str) -> list:
        async with UnitOfWork(self.project_id) as uow:
            tasks = await uow.tasks.list_active(session_id)
            return [task_to_dict(t) for t in tasks]

    async def _agent_session_usage_delta(
        self, agent_session_id: str, baseline: dict,
    ) -> dict[str, int]:
        async with UnitOfWork(self.project_id) as uow:
            messages = await uow.messages.get_messages(agent_session_id)
        current = {
            "input": sum(int(getattr(m, "input_tokens", 0) or 0) for m in messages),
            "output": sum(int(getattr(m, "token_count", 0) or 0) for m in messages),
            "cached": sum(int(getattr(m, "cached_tokens", 0) or 0) for m in messages),
        }
        return {
            key: max(0, current[key] - int((baseline or {}).get(key) or 0))
            for key in ("input", "output", "cached")
        }

    # ------------------------------------------------------------------
    # Tool execution with permission gates
    # ------------------------------------------------------------------

    async def _execute_tool_with_permissions(
        self, tool_name: str, tool_args: dict
    ) -> str:
        """Delegate to shared permission executor (single source of truth).

        Raises PermissionRequestPause when user approval is needed — the runner
        catches it and parks the task as awaiting_input.
        """
        from app.services.permission_executor import execute_with_permission
        tool_def = tool_registry.get(tool_name)
        return await execute_with_permission(
            tool_name, tool_args, tool_def,
            project_id=self.project_id,
        )

    # ------------------------------------------------------------------
    # Resume from interaction checkpoint
    # ------------------------------------------------------------------

    async def _resume_from_interaction(self) -> AsyncIterator[dict]:
        """Resume the query loop from a pending user interaction."""

        try:
            async with UnitOfWork(self.project_id) as uow:
                response = self._interaction_response or {}
                state = await uow.task_state.get_pending_interaction(
                    response.get("task_id", ""),
                    response.get("interaction_id", ""),
                    response.get("interaction_type", ""),
                )
            if not state:
                # Client-side race or stale checkpoint (e.g. the approval was
                # already consumed by another resume): not a task failure, so
                # end the stream cleanly instead of persisting a phantom
                # error bubble.
                logger.warning(
                    "No pending interaction found for session %s", self.session_id,
                )
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            # ── Subagent interaction: resume the subagent mid-loop ──
            if state.get("is_subagent_interaction"):
                async for event in self._resume_subagent_interaction(state):
                    yield event
                return

            # ── Permission approval resume ──
            interaction_data = state.get("interaction_data", {})
            if interaction_data.get("interaction_type") == "permission":
                async for event in self._resume_from_permission(state):
                    yield event
                return

            # ── Fork-agent interactive resume ──
            # A fork subagent has no persistent session, so its interactive
            # pause is checkpointed as the outer agent tool call (tool_name
            # "agent" + the inner tool's identity) instead of a subagent
            # interaction. Matched before the direct branch: "agent" is not
            # an interactive tool and would be discarded as stale there.
            if state.get("tool_name") == "agent" and state.get("inner_tool_name"):
                async for event in self._resume_from_fork_interaction(state):
                    yield event
                return

            # ── Normal (direct) interaction resume ──
            tool_name = state.get("tool_name", "")
            tool_args = state.get("tool_args", {})
            tool_def = tool_registry.get(tool_name)

            if not tool_def or not tool_def.requires_user_interaction:
                # Keep the checkpoint identity durable even when the tool can
                # no longer be resolved. This makes the failure visible and
                # gives the UI a stable retry/recovery handle.
                async with UnitOfWork(self.project_id) as uow:
                    claimed = await uow.task_state.claim_interaction(
                        response.get("task_id", ""), response.get("interaction_id", ""),
                        response.get("interaction_type", ""),
                    )
                    if claimed is not None:
                        await uow.task_state.fail_interaction(
                            response.get("task_id", ""),
                            f"Interactive tool '{tool_name}' is no longer available",
                        )
                logger.warning(
                    "Interaction checkpoint for session %s references tool "
                    "'%s' which does not support interaction",
                    self.session_id, tool_name,
                )
                yield await self._error_event(
                    f"Interactive tool '{tool_name}' is no longer available"
                )
                return

            messages = await self._build_messages()
            if not messages:
                # Claim and retain the checkpoint as failed. The approval is
                # still recoverable after the transient load failure.
                async with UnitOfWork(self.project_id) as uow:
                    claimed = await uow.task_state.claim_interaction(
                        response.get("task_id", ""), response.get("interaction_id", ""),
                        response.get("interaction_type", ""),
                    )
                    if claimed is not None:
                        await uow.task_state.fail_interaction(
                            response.get("task_id", ""), "Failed to load checkpoint",
                        )
                yield await self._error_event("Failed to load checkpoint")
                return

            tool_call_id = state.get("tool_call_id", "")

            # Claim the checkpoint BEFORE any tool executes: the guarded
            # UPDATE's rowcount ensures only one of several concurrent resumes
            # can consume it, so a resume racing a cancel of the parked task
            # cannot execute the interactive tool on a checkpoint that was
            # already taken (same fail-safe contract as _resume_from_permission
            # — losing the response is safer than a duplicated side effect).
            async with UnitOfWork(self.project_id) as uow:
                claimed = await uow.task_state.claim_interaction(
                    response.get("task_id", ""), response.get("interaction_id", ""),
                    response.get("interaction_type", ""),
                )
            if claimed is None:
                logger.warning(
                    "Interaction checkpoint for session %s was already "
                    "consumed by another resume", self.session_id,
                )
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            tool_result = await LLMLoopRunner.call_interactive_tool(
                tool_def, tool_args, self._interaction_response,
            )

            if tool_call_id:
                messages.append(LLMLoopRunner.msg(
                    "tool", tool_result, tool_call_id=tool_call_id
                ))
                # Persist the executed result before the LLM continues:
                # the tool's side effect already happened, so its outcome
                # must survive a crash or a restart mid-loop.
                await self._save_messages(messages)

            await self._finish_checkpoint(True)

            yield LLMLoopRunner.sse(SSE_TOOL_END, {
                "tool": tool_name, "result_summary": strip_image_refs_tag(tool_result)[:200],
                "tool_call_id": tool_call_id,
            })

            ctx = self._build_loop_context()
            async for event in self._run_loop_guarded(ctx, messages):
                yield event

        except Exception as e:
            logger.error("Resume from interaction error: %s", e, exc_info=True)
            await self._finish_checkpoint(False, str(e))
            yield await self._error_event(e)

    async def _execute_approved_tool(
        self, tool_name: str, tool_args: dict, session_id: str = "",
    ) -> str:
        """Execute a tool the user just approved, for every approval resume.

        Direct, fork, and subagent approval resumes run the approved tool
        through one helper: context params are injected the same way the
        runner does before its calls, the execution shares the main loop's
        cancellation wrapper (a stop during a slow approved bash cancels the
        tool instead of outliving the turn), and a failure becomes the
        error-string tool result — an approved operation is never re-executed.
        """
        tool_def = tool_registry.get(tool_name)
        if not (tool_def and tool_def.call):
            return f"Error: Tool '{tool_name}' is not available"
        call_args = dict(tool_args)
        if tool_def.requires_project_id:
            call_args.setdefault("project_id", self.project_id)
        if tool_def.requires_session_id and (session_id or self.session_id):
            call_args.setdefault("session_id", session_id or self.session_id)
        if tool_def.requires_model_role:
            call_args.setdefault("model_role", self.model_role)
        # The must-read-first cache is in-memory and empty after the restart
        # that made this resume necessary, while the paused call's preflight
        # had already proven the target was read. Re-recording it keeps the
        # approved execution from being consumed by the tool's own gate.
        restore_approved_read_state(
            tool_name, call_args, self.project_id, session_id or self.session_id,
        )
        try:
            return str(await LLMLoopRunner.execute_tool_cancellable(
                self._cancel_event, LLMLoopRunner.call_tool(tool_def, call_args),
            ))
        except Exception as exc:
            logger.exception("Approved tool '%s' failed", tool_name)
            return f"Tool '{tool_name}' error: {exc}"

    async def _resume_from_permission(self, state: dict) -> AsyncIterator[dict]:
        """Resume the query loop from a permission approval checkpoint.

        Direct tool pause (main loop): if approved, the tool is executed
        directly (bypassing the permission gate — the user just approved it);
        if denied, a rejection string is injected as the tool result.

        Fork-agent pause (``tool_name == "agent"`` with ``inner_tool_name``):
        the fork subagent has no persistent session, so it cannot be resumed.
        If approved, the inner tool (write/edit/bash/...) is executed directly
        and its result is injected as the ``agent`` tool's result — the main
        loop re-runs with the operation already done. If denied, a rejection
        string is injected instead.

        When the checkpoint carries an approval-time resolved-target snapshot
        (``resolved_path``, file write pauses), the approved operation runs
        only if the target still resolves to the snapshot: a target that
        drifted while the task was parked (e.g. a flipped symlink) is not
        executed and gets an error tool result instead.

        The checkpoint is atomically claimed BEFORE any tool executes: the
        guarded UPDATE's rowcount ensures only one of several concurrent
        resumes can run the approved operation; the rest end silently. The
        trade-off is fail-safe — a crash between the claim and the execution
        loses one approval (the user re-approves) rather than risking a
        duplicated side effect.

        Subagent pauses from persistent agents (general/resume) never reach
        here — they are saved as ``is_subagent_interaction`` checkpoints and
        resumed via ``_resume_subagent_interaction``.
        """
        try:
            response = self._interaction_response or {}
            interaction_data = state.get("interaction_data", {})
            tool_name = state.get("tool_name", "")
            tool_args = state.get("tool_args", {})
            tool_call_id = state.get("tool_call_id", "")
            # Fork-agent checkpoints store the gated tool's identity in the
            # inner fields; the outer "agent" args carry no target.
            inner_tool_name = state.get("inner_tool_name", "")
            inner_tool_args = state.get("inner_tool_args", {})

            messages = await self._build_messages()
            if not messages:
                # Retain the approval as a failed, durable checkpoint so a
                # refresh can retry it with the same identity.
                async with UnitOfWork(self.project_id) as uow:
                    claimed = await uow.task_state.claim_interaction(
                        response.get("task_id", ""), response.get("interaction_id", ""),
                        response.get("interaction_type", ""),
                    )
                    if claimed is not None:
                        await uow.task_state.fail_interaction(
                            response.get("task_id", ""), "Failed to load checkpoint",
                        )
                yield await self._error_event("Failed to load checkpoint")
                return

            # Claim only after every read-only step succeeded: a failure in
            # message building must not consume the checkpoint the approval
            # still depends on.
            async with UnitOfWork(self.project_id) as uow:
                claimed = await uow.task_state.claim_interaction(
                    response.get("task_id", ""), response.get("interaction_id", ""),
                    response.get("interaction_type", ""),
                )
            if claimed is None:
                logger.warning(
                    "Permission checkpoint for session %s was already "
                    "consumed by another resume", self.session_id,
                )
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            response = self._interaction_response or {}
            approved = response.get("approved", False)
            reason = response.get("reason", "")

            is_subagent = tool_name == "agent"

            # Approval binds the approved operation's snapshot: when the
            # checkpoint carries a resolved-target snapshot (file write
            # pauses), the target must still resolve to it; when it carries a
            # cell-source digest (notebook pauses), the cell must still hash
            # to it. A target that drifted while the task was parked (e.g. a
            # flipped symlink) or a cell edited under an old approval is not
            # executed and gets an error tool result instead. A denial
            # executes nothing, so its message is unaffected.
            drift_error = ""
            if approved:
                drift_error = approved_target_drift_error(
                    self.project_id,
                    interaction_data.get("resolved_path", ""),
                    # The fork checkpoint stores the gated tool's args in
                    # inner_tool_args; the outer "agent" args carry no target.
                    inner_tool_args if is_subagent else tool_args,
                    interaction_data.get("tool", ""),
                )
                if not drift_error:
                    drift_error = await approved_notebook_drift_error(
                        self.project_id,
                        inner_tool_args if is_subagent else tool_args,
                        interaction_data.get("content_sha256", ""),
                    )
            executed = approved and not drift_error

            if drift_error:
                logger.warning(
                    "Approved target for session %s changed since approval; "
                    "the tool was not executed", self.session_id,
                )
                tool_result = drift_error
            elif approved:
                if is_subagent:
                    # Fork agent — execute the approved inner tool directly
                    # and inject the result as the agent tool's result.
                    tool_result = await self._execute_approved_tool(
                        inner_tool_name, inner_tool_args,
                    )
                else:
                    # Execute the tool directly, bypassing the permission gate
                    # — the user just explicitly approved this exact operation.
                    # Re-checking auto-approve via execute_with_permission would
                    # risk pausing again.
                    tool_result = await self._execute_approved_tool(
                        tool_name, tool_args,
                    )
            else:
                tool_result = self._permission_denial_message(
                    interaction_data, reason,
                )

            if tool_call_id:
                # A fork-agent pause carries the subagent spend accrued
                # before the pause (agent_usage_carry): the injected result
                # completes the outer agent call, so its row must hold the
                # fork's whole usage, not just nothing. Direct pauses have
                # no carry field and keep zero token fields.
                messages.append(LLMLoopRunner.msg(
                    "tool", tool_result, tool_call_id=tool_call_id,
                    **LLMLoopRunner.usage_extra(state.get("agent_usage_carry")),
                ))
                # Persist the executed tool's result immediately: a process
                # restart mid-loop marks the task failed at startup, and the
                # persisted result keeps an already-executed operation's
                # outcome visible in history.
                await self._save_messages(messages)

            await self._finish_checkpoint(True)

            # Only an actually executed operation carries file-edit metadata
            # and the file_changed side-effect event — denials and
            # target-drift refusals inject synthetic strings, not file ops,
            # so they pass None for the args.
            end_payload = LLMLoopRunner.tool_end_payload(
                tool_name, tool_args if executed else None,
                tool_result, tool_call_id,
            )
            if is_subagent and executed:
                # Fork agent: forward the inner tool's file-edit metadata on
                # the outer agent step, mirroring the persistent-subagent
                # path's inner tool_end (same checkpointed inner args, same
                # result string — nothing is synthesized).
                inner_end = LLMLoopRunner.tool_end_payload(
                    inner_tool_name, inner_tool_args, tool_result, tool_call_id,
                )
                if "file_edit" in inner_end:
                    end_payload["file_edit"] = inner_end["file_edit"]
            yield LLMLoopRunner.sse(SSE_TOOL_END, end_payload)

            # A file-mutating tool was just executed on approval — emit
            # file_changed so the frontend refreshes the file tree, matching the
            # main loop's side-effect (llm_loop_runner emits it after call_tool).
            if executed:
                if is_subagent:
                    fc_evt = LLMLoopRunner._emit_file_changed(
                        inner_tool_name, inner_tool_args, tool_result,
                    )
                else:
                    fc_evt = LLMLoopRunner._emit_file_changed(
                        tool_name, tool_args, tool_result,
                    )
                if fc_evt:
                    yield fc_evt

            ctx = self._build_loop_context()
            async for event in self._run_loop_guarded(ctx, messages):
                yield event

        except Exception as e:
            logger.error("Resume from permission error: %s", e, exc_info=True)
            await self._finish_checkpoint(False, str(e))
            yield await self._error_event(e)

    @staticmethod
    def _permission_denial_message(interaction_data: dict, reason: str) -> str:
        """Build a category-appropriate denial string for the LLM.

        The reason is user free text that lands in the persisted message
        history, so it is capped at the same soft limit the permission layer
        applies to every other persisted approval payload. A truncated reason
        still tells the model why the user refused.
        """
        category = interaction_data.get("tool", "")
        operation = interaction_data.get("operation", "")
        path = interaction_data.get("path", "")

        if category == "bash":
            denial = "User rejected to execute this command"
        elif category == "notebook":
            denial = f"User denied permission to execute code in notebook: {path}"
        else:
            denial = f"User denied permission to {operation or 'modify'} file: {path}"
        if reason:
            if len(reason) > CONTENT_SOFT_LIMIT:
                reason = reason[:CONTENT_SOFT_LIMIT] + " ... [truncated]"
            denial += f". User says: {reason}"
        return denial

    async def _resume_from_fork_interaction(self, state: dict) -> AsyncIterator[dict]:
        """Resume the query loop from a fork subagent's interactive pause.

        A fork subagent has no persistent session, so it cannot be resumed
        mid-loop. Same fallback as the fork branch of _resume_from_permission:
        the inner interactive tool (ask_user_question, plan approval) executes
        with the user's response and its result is injected as the outer
        ``agent`` tool's result — the main loop re-runs with the answer
        already delivered. The session re-stamp targets the main session: the
        fork carries no agent session, and the plan-approval tool's plan
        belongs to the session that owns the turn anyway.

        The claim-before-execute contract matches _resume_from_permission: a
        crash between the claim and the execution loses one answer rather
        than risking a duplicated side effect.
        """
        try:
            response = self._interaction_response or {}
            tool_call_id = state.get("tool_call_id", "")
            inner_tool_name = state.get("inner_tool_name", "")
            inner_tool_args = state.get("inner_tool_args", {})

            messages = await self._build_messages()
            if not messages:
                # Retain the answer as a failed, durable checkpoint so a
                # refresh can retry it with the same identity.
                async with UnitOfWork(self.project_id) as uow:
                    claimed = await uow.task_state.claim_interaction(
                        response.get("task_id", ""), response.get("interaction_id", ""),
                        response.get("interaction_type", ""),
                    )
                    if claimed is not None:
                        await uow.task_state.fail_interaction(
                            response.get("task_id", ""), "Failed to load checkpoint",
                        )
                yield await self._error_event("Failed to load checkpoint")
                return

            # Claim only after every read-only step succeeded: a failure in
            # message building must not consume the checkpoint the answer
            # still depends on.
            async with UnitOfWork(self.project_id) as uow:
                claimed = await uow.task_state.claim_interaction(
                    response.get("task_id", ""), response.get("interaction_id", ""),
                    response.get("interaction_type", ""),
                )
            if claimed is None:
                logger.warning(
                    "Fork interaction checkpoint for session %s was already "
                    "consumed by another resume", self.session_id,
                )
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            inner_tool_def = tool_registry.get(inner_tool_name)
            if inner_tool_def and inner_tool_def.requires_user_interaction:
                # Same interactive-tool merge contract as the direct resume
                # path (schema filter → response overlay → context re-stamp,
                # see LLMLoopRunner.call_interactive_tool).
                inner_result = await LLMLoopRunner.call_interactive_tool(
                    inner_tool_def, inner_tool_args or {},
                    self._interaction_response, session_id=self.session_id,
                )
            else:
                # A checkpoint whose inner tool no longer resolves to an
                # interactive tool is stale; the raw response is still
                # delivered so the model sees the user's answer attempt.
                inner_result = str(self._interaction_response or "")

            if tool_call_id:
                # Carry the fork's pre-pause spend onto the completing agent
                # tool row (see _resume_from_permission's fork branch).
                messages.append(LLMLoopRunner.msg(
                    "tool", inner_result, tool_call_id=tool_call_id,
                    **LLMLoopRunner.usage_extra(state.get("agent_usage_carry")),
                ))
                # Persist the injected result before the LLM continues: the
                # user's answer must survive a crash or restart mid-loop.
                await self._save_messages(messages)

            await self._finish_checkpoint(True)

            yield LLMLoopRunner.sse(SSE_TOOL_END, LLMLoopRunner.tool_end_payload(
                "agent", {}, inner_result, tool_call_id,
            ))

            ctx = self._build_loop_context()
            async for event in self._run_loop_guarded(ctx, messages):
                yield event

        except Exception as e:
            logger.error("Resume from fork interaction error: %s", e, exc_info=True)
            await self._finish_checkpoint(False, str(e))
            yield await self._error_event(e)

    # ------------------------------------------------------------------
    # Subagent interaction resume
    # ------------------------------------------------------------------

    async def _resume_subagent_interaction(self, state: dict) -> AsyncIterator[dict]:
        """Resume a subagent that paused for user interaction (e.g. plan approval)."""
        response = self._interaction_response or {}
        parent_tool_call_id = state.get("parent_tool_call_id", "")
        agent_session_id = state.get("agent_session_id", "")
        agent_type = state.get("agent_type", "")
        agent_usage_baseline = state.get("agent_usage_baseline") or {
            "input": 0, "output": 0, "cached": 0,
        }
        inner_tool_name = state.get("inner_tool_name", "")
        inner_tool_args = state.get("inner_tool_args", {})
        inner_tool_call_id = state.get("inner_tool_call_id", "")
        # Snapshot at resume start: the tracker's accrual from here until a
        # re-pause is exactly the post-resume subagent spend (the main loop
        # makes no LLM call until the agent call completes), so the carried
        # pre-pause spend can be chained with it below.
        resume_start_usage = LLMLoopRunner.budget_tracker_usage(
            self._token_budget_tracker,
        )

        if not agent_session_id or not parent_tool_call_id:
            # The checkpoint cannot be resumed, but its identity remains
            # durable so the UI can offer recovery instead of losing it.
            async with UnitOfWork(self.project_id) as uow:
                claimed = await uow.task_state.claim_interaction(
                    response.get("task_id", ""), response.get("interaction_id", ""),
                    response.get("interaction_type", ""),
                )
            if claimed is not None:
                await self._finish_checkpoint(False, "Invalid subagent checkpoint")
            yield await self._error_event("Invalid subagent checkpoint")
            return

        # Claim the checkpoint BEFORE the inner tool executes — the same
        # contract as _resume_from_permission: the guarded UPDATE's rowcount
        # ensures only one of several concurrent resumes can run the
        # operation, and a cancel that consumed the checkpoint (or a user
        # answering from a second tab) prevents the side effect. A crash
        # between the claim and the execution loses one approval rather
        # than risking a duplicated side effect.
        async with UnitOfWork(self.project_id) as uow:
            claimed = await uow.task_state.claim_interaction(
                response.get("task_id", ""), response.get("interaction_id", ""),
                response.get("interaction_type", ""),
            )
        if claimed is None:
            logger.warning(
                "Subagent interaction checkpoint for session %s was already "
                "consumed by another resume", self.session_id,
            )
            yield LLMLoopRunner.sse(SSE_DONE, {})
            return

        # ── Phase 2: process the inner tool with the user's response ──
        inner_interaction_data = state.get("inner_interaction_data", {})
        is_permission = (
            inner_interaction_data.get("interaction_type") == "permission"
        )
        inner_tool_def = tool_registry.get(inner_tool_name)
        inner_result = ""
        # Only a permission-approved execution runs a real side effect;
        # every branch below that injects a synthetic string leaves False.
        inner_executed = False

        if is_permission:
            # Permission approval/denial for a subagent's write/bash/notebook
            # tool. If approved, execute the tool directly (the user just
            # approved this exact operation — re-checking the permission gate
            # would pause again), unless the resolved write target drifted
            # while the task was parked (same snapshot contract as
            # _resume_from_permission). If denied, inject a rejection string.
            response = self._interaction_response or {}
            approved = response.get("approved", False)
            reason = response.get("reason", "")
            # Same snapshot contract as _resume_from_permission: the resolved
            # write target must still resolve to the approval-time snapshot,
            # and the approved notebook cell must still hash to its snapshot.
            drift_error = ""
            if approved:
                drift_error = approved_target_drift_error(
                    self.project_id,
                    inner_interaction_data.get("resolved_path", ""),
                    inner_tool_args or {},
                    inner_interaction_data.get("tool", ""),
                )
                if not drift_error:
                    drift_error = await approved_notebook_drift_error(
                        self.project_id,
                        inner_tool_args or {},
                        inner_interaction_data.get("content_sha256", ""),
                    )
            inner_executed = bool(approved and not drift_error)
            if drift_error:
                logger.warning(
                    "Approved subagent target for session %s changed since "
                    "approval; the tool was not executed", self.session_id,
                )
                inner_result = drift_error
            elif approved:
                # The agent session re-stamp targets the subagent's session —
                # the same session the paused call ran against.
                inner_result = await self._execute_approved_tool(
                    inner_tool_name, inner_tool_args or {},
                    session_id=agent_session_id,
                )
            else:
                inner_result = self._permission_denial_message(
                    inner_interaction_data, reason,
                )
        elif inner_tool_def and inner_tool_def.requires_user_interaction:
            # Same interactive-tool merge contract as the direct resume path
            # (schema filter → response overlay → context re-stamp, see
            # LLMLoopRunner.call_interactive_tool). The session re-stamp
            # targets the agent session — except the plan-approval tool,
            # whose plan belongs to the main session that owns the turn.
            session_for_tool = (
                self.session_id
                if inner_tool_name == "submit_plan_for_approval"
                else agent_session_id
            )
            inner_result = await LLMLoopRunner.call_interactive_tool(
                inner_tool_def, inner_tool_args or {},
                self._interaction_response, session_id=session_for_tool,
            )
        else:
            inner_result = str(self._interaction_response or "")

        await self._finish_checkpoint(True)

        # Emit tool_end for the inner tool. Denials, plain interaction
        # responses, and target-drift refusals executed no edit/write, so they
        # pass None for the args and carry no file-edit metadata.
        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": parent_tool_call_id,
            "agent_type": agent_type,
            "inner_type": SSE_TOOL_END,
            "inner_data": LLMLoopRunner.tool_end_payload(
                inner_tool_name,
                inner_tool_args if inner_executed else None,
                inner_result,
                inner_tool_call_id,
            ),
        })

        # A permission-approved file-mutating tool was just executed directly —
        # emit file_changed so the frontend refreshes the file tree, matching the
        # main loop's side-effect. Denials and drift refusals inject synthetic
        # strings, not file ops.
        if inner_executed:
            fc_evt = LLMLoopRunner._emit_file_changed(
                inner_tool_name, inner_tool_args, inner_result,
            )
            if fc_evt:
                yield fc_evt

        # ── Short-circuit: if plan approved, skip agent resume entirely ──
        approved = (self._interaction_response or {}).get("approved", False)
        subagent_error = None

        if agent_type == "plan" and approved:
            plan_content = inner_tool_args.get("plan_content", "")
            final_text = f"{inner_result}\n\n{plan_content}"
        else:
            # Rejection or general agent: resume the agent LLM loop.
            from app.services.agent_service import agent_service

            event_queue: asyncio.Queue = asyncio.Queue()

            async def _subagent_emit(event: dict):
                await event_queue.put(event)

            async def _run_plan_resume():
                try:
                    if agent_type == "plan":
                        result = await agent_service.resume_plan_from_interaction(
                            project_id=self.project_id,
                            agent_session_id=agent_session_id,
                            parent_tool_call_id=parent_tool_call_id,
                            inner_tool_call_id=inner_tool_call_id,
                            inner_tool_result=inner_result,
                            emit_event=_subagent_emit,
                            cancel_event=self._cancel_event,
                            token_budget_tracker=self._token_budget_tracker,
                        )
                    elif agent_type == "general":
                        result = await agent_service.resume_general_from_interaction(
                            project_id=self.project_id,
                            agent_session_id=agent_session_id,
                            parent_tool_call_id=parent_tool_call_id,
                            inner_tool_call_id=inner_tool_call_id,
                            inner_tool_result=inner_result,
                            emit_event=_subagent_emit,
                            cancel_event=self._cancel_event,
                            token_budget_tracker=self._token_budget_tracker,
                        )
                    else:
                        result = f"Error: Cannot resume '{agent_type}' agent interactions."
                    await event_queue.put({"__done__": result})
                except InteractiveToolPause as e:
                    await event_queue.put({"__interactive_pause__": e})
                except Exception as e:
                    if is_permission_pause(e):
                        await event_queue.put({"__interactive_pause__": e})
                    else:
                        logger.error("Subagent resume failed: %s", e, exc_info=True)
                        await event_queue.put({"__error__": str(e)})

            resume_task = asyncio.create_task(_run_plan_resume())

            try:
                while True:
                    if self._cancel_event and self._cancel_event.is_set():
                        resume_task.cancel()
                        is_budget = (
                            self._token_budget_tracker
                            and self._token_budget_tracker.exceeded
                        )
                        final_text = (
                            "Agent stopped: token budget exceeded."
                            if is_budget else "Agent cancelled by user."
                        )
                        break

                    try:
                        item = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue

                    if "__done__" in item:
                        final_text = item["__done__"]
                        break
                    elif "__interactive_pause__" in item:
                        pause = item["__interactive_pause__"]
                        # Chain the carried spend: what was parked before this
                        # resume plus what the resumed subagent spent since.
                        # The main loop makes no LLM call while the agent call
                        # is pending, so the diff since resume start is the
                        # subagent's own post-resume spend.
                        now_usage = LLMLoopRunner.budget_tracker_usage(
                            self._token_budget_tracker,
                        )
                        pause.agent_usage_carry = {
                            key: int((state.get("agent_usage_carry") or {}).get(key) or 0)
                            + max(0, now_usage[key] - resume_start_usage[key])
                            for key in ("input", "output", "cached")
                        }
                        if is_permission_pause(pause):
                            # Subagent's tool hit another permission gate during
                            # resume. Re-checkpoint as a subagent interaction so
                            # the subagent resumes mid-loop again on next approval.
                            if not pause.parent_tool_call_id:
                                pause.parent_tool_call_id = parent_tool_call_id
                            async for event in self._emit_subagent_permission_pause(pause):
                                yield event
                            return
                        if not pause.parent_tool_call_id:
                            pause.parent_tool_call_id = parent_tool_call_id
                        if not pause.agent_session_id:
                            pause.agent_session_id = agent_session_id
                        if not pause.agent_type:
                            pause.agent_type = agent_type
                        pause.agent_usage_baseline = agent_usage_baseline
                        async for event in self._emit_subagent_pause(pause):
                            yield event
                        return
                    elif "__error__" in item:
                        final_text = f"Agent error: {item['__error__']}"
                        subagent_error = RuntimeError(final_text)
                        break
                    else:
                        # Forward subagent event wrapped in agent_event envelope
                        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
                            "parent_tool_call_id": parent_tool_call_id,
                            "agent_type": agent_type,
                            "inner_type": item.get("type", ""),
                            "inner_data": item.get("data", {}),
                        })
            finally:
                if not resume_task.done():
                    resume_task.cancel()
                    try:
                        await resume_task
                    except asyncio.CancelledError:
                        pass  # cleanup: subagent task cancellation
                    except Exception:
                        logger.debug("Subagent task cleanup raised non-cancelled error", exc_info=True)

        # ── Emit tool_end for the outer agent tool ──
        tool_result = final_text
        if len(tool_result) > MAX_TOOL_OUTPUT_CHARS:
            tool_result = tool_result[:MAX_TOOL_OUTPUT_CHARS] + "\n... [truncated]"

        yield LLMLoopRunner.sse(SSE_TOOL_END, {
            "tool": "agent",
            "result_summary": strip_image_refs_tag(tool_result)[:200],
            "tool_call_id": parent_tool_call_id,
        })

        # ── Append agent tool result to main messages and continue main loop ──
        messages = await self._build_messages()
        if not messages:
            yield await self._error_event("Failed to load messages")
            return

        # Check if agent tool result was already persisted
        existing = None
        for m in reversed(messages):
            if m.get("role") == "tool" and m.get("tool_call_id") == parent_tool_call_id:
                existing = m
                break

        if not existing:
            usage_delta = await self._agent_session_usage_delta(
                agent_session_id, agent_usage_baseline,
            )
            messages.append(LLMLoopRunner.msg(
                "tool", tool_result, tool_call_id=parent_tool_call_id,
                **LLMLoopRunner.usage_extra(usage_delta),
            ))
            await self._save_messages(messages)

        if subagent_error:
            content = LLMLoopRunner._format_error_message(subagent_error)
            messages.append(LLMLoopRunner.msg("assistant", content))
            await self._save_messages(messages)
            yield LLMLoopRunner.sse(SSE_ERROR, {
                "error": str(subagent_error),
                "content": content,
            })
            return

        ctx = self._build_loop_context()
        async for event in self._run_loop_guarded(ctx, messages):
            yield event

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    async def _build_messages(self) -> list[dict]:
        messages = []
        self._persisted_real_input_tokens = 0
        self._persisted_real_count_at_index = 0

        async with UnitOfWork(self.project_id) as uow:
            tips = await uow.config.get("tips", "")
            history = await uow.messages.get_messages_for_llm(self.session_id)

        from app.services.skill_service import skill_service
        skills_summary = skill_service.build_skills_prompt()
        from app.services.project_service import project_service
        project_meta = project_service.get_project_meta(self.project_id)
        session_temp_dir = session_temp_service.session_dir_for_prompt(
            self.project_id, self.session_id,
        )
        sys_prompt = prompt_service.build_system_prompt(
            project_id=self.project_id,
            working_dir=str(settings.get_project_path(self.project_id)),
            project_name=project_meta["name"],
            project_description=project_meta["description"],
            session_temp_dir=session_temp_dir,
            tips=tips,
            skills_summary=skills_summary,
        )

        messages.append(LLMLoopRunner.msg("system", sys_prompt))

        accepts_images = model_role_accepts_images(self.model_role)
        for msg in history:
            if msg.role == "assistant" and (msg.input_tokens or 0) > 0:
                self._persisted_real_input_tokens = msg.input_tokens
                self._persisted_real_count_at_index = len(messages)
            content = await self._content_for_model(msg.role, msg.content, accepts_images)
            entry = LLMLoopRunner.entry_from_history(msg, content)
            messages.append(entry)
            if msg.role == "tool" and accepts_images:
                messages.extend(await self._image_ref_messages(msg.content))

        return messages

    def _real_token_baseline(self, loop_ctx: LoopContext | None) -> tuple[int, int]:
        if loop_ctx and loop_ctx.last_real_input_tokens > 0:
            return loop_ctx.last_real_input_tokens, loop_ctx.last_real_count_at_index
        return self._persisted_real_input_tokens, self._persisted_real_count_at_index

    def _context_threshold_warning(self, stats) -> str:
        if stats.compact_threshold <= 0:
            return ""
        ratio = stats.current_tokens / stats.compact_threshold
        if ratio >= 0.9 and self._context_warning_level < 90:
            self._context_warning_level = 90
            return (
                "CRITICAL: The current context has reached 90% of the configured "
                "compaction threshold. At 100%, this session may be compacted and "
                "many details may be lost. If this task still needs substantial "
                "work and has important details that must persist, save them to "
                "the session temporary storage now, then continue the task. If "
                "there is nothing important to preserve, ignore this message."
            )
        if ratio >= 0.6 and self._context_warning_level < 60:
            self._context_warning_level = 60
            return (
                "WARNING: The current context has reached 60% of the configured "
                "compaction threshold. At 100%, this session may be compacted and "
                "many details may be lost. If this task still needs substantial "
                "work and has important details that must persist, save them to "
                "the session temporary storage, then continue the task. If the "
                "task is nearly complete or there is nothing important to "
                "preserve, ignore this message."
            )
        return ""

    async def _content_for_model(self, role: str, content: str, accepts_images: bool):
        if role == "tool":
            clean_content = strip_image_refs_tag(content)
            if accepts_images:
                return clean_content
            refs = extract_image_refs(content)
            if not refs:
                return clean_content
            status = self._format_image_ref_status(refs)
            return f"{clean_content}\n{status}" if clean_content else status

        if role != "user":
            return content
        attachments = extract_attachments(content)
        if not attachments:
            return strip_internal_image_tags(content)

        clean_content = strip_internal_image_tags(content)
        if not accepts_images:
            status = format_attachment_status(attachments)
            if not status:
                return clean_content
            return self._append_status(clean_content, status)

        parts = [{"type": "text", "text": clean_content}]
        for attachment in attachments:
            try:
                image_base64, media_type = await read_attachment_base64(
                    self.project_id, self.session_id, attachment["path"]
                )
            except Exception as exc:
                logger.debug(
                    "Failed to load image attachment %s",
                    attachment.get("path"),
                    exc_info=True,
                )
                parts[0]["text"] += (
                    f"\n\n<status>Unable to load image attachment "
                    f"{attachment.get('path')}: {exc}</status>"
                )
                continue
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
            })
        return parts

    async def _image_ref_messages(self, content: str) -> list[dict]:
        messages = []
        for image_ref in extract_image_refs(content):
            path = image_ref.get("path", "")
            try:
                image_base64, media_type = await read_image_path_base64(
                    self.project_id, path,
                )
            except Exception as exc:
                logger.debug("Failed to load image ref %s", path, exc_info=True)
                messages.append(LLMLoopRunner.msg(
                    "user",
                    f"<status>Unable to load image referenced by tool result {path}: {exc}</status>",
                    _ephemeral=True,
                ))
                continue
            text = image_ref.get("text") or f"Image referenced by tool result: {path}"
            messages.append(LLMLoopRunner.msg(
                "user",
                [
                    {"type": "text", "text": text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
                    },
                ],
                _ephemeral=True,
            ))
        return messages

    @staticmethod
    def _format_image_ref_status(image_refs: list[dict[str, Any]]) -> str:
        lines = [
            "Image file(s) referenced by previous tool results:",
            *[
                f"- {item['path']} ({item.get('mime_type') or 'image'})"
                for item in image_refs
                if item.get("path")
            ],
            "Use the vision_analyze tool with a specific prompt to inspect these images when needed.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _append_status(content: str, status_text: str) -> str:
        insert = f"\n{status_text}"
        marker = "</status>"
        if marker in content:
            return content.replace(marker, f"{insert}\n{marker}", 1)
        return f"<status>{status_text}</status>\n{content}"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _save_messages(self, messages: list[dict]) -> None:
        async def _operation(uow):
            history = await uow.messages.get_messages_for_llm(self.session_id)
            # Boundary rows count even though they are staged as system:
            # entry_from_history presents them to the LLM list as user, and
            # this count must use the same role view or the slice runs one
            # short and re-persists the newest history row.
            history_count = sum(
                1 for msg in history
                if getattr(msg, "is_boundary", False)
                or getattr(msg, "role", "") != "system"
            )
            candidates = [
                msg for msg in messages[1:]
                if not msg.get("_ephemeral") and msg.get("role") != "system"
            ]
            # Candidates already holding a checkpoint row id sit below the
            # slice (their rows are in history_count) but must be included so
            # the partial-assistant checkpoint refreshes them in place instead
            # of leaving stale content or inserting a duplicate.
            new_messages = [
                msg for idx, msg in enumerate(candidates)
                if idx >= history_count
                or msg.get(CHECKPOINT_ROW_ID) is not None
            ]

            await stage_new_messages(
                new_messages,
                partial(uow.messages.stage_create, session_id=self.session_id),
                update=uow.messages.stage_update_content,
            )

            await uow.sessions.stage_touch(self.session_id)

        await UnitOfWork.execute_atomic(self.project_id, _operation)

    async def _persist_error_message(self, error: Exception) -> str:
        """Persist a visible assistant error when failure happens outside the runner."""
        content = LLMLoopRunner._format_error_message(error)
        if not self.session_id:
            return content
        try:
            async def _operation(uow):
                await uow.messages.stage_create(
                    session_id=self.session_id,
                    role="assistant",
                    content=content,
                )
                await uow.sessions.stage_touch(self.session_id)

            await UnitOfWork.execute_atomic(self.project_id, _operation)
        except Exception:
            logger.debug("Failed to persist QueryLoop error message", exc_info=True)
        return content

    async def _error_event(self, error: Exception | str) -> dict:
        """Persist a visible assistant error, then return its SSE_ERROR event.

        Every terminal error must be persisted BEFORE it is streamed: the
        frontend writes error text into the live bubble only, so an
        unpersisted error vanishes on refresh and the failure leaves no
        trace behind.
        """
        text = str(error)
        content = await self._persist_error_message(
            error if isinstance(error, Exception) else RuntimeError(text),
        )
        return LLMLoopRunner.sse(SSE_ERROR, {"error": text, "content": content})
