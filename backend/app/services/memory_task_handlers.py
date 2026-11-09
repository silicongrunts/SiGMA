"""Background memory task handlers — extraction and consolidation.

Registered into the shared background-task handler registry (see
``library_task_protocol``); dispatched by ``MemoryTaskRunner`` from the
dedicated memory queue. Both handlers are bounded agent runs over
``memory_agent_loop``; both are idempotent-friendly: the extraction cursor
only advances after a run completes, so a retried run reprocesses the same
window.

Extraction request shape (cache contract): ``[system, ...history,
extraction_prompt]`` where the system prompt and history are rebuilt through
the same code paths the main loop uses (``prompt_service`` +
``get_messages_for_llm`` + ``entry_from_history``), and the LLM calls carry
the chat session's routing id so a byte-identical prefix can hit the
provider prefix cache. The hit is best-effort, never guaranteed: only the
first extraction (cursor at the start) shares the main loop's last full
request — later windows share the system prefix only — and messages carrying
images/attachments, or tool rows the interactive loop decorates with image
references, may diverge because the twin rebuild works from stored content.
The extraction instruction is appended as the final user message.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork
from app.services.llm_loop_runner import LLMLoopRunner

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


async def _build_system_prompt_like_main_loop(project_id: str, session_id: str) -> str:
    """System-prompt twin of ``QueryLoop._build_messages`` (same builder and
    kwargs, so the prefix matches the main loop byte for byte)."""
    from app.agents.prompt_service import prompt_service
    from app.core.config import settings as app_settings
    from app.services.project_service import project_service
    from app.services.session_temp_service import session_temp_service
    from app.services.skill_service import skill_service

    async with UnitOfWork(project_id) as uow:
        tips = await uow.config.get("tips", "")
    skills_summary = skill_service.build_skills_prompt()
    project_meta = project_service.get_project_meta(project_id)
    session_temp_dir = session_temp_service.session_dir_for_prompt(project_id, session_id)
    return prompt_service.build_system_prompt(
        project_id=project_id,
        working_dir=str(app_settings.get_project_path(project_id)),
        project_name=project_meta["name"],
        project_description=project_meta["description"],
        session_temp_dir=session_temp_dir,
        tips=tips,
        skills_summary=skills_summary,
    )


def _memory_manifest(project_id: str) -> list[dict]:
    """List memory files with light metadata (name, chars, description).

    ``chars`` counts characters, never bytes: the consolidation prompt's
    size rule is expressed in characters, and CJK byte sizes run ~3x that
    count, so byte figures would flag compliant files as oversized.
    """
    from app.services.memory_service import memory_root

    root = memory_root(project_id)
    entries: list[dict] = []
    try:
        paths = sorted(root.iterdir())
    except OSError:
        return entries
    for path in paths:
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        description = ""
        for line in content.splitlines()[:12]:
            stripped = line.strip()
            if stripped.startswith("description:"):
                description = stripped[len("description:"):].strip().strip("\"'")
                break
        entries.append({
            "file": path.name,
            "chars": len(content),
            "description": description[:120],
        })
    return entries


def _manifest_block(entries: list[dict]) -> str:
    """Render the manifest as the prompt's existing-files section.

    Disk I/O in the callers goes through ``asyncio.to_thread`` — same
    discipline as the injection read in memory_service.
    """
    if not entries:
        return (
            "\n\n## Existing memory files\n\n"
            "(none yet — the memory directory is empty)"
        )
    lines = "\n".join(
        f"- {e['file']} ({e['chars']} chars) — {e['description'] or '(no description)'}"
        for e in entries
    )
    return (
        "\n\n## Existing memory files\n\n" + lines
        + "\n\nCheck this list before writing — update an existing file "
        "rather than creating a duplicate."
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


async def handle_memory_extract(ctx, payload: dict) -> None:
    """Extract memories from the not-yet-extracted tail of one chat session."""
    from app.services.memory_agent_loop import (
        EXTRACTION_ALLOWED_TOOLS,
        run_memory_agent_loop,
    )
    from app.services.memory_service import (
        get_cursor,
        memory_index_path,
        memory_root,
        memory_service,
        set_cursor,
    )

    project_id = ctx.project_id
    session_id = (payload or {}).get("session_id", "")
    if not session_id:
        logger.info("Memory extraction for %s skipped: no session", project_id)
        return

    cursor = await get_cursor(project_id, session_id)
    system_prompt = await _build_system_prompt_like_main_loop(project_id, session_id)

    async with UnitOfWork(project_id) as uow:
        history_rows = await uow.messages.get_messages_for_llm(session_id)

    pairs = [(row, LLMLoopRunner.entry_from_history(row, row.content)) for row in history_rows]
    recent = [(row, entry) for row, entry in pairs if row.seq > cursor]
    max_seq = max((row.seq for row, _ in pairs), default=cursor)

    if not recent:
        logger.info(
            "Memory extraction for %s/%s skipped: nothing after cursor seq %d",
            project_id, session_id, cursor,
        )
        return

    from app.agents.prompt_service import prompt_service

    manifest = await asyncio.to_thread(_memory_manifest, project_id)
    prompt = prompt_service.render(
        "agents/memory_extraction_prompt",
        message_count=len(recent),
        memory_dir=str(memory_root(project_id)),
        existing_memories=_manifest_block(manifest),
    )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(entry for _, entry in recent)
    messages.append({"role": "user", "content": prompt})
    # Cache markers are placed per call inside run_memory_agent_loop
    # (extraction's anchor rests on the last history message — the byte
    # position the main loop's request cached).

    logger.info(
        "Memory extraction running for %s/%s: %d new message(s), cursor=%d",
        project_id, session_id, len(recent), cursor,
    )
    result = await run_memory_agent_loop(
        project_id=project_id,
        messages=messages,
        tool_schemas=_extraction_schemas(),
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=settings.memory.extraction_max_turns,
        max_tokens=_supervisor_max_tokens(),
        cancel_event=ctx.cancel_event,
        task_label="memory-extract",
        # Route through the replayed chat session so the byte-identical
        # prefix lands on the instance holding the main loop's cache.
        routing_session_id=session_id,
    )
    if ctx.cancel_event.is_set():
        raise asyncio.CancelledError()

    # Success (including a no-op) advances the cursor: the window is done.
    await set_cursor(project_id, session_id, max_seq)

    # Fact files this run actually wrote deplete the project-wide
    # consolidation window — a no-op run writes nothing and leaves it
    # untouched. Distinct files, not tool calls: a write plus a follow-up
    # edit is one file's worth of new material. The index edit is
    # bookkeeping and is excluded; consolidation's own rewrites don't count
    # either (reorganizing is not new material, and its task completion
    # resets the window anyway). Empty session id: a background write must
    # not reset any chat session's extraction window — task completion owns
    # the source session's reset.
    index = str(memory_index_path(project_id).resolve())
    fact_files = {p for p in result.memory_mutations if p != index}
    for path in sorted(fact_files):
        await memory_service.note_memory_file_write(
            project_id, "", Path(path),
        )

    logger.info(
        "Memory extraction for %s/%s done: turns=%d noop=%s errors=%d wrote=%d",
        project_id, session_id, result.turns, result.is_noop,
        result.tool_error_count, len(fact_files),
    )


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------


async def handle_memory_consolidate(ctx, payload: dict) -> None:
    """Run the memory consolidation agent over the whole memory directory."""
    from app.agents.prompt_service import prompt_service
    from app.services.memory_agent_loop import (
        CONSOLIDATION_ALLOWED_TOOLS,
        run_memory_agent_loop,
    )
    from app.services.memory_service import (
        MEMORY_USAGE_MAX_ENTRIES,
        memory_root,
        memory_usage_summary,
    )

    project_id = ctx.project_id
    manifest = await asyncio.to_thread(_memory_manifest, project_id)
    usage = await memory_usage_summary(project_id)
    usage_lines = "\n".join(
        f"- {u.get('time', '')} {u.get('file', '')}: {u.get('memory_use', '')}"
        for u in usage[-50:]
    )
    usage_block = (
        f"\n\n## Recent memory usage (newest last, max {MEMORY_USAGE_MAX_ENTRIES} kept)\n\n"
        + (usage_lines or "(no recorded usage)")
    ) if usage else "\n\n## Recent memory usage\n\n(no recorded usage)"

    system_prompt = prompt_service.render(
        "agents/memory_consolidation_system",
        memory_dir=str(memory_root(project_id)),
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        max_file_chars=settings.memory.max_file_chars,
    )
    user_brief = (
        f"Consolidate the project memory now. Directory: {memory_root(project_id)}\n\n"
        f"## Files on disk\n\n"
        + ("\n".join(
            f"- {e['file']} ({e['chars']} chars) — {e['description'] or '(no description)'}"
            for e in manifest
        ) or "(memory directory is empty — nothing to consolidate)")
        + usage_block
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_brief},
    ]
    # Cache markers are placed per call inside run_memory_agent_loop; this
    # fresh synthetic session has no prior cache to read, so the first call
    # rides on the system marker alone.

    logger.info("Memory consolidation running for %s: %d file(s)", project_id, len(manifest))
    result = await run_memory_agent_loop(
        project_id=project_id,
        messages=messages,
        tool_schemas=_consolidation_schemas(),
        allowed_tools=CONSOLIDATION_ALLOWED_TOOLS,
        max_turns=settings.memory.consolidation_max_turns,
        max_tokens=_supervisor_max_tokens(),
        cancel_event=ctx.cancel_event,
        task_label="memory-consolidate",
    )
    if ctx.cancel_event.is_set():
        raise asyncio.CancelledError()
    logger.info(
        "Memory consolidation for %s done: turns=%d noop=%s errors=%d",
        project_id, result.turns, result.is_noop, result.tool_error_count,
    )


def _extraction_schemas() -> list[dict]:
    """Full supervisor catalog — identical to the main loop's payload."""
    from app.agents.tool_schema_service import tool_schemas_for_model_role

    return tool_schemas_for_model_role("supervisor")


def _consolidation_schemas() -> list[dict]:
    from app.agents.tool_schema_service import tool_schemas_for_model_role

    return tool_schemas_for_model_role(
        "supervisor",
        allowed_tools=frozenset({"read", "edit", "write", "rm"}),
    )


def _supervisor_max_tokens() -> int:
    from app.services.compaction_service import compaction_service

    return compaction_service.budget_for_role("supervisor").response_max_tokens


# ---------------------------------------------------------------------------
# Registration (import-time, mirroring the other executors)
# ---------------------------------------------------------------------------


from app.services.library_task_protocol import (  # noqa: E402
    KIND_MEMORY_CONSOLIDATE,
    KIND_MEMORY_EXTRACT,
    register_task_handler,
)

register_task_handler(KIND_MEMORY_EXTRACT, handle_memory_extract)
register_task_handler(KIND_MEMORY_CONSOLIDATE, handle_memory_consolidate)
