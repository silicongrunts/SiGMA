"""
AnnotationLoop — LLM interaction loop for annotation AI replies.

Delegates the LLM ↔ tool loop to LLMLoopRunner and handles:
- Message building with annotation context
- Diff validation on final response
- Restricted tool set (whitelist enforced)
- Agent(explore-only) restriction
"""

from functools import partial
from typing import AsyncIterator

from app.core.logging import get_logger
from app.agents.prompt_service import prompt_service
from app.agents.tool_schema_service import tool_schemas_for_model_role
from app.agents.tools.annotation_tools import validate_diffs
from app.agents.tools.read_state import read_state_cache
from app.agents.toolsets import ANNOTATION_TOOLS, ALLOWED_AGENT_TYPES
from app.services.file_service import file_service
from app.database.unit_of_work import UnitOfWork
from app.services.annotation_service import serialize_annotation
from app.services.llm_loop_runner import (
    LLMLoopRunner, LoopContext,
    SSE_ERROR,
    SSE_CONTEXT_STATS, SSE_COMPACT_START, SSE_COMPACT_DONE,
)
from app.services.compaction_service import compaction_service
from app.services.token_budget import TokenBudgetTracker
from app.services.message_persist import stage_new_messages

logger = get_logger(__name__)


class AnnotationLoop:
    """LLM interaction loop for a single annotation AI reply."""

    def __init__(
        self,
        project_id: str,
        file_path: str,
        annotation_id: str,
        cancel_event: "asyncio.Event | None" = None,
    ):
        self.project_id = project_id
        self.file_path = file_path
        self.annotation_id = annotation_id
        self._cancel_event = cancel_event
        self._token_budget_tracker = TokenBudgetTracker()

        self._tool_schemas = tool_schemas_for_model_role("supervisor", ANNOTATION_TOOLS)

    async def run(self) -> AsyncIterator[dict]:
        """Run one annotation AI reply turn. Yields SSE event dicts."""
        try:
            async with UnitOfWork(self.project_id) as uow:
                annotation, err = await uow.annotations.resolve(self.annotation_id)
                if err:
                    yield LLMLoopRunner.sse(SSE_ERROR, {
                        "error": err,
                        "content": LLMLoopRunner._format_error_message(RuntimeError(err)),
                    })
                    return
                annotation_data = serialize_annotation(annotation)

            messages = await self._build_messages(annotation_data)
            if not messages:
                error = RuntimeError("Failed to build messages")
                content = await self._persist_error_message(error)
                yield LLMLoopRunner.sse(SSE_ERROR, {"error": str(error), "content": content})
                return

            ctx = self._build_loop_context()
            async for event in LLMLoopRunner().run(ctx, messages):
                yield event

        except Exception as e:
            logger.error("AnnotationLoop error: %s", e, exc_info=True)
            content = await self._persist_error_message(e)
            yield LLMLoopRunner.sse(SSE_ERROR, {"error": str(e), "content": content})

    def _build_loop_context(self) -> LoopContext:
        """Build LoopContext with annotation-specific restrictions."""
        ctx = LoopContext(
            project_id=self.project_id,
            # Annotations have no session row (messages live under annotation_id),
            # but tools like read/notebook_read declare session_id as a required
            # positional param and use it as the read-state cache key. A None
            # session_id skips injection (llm_loop_runner) and the call raises
            # "missing 1 required positional argument: 'session_id'". We pass a
            # stable, annotation-scoped namespace key so read-state is isolated
            # per annotation without impersonating a real session.
            session_id=f"annotation:{self.annotation_id}",
            model_role="supervisor",
            context_kind="annotation",
            response_max_tokens=compaction_service.budget_for_role("supervisor").response_max_tokens,
            tool_schemas=self._tool_schemas,
            allowed_tools=ANNOTATION_TOOLS,
            cancel_event=self._cancel_event,
            execute_tool=self._execute_tool_with_agent_check,
            persist_messages=self._save_messages,
            prepare_messages=self._prepare_messages,
            validate_final_response=self._validate_diff_response,
            token_budget_tracker=self._token_budget_tracker,
        )
        self._loop_ctx = ctx
        return ctx

    async def _prepare_messages(self, messages: list[dict]) -> tuple[list[dict], list[dict]]:
        loop_ctx = getattr(self, '_loop_ctx', None)
        stats = compaction_service.stats_for_messages_incremental(
            messages,
            model_role="supervisor",
            tools=self._tool_schemas,
            last_real_input_tokens=loop_ctx.last_real_input_tokens if loop_ctx else 0,
            last_real_count_at_index=loop_ctx.last_real_count_at_index if loop_ctx else 0,
        )
        events = [LLMLoopRunner.sse(SSE_CONTEXT_STATS, stats.to_dict())]
        if stats.current_tokens <= stats.compact_threshold:
            LLMLoopRunner.apply_cache_control(messages, target_offset=1)
            return messages, events

        events.append(LLMLoopRunner.sse(SSE_COMPACT_START, {
            "message": "Session Compacting...",
            **stats.to_dict(),
        }))
        try:
            result = await compaction_service.compact_messages(
                messages,
                model_role="supervisor",
                mode="passive",
                tools=self._tool_schemas,
                token_budget_tracker=self._token_budget_tracker,
                session_id=f"annotation:{self.annotation_id}",
            )
        except Exception as exc:
            raise RuntimeError(
                f"Unable to compact this annotation thread: {exc}. "
                "Please create a new session and continue from there."
            ) from exc
        async def _operation(uow):
            await compaction_service.insert_annotation_boundary(
                uow, self.annotation_id, result.boundary_content,
            )

        await UnitOfWork.execute_atomic(self.project_id, _operation)

        # Mirror query_loop: compaction collapses the visible context, so the
        # must-read-first cache must reset — the LLM must re-read a file before
        # editing it again (prior tool output is no longer in context).
        read_state_cache.clear(f"annotation:{self.annotation_id}")

        # Compaction replaced the message list — invalidate cached real tokens.
        if loop_ctx:
            loop_ctx.last_real_input_tokens = 0
            loop_ctx.last_real_count_at_index = 0

        events.append(LLMLoopRunner.sse(SSE_COMPACT_DONE, result.stats.to_dict()))
        events.append(LLMLoopRunner.sse(SSE_CONTEXT_STATS, result.stats.to_dict()))
        LLMLoopRunner.apply_cache_control(result.messages, target_offset=1)
        return result.messages, events

    # ------------------------------------------------------------------
    # Tool execution with Agent(explore-only) enforcement
    # ------------------------------------------------------------------

    async def _execute_tool_with_agent_check(
        self, tool_name: str, tool_args: dict
    ) -> str:
        """Execute tool with Agent restriction for annotation context."""
        if tool_name == "agent":
            agent_type = tool_args.get("agent_type", "")
            resume_id = tool_args.get("resume_id", "")
            allowed = ALLOWED_AGENT_TYPES.get("annotation", frozenset())

            if resume_id:
                return "Error: Resume is not available in annotation context."
            if agent_type not in allowed:
                names = ", ".join(sorted(allowed))
                return (
                    f"Error: Only '{names}' agent(s) are available in "
                    f"annotation context. Got '{agent_type}'."
                )

        return await LLMLoopRunner.execute_tool_default(tool_name, tool_args)

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    async def _build_messages(self, annotation_data: dict) -> list[dict]:
        """Build the full message list for the LLM: system prompt + DB history."""
        system_prompt = prompt_service.render("tools/annotation_loop_system")
        status_block = (
            f"<status>\n"
            f"Current project ID: {self.project_id}\n"
            f"Currently editing file: {self.file_path}\n"
            f"</status>"
        )
        content = f"{system_prompt}\n\n{status_block}"

        async with UnitOfWork(self.project_id) as uow:
            tips = await uow.config.get("tips", "")
            history = await uow.messages.get_messages_for_annotation_llm(self.annotation_id)

        if tips and tips.strip():
            content += f"\n\n{prompt_service._format_tips(tips)}"

        from app.services.skill_service import skill_service
        skills_summary = skill_service.build_skills_prompt()
        if skills_summary:
            content += f"\n\n{skills_summary}"

        messages = [{"role": "system", "content": content}]

        for msg in history:
            entry = LLMLoopRunner.entry_from_history(msg, msg.content)
            messages.append(entry)

        # Read file content for context injection
        try:
            file_content = await file_service.read_file(self.project_id, self.file_path)
        except Exception:
            logger.debug("Failed to read annotation file context %s", self.file_path, exc_info=True)
            file_content = ""

        # Inject context block into the last user message
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                context_block = self._build_context_block(annotation_data, file_content)
                messages[i]["content"] = f"{context_block}\n{messages[i]['content']}"
                break

        return messages

    def _build_context_block(self, annotation_data: dict, file_content: str) -> str:
        """Build the <status><content>...</content></status> block."""
        from_pos = annotation_data.get("from", 0)
        to_pos = annotation_data.get("to", 0)
        original_text = annotation_data.get("originalText", "")

        margin = 500
        ctx_start = max(0, from_pos - margin)
        ctx_end = min(len(file_content), to_pos + margin)
        before = file_content[ctx_start:from_pos]
        after = file_content[to_pos:ctx_end]

        return (
            f"<status><content>"
            f"{_escape_xml(before)}"
            f"<annotation>{_escape_xml(original_text)}</annotation>"
            f"{_escape_xml(after)}"
            f"</content></status>"
        )

    # ------------------------------------------------------------------
    # Diff validation
    # ------------------------------------------------------------------

    async def _validate_diff_response(self, text_content: str) -> str | None:
        """Validate <diff> blocks in the LLM's final response."""
        if not text_content or "<diff>" not in text_content:
            return None
        try:
            file_content = await file_service.read_file(self.project_id, self.file_path)
        except Exception:
            logger.debug("Failed to read annotation file for diff validation %s", self.file_path, exc_info=True)
            return None
        return validate_diffs(text_content, file_content)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def _save_messages(self, messages: list[dict]) -> None:
        """Save new messages to the database (annotation variant).

        Atomic: all new messages for this turn are staged and committed in one
        transaction (mirrors QueryLoop), so a mid-batch failure cannot leave a
        partial message list with broken tool_call/tool_call_id pairings.
        """
        async def _operation(uow):
            history = await uow.messages.get_messages_for_annotation_llm(self.annotation_id)
            history_count = sum(
                1 for msg in history if getattr(msg, "role", "") != "system"
            )
            candidates = [
                msg for msg in messages[1:]
                if not msg.get("_ephemeral") and msg.get("role") != "system"
            ]
            new_messages = candidates[history_count:]

            await stage_new_messages(
                new_messages,
                partial(uow.messages.stage_create_for_annotation, annotation_id=self.annotation_id),
            )

        await UnitOfWork.execute_atomic(self.project_id, _operation)

    async def _persist_error_message(self, error: Exception) -> str:
        """Persist a visible annotation-thread error when possible.

        Best-effort: failures are logged but never mask the original error that
        triggered this call. Atomic via execute_atomic for consistency with
        QueryLoop._persist_error_message.
        """
        content = LLMLoopRunner._format_error_message(error)

        async def _operation(uow):
            annotation, err = await uow.annotations.resolve(self.annotation_id)
            if err or not annotation:
                return
            await uow.messages.stage_create_for_annotation(
                annotation_id=annotation.id,
                role="assistant",
                content=content,
            )

        try:
            await UnitOfWork.execute_atomic(self.project_id, _operation)
        except Exception:
            logger.debug("Failed to persist AnnotationLoop error message", exc_info=True)
        return content


def _escape_xml(text: str) -> str:
    """Escape special XML characters in text content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
