"""
AI Service — single entry point for AI / chat orchestration.

Uses QueryLoop for all AI interactions.
"""

import asyncio
import json
import re
from typing import AsyncGenerator, Dict, Any, Optional

from sqlalchemy.exc import IntegrityError, OperationalError
from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from app.core.exceptions import (
    FileSystemError,
    SessionNotFoundError,
    TaskActiveError,
    TaskStateUnavailableError,
    SkillError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.utils import generate_id, utcnow
from app.core.task_status import (
    SSE_CANCELLED,
    SSE_DONE,
    SSE_ERROR,
    STATUS_AWAITING_INPUT,
    STATUS_INTERACTION_CONSUMING,
    STATUS_INTERACTION_FAILED,
    STATUS_CANCELLING,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from app.core.chat_attachments import (
    render_attachments_tag,
    strip_internal_image_tags,
)
from app.core.chat_search import (
    compile_search_pattern,
    sql_prefilter_reliable,
    visible_search_matches,
)
from app.core.message_format import page_ui_turns, shape_messages_for_ui
from app.services import task_runtime
from app.services.query_loop import QueryLoop
from app.services.project_service import project_service
from app.services.stream_hub import stream_hub
from app.database.unit_of_work import UnitOfWork
from app.database.seq_utils import MAX_RETRIES, RETRY_DELAY
from app.services.task_service import (
    render_task_reminder_tag,
    task_to_dict,
    unfinished_tasks,
)
from app.services.session_temp_service import session_temp_service
from app.models.requests import InteractionResponse

logger = get_logger(__name__)

# Attempts to read the active task before a failed read surfaces as 503
# instead of a false "no active task".
MAX_ATTEMPTS_ACTIVE_TASK_READ = 2

# Patterns to strip internal blocks from user messages for UI / title generation
_STATUS_TAG_RE = re.compile(r'<status>.*?</status>\s*', re.DOTALL)
_CITATION_TAG_RE = re.compile(r'<citation>.*?</citation>\s*', re.DOTALL)

PLAN_STATUS_INSTRUCTION = (
    "<system>IMPORTANT: The user requires planning precede execution. You **must call the plan agent** to "
    "create an implementation plan before proceeding.</system>"
)


def _resolve_fork_cutoff(messages: list, message_id: str) -> int:
    """Return the seq up to which a fork copies messages (rows ordered by seq).

    Forking from a user message copies that message and everything before it.
    Forking from an assistant message copies its whole turn — through the last
    row before the next user message — so tool calls stay paired with their
    results and the copied prefix remains valid LLM history.
    """
    index = next((i for i, m in enumerate(messages) if m.id == message_id), None)
    if index is None:
        raise ValidationError(f"Message not found in this session: {message_id}")
    target = messages[index]
    if target.role not in ("user", "assistant"):
        raise ValidationError("Fork is only allowed on user or assistant messages")
    cutoff_seq = target.seq
    if target.role == "assistant":
        for msg in messages[index + 1:]:
            if msg.role == "user":
                break
            cutoff_seq = msg.seq
    return cutoff_seq


def _collect_referenced_agent_ids(copied_messages: list, agent_session_ids: list) -> list:
    """Filter agent session ids referenced by the copied message prefix.

    An agent session is referenced when its id appears in a copied message's
    content (the ``<resume_id>`` tag of a tool result) or tool_calls (the
    resume_id argument of an Agent call). Agents spawned after the fork point
    leave no reference in the prefix and are correctly left behind.
    """
    referenced = []
    for agent_id in agent_session_ids:
        for msg in copied_messages:
            if agent_id in (msg.content or "") or agent_id in (msg.tool_calls or ""):
                referenced.append(agent_id)
                break
    return referenced


def _agent_prefix_at_fork(copied_messages: list, agent, agent_messages: list) -> list:
    """Return the child messages belonging to the parent's fork snapshot.

    Persistent agent rows can continue after the parent message that is being
    forked. When timestamps are available, the parent tool-call/result that
    references this agent is the durable message boundary for the child
    history. Older rows remain compatible with the legacy in-memory fixtures:
    without comparable timestamps the already-read child snapshot is used,
    while the final source/agent version CAS still rejects concurrent changes.
    """
    parent_tool_call_id = getattr(agent, "parent_tool_call_id", "")
    boundaries = []
    for message in copied_messages:
        tool_calls = message.tool_calls or ""
        content = message.content or ""
        if (
            (parent_tool_call_id and parent_tool_call_id in tool_calls)
            or agent.id in tool_calls
            or agent.id in content
        ) and getattr(message, "created_at", None) is not None:
            boundaries.append(message.created_at)
    if not boundaries:
        return list(agent_messages)
    boundary = max(boundaries)
    comparable = [m for m in agent_messages if getattr(m, "created_at", None) is not None]
    if len(comparable) != len(agent_messages):
        return list(agent_messages)
    return [m for m in agent_messages if m.created_at <= boundary]


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
            sessions = await uow.sessions.list_all(include_archived=include_archived)
            return [s.to_dict() for s in sessions]

    async def create_session(self, project_id: str) -> Dict:
        """Create a new session. Returns the session dict."""
        async with UnitOfWork(project_id) as uow:
            session = await uow.sessions.create()
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
        """Delete a session, its agent children, their task state, and temp storage.

        Active descendants are cancelled and drained before their durable rows
        are removed. Parked interaction checkpoints have no live runner and
        are removed by the same final transaction.
        """
        from app.services import task_runtime

        session_ids: list[str] = []
        for _ in range(MAX_RETRIES):
            async with UnitOfWork(project_id) as uow:
                session_ids = await uow.sessions.collect_descendant_session_ids(session_id)
                active_tasks = []
                for sid in session_ids:
                    active = await uow.task_state.get_active_by_session(sid)
                    if active:
                        active_tasks.append(active["task_id"])

            for task_id in active_tasks:
                task_runtime.cancel(task_id)
            drained = await asyncio.gather(
                *(task_runtime.wait_for_task(task_id) for task_id in active_tasks),
            )
            if not all(drained):
                raise TaskActiveError(task_id=active_tasks[drained.index(False)])

            async with UnitOfWork(project_id, immediate=True) as uow:
                session_ids = await uow.sessions.collect_descendant_session_ids(session_id)
                late_tasks = []
                for sid in session_ids:
                    active = await uow.task_state.get_active_by_session(sid)
                    if active and task_runtime.cancel(active["task_id"]):
                        late_tasks.append(active["task_id"])

            if late_tasks:
                drained = await asyncio.gather(
                    *(task_runtime.wait_for_task(task_id) for task_id in late_tasks),
                )
                if not all(drained):
                    raise TaskActiveError(task_id=late_tasks[drained.index(False)])
                continue

            try:
                for sid in session_ids:
                    session_temp_service.delete_session_dir(project_id, sid)
            except Exception as exc:
                if isinstance(exc, FileSystemError):
                    raise
                raise FileSystemError(
                    f"Could not remove temporary storage for session {session_id}; retry later.",
                    code="SESSION_TEMP_CLEANUP_FAILED",
                ) from exc

            async with UnitOfWork(project_id, immediate=True) as uow:
                session_ids = await uow.sessions.collect_descendant_session_ids(session_id)
                late_tasks = []
                for sid in session_ids:
                    active = await uow.task_state.get_active_by_session(sid)
                    if active and task_runtime.cancel(active["task_id"]):
                        late_tasks.append(active["task_id"])
                if late_tasks:
                    for task_id in late_tasks:
                        task_runtime.cancel(task_id)
                    continue
                await uow.sessions.delete(session_id)
            break
        else:
            raise TaskActiveError(task_id=session_id)

    async def fork_session(
        self, project_id: str, session_id: str, message_id: str, title: str = "",
    ) -> Dict:
        """Create a new session copying everything up to and including a message.

        Read-only with respect to the source session: it copies the message
        prefix (attachment paths and resume_id references rewritten to the
        fork) plus the agent sub-sessions that prefix references. Tasks,
        runtime task state, and messages after the fork point are not copied.

        Refuses while the session holds an active task (runnable or parked on
        a checkpoint), same as delete_session: a fork taken mid-stream would
        bake a truncated partial checkpoint into the new session.
        """
        async with UnitOfWork(project_id) as uow:
            source = await uow.sessions.get_by_id(session_id)
            if source is None:
                raise SessionNotFoundError(session_id)
            if source.session_kind != "chat":
                raise ValidationError("Only chat sessions can be forked")
            active = await uow.task_state.get_active_by_session(session_id)
            if active:
                raise TaskActiveError(task_id=active["task_id"])
            messages = await uow.messages.get_messages(session_id)
            cutoff_seq = _resolve_fork_cutoff(messages, message_id)
            copied_messages = [m for m in messages if m.seq <= cutoff_seq]
            if not copied_messages:
                raise ValidationError("Fork point precedes the first message")

            descendant_ids = await uow.sessions.collect_descendant_session_ids(session_id)
            agent_ids = [sid for sid in descendant_ids if sid != session_id]
            referenced_ids = _collect_referenced_agent_ids(copied_messages, agent_ids)
            agent_sessions = {}
            for sid in referenced_ids:
                agent = await uow.sessions.get_by_id(sid)
                if agent is not None:  # deleted concurrently — drop it
                    agent_sessions[sid] = agent
            agent_messages_snapshot = {
                sid: await uow.messages.get_messages(sid) for sid in agent_sessions
            }
            agent_messages = {
                sid: _agent_prefix_at_fork(copied_messages, agent_sessions[sid], msgs)
                for sid, msgs in agent_messages_snapshot.items()
            }
            fork_title = title or source.title
            source_version = getattr(source, "updated_at", None)
            source_last_seq = messages[-1].seq if messages else None
            agent_versions = {
                sid: (
                    getattr(agent_sessions[sid], "updated_at", None),
                    agent_messages_snapshot[sid][-1].seq
                    if agent_messages_snapshot[sid] else None,
                )
                for sid in agent_sessions
            }

        new_parent_id = generate_id()
        session_id_map = {session_id: new_parent_id}
        session_id_map.update(
            {sid: generate_id() for sid in agent_sessions}
        )
        # Each mapped id is replaced verbatim. That single rule covers both
        # ``<resume_id>`` tags and tool-call arguments, and temp-storage path
        # prefixes as well — every ``.SiGMA/sessions/<id>/...`` path contains
        # the id itself, so no separate path rewrite is needed.
        rewrite_pairs = list(session_id_map.items())

        async def _operation(uow):
            # Revalidate the read snapshot in the same transaction that stages
            # the fork. A send, delete, or agent resume that changed the source
            # after the read makes this fork fail rather than copying stale
            # rows and creating a fork that can later resurrect deleted work.
            current_source = await uow.sessions.get_by_id(session_id)
            if current_source is None:
                raise SessionNotFoundError(session_id)
            if await uow.task_state.get_active_by_session(session_id):
                raise TaskActiveError()
            current_messages = await uow.messages.get_messages(session_id)
            current_last_seq = current_messages[-1].seq if current_messages else None
            if (
                current_last_seq != source_last_seq
                or getattr(current_source, "updated_at", None) != source_version
            ):
                raise ValidationError(
                    "Session changed while the fork was being prepared; retry the fork."
                )
            for sid, (version, last_seq) in agent_versions.items():
                current_agent = await uow.sessions.get_by_id(sid)
                current_agent_messages = await uow.messages.get_messages(sid)
                if current_agent is None or (
                    getattr(current_agent, "updated_at", None) != version
                    or (current_agent_messages[-1].seq if current_agent_messages else None) != last_seq
                ):
                    raise ValidationError(
                        "An agent changed while the fork was being prepared; retry the fork."
                    )
            await uow.sessions.stage_create(
                session_id=new_parent_id, title=fork_title,
            )
            for sid, agent in agent_sessions.items():
                await uow.sessions.stage_create(
                    session_id=session_id_map[sid],
                    title=agent.title,
                    session_kind=agent.session_kind,
                    agent_type=agent.agent_type,
                    parent_session_id=new_parent_id,
                    parent_tool_call_id=agent.parent_tool_call_id,
                )
            await uow.messages.stage_copy_messages(
                copied_messages, new_parent_id, rewrite_pairs,
            )
            for sid, msgs in agent_messages.items():
                await uow.messages.stage_copy_messages(
                    msgs, session_id_map[sid], rewrite_pairs,
                )

        try:
            # Copy temp storage (attachments, caches) before committing rows
            # so the paths referenced by rewritten messages already exist on
            # disk. Inside the try so a mid-copy failure still runs the
            # cleanup below instead of leaking half-copied directories.
            for old_id, new_id in session_id_map.items():
                session_temp_service.copy_session_dir(project_id, old_id, new_id)
            await UnitOfWork.execute_atomic(project_id, _operation)
        except Exception:
            # Best-effort cleanup: a failed fork must not leave orphan rows or
            # directories behind. Cleanup failures are tolerated (a leftover
            # invisible row/dir is harmless and the user can retry the fork).
            logger.warning("Fork of session %s failed; cleaning up", session_id, exc_info=True)
            try:
                async with UnitOfWork(project_id) as uow:
                    await uow.sessions.delete(new_parent_id)
            except Exception:
                logger.warning(
                    "Fork row cleanup failed for session %s", new_parent_id, exc_info=True,
                )
            for new_id in session_id_map.values():
                try:
                    session_temp_service.delete_session_dir(project_id, new_id)
                except Exception:
                    logger.warning(
                        "Fork temp cleanup failed for session %s", new_id, exc_info=True,
                    )
            raise

        async with UnitOfWork(project_id) as uow:
            forked = await uow.sessions.get_by_id(new_parent_id)
            if forked is None:
                raise SessionNotFoundError(new_parent_id)
            return forked.to_dict()

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
            # The four rows land on the session message tail, which a running
            # chat turn owns: appending under an active task would shift the
            # running loop's tail-slice accounting and silently skip its own
            # newest messages at the next save. Agent sessions are likewise
            # owned by their parent task, so only idle chat sessions qualify.
            session = await uow.sessions.get_by_id(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            if session.session_kind != "chat":
                raise ValidationError("Only chat sessions can receive a skill")
            active = await uow.task_state.get_active_by_session(session_id)
            if active:
                raise TaskActiveError(task_id=active["task_id"])
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

        await UnitOfWork.execute_atomic(project_id, _operation, immediate=True)
        return {"skill_id": skill_id, "name": skill_name}

    async def get_session_messages(self, project_id: str, session_id: str) -> list[Dict]:
        """Get UI-shaped messages for a specific session (archive preview)."""
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
        async with UnitOfWork(project_id) as uow:
            if session_id is None:
                sessions = await uow.sessions.list_all()
                if sessions:
                    session_id = sessions[0].id
                else:
                    session = await uow.sessions.create()
                    session_id = session.id
                    session_temp_service.ensure_session_dir(project_id, session_id)
            messages, boundary_seq = await uow.messages.get_messages_with_boundary(session_id)
            # The authoritative park fact: an awaiting_input task with a
            # checkpoint. History shaping stamps the parked turn's open
            # tool call as awaiting_input instead of interrupted — the UI
            # renders state, it never guesses it.
            pending_interaction = await uow.task_state.get_pending_interaction_by_session(
                session_id
            )

        entries = shape_messages_for_ui(
            messages, boundary_seq,
            session_parked=pending_interaction is not None,
        )
        page = page_ui_turns(entries, limit=limit, before_seq=before_seq)
        # Clients lock edits on pre-boundary messages they hold from older
        # pages; passive boundaries hide inside turn process steps, so the
        # seq cannot be recovered from the entries alone.
        page["boundary_seq"] = boundary_seq
        return page

    async def clear_history(self, project_id: str, session_id: str = None) -> Dict[str, Any]:
        """Clear chat history for a session. Falls back to most recent session if session_id is None."""
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(project_id, immediate=True) as uow:
                    if session_id is None:
                        sessions = await uow.sessions.list_all()
                        if sessions:
                            session_id = sessions[0].id
                        else:
                            return {"success": True, "message": "No sessions to clear"}
                    # Deleting history mid-turn corrupts the running loop's
                    # tail-slice persistence, and a parked checkpoint loses
                    # the boundary rows its resume depends on — the active
                    # task must finish or be answered (or cancelled) before
                    # history may go. The guard reads inside the same
                    # BEGIN IMMEDIATE transaction the delete commits in, so a
                    # concurrent claim insert is either visible to the guard
                    # or serializes after the cleared history.
                    active = await uow.task_state.get_active_by_session(session_id)
                    if active:
                        raise TaskActiveError(task_id=active["task_id"])
                    await uow.messages.delete_by_session(session_id)
                    return {"success": True, "message": "History cleared"}
            except OperationalError:
                if attempt >= MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    # ------------------------------------------------------------------
    # Chat search
    # ------------------------------------------------------------------

    # Caps keep one search response bounded. total_matches only covers
    # sessions inside the cap — candidates beyond it are never shaped, so
    # their hits stay uncounted. total_sessions is the pre-filter candidate
    # count: an upper bound on groups, since raw-row hits in invisible text
    # (internal tags, process-only rows) yield no group.
    SEARCH_MAX_SESSIONS = 20
    SEARCH_MAX_MATCHES_PER_SESSION = 5

    async def search_chat(self, project_id: str, query: str) -> Dict[str, Any]:
        """Search session titles and user-visible message text across all sessions.

        Message matching runs over ``shape_messages_for_ui`` output, so only
        user bubbles and final assistant bubbles match — intermediate
        process content (tool calls, hints) and internal tags never do.
        Archived sessions are included.
        """
        query = query.strip()
        if not query:
            raise ValidationError("Search query must not be empty")

        pattern = compile_search_pattern(query)
        async with UnitOfWork(project_id) as uow:
            sessions = await uow.sessions.list_all(include_archived=True)
            # The SQL pre-filter folds ASCII case only; needles with non-ASCII
            # cased letters skip it so message matching stays as case-
            # insensitive as the title match, at the cost of scanning every
            # session.
            scan_all = not sql_prefilter_reliable(query)
            matched_ids: set = set()
            if sessions and not scan_all:
                matched_ids = set(await uow.messages.search_session_ids_containing(
                    [s.id for s in sessions], query,
                ))

            candidates = [
                s for s in sessions
                if scan_all
                or s.id in matched_ids
                or pattern.search(s.title or "")
            ]
            groups: list[Dict[str, Any]] = []
            total_matches = 0
            for session in candidates[:self.SEARCH_MAX_SESSIONS]:
                messages, boundary_seq = await uow.messages.get_messages_with_boundary(session.id)
                entries = shape_messages_for_ui(messages, boundary_seq)
                matches = visible_search_matches(entries, pattern)
                title_match = bool(pattern.search(session.title or ""))
                if not matches and not title_match:
                    # Pre-filter false positive: the raw row hit an internal
                    # tag or intermediate process text only.
                    continue
                total_matches += len(matches)
                groups.append({
                    "session": session.to_dict(),
                    "title_match": title_match,
                    "matches": matches[:self.SEARCH_MAX_MATCHES_PER_SESSION],
                    "match_count": len(matches),
                })

        return {
            "query": query,
            "groups": groups,
            "total_matches": total_matches,
            "total_sessions": len(candidates),
        }

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
        """Validate, check for active tasks, persist user message, launch the chat task."""
        task_id = generate_id()
        if interaction_response is not None:
            try:
                interaction_response = TypeAdapter(InteractionResponse).validate_python(
                    interaction_response,
                ).model_dump(exclude_none=True)
            except PydanticValidationError as exc:
                raise ValidationError("Invalid interaction response", details={"errors": exc.errors()}) from exc
        compact_only = message.strip() in {"/compact", "/compress"}

        # A resume answers the parked checkpoint; its request must not carry
        # conversation content. Any non-empty text — including /compact —
        # would either insert a user message between the parked assistant
        # tool call and its tool result (an invalid provider sequence) or bury
        # the checkpoint under a compaction boundary.
        if resume and message.strip():
            raise ValidationError(
                "A resume cannot carry a new message; answer the pending "
                "prompt without text or cancel the task first."
            )
        # The response is what the resumed loop feeds to the pending tool.
        # A resume without one would run a fresh turn over the parked
        # session and leave the checkpoint blocking every later message.
        # Every frontend resume caller (permission dialog, question modal,
        # plan dialog) sends the response it rendered.
        if resume and not (interaction_response or {}):
            raise ValidationError(
                "A resume must carry the interaction response for the "
                "pending prompt."
            )
        # The inverse guard: a non-resume submit has no checkpoint to feed.
        # Accepting the response would persist the message and launch a task
        # whose loop takes the resume path, finds no parked tool call, and
        # ends with a bare done — an LLM turn that never runs.
        if not resume and interaction_response:
            raise ValidationError(
                "interaction_response is only valid when resuming; there is "
                "no pending prompt to answer. Cancel the running task first "
                "if you want a fresh turn."
            )
        # An empty submit would launch a real LLM turn with nothing to say;
        # attachments alone are a legitimate image-only message.
        if not resume and not message.strip() and not context.get("attachments"):
            raise ValidationError("Message must not be empty")

        # Which active statuses reject a submission is decided here and
        # enforced inside the claim's serialized transaction: a new message
        # is also refused while the session is parked awaiting_input — the
        # pending prompt must be answered or the task cancelled first —
        # while a resume targets exactly that parked checkpoint. Startup
        # reconciliation guarantees no zombie active rows can outlive a
        # process restart, so the row's status alone decides.
        rejected = {STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING}
        if not resume:
            rejected.update({STATUS_AWAITING_INPUT, STATUS_INTERACTION_CONSUMING, STATUS_INTERACTION_FAILED})

        # Resolve or create session before persisting messages. A resume
        # requires an existing session: the frontend's restore flow reads the
        # session from history first, so a missing row is always a stale
        # client — minting a fresh one would strand the response in an empty
        # session shell no checkpoint points at.
        if session_id:
            async with UnitOfWork(project_id) as uow:
                db_session = await uow.sessions.get_by_id(session_id)
                if db_session is None:
                    if resume:
                        raise ValidationError(
                            "Cannot resume: session not found in this project",
                        )
                    session_id = None
            if session_id is None:
                async with UnitOfWork(project_id) as uow:
                    db_session = await uow.sessions.create()
                    session_id = db_session.id

        if not session_id:
            raise ValidationError("session_id is required for llm_chat tasks")

        if resume:
            response = interaction_response or {}
            async with UnitOfWork(project_id) as uow:
                checkpoint = await uow.task_state.get_pending_interaction(
                    response["task_id"],
                    response["interaction_id"],
                    response["interaction_type"],
                )
            if not checkpoint or checkpoint.get("session_id") != session_id:
                raise ValidationError(
                    "This interaction is stale or belongs to another session. "
                    "Refresh the chat to restore the current task.",
                )

        # Claim the session BEFORE any side effects (message writes,
        # interactive tool execution, task launch). The claim's
        # BEGIN IMMEDIATE transaction holds the guard read and the queued-row
        # insert together — the insert's own commit finalizes the claim — so
        # a concurrent submission that parks the session cannot land between
        # them; the partial unique index on runnable statuses arbitrates two
        # concurrent claims. Terminal-row pruning after that commit is
        # best-effort housekeeping and cannot invalidate a committed claim.
        await self._claim_chat_task(project_id, session_id, task_id, rejected=rejected)

        # From here on the queued row owns the session; a failure in any of
        # the remaining steps must fail the row instead of leaving it queued
        # (a stranded queued row blocks the session).
        try:
            # Persist user message immediately — survives refresh/crash.
            # An attachments-only submit has no text but its rendered
            # attachments content is still the conversation turn; /compact is
            # a command, not conversation content.
            if (message.strip() or context.get("attachments")) and not compact_only:
                full_content = await self._build_user_message_content(
                    message, context, project_id, session_id,
                )
                await self._append_session_message(
                    project_id,
                    session_id,
                    role="user",
                    content=full_content,
                )

            # The interaction response is handed to the task untouched: the
            # resumed loop executes the parked interactive tool AFTER it has
            # claimed the checkpoint (_resume_from_interaction), so a cancel
            # or a duplicate resume can never double-run the tool's side
            # effect, and the executed result is persisted before the LLM
            # continues.

            # A chat turn is project activity — refresh the list ordering
            # timestamp.
            project_service.touch_project(project_id)

            task_context = dict(context)
            if compact_only:
                task_context["compact_only"] = True

            from app.services.chat_executor import stream_chat_for_task

            task_runtime.launch(
                task_id=task_id,
                project_id=project_id,
                source_factory=lambda cancel_event: stream_chat_for_task(
                    project_id=project_id,
                    context=task_context,
                    session_id=session_id,
                    interaction_response=interaction_response,
                    task_id=task_id,
                    cancel_event=cancel_event,
                ),
            )
        except BaseException as e:
            await self._finalize_launch_failure(project_id, task_id, e)
            raise
        return {"task_id": task_id}

    async def _finalize_launch_failure(
        self, project_id: str, task_id: str, exc: BaseException,
    ) -> None:
        """Finalize the queued row when a step after the claim commit failed.

        The committed claim owns the session, so every failure between it and
        the runner launch — including a CancelledError from a disconnecting
        client, which ``except Exception`` would let through — must finalize
        the row before propagating, or a stranded queued row would block the
        session until the stranded-row sweep finalizes it.
        """
        if isinstance(exc, asyncio.CancelledError):
            error = "Task was cancelled before it started."
        else:
            error = f"Failed to start chat task: {exc}"
        try:
            async with UnitOfWork(project_id) as uow:
                await uow.task_state.mark_failed(task_id, error)
        except Exception:
            # Best-effort cleanup: the original startup failure must not
            # be masked by a failure to finalize the task row.
            logger.warning(
                "Failed to finalize chat task %s after startup error",
                task_id, exc_info=True,
            )

    async def _claim_chat_task(
        self, project_id: str, session_id: str, task_id: str,
        rejected: set[str],
    ) -> None:
        """Insert the queued task row that claims the session for this task.

        The active-row guard read and the insert run inside one BEGIN
        IMMEDIATE transaction — the insert's own commit finalizes the claim:
        a concurrent submission that parks or claims the session either
        committed before the guard read (and is rejected) or serializes after
        this claim commits — the gap a plain read-then-write guard leaves open
        cannot occur. ``rejected`` holds the statuses that must refuse this
        submission (callers widen it with ``awaiting_input`` for new messages;
        a resume skips it to target the parked checkpoint).

        The partial unique index on (owner_type, owner_id) over runnable
        statuses is the backstop: a conflict means another live submission
        owns the session. On a conflict the active row is re-read — a real
        conflicting row surfaces as TaskActiveError, while a stale conflict
        (the row finalized in the gap) retries the claim insert once; a stale
        conflict on the retry ends the loop with TaskActiveError too, so the
        caller-visible failure mode is always the 409 contract, never a raw
        IntegrityError. Rows left active by a crashed process are failed by
        startup reconciliation before any new submission can arrive, so no
        stale takeover is needed here.
        """
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(project_id, immediate=True) as uow:
                    existing = await uow.task_state.get_active_by_session(session_id)
                    if existing and existing["task_id"] == task_id:
                        # A prior attempt's insert committed and only a later
                        # step failed transiently: the claim already holds.
                        return
                    if existing and existing["status"] in rejected:
                        raise TaskActiveError(task_id=existing["task_id"])
                    await uow.task_state.set_queued(
                        task_id,
                        task_type="llm_chat",
                        session_id=session_id,
                        owner_type="chat_session",
                        owner_id=session_id,
                    )
                    # Terminal rows are never read back for a session
                    # (arbitration uses the partial unique index over
                    # runnable statuses only); prune the old ones so the
                    # table does not grow without bound. Best-effort: the
                    # claim committed above, so a transient failure here
                    # must not fail the submission — a retry would re-enter
                    # the guard with this task's own row already committed.
                    try:
                        await uow.task_state.prune_terminal_by_session(session_id)
                    except Exception:
                        logger.debug(
                            "Terminal-row prune failed for session %s",
                            session_id, exc_info=True,
                        )
                return
            except IntegrityError:
                async with UnitOfWork(project_id) as uow:
                    existing = await uow.task_state.get_active_by_session(session_id)
                if existing and existing["status"] in rejected:
                    raise TaskActiveError(task_id=existing["task_id"])
                if attempt >= 1:
                    # The conflicting row finalized before this re-read, so
                    # its task id is unknowable — the caller-visible contract
                    # is still TaskActiveError (409), never a raw
                    # IntegrityError.
                    raise TaskActiveError()
                await asyncio.sleep(RETRY_DELAY)
            except OperationalError:
                if attempt >= MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))

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
        task_id = generate_id()

        # Read-only validation and content build: safe to run before the
        # claim because neither mutates the session.
        async with UnitOfWork(project_id) as uow:
            target = await uow.messages.get_by_id(message_id)
            if (
                target is None
                or target.session_id != session_id
                or target.role != "user"
            ):
                raise ValidationError("Only user messages in this session can be edited")

            boundary_seq = await uow.messages.get_last_boundary_seq(session_id)
            if boundary_seq is not None and target.seq <= boundary_seq:
                raise ValidationError("Compressed messages cannot be edited")

            target_seq = target.seq

        full_content = await self._build_user_message_content(
            message, context, project_id, session_id,
        )

        # Claim the session BEFORE any side effects (message truncation and
        # rewrite, task launch), mirroring submit_chat: the guard read and
        # the queued-row insert share one BEGIN IMMEDIATE transaction, and
        # the partial unique index is the serialization point a
        # read-then-write guard alone cannot provide. An edit that mutated
        # messages before claiming would truncate a concurrently submitted
        # task's history even though its own claim then fails. Any active
        # row — including a parked checkpoint — blocks an edit: the pending
        # prompt must be answered or the task cancelled first.
        await self._claim_chat_task(
            project_id, session_id, task_id,
            rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING,
                      STATUS_AWAITING_INPUT},
        )

        async def _operation(uow):
            await uow.messages.stage_truncate_from(session_id, target_seq)
            await uow.messages.stage_create(
                session_id=session_id,
                role="user",
                content=full_content,
            )
            await uow.sessions.stage_touch(session_id)

        # From here on the queued row owns the session; a failure in any of
        # the remaining steps must fail the row instead of leaving it queued
        # (a stranded queued row blocks the session).
        try:
            await UnitOfWork.execute_atomic(project_id, _operation)

            # A chat turn is project activity — refresh the list ordering timestamp.
            project_service.touch_project(project_id)

            from app.services.chat_executor import stream_chat_for_task

            task_runtime.launch(
                task_id=task_id,
                project_id=project_id,
                source_factory=lambda cancel_event: stream_chat_for_task(
                    project_id=project_id,
                    context=dict(context),
                    session_id=session_id,
                    interaction_response=None,
                    task_id=task_id,
                    cancel_event=cancel_event,
                ),
            )
        except BaseException as e:
            await self._finalize_launch_failure(project_id, task_id, e)
            raise
        return {"task_id": task_id}

    async def get_active_task(self, project_id: str, session_id: str = None) -> Dict[str, Any]:
        """Return the session's active task row, if any.

        A failing read is retried once, then surfaces as a typed 503 —
        answering "no active task" from a failed read would tell the client
        the session is idle when it merely could not be asked.
        """
        for attempt in range(MAX_ATTEMPTS_ACTIVE_TASK_READ):
            try:
                async with UnitOfWork(project_id) as uow:
                    active = await uow.task_state.get_active_by_session(session_id)
                    if not active:
                        return {"active": False, "task_id": None, "status": None}

                result = {
                    "active": True,
                    "task_id": active["task_id"],
                    "status": active["status"],
                    "task_type": active.get("task_type"),
                    "session_id": active.get("session_id"),
                }
                # Include interaction data so frontend can restore modal on page reload
                if active["status"] in (STATUS_AWAITING_INPUT, STATUS_INTERACTION_FAILED):
                    interaction = active.get("interaction_state")
                    if interaction:
                        interaction["task_id"] = active["task_id"]
                    result["interaction"] = interaction
                return result
            except Exception:
                logger.debug(
                    "Failed to read active chat task for session %s (attempt %d)",
                    session_id, attempt + 1, exc_info=True,
                )
                if attempt < MAX_ATTEMPTS_ACTIVE_TASK_READ - 1:
                    await asyncio.sleep(RETRY_DELAY)
        raise TaskStateUnavailableError(
            f"Could not read the active task state for session {session_id}"
        )

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

        Records the cancel intent in the database — the durable status the
        UI reads — and signals the task's in-process runner through its
        cancel event so the source winds down immediately. When no live
        session exists the runner cannot finalize the row, so this path
        finalizes it directly instead. Returns the task's effective status
        so the caller can report honestly.
        """
        status = await self._request_cancel_with_retry(project_id, task_id)

        if status == "not_found":
            # No row in this project's database: the task either never existed
            # here or belongs to another project. The in-process cancel signal
            # must stay gated on this database confirmation, or a cross-project
            # task id would be cancelled for real while the API reports
            # not_found.
            return {
                "cancelled": False,
                "status": status,
                "task_id": task_id,
            }

        if not task_runtime.cancel(task_id):
            status = await self._finalize_without_runner(project_id, task_id, status)

        return {
            "cancelled": status in (STATUS_CANCELLING, STATUS_CANCELLED),
            "status": status,
            "task_id": task_id,
        }

    async def _finalize_without_runner(self, project_id: str, task_id: str, status: str) -> str:
        """Finalize an active row whose runner is gone (its session detached).

        ``request_cancel`` has already mapped queued/running to cancelling and
        awaiting_input to cancelled, so ``cancelling`` is the only status a
        stranded row can still hold; ``mark_cancelled``'s guarded CAS makes a
        double-finalize against a runner that is mid-finalize a harmless
        no-op. Returns the row's resulting status.
        """
        if status not in (STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING):
            return status
        async with UnitOfWork(project_id) as uow:
            await uow.task_state.mark_cancelled(task_id)
            row = await uow.task_state.get_by_id(task_id)
        return row["status"] if row else status

    async def _request_cancel_with_retry(self, project_id: str, task_id: str) -> str:
        """Record cancel intent, retrying on a transient database lock.

        ``request_cancel`` is a compare-and-swap and therefore idempotent, so
        retrying the whole sequence on a locked-DB ``OperationalError`` is safe.
        This avoids surfacing a 500 when concurrent writers contend on
        SQLite's single writer, which the durable-cancel design relies on.
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
    # 4. SSE listening
    # ==================================================================

    async def sse_listen(
        self,
        task_id: str,
        cursor: Optional[int] = None,
        project_id: str = "",
    ) -> AsyncGenerator[str, None]:
        """Subscribe to the SSE stream for a task.

        Yields the task id as the first frame so the frontend can cancel the
        task before any task event arrives. When the task has a live stream
        session its frames are forwarded verbatim — buffered events after
        ``cursor`` are replayed first, so a reconnecting client neither
        re-applies delivered chunks nor misses the ones pushed while it was
        offline. Without a session the durable task row decides the outcome:
        a finished task renders its terminal frame immediately instead of
        leaving the subscriber waiting; a row that cannot be read ends the
        stream with no terminal frame so the client's reconnect retries.
        """
        # Yield task_id as the first event so the frontend can cancel the task
        yield self._format_sse("task_id", {"task_id": task_id})

        # A hub session belongs to the project that launched its task: a task
        # id from another project must look exactly like an unknown one, so it
        # falls through to the project-database lookup instead of streaming
        # another project's frames.
        session = stream_hub.get(task_id)
        if session is None or session.project_id != project_id:
            frame = await self._terminal_frame_without_session(task_id, project_id)
            if frame is not None:
                yield frame
            return
        try:
            async for frame in session.subscribe(cursor=cursor, idle_timeout=1800.0):
                yield frame
        except Exception:
            logger.warning("Stream subscription lost for task %s", task_id, exc_info=True)
            yield self._format_sse(SSE_ERROR, {"error": "Stream connection lost"})

    async def _terminal_frame_without_session(
        self, task_id: str, project_id: str,
    ) -> Optional[str]:
        """Render the terminal SSE frame for a task with no live session.

        The hub only holds sessions for tasks running in this process, so a
        lookup miss means the task already finished here or never ran here;
        the durable row in the task's project says which. An
        ``awaiting_input`` row is a designed park that survives restarts and
        renders the same done frame the runner pushes when it parks. A row
        still in a runnable status is a legal intermediate state, not a
        broken invariant: the claim commit and the runner's launch (message
        persistence, project touch) sit between them, and a second tab or a
        refresh reconnects through getActive in that window — so the stream
        ends with no terminal frame and the client's reconnect retries. The
        periodic stranded-row sweep finalizes a runnable row whose runner
        never appears.

        Only a successful read may claim the row is absent: a read failing
        with transient contention (``OperationalError``, a locked database)
        is retried briefly and then returns None — the stream ends with no
        terminal frame and the client's reconnect logic retries — instead of
        a false "not active" terminal that would stop reconnection for a
        task that may have just completed. Any other lookup failure is
        deterministic (e.g. the project has no database) and keeps the
        not-active error terminal.
        """
        row = None
        for attempt in range(MAX_RETRIES):
            try:
                async with UnitOfWork(project_id) as uow:
                    row = await uow.task_state.get_by_id(task_id)
                break
            except OperationalError:
                if attempt >= MAX_RETRIES - 1:
                    logger.warning(
                        "Task row lookup kept failing in project %s",
                        project_id, exc_info=True,
                    )
                    return None
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            except Exception:
                logger.debug(
                    "Task row lookup failed in project %s", project_id, exc_info=True,
                )
                break
        if row is not None:
            status = row["status"]
            if status in (STATUS_COMPLETED, STATUS_AWAITING_INPUT, STATUS_INTERACTION_CONSUMING, STATUS_INTERACTION_FAILED):
                return self._format_sse(SSE_DONE, {})
            if status == STATUS_FAILED:
                return self._format_sse(
                    SSE_ERROR, {"error": row.get("error") or "Task failed"},
                )
            if status == STATUS_CANCELLED:
                return self._format_sse(
                    SSE_CANCELLED, {"message": "Task cancelled by user"},
                )
            if status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING):
                # Claim-to-launch window or a stranded row — not terminal:
                # end with no frame so the client reconnects.
                return None
        return self._format_sse(SSE_ERROR, {"error": "Task is not active."})

    # ==================================================================
    # 5. Internal helpers
    # ==================================================================

    async def _build_user_message_content(
        self,
        message: str,
        context: Dict[str, Any],
        project_id: str,
        session_id: str,
    ) -> str:
        """Build hidden status/citation/reminder blocks plus user-visible content.

        The reminder snapshots the session's unfinished tasks at submit
        time so the persisted content is final: message rows are written
        once and never modified afterwards, keeping rebuilt request
        prefixes byte-identical for prompt-cache hits.
        """
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
        reminder_block = ""
        unfinished = await unfinished_tasks(project_id, session_id)
        if unfinished:
            reminder_block = render_task_reminder_tag(unfinished)
        return f"{status_block}{citation_block}{attachments_block}{reminder_block}\n{cleaned_message}"

    @staticmethod
    def _strip_slash_command(message: str) -> tuple[str, bool]:
        stripped = message.strip()
        if not stripped.startswith("/plan"):
            return message, False
        if len(stripped) > 5 and not stripped[5].isspace():
            return message, False
        cleaned = stripped[5:].lstrip()
        return cleaned or "Create a plan for the current task.", True

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
