"""
AI Service — single entry point for AI / chat orchestration.

Uses QueryLoop for all AI interactions.
"""

import asyncio
import json
import re
from typing import AsyncGenerator, Dict, Any

from sqlalchemy.exc import OperationalError

from app.core.exceptions import TaskActiveError, SkillError, ValidationError
from app.core.logging import get_logger
from app.core.utils import generate_id, utcnow
from app.core.task_status import (
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLING,
    STATUS_CANCELLED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from app.core.chat_attachments import (
    render_attachments_tag,
    strip_internal_image_tags,
)
from app.services.query_loop import QueryLoop
from app.database.unit_of_work import UnitOfWork
from app.database.seq_utils import MAX_RETRIES, RETRY_DELAY
from app.services.task_service import task_to_dict
from app.services.token_budget import TokenBudgetTracker
from app.services.session_temp_service import session_temp_service

logger = get_logger(__name__)

# Patterns to strip internal blocks from user messages for UI / title generation
_STATUS_TAG_RE = re.compile(r'<status>.*?</status>\s*', re.DOTALL)
_CITATION_TAG_RE = re.compile(r'<citation>.*?</citation>\s*', re.DOTALL)

PLAN_STATUS_INSTRUCTION = (
    "<system>IMPORTANT: The user requires planning precede execution. You **must call the plan agent** to "
    "create an implementation plan before proceeding.</system>"
)


class AIService:
    """Central AI service — chat via QueryLoop."""

    async def _append_session_message(self, project_id: str, session_id: str, **message_fields):
        """Append one session message and touch the session atomically."""
        async def _operation(uow):
            message = await uow.messages.stage_create(
                session_id=session_id,
                **message_fields,
            )
            await uow.sessions.stage_touch(session_id)
            return message

        return await UnitOfWork.execute_atomic(project_id, _operation)

    # ==================================================================
    # 1. Session management
    # ==================================================================

    async def list_sessions(self, project_id: str, include_archived: bool = False) -> list[Dict]:
        """List sessions for a project."""
        async with UnitOfWork(project_id) as uow:
            sessions = await uow.sessions.list_by_project(project_id, include_archived=include_archived)
            return [s.to_dict() for s in sessions]

    async def create_session(self, project_id: str) -> Dict:
        """Create a new session. Returns the session dict."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.create(project_id)
            session_dict = session.to_dict()
        session_temp_service.ensure_session_dir(project_id, session_dict["id"])
        return session_dict

    async def update_session(self, project_id: str, session_id: str,
                             title: str = None, is_archived: bool = None) -> None:
        """Update session title or archive state."""
        fields = {}
        if title is not None:
            fields["title"] = title
        if is_archived is not None:
            fields["is_archived"] = is_archived
        if fields:
            async with UnitOfWork(project_id) as uow:
                await uow.sessions.update(session_id, **fields)

    async def delete_session(self, project_id: str, session_id: str) -> None:
        """Delete a session and all its children, plus associated task state."""
        async with UnitOfWork(project_id) as uow:
            await uow.sessions.delete(session_id)
            await uow.task_state.delete_by_session(session_id)
        try:
            session_temp_service.delete_session_dir(project_id, session_id)
        except Exception:
            logger.debug(
                "Failed to delete session temporary storage for session %s",
                session_id,
                exc_info=True,
            )

    async def generate_title(self, project_id: str, session_id: str) -> str:
        """Generate a title for a session based on its first exchange."""
        from app.services.llm_service import llm_service

        async with UnitOfWork(project_id) as uow:
            messages = await uow.messages.get_messages(session_id)
            user_msgs = [m for m in messages if m.role == "user"]
            assistant_msgs = [m for m in messages if m.role == "assistant"]

            if not user_msgs:
                return "Untitled"

            context_parts = []
            for um in user_msgs[:1]:
                context_parts.append(f"User: {self._strip_status(um.content)[:200]}")
            for am in assistant_msgs[:1]:
                context_parts.append(f"Assistant: {am.content[:200]}")

            context = "\n".join(context_parts)

            try:
                from app.agents.prompt_service import prompt_service
                title_system = prompt_service.render("tools/title_generator")
                result = await llm_service.call_json(
                    prompt=context,
                    system=title_system,
                    model_role="ra",
                )
                new_title = result.get("title", "").strip()[:50] if isinstance(result, dict) else ""
                if not new_title:
                    new_title = self._strip_status(user_msgs[0].content)[:50]
            except Exception:
                logger.debug("Failed to generate session title", exc_info=True)
                new_title = self._strip_status(user_msgs[0].content)[:50]

            await uow.sessions.update(session_id, title=new_title)
            return new_title

    async def load_skill_into_session(
        self, project_id: str, session_id: str, skill_id: str,
    ) -> Dict:
        """Inject a completed ``skill_load`` tool turn into the session.

        Persists a full turn — user command, assistant tool_call, tool result,
        and a short assistant confirmation — so the next LLM turn sees the
        skill content as context **without** an extra LLM round-trip. The
        shape mirrors what the LLM itself produces when it calls skill_load,
        so the existing history grouping and renderer handle it unchanged.

        Raises ``SkillError`` if the skill is missing or not enabled.
        """
        from app.services.skill_service import skill_service

        skills = skill_service.get_all_skills()
        match = next((s for s in skills if s["id"] == skill_id and s["enabled"]), None)
        if match is None:
            raise SkillError(f"Skill '{skill_id}' is not available or not enabled")

        content = skill_service.get_skill_content(skill_id)
        skill_name = match["name"] or skill_id
        tool_call_id = f"call_{generate_id()}"
        tool_calls_json = json.dumps([{
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": "skill_load",
                "arguments": json.dumps({"id": skill_id}),
            },
        }], ensure_ascii=False)
        confirmation = f'Skill "{skill_name}" loaded.'

        async def _operation(uow):
            await uow.messages.stage_create(
                session_id=session_id, role="user",
                content=f"/skill {skill_id}",
            )
            await uow.messages.stage_create(
                session_id=session_id, role="assistant",
                content="", tool_calls=tool_calls_json,
            )
            await uow.messages.stage_create(
                session_id=session_id, role="tool",
                content=content, tool_call_id=tool_call_id,
            )
            await uow.messages.stage_create(
                session_id=session_id, role="assistant",
                content=confirmation,
            )
            await uow.sessions.stage_touch(session_id)

        await UnitOfWork.execute_atomic(project_id, _operation)
        return {"skill_id": skill_id, "name": skill_name}

    async def get_session_messages(self, project_id: str, session_id: str) -> list[Dict]:
        """Get UI-shaped messages for a specific session (archive preview)."""
        from app.core.message_format import shape_messages_for_ui

        async with UnitOfWork(project_id) as uow:
            messages, boundary_seq = await uow.messages.get_messages_with_boundary(session_id)
        return shape_messages_for_ui(messages, boundary_seq)

    # ==================================================================
    # 2. Chat history
    # ==================================================================

    async def get_history(
        self,
        project_id: str,
        session_id: str = None,
        limit: int = 10,
        before_seq: int | None = None,
    ) -> Dict[str, Any]:
        """Get a cursor-paginated UI chat history page."""
        from app.core.message_format import page_ui_turns, shape_messages_for_ui

        async with UnitOfWork(project_id) as uow:
            if session_id is None:
                sessions = await uow.sessions.list_by_project(project_id)
                if sessions:
                    session_id = sessions[0].id
                else:
                    session = await uow.sessions.create(project_id)
                    session_id = session.id
                    session_temp_service.ensure_session_dir(project_id, session_id)
            messages, boundary_seq = await uow.messages.get_messages_with_boundary(session_id)

        entries = shape_messages_for_ui(messages, boundary_seq)
        return page_ui_turns(entries, limit=limit, before_seq=before_seq)

    async def clear_history(self, project_id: str, session_id: str = None) -> Dict[str, Any]:
        """Clear chat history for a session. Falls back to most recent session if session_id is None."""
        async with UnitOfWork(project_id) as uow:
            if session_id is None:
                sessions = await uow.sessions.list_by_project(project_id)
                if sessions:
                    session_id = sessions[0].id
                else:
                    return {"success": True, "message": "No sessions to clear"}
            await uow.messages.delete_by_session(session_id)
            return {"success": True, "message": "History cleared"}

    # ==================================================================
    # 3. Task submission
    # ==================================================================

    async def submit_chat(
        self,
        project_id: str,
        message: str,
        context: Dict[str, Any],
        session_id: str = None,
        resume: bool = False,
        interaction_response: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Validate, check for active tasks, persist user message, submit Huey task."""
        from app.workers.huey_tasks import run_llm_chat

        task_id = generate_id()
        compact_only = message.strip() in {"/compact", "/compress"}

        if not resume:
            async with UnitOfWork(project_id) as uow:
                existing = await uow.task_state.get_active_by_session(session_id)
                if existing:
                    liveness = await uow.task_state.check_liveness(existing["task_id"])
                    if liveness in (STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING):
                        raise TaskActiveError(task_id=existing["task_id"])

        # Resolve or create session before persisting messages
        if session_id:
            async with UnitOfWork(project_id) as uow:
                db_session = await uow.sessions.get_by_id(session_id)
                if db_session is None:
                    session_id = None
            if session_id is None:
                async with UnitOfWork(project_id) as uow:
                    db_session = await uow.sessions.create(project_id)
                    session_id = db_session.id

        # Persist user message immediately — survives refresh/worker crash.
        # /compact is a command, not conversation content.
        if message.strip() and session_id and not compact_only:
            full_content = self._build_user_message_content(message, context)
            await self._append_session_message(
                project_id,
                session_id,
                role="user",
                content=full_content,
            )

        # Persist interactive tool response immediately as a tool result.
        # Without this the user's feedback / answers are only visible after
        # the Huey worker picks up the task and runs _resume_from_interaction.
        if interaction_response and session_id:
            await self._persist_interaction_result(
                project_id, session_id, interaction_response
            )

        if not session_id:
            raise ValidationError("session_id is required for llm_chat tasks")
        async with UnitOfWork(project_id) as uow:
            await uow.task_state.set_queued(
                task_id,
                task_type="llm_chat",
                session_id=session_id,
                owner_type="chat_session",
                owner_id=session_id,
            )

        task_context = dict(context)
        if compact_only:
            task_context["compact_only"] = True

        run_llm_chat(
            task_id=task_id,
            project_id=project_id,
            context=task_context,
            session_id=session_id,
            interaction_response=interaction_response,
        )
        return {"task_id": task_id}

    async def edit_and_submit_chat(
        self,
        *,
        project_id: str,
        session_id: str,
        message_id: str,
        message: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Replace a boundary-local user message and submit a fresh turn."""
        from app.workers.huey_tasks import run_llm_chat

        async with UnitOfWork(project_id) as uow:
            existing = await uow.task_state.get_active_by_session(session_id)
            if existing:
                liveness = await uow.task_state.check_liveness(existing["task_id"])
                if liveness in (
                    STATUS_QUEUED, STATUS_RUNNING,
                    STATUS_AWAITING_INPUT, STATUS_CANCELLING,
                ):
                    raise TaskActiveError(task_id=existing["task_id"])

        full_content = self._build_user_message_content(message, context)
        task_id = generate_id()

        async with UnitOfWork(project_id) as uow:
            target = await uow.messages.get_by_id(message_id)
            if (
                target is None
                or target.session_id != session_id
                or target.role != "user"
            ):
                from app.core.exceptions import ValidationError
                raise ValidationError("Only user messages in this session can be edited")

            boundary_seq = await uow.messages.get_last_boundary_seq(session_id)
            if boundary_seq is not None and target.seq <= boundary_seq:
                from app.core.exceptions import ValidationError
                raise ValidationError("Compressed messages cannot be edited")

            target_seq = target.seq

        async def _operation(uow):
            await uow.messages.stage_truncate_from(session_id, target_seq)
            await uow.messages.stage_create(
                session_id=session_id,
                role="user",
                content=full_content,
            )
            await uow.sessions.stage_touch(session_id)

        await UnitOfWork.execute_atomic(project_id, _operation)

        async with UnitOfWork(project_id) as uow:
            await uow.task_state.delete_by_session(session_id)
            await uow.task_state.set_queued(
                task_id,
                task_type="llm_chat",
                session_id=session_id,
                owner_type="chat_session",
                owner_id=session_id,
            )
        run_llm_chat(
            task_id=task_id,
            project_id=project_id,
            context=dict(context),
            session_id=session_id,
            interaction_response=None,
        )
        return {"task_id": task_id}

    async def get_active_task(self, project_id: str, session_id: str = None) -> Dict[str, Any]:
        """Check for active task without hiding stale worker failures."""
        try:
            async with UnitOfWork(project_id) as uow:
                active = await uow.task_state.get_active_by_session(session_id)
                if not active:
                    return {"active": False, "task_id": None, "status": None}
                liveness = await uow.task_state.check_liveness(active["task_id"])

            if liveness == "stale":
                return {
                    "active": False,
                    "task_id": active["task_id"],
                    "status": "stale",
                    "task_type": active.get("task_type"),
                    "session_id": active.get("session_id"),
                    "recoverable": True,
                    "message": (
                        "The previous task stopped sending heartbeats before "
                        "it produced a final response."
                    ),
                }

            result = {
                "active": liveness in (
                    STATUS_QUEUED, STATUS_RUNNING,
                    STATUS_AWAITING_INPUT, STATUS_CANCELLING,
                ),
                "task_id": active["task_id"],
                "status": liveness,
                "task_type": active.get("task_type"),
                "session_id": active.get("session_id"),
            }
            # Include interaction data so frontend can restore modal on page reload
            if liveness == STATUS_AWAITING_INPUT:
                interaction = active.get("interaction_state")
                if interaction:
                    interaction["task_id"] = active["task_id"]
                result["interaction"] = interaction
            return result
        except Exception:
            logger.debug("Failed to read active chat task for session %s", session_id, exc_info=True)
            return {
                "active": False,
                "task_id": None,
                "status": "unknown",
                "session_id": session_id,
                "recoverable": True,
                "message": "Could not confirm whether a task is still active.",
            }

    async def get_tasks(self, project_id: str, session_id: str) -> list[Dict]:
        """Get all active (non-deleted) tasks for a session."""
        async with UnitOfWork(project_id) as uow:
            tasks = await uow.tasks.list_active(session_id)
            return [task_to_dict(t) for t in tasks]

    async def get_context_stats(self, project_id: str, session_id: str) -> dict:
        """Return the current estimated LLM context size for a chat session."""
        query_loop = QueryLoop(project_id=project_id, session_id=session_id)
        return await query_loop.context_stats()

    async def cancel_task(self, project_id: str, task_id: str) -> dict:
        """Cancel a task truthfully and durably.

        Records the cancel intent in the database — the source of truth the
        worker polls at startup and on every heartbeat — and additionally prods
        the worker through the StreamServer TCP signal as a best-effort fast
        path. Returns the task's effective status so the caller can report
        honestly. A TCP failure never masks the database truth.
        """
        from app.workers.stream_server import stream_server

        status = await self._request_cancel_with_retry(project_id, task_id)

        try:
            await stream_server.cancel_task(task_id)
        except Exception:
            logger.warning("Cancel signal failed for task %s", task_id, exc_info=True)

        return {
            "cancelled": status in (STATUS_CANCELLING, STATUS_CANCELLED),
            "status": status,
            "task_id": task_id,
        }

    async def _request_cancel_with_retry(self, project_id: str, task_id: str) -> str:
        """Record cancel intent, retrying on a transient database lock.

        ``request_cancel`` is a compare-and-swap and therefore idempotent, so
        retrying the whole sequence on a locked-DB ``OperationalError`` is safe.
        This avoids surfacing a 500 when the worker and the web process contend
        on SQLite's single writer, which the durable-cancel design relies on.
        """
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(project_id) as uow:
                    return await uow.task_state.request_cancel(task_id)
            except OperationalError:
                if attempt >= MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    # ==================================================================
    # 4. Streaming — main chat (via QueryLoop)
    # ==================================================================

    async def stream_chat(
        self,
        project_id: str,
        context: Dict[str, Any],
        session_id: str = None,
        interaction_response: Dict[str, Any] = None,
        task_id: str = "",
        cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncGenerator[str, None]:
        """Stream chat via QueryLoop. Yields SSE-formatted strings.

        Thin delegate over ``services.chat_executor.stream_chat_for_task``
        so the worker layer can execute a chat turn without importing
        this service (which would create a cycle, since this service
        enqueues the worker).
        """
        from app.services.chat_executor import stream_chat_for_task

        async for chunk in stream_chat_for_task(
            project_id=project_id,
            context=context,
            session_id=session_id,
            interaction_response=interaction_response,
            task_id=task_id,
            cancel_event=cancel_event,
        ):
            yield chunk

    def _build_user_message_content(self, message: str, context: Dict[str, Any]) -> str:
        """Build hidden status/citation blocks plus user-visible content."""
        cleaned_message, plan_requested = self._strip_slash_command(message)

        def _format_value(v):
            if isinstance(v, dict):
                inner = ", ".join(f"{k}: {_format_value(v2)}" for k, v2 in v.items())
                return f"({inner})"
            if isinstance(v, list):
                inner = ", ".join(_format_value(i) for i in v)
                return f"[{inner}]"
            if isinstance(v, str):
                return v
            return str(v)

        attachments = [
            item for item in (context.get("attachments") or [])
            if isinstance(item, dict)
        ]
        status_lines = [f"current_time: {utcnow().strftime('%Y-%m-%d %H:%M:%S')}"]
        if plan_requested:
            status_lines.append(PLAN_STATUS_INSTRUCTION)
        user_state = context.get("user_state")
        citation_text = ""
        if user_state:
            for k, v in user_state.items():
                if k == "citation":
                    citation_text = v if isinstance(v, str) else str(v)
                else:
                    status_lines.append(f"{k}: {_format_value(v)}")
        status_block = "<status>\n" + "\n".join(status_lines) + "\n</status>"
        citation_block = f"\n<citation>{citation_text}</citation>" if citation_text else ""
        attachments_block = render_attachments_tag(attachments)
        return f"{status_block}{citation_block}{attachments_block}\n{cleaned_message}"

    @staticmethod
    def _strip_slash_command(message: str) -> tuple[str, bool]:
        stripped = message.strip()
        if not stripped.startswith("/plan"):
            return message, False
        if len(stripped) > 5 and not stripped[5].isspace():
            return message, False
        cleaned = stripped[5:].lstrip()
        return cleaned or "Create a plan for the current task.", True

    # ==================================================================
    # 5. SSE listening
    # ==================================================================

    async def sse_listen(
        self, task_id: str, project_id: str = None
    ) -> AsyncGenerator[str, None]:
        """Subscribe to the SSE stream for a task.

        ``project_id`` lets the subscriber poll task liveness while waiting for
        the worker to connect, so a task cancelled during the connect window is
        surfaced promptly instead of blocking until the connect timeout. It may
        be omitted when the caller cannot supply it (the subscriber then falls
        back to the connect timeout).
        """
        from app.workers.stream_server import stream_server

        # Yield task_id as the first event so the frontend can cancel the task
        yield self._format_sse("task_id", {"task_id": task_id})

        try:
            async for event in stream_server.subscribe(
                task_id, timeout=1800, project_id=project_id
            ):
                yield event
        except Exception:
            logger.warning("Stream subscription lost for task %s", task_id, exc_info=True)
            yield self._format_sse("error", {"error": "Stream connection lost"})

    # ==================================================================
    # 6. Internal helpers
    # ==================================================================

    async def _persist_interaction_result(self, project_id: str, session_id: str,
                                          interaction_response: dict) -> None:
        """Persist interactive-tool response as a tool-result message immediately.

        Called from *submit_chat* so the user's feedback/answers are visible in
        chat history right away — no waiting for the Huey worker.

        Uses the tool registry to produce the result text, same as QueryLoop's
        _resume_from_interaction. The worker will detect this pre-existing
        result and skip re-creating it.
        """
        from app.agents.tools.registry import tool_registry

        async with UnitOfWork(project_id) as uow:
            state = await uow.task_state.get_pending_interaction_by_session(session_id)
        if not state:
            return

        # Subagent interactions are resumed and persisted by QueryLoop's
        # _resume_subagent_interaction path — never via this direct persist.
        if state.get("is_subagent_interaction"):
            return

        # Permission approvals are handled by QueryLoop._resume_from_permission,
        # which executes the approved tool (or injects a denial string) inside
        # the worker. The user's response is not a tool-call argument.
        interaction_data = state.get("interaction_data", {})
        if interaction_data.get("interaction_type") == "permission":
            return

        tool_name = state.get("tool_name", "")
        tool_call_id = state.get("tool_call_id", "")
        tool_def = tool_registry.get(tool_name)
        if not tool_def or not tool_call_id:
            return

        merged_args = {**state.get("tool_args", {}), **interaction_response}
        try:
            result = await tool_def.call(**merged_args)
        except Exception:
            logger.exception("Failed to persist interaction result for tool %s", tool_name)
            return

        await self._append_session_message(
            project_id,
            session_id,
            role="tool",
            content=str(result),
            tool_call_id=tool_call_id,
        )

    @staticmethod
    def _strip_status(text: str) -> str:
        """Remove <status>...</status> and <citation>...</citation> blocks from user message content."""
        return strip_internal_image_tags(_CITATION_TAG_RE.sub('', _STATUS_TAG_RE.sub('', text))).strip()

    @staticmethod
    def _format_sse(event: str, data: dict) -> str:
        """Format an SSE event string."""
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# Singleton
ai_service = AIService()
