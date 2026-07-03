"""
Agent Registry — defines available agent types and their capabilities.

Used for system prompt descriptions only. Actual execution is handled by
AgentService (backend/app/services/agent_service.py).
"""

from dataclasses import dataclass


@dataclass
class AgentDefinition:
    """Definition of a sub-agent the main LLM can spawn."""

    name: str
    description: str
    when_to_use: str
    model: str = "supervisor"  # "supervisor" or "ra"
    system_prompt: str = ""    # Prompt template name, e.g. "agents/general"


class AgentRegistry:
    """Central registry for agent definitions."""

    def __init__(self):
        self._agents: dict[str, AgentDefinition] = {}

    def register(self, agent: AgentDefinition) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinition | None:
        return self._agents.get(name)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._agents.values())

    @property
    def names(self) -> list[str]:
        return list(self._agents.keys())


# Global singleton
agent_registry = AgentRegistry()


# ── Register built-in agents ──

agent_registry.register(AgentDefinition(
    name="general",
    description="General-purpose agent for complex, multi-step tasks.",
    when_to_use=(
        "For implementation work, multi-step operations, code analysis, "
        "and tasks requiring tool execution. Creates a persistent session "
        "that can be resumed with resume_id."
    ),
    model="supervisor",
    system_prompt="agents/general",
))

agent_registry.register(AgentDefinition(
    name="explore",
    description="Fast agent for read-only project, library, and browser exploration.",
    when_to_use=(
        "For read-only investigation: finding files by pattern, searching code "
        "or documents, understanding project structure, querying the Library "
        "knowledge base, or browsing the web. Use when a simple glob or grep "
        "is insufficient."
    ),
    model="ra",
    system_prompt="agents/explore",
))

agent_registry.register(AgentDefinition(
    name="plan",
    description="Software architect agent for designing implementation plans.",
    when_to_use=(
        "For planning complex implementation tasks that need user approval. "
        "The plan agent investigates the project, Library, and web sources "
        "(read-only), then creates a structured plan that the user can "
        "review and approve."
    ),
    model="supervisor",
    system_prompt="agents/plan",
))
