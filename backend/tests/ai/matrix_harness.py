"""Shared harness for the chat pipeline matrix tests.

Provides the scripted-LLM helper (``script_llm`` and one ``script_*`` factory
per tool-call shape), the runtime lifecycle helpers (``make_turn``,
``start_running_task``), and the state/event readers every pipeline test
asserts against. Plain module (no ``test_`` prefix) so pytest never collects
it; the split matrix test modules import from here.
"""

from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.core.config import settings
from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.project_service import project_service


USAGE = {"prompt_tokens": 100, "completion_tokens": 20}


@pytest.fixture(autouse=True)
def use_fixture_project_root(project, monkeypatch):
    root = settings.USERDATA_DIR.resolve()
    monkeypatch.setattr(project_service, "USERDATA_DIR", root)
    monkeypatch.setattr(project_service, "SIGMA_DIR", root / ".SiGMA")
    monkeypatch.setattr(project_service, "PROJECTS_FILE", root / ".SiGMA" / "projects.json")


def script_llm(monkeypatch, steps):
    """Script ``_stream_llm`` responses ("tools", "text", "raise") in order.

    The same staticmethod serves the main loop and every subagent loop, so
    one call sequence covers the whole nested turn. The exact message list
    handed to each call is captured (order frozen at call time) so tests can
    pin what the loop sends across the LLM service boundary.
    """
    calls = {"n": 0, "messages": []}

    async def fake_stream_llm(ctx, messages, delta_queue):
        index = calls["n"]
        calls["n"] += 1
        calls["messages"].append(list(messages))
        if index >= len(steps):
            raise AssertionError(f"unexpected LLM call #{index + 1}")
        kind, payload = steps[index]
        if kind == "raise":
            raise payload
        if kind == "tools":
            return ("", "", payload, USAGE)
        return (payload, "", [], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))
    return calls


async def make_turn(project, text="do the work"):
    """Create session + persisted user message + queued task, as submit_chat would."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
        await uow.messages.create(session_id=session.id, role="user", content=text)
        task_id = generate_id()
        await uow.task_state.set_queued(task_id, session_id=session.id)
    return session.id, task_id


async def start_running_task(project, session_id):
    """Start a fresh task row for one turn/resume, promoted to running.

    Mirrors the runtime lifecycle: every submit creates a NEW task row and
    the runner's ``mark_running`` promotes it queued→running before the loop
    starts. The resume's identity claim then completes the
    PREVIOUS awaiting_input row (not the running one), so a re-park
    mid-resume via the guarded ``mark_awaiting_input`` (queued/running only)
    works as in prod.

    A still-runnable prior row is completed first: the schema allows at most
    one runnable row per session, and in production the runner finalizes the
    previous turn's row before the next submit claims the session. Interaction
    checkpoint rows are left for the resume to consume.
    """
    async with UnitOfWork(project) as uow:
        active = await uow.task_state.get_active_by_session(session_id)
        if active and active["status"] in ("queued", "running", "cancelling"):
            await uow.task_state.mark_completed(active["task_id"])
        task_id = generate_id()
        await uow.task_state.set_queued(task_id, session_id=session_id)
        await uow.task_state.mark_running(task_id)
    return task_id


def script_main_tool_call(tool_call_id="call_main"):
    return ("tools", [{
        "id": tool_call_id, "name": "bash",
        "params": {"command": "mkdir -p made_by_main"},
    }])


def script_parallel_batch():
    """One assistant message whose first (gated) call pauses the run, leaving
    the sibling calls unanswered in the checkpointed history."""
    return ("tools", [
        {
            "id": "call_main", "name": "bash",
            "params": {"command": "mkdir -p made_by_main"},
        },
        *[
            {
                "id": f"call_sib_{i}", "name": "read",
                "params": {"file_path": f"sib_{i}.md"},
            }
            for i in range(4)
        ],
    ])


def script_write_tool_call(project_dir, tool_call_id="call_write"):
    """A gated write to an absolute path inside the project sandbox.

    Absolute paths keep the write tool fully functional in this harness
    (``write_file_absolute`` never consults the project registry)."""
    return ("tools", [{
        "id": tool_call_id, "name": "write",
        "params": {
            "file_path": str(project_dir / "made_by_write" / "note.md"),
            "content": "body",
        },
    }])


def script_read_tool_call(project_dir, tool_call_id="call_read"):
    return ("tools", [{
        "id": tool_call_id, "name": "read",
        "params": {"file_path": str(project_dir / "report.md")},
    }])


def script_edit_tool_call(project_dir, tool_call_id="call_edit"):
    """A gated edit of an existing file. The must-read preflight only passes
    because a read of the same file precedes it in the script."""
    return ("tools", [{
        "id": tool_call_id, "name": "edit",
        "params": {
            "file_path": str(project_dir / "report.md"),
            "old_string": "alpha", "new_string": "beta",
        },
    }])


def script_notebook_read_call(tool_call_id="call_nb_read"):
    return ("tools", [{
        "id": tool_call_id, "name": "notebook_read",
        "params": {"notebook_path": "analysis.ipynb"},
    }])


def script_notebook_run_call(tool_call_id="call_nb"):
    return ("tools", [{
        "id": tool_call_id, "name": "notebook_run_cell",
        "params": {"notebook_path": "analysis.ipynb", "cell_id": "c1"},
    }])


def script_agent_tool_call(tool_call_id="call_agent"):
    return ("tools", [{
        "id": tool_call_id, "name": "agent",
        "params": {"agent_type": "general", "prompt": "create the directory"},
    }])


def script_fork_agent_tool_call(tool_call_id="call_agent"):
    """Fork mode: empty agent_type — the subagent inherits the parent
    conversation and has no persistent session."""
    return ("tools", [{
        "id": tool_call_id, "name": "agent",
        "params": {"agent_type": "", "prompt": "write the file"},
    }])


def script_plan_agent_tool_call(tool_call_id="call_agent"):
    return ("tools", [{
        "id": tool_call_id, "name": "agent",
        "params": {"agent_type": "plan", "prompt": "draft the plan"},
    }])


def script_plan_submission(tool_call_id="call_plan", plan="# The real plan"):
    return ("tools", [{
        "id": tool_call_id, "name": "submit_plan_for_approval",
        "params": {"plan_content": plan},
    }])


def script_subagent_bash(tool_call_id="call_sub_bash", command="mkdir -p made_by_agent"):
    return ("tools", [{
        "id": tool_call_id, "name": "bash",
        "params": {"command": command},
    }])


def script_fork_ask_user(tool_call_id="call_sub_ask"):
    """A fork subagent asking the user a question — a valid ask_user_question
    payload (phase 1), which is what parks the run."""
    return ("tools", [{
        "id": tool_call_id, "name": "ask_user_question",
        "params": {"questions": [{
            "question": "Which database?",
            "type": "single",
            "options": [
                {"label": "Postgres", "description": "relational server"},
                {"label": "SQLite", "description": "embedded file"},
            ],
        }]},
    }])


def script_subagent_write(project_dir, tool_call_id="call_sub_write"):
    """A gated write issued by a subagent — absolute in-sandbox path, same
    contract as script_write_tool_call."""
    return ("tools", [{
        "id": tool_call_id, "name": "write",
        "params": {
            "file_path": str(project_dir / "made_by_agent_write" / "note.md"),
            "content": "agent body",
        },
    }])


async def collect(loop):
    return [event async for event in loop.run()]


async def load_state(project, session_id, task_id):
    async with UnitOfWork(project) as uow:
        status = await uow.task_state.get_status(task_id)
        interaction = await uow.task_state.get_pending_interaction_by_session(session_id)
        messages = await uow.messages.get_messages(session_id)
    return status, interaction, messages


async def owned_response(project, session_id, response):
    """Build the wire response from the checkpoint currently under test."""
    async with UnitOfWork(project) as uow:
        state = await uow.task_state.get_pending_interaction_by_session(session_id)
    data = state["interaction_data"]
    checkpoint = state["checkpoint"]
    return {
        **response,
        "task_id": checkpoint.get("task_id", data.get("task_id", "")),
        "interaction_id": checkpoint["interaction_id"],
        "interaction_type": checkpoint["interaction_type"],
    }


async def load_state_row(project, task_id):
    async with UnitOfWork(project) as uow:
        return await uow.task_state.get_by_id(task_id)


async def park_permission_checkpoint(project, session_id, task_id):
    """Park a task on a permission checkpoint, as a gated tool would."""
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(task_id, {
            "tool_name": "bash",
            "tool_call_id": "call_park",
            "tool_args": {"command": "mkdir -p parked"},
            "interaction_data": {"interaction_type": "permission"},
            "checkpoint": {
                "task_id": task_id,
                "interaction_id": f"interaction-{task_id}",
                "interaction_type": "permission",
            },
        })
