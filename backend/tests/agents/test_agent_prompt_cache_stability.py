"""Agent prompt invariants that protect LLM prefix-cache reuse."""

from app.agents.prompt_service import prompt_service


def test_plan_prompt_is_stable_across_task_texts():
    """Plan tasks belong in user messages, not the system prompt."""
    first = prompt_service.render("agents/plan")
    second = prompt_service.render("agents/plan")

    assert first == second
    assert "{{ prompt }}" not in first
    assert "# Your Task" not in first


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
