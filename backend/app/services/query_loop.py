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
from app.database.unit_of_work import UnitOfWork
from app.agents.prompt_service import prompt_service
from app.agents.tool_schema_service import tool_schemas_for_model_role
from app.agents.tools import tool_registry
from app.agents.tools.read_state import read_state_cache
from app.services.compaction_service import compaction_service
from app.services.chat_attachments import read_attachment_base64, read_image_path_base64
from app.services.session_temp_service import session_temp_service
from app.services.task_service import task_to_dict
from app.services.llm_loop_runner import (
    LLMLoopRunner, LoopContext, InteractiveToolPause,
    SSE_ERROR, SSE_DONE, SSE_TOOL_END, SSE_AGENT_EVENT,
    SSE_CONTEXT_STATS, SSE_COMPACT_START, SSE_COMPACT_DONE,
    MAX_TOOL_OUTPUT_CHARS,
)
from app.services.token_budget import extract_llm_usage
from app.services.message_persist import stage_new_messages

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

    @staticmethod
    def _resolve_model(role: str) -> str:
        return get_model_endpoint(role).litellm_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        """Run one turn of the query loop. Yields SSE event dicts."""
        messages = []
        try:
            # ── Resume from interaction checkpoint ──
            if self._interaction_response:
                async for event in self._resume_from_interaction():
                    yield event
                return

            # Build messages
            messages = await self._build_messages()
            if not messages:
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": "Failed to build messages"})
                return

            # Build loop context
            ctx = self._build_loop_context()
            async for event in LLMLoopRunner().run(ctx, messages):
                yield event

        except InteractiveToolPause as e:
            # Subagent hit an interactive tool — persist main messages first
            # (includes the assistant→agent tool_call), then save checkpoint.
            await self._save_messages(messages)
            async for event in self._emit_subagent_pause(e):
                yield event

        except Exception as e:
            # Subagent hit a permission gate — same checkpoint pattern as
            # InteractiveToolPause, but the interaction_data is a permission
            # payload (interaction_type="permission"). The checkpoint is saved
            # as a direct (non-subagent) interaction keyed on the agent tool,
            # so resume routes to _resume_from_permission.
            if type(e).__name__ == "PermissionRequestPause" and hasattr(e, "tool"):
                await self._save_messages(messages)
                async for event in self._emit_subagent_permission_pause(e):
                    yield event
            else:
                logger.error("QueryLoop error: %s", e, exc_info=True)
                content = await self._persist_error_message(e)
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": str(e), "content": content})

    async def compact_active(self) -> AsyncIterator[dict]:
        """Run an explicit user-requested session compaction and stop."""
        try:
            messages = await self._build_messages()
            if not messages:
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": "Failed to build messages"})
                yield LLMLoopRunner.sse(SSE_DONE, {})
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
            result = await compaction_service.compact_messages(
                messages,
                model_role=self.model_role,
                mode="active",
                tools=tools,
                token_budget_tracker=self._token_budget_tracker,
                session_id=self.session_id,
            )
            async def _operation(uow):
                await compaction_service.stage_session_boundary(
                    uow, self.session_id, result.boundary_content,
                )
                await uow.sessions.stage_touch(self.session_id)

            await UnitOfWork.execute_atomic(self.project_id, _operation)

            # Compaction collapses the conversation — must-read-first cache must
            # reset so the LLM is forced to re-read files before further edits.
            read_state_cache.clear(self.session_id)

            done_data = {}
            if result.usage:
                done_data["usage"] = extract_llm_usage(result.usage).to_dict()
            yield LLMLoopRunner.sse(SSE_COMPACT_DONE, result.stats.to_dict())
            yield LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict())
            yield LLMLoopRunner.sse(SSE_DONE, done_data)
        except Exception as e:
            logger.error("Active compact failed: %s", e, exc_info=True)
            yield LLMLoopRunner.sse(SSE_ERROR, {
                "error": (
                    f"Unable to compact this session: {e}. "
                    "Please create a new session and continue from there."
                )
            })
            yield LLMLoopRunner.sse(SSE_DONE, {})

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
            prepare_messages=self._prepare_messages,
            get_active_tasks=self._get_active_tasks,
            on_interactive_pause=self._on_interactive_pause,
            on_permission_pause=self._on_permission_pause,
            token_budget_tracker=self._token_budget_tracker,
        )
        self._loop_ctx = ctx
        return ctx

    # ------------------------------------------------------------------
    # Interactive tool pause hook (direct tools like ask_user_question)
    # ------------------------------------------------------------------

    async def _on_interactive_pause(
        self, *, tool_name, tool_args, tool_call_id, interaction_data,
    ) -> None:
        """Save interaction checkpoint for a direct interactive tool."""
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.mark_awaiting_input(
                self._task_id, {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call_id,
                    "interaction_data": interaction_data,
                }
            )

    async def _on_permission_pause(
        self, *, tool_name, tool_args, tool_call_id, interaction_data,
    ) -> None:
        """Save interaction checkpoint for a permission approval request."""
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.mark_awaiting_input(
                self._task_id, {
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_call_id": tool_call_id,
                    "interaction_data": interaction_data,
                }
            )

    # ------------------------------------------------------------------
    # Subagent checkpoint (when plan agent's interactive tool pauses)
    # ------------------------------------------------------------------

    async def _save_subagent_checkpoint(self, pause: InteractiveToolPause) -> None:
        """Save a rich checkpoint for subagent interaction resume."""
        # Mirror interaction_type/interaction_data to the top level so the
        # frontend restore path (getActive → active.interaction.interaction_type)
        # works uniformly for subagent checkpoints. The resume dispatch in
        # _resume_from_interaction checks is_subagent_interaction first, so the
        # redundant top-level interaction_data never causes a wrong branch.
        interaction = pause.interaction_data or {}
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.mark_awaiting_input(
                self._task_id, {
                    # Sentinel for resume dispatch
                    "is_subagent_interaction": True,
                    # Top-level interaction fields (for frontend restore only)
                    "interaction_type": interaction.get("interaction_type"),
                    "interaction_data": interaction,
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
                }
            )

    async def _emit_subagent_pause(self, pause: InteractiveToolPause) -> AsyncIterator[dict]:
        """Persist a subagent pause and emit the matching UI events."""
        await self._save_subagent_checkpoint(pause)
        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": pause.parent_tool_call_id,
            "agent_type": pause.agent_type,
            "inner_type": "awaiting_input",
            "inner_data": pause.interaction_data,
        })
        yield LLMLoopRunner.sse("awaiting_input", pause.interaction_data)
        done_data = {}
        usage = self._current_turn_usage()
        if usage:
            done_data["usage"] = usage
        yield LLMLoopRunner.sse(SSE_DONE, done_data)

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
            "tool": pause.tool,
            "tool_name": pause.tool_name,
            "path": pause.path,
            "operation": pause.operation,
            "content": pause.content,
            "description": pause.description,
            "diff_lines": pause.diff_lines,
            "diff_truncated": pause.diff_truncated,
        }

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
                        "inner_tool_name": pause.tool_name,
                        "inner_tool_args": getattr(pause, "tool_args", {}),
                    }
                )

        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": pause.parent_tool_call_id,
            "agent_type": getattr(pause, "agent_type", ""),
            "inner_type": "awaiting_input",
            "inner_data": interaction_data,
        })
        yield LLMLoopRunner.sse("awaiting_input", interaction_data)
        done_data = {}
        usage = self._current_turn_usage()
        if usage:
            done_data["usage"] = usage
        yield LLMLoopRunner.sse(SSE_DONE, done_data)

    def _current_turn_usage(self) -> dict | None:
        if not self._token_budget_tracker:
            return None
        usage = self._token_budget_tracker.usage
        return {
            "input": usage.input,
            "output": usage.output,
            "cached": usage.cached,
        }

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
                messages.append(LLMLoopRunner.msg(
                    "system",
                    warning_message,
                    _ephemeral=True,
                ))
            LLMLoopRunner.apply_cache_control(messages, target_offset=0)
            return messages, events

        events.append(LLMLoopRunner.sse(SSE_COMPACT_START, {
            "message": "Session Compacting...",
            **stats.to_dict(),
        }))
        try:
            result = await compaction_service.compact_messages(
                messages,
                model_role=self.model_role,
                mode="passive",
                tools=tool_schemas_for_model_role(self.model_role),
                token_budget_tracker=self._token_budget_tracker,
                session_id=self.session_id,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Unable to compact this session: {exc}. "
                "Please create a new session and continue from there."
            ) from exc
        async def _operation(uow):
            await compaction_service.stage_session_boundary(
                uow, self.session_id, result.boundary_content,
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

        events.append(LLMLoopRunner.sse(SSE_COMPACT_DONE, result.stats.to_dict()))
        events.append(LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict()))
        LLMLoopRunner.apply_cache_control(result.messages, target_offset=0)
        return result.messages, events

    async def _get_active_tasks(self, session_id: str) -> list:
        from app.services.task_service import task_to_dict
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
                state = await uow.task_state.get_pending_interaction_by_session(
                    self.session_id
                )
            if not state:
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": "No pending interaction found"})
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            # ── Subagent interaction: resume the plan agent ──
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

            # ── Normal (direct) interaction resume ──
            tool_name = state.get("tool_name", "")
            tool_args = state.get("tool_args", {})
            tool_def = tool_registry.get(tool_name)

            if not tool_def or not tool_def.requires_user_interaction:
                yield LLMLoopRunner.sse(SSE_ERROR, {
                    "error": f"Tool '{tool_name}' does not support interaction"
                })
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            messages = await self._build_messages()
            if not messages:
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": "Failed to load checkpoint"})
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            tool_call_id = state.get("tool_call_id", "")
            existing = None
            if tool_call_id:
                for m in reversed(messages):
                    if m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id:
                        existing = m
                        break

            if existing:
                tool_result = existing.get("content", "")
            else:
                merged_args = {**tool_args, **self._interaction_response}
                try:
                    result = await tool_def.call(**merged_args)
                    tool_result = str(result)
                except Exception as e:
                    logger.exception("Interactive tool response failed for %s", tool_name)
                    tool_result = f"Tool '{tool_name}' error processing response: {e}"

                if tool_call_id:
                    messages.append(LLMLoopRunner.msg(
                        "tool", tool_result, tool_call_id=tool_call_id
                    ))

            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.clear_interaction_by_session(self.session_id)

            yield LLMLoopRunner.sse(SSE_TOOL_END, {
                "tool": tool_name, "result_summary": strip_image_refs_tag(tool_result)[:200],
                "tool_call_id": tool_call_id,
            })

            ctx = self._build_loop_context()
            async for event in LLMLoopRunner().run(ctx, messages):
                yield event

        except Exception as e:
            logger.error("Resume from interaction error: %s", e, exc_info=True)
            yield LLMLoopRunner.sse(SSE_ERROR, {"error": str(e)})

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

        Subagent pauses from persistent agents (general/resume) never reach
        here — they are saved as ``is_subagent_interaction`` checkpoints and
        resumed via ``_resume_subagent_interaction``.
        """
        try:
            interaction_data = state.get("interaction_data", {})
            tool_name = state.get("tool_name", "")
            tool_args = state.get("tool_args", {})
            tool_call_id = state.get("tool_call_id", "")

            messages = await self._build_messages()
            if not messages:
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": "Failed to load checkpoint"})
                yield LLMLoopRunner.sse(SSE_DONE, {})
                return

            response = self._interaction_response or {}
            approved = response.get("approved", False)
            reason = response.get("reason", "")

            is_subagent = tool_name == "agent"

            if approved:
                if is_subagent:
                    # Fork agent — execute the approved inner tool directly
                    # and inject the result as the agent tool's result.
                    inner_tool_name = state.get("inner_tool_name", "")
                    inner_tool_args = state.get("inner_tool_args", {})
                    inner_tool_def = tool_registry.get(inner_tool_name)
                    if inner_tool_def and inner_tool_def.call:
                        call_args = dict(inner_tool_args)
                        if inner_tool_def.requires_project_id:
                            call_args.setdefault("project_id", self.project_id)
                        if inner_tool_def.requires_session_id and self.session_id:
                            call_args.setdefault("session_id", self.session_id)
                        if inner_tool_def.requires_model_role:
                            call_args.setdefault("model_role", self.model_role)
                        try:
                            tool_result = str(await LLMLoopRunner.call_tool(
                                inner_tool_def, call_args,
                            ))
                        except Exception as exc:
                            logger.exception(
                                "Permission-approved fork tool '%s' failed",
                                inner_tool_name,
                            )
                            tool_result = f"Tool '{inner_tool_name}' error: {exc}"
                    else:
                        tool_result = (
                            f"Error: Tool '{inner_tool_name}' is not available"
                        )
                else:
                    # Execute the tool directly, bypassing the permission gate
                    # — the user just explicitly approved this exact operation.
                    # Re-checking auto-approve via execute_with_permission would
                    # risk pausing again.
                    tool_def = tool_registry.get(tool_name)
                    if tool_def and tool_def.call:
                        # Inject context params the same way the main loop does
                        # (llm_loop_runner injects them before call_tool).
                        call_args = dict(tool_args)
                        if tool_def.requires_project_id:
                            call_args.setdefault("project_id", self.project_id)
                        if tool_def.requires_session_id and self.session_id:
                            call_args.setdefault("session_id", self.session_id)
                        if tool_def.requires_model_role:
                            call_args.setdefault("model_role", self.model_role)
                        try:
                            tool_result = await LLMLoopRunner.call_tool(
                                tool_def, call_args,
                            )
                        except Exception as exc:
                            logger.exception(
                                "Permission-approved tool '%s' failed",
                                tool_name,
                            )
                            tool_result = f"Tool '{tool_name}' error: {exc}"
                    else:
                        tool_result = f"Error: Tool '{tool_name}' is not available"
            else:
                tool_result = self._permission_denial_message(
                    interaction_data, reason,
                )

            if tool_call_id:
                messages.append(LLMLoopRunner.msg(
                    "tool", tool_result, tool_call_id=tool_call_id,
                ))

            # Clear the checkpoint before re-running the loop.
            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.clear_interaction_by_session(self.session_id)

            yield LLMLoopRunner.sse(SSE_TOOL_END, {
                "tool": tool_name,
                "result_summary": strip_image_refs_tag(tool_result)[:200],
                "tool_call_id": tool_call_id,
            })

            # A file-mutating tool was just executed on approval — emit
            # file_changed so the frontend refreshes the file tree, matching the
            # main loop's side-effect (llm_loop_runner emits it after call_tool).
            # Denials inject synthetic strings, not file ops, so skip those.
            if approved:
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
            async for event in LLMLoopRunner().run(ctx, messages):
                yield event

        except Exception as e:
            # A fork-agent sub-task hit another permission gate during the
            # re-run. Re-checkpoint it (don't lose the pause) instead of
            # treating it as an unrecoverable error.
            if type(e).__name__ == "PermissionRequestPause" and hasattr(e, "tool"):
                await self._save_messages(messages)
                async for event in self._emit_subagent_permission_pause(e):
                    yield event
            else:
                logger.error("Resume from permission error: %s", e, exc_info=True)
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": str(e)})

    @staticmethod
    def _permission_denial_message(interaction_data: dict, reason: str) -> str:
        """Build a category-appropriate denial string for the LLM."""
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
            denial += f". User says: {reason}"
        return denial

    # ------------------------------------------------------------------
    # Subagent interaction resume
    # ------------------------------------------------------------------

    async def _resume_subagent_interaction(self, state: dict) -> AsyncIterator[dict]:
        """Resume a subagent that paused for user interaction (e.g. plan approval)."""
        parent_tool_call_id = state.get("parent_tool_call_id", "")
        agent_session_id = state.get("agent_session_id", "")
        agent_type = state.get("agent_type", "")
        agent_usage_baseline = state.get("agent_usage_baseline") or {
            "input": 0, "output": 0, "cached": 0,
        }
        inner_tool_name = state.get("inner_tool_name", "")
        inner_tool_args = state.get("inner_tool_args", {})
        inner_tool_call_id = state.get("inner_tool_call_id", "")

        if not agent_session_id or not parent_tool_call_id:
            yield LLMLoopRunner.sse(SSE_ERROR, {
                "error": "Invalid subagent checkpoint"
            })
            yield LLMLoopRunner.sse(SSE_DONE, {})
            return

        # ── Phase 2: process the inner tool with the user's response ──
        inner_interaction_data = state.get("inner_interaction_data", {})
        is_permission = (
            inner_interaction_data.get("interaction_type") == "permission"
        )
        inner_tool_def = tool_registry.get(inner_tool_name)
        inner_result = ""

        if is_permission:
            # Permission approval/denial for a subagent's write/bash/notebook
            # tool. If approved, execute the tool directly (the user just
            # approved this exact operation — re-checking the permission gate
            # would pause again). If denied, inject a rejection string.
            response = self._interaction_response or {}
            approved = response.get("approved", False)
            reason = response.get("reason", "")
            if approved:
                if inner_tool_def and inner_tool_def.call:
                    call_args = dict(inner_tool_args or {})
                    if inner_tool_def.requires_project_id:
                        call_args.setdefault("project_id", self.project_id)
                    if inner_tool_def.requires_session_id:
                        call_args.setdefault("session_id", agent_session_id)
                    if inner_tool_def.requires_model_role:
                        call_args.setdefault("model_role", "supervisor")
                    try:
                        inner_result = str(
                            await LLMLoopRunner.call_tool(inner_tool_def, call_args)
                        )
                    except Exception as exc:
                        logger.exception(
                            "Permission-approved subagent tool '%s' failed",
                            inner_tool_name,
                        )
                        inner_result = f"Tool '{inner_tool_name}' error: {exc}"
                else:
                    inner_result = (
                        f"Error: Tool '{inner_tool_name}' is not available"
                    )
            else:
                inner_result = self._permission_denial_message(
                    inner_interaction_data, reason,
                )
        elif inner_tool_def and inner_tool_def.requires_user_interaction:
            merged = {**inner_tool_args, **(self._interaction_response or {})}
            if inner_tool_def.requires_project_id:
                merged["project_id"] = self.project_id
            if inner_tool_def.requires_session_id:
                merged["session_id"] = (
                    self.session_id
                    if inner_tool_name == "submit_plan_for_approval"
                    else agent_session_id
                )
            try:
                result = await inner_tool_def.call(**merged)
                inner_result = str(result)
            except Exception as e:
                logger.exception("Subagent interactive tool response failed for %s", inner_tool_name)
                inner_result = f"Error processing response: {e}"
        else:
            inner_result = str(self._interaction_response or "")

        # Emit tool_end for the inner tool
        yield LLMLoopRunner.sse(SSE_AGENT_EVENT, {
            "parent_tool_call_id": parent_tool_call_id,
            "agent_type": agent_type,
            "inner_type": SSE_TOOL_END,
            "inner_data": {
                "tool": inner_tool_name,
                "result_summary": inner_result[:200],
                "tool_call_id": inner_tool_call_id,
            },
        })

        # A permission-approved file-mutating tool was just executed directly —
        # emit file_changed so the frontend refreshes the file tree, matching the
        # main loop's side-effect. Denials inject synthetic strings, not file ops.
        if is_permission and approved:
            fc_evt = LLMLoopRunner._emit_file_changed(
                inner_tool_name, inner_tool_args, inner_result,
            )
            if fc_evt:
                yield fc_evt

        # The user's response has been consumed. Clear the old awaiting_input
        # checkpoint before continuing; a rejected plan may create a new one.
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.clear_interaction_by_session(self.session_id)

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
                    if type(e).__name__ == "PermissionRequestPause" and hasattr(e, "tool"):
                        await event_queue.put({"__interactive_pause__": e})
                    else:
                        logger.error("Plan agent resume failed: %s", e, exc_info=True)
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
                        if (
                            type(pause).__name__ == "PermissionRequestPause"
                            and hasattr(pause, "tool")
                        ):
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
            yield LLMLoopRunner.sse(SSE_ERROR, {"error": "Failed to load messages"})
            yield LLMLoopRunner.sse(SSE_DONE, {})
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
        async for event in LLMLoopRunner().run(ctx, messages):
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

        # agents_desc = self._describe_agents()
        # if agents_desc:
        #     sys_prompt += f"\n\n# Available Agents\n{agents_desc}"

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
            history_count = sum(
                1 for msg in history
                if getattr(msg, "role", "") != "system"
            )
            candidates = [
                msg for msg in messages[1:]
                if not msg.get("_ephemeral") and msg.get("role") != "system"
            ]
            new_messages = candidates[history_count:]

            await stage_new_messages(
                new_messages,
                partial(uow.messages.stage_create, session_id=self.session_id),
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _describe_agents(self) -> str:
        from app.agents.registry import agent_registry
        lines = []
        for a in agent_registry.list_all():
            lines.append(f"- **{a.name}**: {a.when_to_use} (Model: {a.model})")
        return "\n".join(lines)
