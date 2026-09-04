"""Agent prompt invariants that protect LLM prefix-cache reuse."""

from app.agents.prompt_service import prompt_service


def test_plan_prompt_has_no_task_slot():
    """Plan tasks belong in user messages, not the system prompt."""
    rendered = prompt_service.render("agents/plan")

    assert "{{ prompt }}" not in rendered
    assert "# Your Task" not in rendered


def test_plan_prompt_is_independent_of_task_text():
    """The plan prompt template has no task-text slot: rendering it with two
    different task texts yields byte-identical prompts, so the system prompt
    stays prefix-cache stable across tasks."""
    with_task_a = prompt_service.render("agents/plan", prompt="Task A: fix the login bug")
    with_task_b = prompt_service.render("agents/plan", prompt="Task B: write the API docs")

    assert with_task_a == with_task_b
    assert "fix the login bug" not in with_task_a
    assert "write the API docs" not in with_task_a


def test_agent_system_prompts_do_not_embed_task_slots():
    """Dynamic task text must not be rendered into agent system prompts."""
    prompts = {
        "general": prompt_service.render("agents/general", project_id="proj-a"),
        "explore": prompt_service.render("agents/explore"),
        "plan": prompt_service.render("agents/plan"),
    }

    for rendered in prompts.values():
        assert "{{ prompt }}" not in rendered
        assert "# Task" not in rendered
        assert "# Your Task" not in rendered
