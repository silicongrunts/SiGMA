"""
Agent Service — orchestrates subagent execution.

Implements the five agent execution modes:
- spawn general: independent context, persists session, returns resume_id
- spawn explore: independent context, read-only, no persistence
- spawn plan: persistent session, read-only + explore + submit_plan_for_approval
- fork: inherits parent loop context, runtime-forbidden tools, no persistence
- resume: resumes existing general agent session
"""

import asyncio
import json
from functools import partial
from typing import Callable, Awaitable, Any
from uuid import uuid4

from app.core.config import settings
from app.core.logging import get_logger
from app.agents.prompt_service import prompt_service
from app.agents.tool_schema_service import tool_schemas_for_model_role
from app.agents.toolsets import (
    READ_ONLY_TOOLS, PLAN_TOOLS, ALL_TOOLS_MINUS_AGENT,
    ALLOWED_AGENT_TYPES, FORK_FORBIDDEN_TOOLS,
)
from app.database.unit_of_work import UnitOfWork
from app.services.llm_loop_runner import (
    LLMLoopRunner, LoopContext, InteractiveToolPause,
    SSE_CONTEXT_STATS, SSE_COMPACT_START, SSE_COMPACT_DONE, SSE_ERROR,
)
from app.services.compaction_service import compaction_service
from app.services.message_persist import stage_new_messages
from app.services.project_service import project_service

logger = get_logger(__name__)


class AgentService:
    """Orchestrate subagent execution modes."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_agent_prompt(
        template: str, *, project_id: str, **extra: Any
    ) -> str:
        """Render an agent system-prompt template with project context.

        Injects ``working_dir``, ``project_id``, ``project_name`` and
        ``project_description``; callers may override any of them via
        ``extra``.
        """
        meta = project_service.get_project_meta(project_id)
        return prompt_service.render(
            template,
            project_id=project_id,
            working_dir=str(settings.get_project_path(project_id)),
            project_name=meta["name"],
            project_description=meta["description"],
            **extra,
        )

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    async def run_agent(
        self,
        *,
        agent_type: str,
        prompt: str,
        resume_id: str | None = None,
        project_id: str = "",
        parent_session_id: str = "",
        parent_tool_call_id: str = "",
        context_kind: str = "main",
        inherited_messages: list[dict] | None = None,
        emit_event: Callable[[dict], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
        token_budget_tracker=None,
        parent_model_role: str | None = None,
        parent_response_max_tokens: int | None = None,
        parent_tool_schemas: list[dict] | None = None,
    ) -> str:
        """Run a subagent and return the tool result string."""
        allowed_types = ALLOWED_AGENT_TYPES.get(context_kind, frozenset())
        if resume_id:
            if context_kind != "main":
                return "Error: Resume is not available in this context."
        else:
            if agent_type not in allowed_types:
                names = ", ".join(sorted(t for t in allowed_types if t))
                fork_note = " or empty for fork" if "" in allowed_types else ""
                return (
                    f"Error: Agent type '{agent_type}' is not available in this "
                    f"context. Available: {names}{fork_note}"
                )

        if resume_id:
            return await self._resume(
                resume_id=resume_id, prompt=prompt, project_id=project_id,
                emit_event=emit_event, cancel_event=cancel_event,
                token_budget_tracker=token_budget_tracker,
            )
        elif agent_type == "general":
            return await self._spawn_general(
                prompt=prompt, project_id=project_id,
                parent_session_id=parent_session_id,
                parent_tool_call_id=parent_tool_call_id,
                emit_event=emit_event, cancel_event=cancel_event,
                token_budget_tracker=token_budget_tracker,
            )
        elif agent_type == "explore":
            return await self._spawn_explore(
                prompt=prompt, project_id=project_id,
                emit_event=emit_event, cancel_event=cancel_event,
                token_budget_tracker=token_budget_tracker,
            )
        elif agent_type == "plan":
            return await self._spawn_plan(
                prompt=prompt, project_id=project_id,
                parent_session_id=parent_session_id,
                parent_tool_call_id=parent_tool_call_id,
                emit_event=emit_event, cancel_event=cancel_event,
                token_budget_tracker=token_budget_tracker,
            )
        else:
            return await self._fork(
                prompt=prompt, project_id=project_id,
                inherited_messages=inherited_messages or [],
                emit_event=emit_event, cancel_event=cancel_event,
                token_budget_tracker=token_budget_tracker,
                model_role=parent_model_role or "supervisor",
                response_max_tokens=parent_response_max_tokens,
                tool_schemas=parent_tool_schemas,
            )

    # ------------------------------------------------------------------
    # Plan agent resume (called by QueryLoop._resume_subagent_interaction)
    # ------------------------------------------------------------------

    async def resume_plan_from_interaction(
        self,
        *,
        project_id: str,
        agent_session_id: str,
        parent_tool_call_id: str,
        inner_tool_call_id: str,
        inner_tool_result: str,
        emit_event: Callable[[dict], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
        token_budget_tracker=None,
    ) -> str:
        """Resume a plan agent session after user responds to its interactive tool.

        Loads the plan agent's persisted messages, appends the inner tool result,
        and continues the plan agent's LLM loop.  Returns the final text.
        """
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.get_by_id(agent_session_id)
            if session is None:
                return "Error: Plan agent session not found."
            history = await uow.messages.get_messages_for_llm(agent_session_id)
        agent_usage_baseline = self._usage_total(history)

        system_prompt = self._render_agent_prompt(
            "agents/plan", project_id=project_id,
        )
        messages, persisted_token_baseline = self._messages_from_history(
            system_prompt, history,
        )

        # Append the inner tool result (approval/rejection)
        messages.append(LLMLoopRunner.msg(
            "tool", inner_tool_result, tool_call_id=inner_tool_call_id
        ))

        tool_schemas = tool_schemas_for_model_role("supervisor", PLAN_TOOLS)

        async def plan_execute_tool(tool_name, tool_args):
            if tool_name == "agent":
                sub_type = tool_args.get("agent_type", "")
                if tool_args.get("resume_id"):
                    return "Error: Resume is not available in plan agent context."
                if sub_type != "explore":
                    return f"Error: Plan agent can only spawn explore agents. Got '{sub_type}'."
                tool_args["project_id"] = project_id
                tool_args["context_kind"] = "plan"
            return await LLMLoopRunner.execute_tool_default(tool_name, tool_args)

        async def persist_fn(msgs):
            await self._persist_agent_messages(project_id, agent_session_id, msgs)

        ctx = LoopContext(
            project_id=project_id,
            session_id=agent_session_id,
            model_role="supervisor",
            is_agent_tool=True,
            context_kind="plan",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=PLAN_TOOLS,
            cancel_event=cancel_event,
            execute_tool=plan_execute_tool,
            persist_messages=persist_fn,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=agent_session_id,
                messages=msgs,
                model_role="supervisor",
                tools=tool_schemas,
                persistent=True,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
                persisted_token_baseline=persisted_token_baseline,
            ),
            token_budget_tracker=token_budget_tracker,
        )

        try:
            async for event in LLMLoopRunner().run(ctx, messages):
                if emit_event:
                    await emit_event(event)
                if event.get("type") == SSE_ERROR:
                    data = event.get("data") or {}
                    raise RuntimeError(str(data.get("error") or "Agent LLM failed"))
        except InteractiveToolPause as e:
            e.agent_session_id = agent_session_id
            e.agent_type = "plan"
            e.parent_tool_call_id = parent_tool_call_id
            if e.agent_usage_baseline is None:
                e.agent_usage_baseline = agent_usage_baseline
            raise

        return self._extract_final_text(messages)

    async def resume_general_from_interaction(
        self,
        *,
        project_id: str,
        agent_session_id: str,
        parent_tool_call_id: str,
        inner_tool_call_id: str,
        inner_tool_result: str,
        emit_event: Callable[[dict], Awaitable[None]] | None = None,
        cancel_event: asyncio.Event | None = None,
        token_budget_tracker=None,
    ) -> str:
        """Resume a general agent session after one of its interactive tools."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.get_agent_session(agent_session_id, agent_type="general")
            if session is None:
                return "Error: General agent session not found."
            history = await uow.messages.get_messages_for_llm(agent_session_id)
        agent_usage_baseline = self._usage_total(history)

        system_prompt = self._render_agent_prompt(
            "agents/general", project_id=project_id,
        )
        messages, persisted_token_baseline = self._messages_from_history(
            system_prompt, history,
        )

        messages.append(LLMLoopRunner.msg(
            "tool", inner_tool_result, tool_call_id=inner_tool_call_id,
        ))

        tool_schemas = tool_schemas_for_model_role("supervisor", ALL_TOOLS_MINUS_AGENT)

        async def persist_fn(msgs):
            await self._persist_agent_messages(project_id, agent_session_id, msgs)

        ctx = LoopContext(
            project_id=project_id,
            session_id=agent_session_id,
            model_role="supervisor",
            is_agent_tool=True,
            context_kind="main",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=ALL_TOOLS_MINUS_AGENT,
            cancel_event=cancel_event,
            persist_messages=persist_fn,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=agent_session_id,
                messages=msgs,
                model_role="supervisor",
                tools=tool_schemas,
                persistent=True,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
                persisted_token_baseline=persisted_token_baseline,
            ),
            execute_tool=self._make_permission_executor(),
            token_budget_tracker=token_budget_tracker,
        )

        try:
            await self._run_and_forward(ctx, messages, emit_event)
        except InteractiveToolPause as e:
            e.agent_session_id = agent_session_id
            e.agent_type = "general"
            e.parent_tool_call_id = parent_tool_call_id
            if e.agent_usage_baseline is None:
                e.agent_usage_baseline = agent_usage_baseline
            raise

        return self._extract_final_text(messages)

    # ------------------------------------------------------------------
    # Permission-aware tool executor
    # ------------------------------------------------------------------

    @staticmethod
    def _make_permission_executor():
        """Create a permission-aware tool executor for subagents.

        Delegates to the shared permission executor (single source of truth
        with QueryLoop). When a write/bash/notebook tool needs approval,
        ``execute_with_permission`` raises ``PermissionRequestPause``, which
        propagates up to the parent loop for checkpointing.
        """
        from app.services.permission_executor import execute_with_permission

        from app.agents.tools.registry import tool_registry

        async def _execute(tool_name, tool_args):
            tool_def = tool_registry.get(tool_name)
            return await execute_with_permission(
                tool_name, tool_args, tool_def,
                project_id=tool_args.get("project_id", ""),
            )

        return _execute

    # ------------------------------------------------------------------
    # Helper: iterate runner and forward events
    # ------------------------------------------------------------------

    @staticmethod
    async def _run_and_forward(
        ctx: LoopContext,
        messages: list[dict],
        emit_event: Callable[[dict], Awaitable[None]] | None = None,
    ):
        """Run the LLM loop, forwarding SSE events to emit_event."""
        async for event in LLMLoopRunner().run(ctx, messages):
            if emit_event:
                await emit_event(event)
            if event.get("type") == SSE_ERROR:
                data = event.get("data") or {}
                raise RuntimeError(str(data.get("error") or "Agent LLM failed"))

    # ------------------------------------------------------------------
    # Spawn General
    # ------------------------------------------------------------------

    async def _spawn_general(
        self, *, prompt, project_id, parent_session_id, parent_tool_call_id,
        emit_event, cancel_event,
        token_budget_tracker=None,
    ) -> str:
        """Spawn a general agent with independent context and persistent session."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.create_agent_session(
                project_id=project_id,
                agent_type="general",
                parent_session_id=parent_session_id,
                parent_tool_call_id=parent_tool_call_id,
            )
            agent_session_id = session.id
        agent_usage_baseline = {"input": 0, "output": 0, "cached": 0}

        system_prompt = self._render_agent_prompt(
            "agents/general", project_id=project_id,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        tool_schemas = tool_schemas_for_model_role("supervisor", ALL_TOOLS_MINUS_AGENT)

        async def persist_fn(msgs):
            await self._persist_agent_messages(project_id, agent_session_id, msgs)

        ctx = LoopContext(
            project_id=project_id,
            session_id=agent_session_id,
            model_role="supervisor",
            is_agent_tool=True,
            context_kind="main",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=ALL_TOOLS_MINUS_AGENT,
            cancel_event=cancel_event,
            persist_messages=persist_fn,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=agent_session_id,
                messages=msgs,
                model_role="supervisor",
                tools=tool_schemas,
                persistent=True,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
            ),
            execute_tool=self._make_permission_executor(),
            token_budget_tracker=token_budget_tracker,
        )

        try:
            await self._run_and_forward(ctx, messages, emit_event)
        except InteractiveToolPause as e:
            e.agent_session_id = agent_session_id
            e.agent_type = "general"
            if e.agent_usage_baseline is None:
                e.agent_usage_baseline = agent_usage_baseline
            raise
        final_text = self._extract_final_text(messages)
        return f"<resume_id>{agent_session_id}</resume_id>\n{final_text}"

    # ------------------------------------------------------------------
    # Spawn Explore
    # ------------------------------------------------------------------

    async def _spawn_explore(
        self, *, prompt, project_id,
        emit_event, cancel_event,
        token_budget_tracker=None,
    ) -> str:
        """Spawn an explore agent with read-only tools, no persistence."""
        system_prompt = self._render_agent_prompt(
            "agents/explore", project_id=project_id,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        tool_schemas = tool_schemas_for_model_role("ra", READ_ONLY_TOOLS)

        # Explore is a single-shot, non-persistent sub-loop with no session
        # row, but read-only tools (read/notebook_read) declare session_id as a
        # required positional param and use it as the read-state cache key. A
        # one-shot scope_id keeps each explore run's read-state isolated
        # without impersonating a real session.
        scope_id = f"agent:explore:{uuid4().hex}"

        ctx = LoopContext(
            project_id=project_id,
            session_id=scope_id,
            model_role="ra",
            is_agent_tool=True,
            context_kind="main",
            response_max_tokens=compaction_service.budget_for_role("ra").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=READ_ONLY_TOOLS,
            cancel_event=cancel_event,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=scope_id,
                messages=msgs,
                model_role="ra",
                tools=tool_schemas,
                persistent=False,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
            ),
            token_budget_tracker=token_budget_tracker,
        )

        await self._run_and_forward(ctx, messages, emit_event)
        return self._extract_final_text(messages)

    # ------------------------------------------------------------------
    # Spawn Plan
    # ------------------------------------------------------------------

    async def _spawn_plan(
        self, *, prompt, project_id, parent_session_id, parent_tool_call_id,
        emit_event, cancel_event,
        token_budget_tracker=None,
    ) -> str:
        """Spawn a plan agent with persistent session for recoverability."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.create_agent_session(
                project_id=project_id,
                agent_type="plan",
                parent_session_id=parent_session_id,
                parent_tool_call_id=parent_tool_call_id,
            )
            plan_session_id = session.id
        agent_usage_baseline = {"input": 0, "output": 0, "cached": 0}

        system_prompt = self._render_agent_prompt(
            "agents/plan", project_id=project_id,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        tool_schemas = tool_schemas_for_model_role("supervisor", PLAN_TOOLS)

        async def plan_execute_tool(tool_name, tool_args):
            if tool_name == "agent":
                sub_type = tool_args.get("agent_type", "")
                if tool_args.get("resume_id"):
                    return "Error: Resume is not available in plan agent context."
                if sub_type != "explore":
                    return f"Error: Plan agent can only spawn explore agents. Got '{sub_type}'."
                tool_args["project_id"] = project_id
                tool_args["context_kind"] = "plan"
            return await LLMLoopRunner.execute_tool_default(tool_name, tool_args)

        async def persist_fn(msgs):
            await self._persist_agent_messages(project_id, plan_session_id, msgs)

        ctx = LoopContext(
            project_id=project_id,
            session_id=plan_session_id,
            model_role="supervisor",
            is_agent_tool=True,
            context_kind="plan",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=PLAN_TOOLS,
            cancel_event=cancel_event,
            execute_tool=plan_execute_tool,
            persist_messages=persist_fn,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=plan_session_id,
                messages=msgs,
                model_role="supervisor",
                tools=tool_schemas,
                persistent=True,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
            ),
            token_budget_tracker=token_budget_tracker,
        )

        try:
            await self._run_and_forward(ctx, messages, emit_event)
        except InteractiveToolPause as e:
            # Enrich with plan agent context before propagating
            e.agent_session_id = plan_session_id
            e.agent_type = "plan"
            if e.agent_usage_baseline is None:
                e.agent_usage_baseline = agent_usage_baseline
            raise

        return self._extract_final_text(messages)

    # ------------------------------------------------------------------
    # Fork
    # ------------------------------------------------------------------

    async def _fork(
        self, *, prompt, project_id, inherited_messages,
        emit_event, cancel_event,
        token_budget_tracker=None,
        model_role: str = "supervisor",
        response_max_tokens: int | None = None,
        tool_schemas: list[dict] | None = None,
    ) -> str:
        """Fork: inherit parent loop messages without persisting child context."""
        messages = list(inherited_messages)
        start_index = len(messages)
        messages.extend(self._fork_task_messages(prompt))

        active_tool_schemas = (
            tool_schemas
            if tool_schemas is not None
            else tool_schemas_for_model_role(model_role)
        )
        active_response_max_tokens = (
            response_max_tokens
            if response_max_tokens is not None
            else compaction_service.budget_for_role(model_role).response_max_tokens
        )

        # Fork is a non-persistent sub-loop with no session row, yet it exposes
        # writable tools (write/edit) whose must-read-first contract keys on
        # session_id. A one-shot scope_id makes that contract behave correctly
        # within the fork (reads must precede edits) instead of crashing on the
        # missing positional argument and silently disabling the contract.
        scope_id = f"agent:fork:{uuid4().hex}"

        ctx = LoopContext(
            project_id=project_id,
            session_id=scope_id,
            model_role=model_role,
            is_agent_tool=True,
            context_kind="main",
            response_max_tokens=active_response_max_tokens,
            tool_schemas=active_tool_schemas,
            allowed_tools=None,
            forbidden_tools=FORK_FORBIDDEN_TOOLS,
            forbidden_tool_context="fork",
            cancel_event=cancel_event,
            execute_tool=self._make_permission_executor(),
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=scope_id,
                messages=msgs,
                model_role=model_role,
                tools=active_tool_schemas,
                persistent=False,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
            ),
            token_budget_tracker=token_budget_tracker,
        )

        await self._run_and_forward(ctx, messages, emit_event)
        final_start_index = min(start_index, max(0, len(messages) - 1))
        final_text = self._extract_final_text(messages, start_index=final_start_index)
        return final_text or "Error: Fork agent produced no final assistant response."

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def _resume(
        self, *, resume_id, prompt, project_id,
        emit_event, cancel_event,
        token_budget_tracker=None,
    ) -> str:
        """Resume an existing general agent session."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.get_agent_session(resume_id, agent_type="general")
            if session is None:
                any_session = await uow.sessions.get_by_id(resume_id)
                if any_session is None:
                    return f"Error: Agent session '{resume_id[:8]}...' not found."
                elif any_session.session_kind != "agent":
                    return f"Error: Session '{resume_id[:8]}...' is not an agent session."
                else:
                    return (
                        f"Error: Agent session '{resume_id[:8]}...' is a "
                        f"'{any_session.agent_type}' agent, not a general agent."
                    )
            agent_session_id = session.id
            history = await uow.messages.get_messages_for_llm(agent_session_id)
        agent_usage_baseline = self._usage_total(history)

        system_prompt = self._render_agent_prompt(
            "agents/general", project_id=project_id,
        )
        messages, persisted_token_baseline = self._messages_from_history(
            system_prompt, history,
        )

        messages.append({"role": "user", "content": prompt})

        tool_schemas = tool_schemas_for_model_role("supervisor", ALL_TOOLS_MINUS_AGENT)

        async def persist_fn(msgs):
            await self._persist_agent_messages(project_id, agent_session_id, msgs)

        ctx = LoopContext(
            project_id=project_id,
            session_id=agent_session_id,
            model_role="supervisor",
            is_agent_tool=True,
            context_kind="main",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=tool_schemas,
            allowed_tools=ALL_TOOLS_MINUS_AGENT,
            cancel_event=cancel_event,
            persist_messages=persist_fn,
            prepare_messages=lambda msgs: self._prepare_agent_messages(
                project_id=project_id,
                session_id=agent_session_id,
                messages=msgs,
                model_role="supervisor",
                tools=tool_schemas,
                persistent=True,
                token_budget_tracker=token_budget_tracker,
                loop_ctx=ctx,
                persisted_token_baseline=persisted_token_baseline,
            ),
            execute_tool=self._make_permission_executor(),
            token_budget_tracker=token_budget_tracker,
        )

        try:
            await self._run_and_forward(ctx, messages, emit_event)
        except InteractiveToolPause as e:
            e.agent_session_id = agent_session_id
            e.agent_type = "general"
            if e.agent_usage_baseline is None:
                e.agent_usage_baseline = agent_usage_baseline
            raise
        final_text = self._extract_final_text(messages)
        return f"<resume_id>{agent_session_id}</resume_id>\n{final_text}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fork_task_messages(prompt: str) -> list[dict]:
        forbidden_tools = ", ".join(sorted(FORK_FORBIDDEN_TOOLS))
        mode_message = prompt_service.render(
            "agents/fork_mode",
            forbidden_tools=forbidden_tools,
        )
        task_message = prompt_service.render(
            "agents/fork_task",
            prompt=prompt.strip(),
        )
        return [
            {"role": "user", "content": mode_message},
            {"role": "user", "content": task_message},
        ]

    @staticmethod
    def _extract_final_text(messages: list[dict], *, start_index: int = 0) -> str:
        for msg in reversed(messages[start_index:]):
            if (
                msg.get("role") == "assistant"
                and msg.get("content")
                and not msg.get("tool_calls")
            ):
                return msg["content"]
        return ""

    @staticmethod
    def _messages_from_history(
        system_prompt: str,
        history: list[Any],
    ) -> tuple[list[dict], dict[str, int]]:
        messages = [{"role": "system", "content": system_prompt}]
        baseline = {"input": 0, "index": 0}

        for msg in history:
            if msg.role == "assistant" and (msg.input_tokens or 0) > 0:
                baseline = {"input": msg.input_tokens, "index": len(messages)}
            entry = LLMLoopRunner.msg(msg.role, msg.content)
            if msg.tool_calls:
                try:
                    entry["tool_calls"] = json.loads(msg.tool_calls)
                except (json.JSONDecodeError, TypeError):
                    pass
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if getattr(msg, "reasoning_content", None):
                entry["reasoning_content"] = msg.reasoning_content
            messages.append(entry)

        return messages, baseline

    @staticmethod
    def _usage_total(messages: list[Any]) -> dict[str, int]:
        return {
            "input": sum(int(getattr(m, "input_tokens", 0) or 0) for m in messages),
            "output": sum(int(getattr(m, "token_count", 0) or 0) for m in messages),
            "cached": sum(int(getattr(m, "cached_tokens", 0) or 0) for m in messages),
        }

    async def _persist_agent_messages(
        self, project_id: str, session_id: str,
        messages: list[dict],
    ) -> None:
        """Persist new messages to an agent session."""
        async def _operation(uow):
            history_count = len(await uow.messages.get_messages_for_llm(session_id))
            new_messages = messages[1 + history_count:]

            await stage_new_messages(
                new_messages,
                partial(uow.messages.stage_create, session_id=session_id),
            )

            await uow.sessions.stage_touch(session_id)

        await UnitOfWork.execute_atomic(project_id, _operation)

    async def _prepare_agent_messages(
        self,
        *,
        project_id: str,
        session_id: str | None,
        messages: list[dict],
        model_role: str,
        tools: list[dict],
        persistent: bool,
        token_budget_tracker=None,
        loop_ctx: LoopContext | None = None,
        persisted_token_baseline: dict[str, int] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        last_real_input_tokens = loop_ctx.last_real_input_tokens if loop_ctx else 0
        last_real_count_at_index = loop_ctx.last_real_count_at_index if loop_ctx else 0
        if last_real_input_tokens <= 0 and persisted_token_baseline:
            last_real_input_tokens = persisted_token_baseline.get("input", 0)
            last_real_count_at_index = persisted_token_baseline.get("index", 0)
        stats = compaction_service.stats_for_messages_incremental(
            messages, model_role=model_role, tools=tools,
            last_real_input_tokens=last_real_input_tokens,
            last_real_count_at_index=last_real_count_at_index,
        )
        events = [LLMLoopRunner.sse(SSE_CONTEXT_STATS, stats.to_dict())]
        if stats.current_tokens <= stats.compact_threshold:
            return messages, events

        events.append(LLMLoopRunner.sse(SSE_COMPACT_START, {
            "message": "Session Compacting...",
            **stats.to_dict(),
        }))
        try:
            result = await compaction_service.compact_messages(
                messages,
                model_role=model_role,
                mode="passive",
                tools=tools,
                token_budget_tracker=token_budget_tracker,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Unable to compact this session: {exc}. "
                "Please create a new session and continue from there."
            ) from exc
        if persistent and session_id:
            async def _operation(uow):
                await compaction_service.stage_session_boundary(
                    uow, session_id, result.boundary_content,
                )
                await uow.sessions.stage_touch(session_id)

            await UnitOfWork.execute_atomic(project_id, _operation)

        # Compaction replaced the message list — invalidate cached real tokens.
        if loop_ctx:
            loop_ctx.last_real_input_tokens = 0
            loop_ctx.last_real_count_at_index = 0
        if persisted_token_baseline:
            persisted_token_baseline["input"] = 0
            persisted_token_baseline["index"] = 0

        events.append(LLMLoopRunner.sse(SSE_COMPACT_DONE, result.stats.to_dict()))
        events.append(LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict()))
        return result.messages, events


# Singleton
agent_service = AgentService()
