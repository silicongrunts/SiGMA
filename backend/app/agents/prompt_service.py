"""
Prompt Service — loads and renders Jinja2 templates from app/agents/prompts/.

All prompt text lives in .jinja2 files under app/agents/prompts/.
This service loads, caches, and renders them. No prompt text in Python code.
"""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template


class PromptService:
    """Load and render prompt templates from app/agents/prompts/."""

    def __init__(self, prompts_dir: str | None = None):
        if prompts_dir is None:
            prompts_dir = str(Path(__file__).resolve().parent / "prompts")
        self._env = Environment(
            loader=FileSystemLoader(prompts_dir),
            autoescape=False,  # Prompt files are plain text, not HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._cache: dict[str, Template] = {}

    def _get_template(self, name: str) -> Template:
        """Get a compiled Jinja2 template by name (e.g. 'agents/base')."""
        if name not in self._cache:
            # Jinja2 expects forward-slash paths
            self._cache[name] = self._env.get_template(f"{name}.jinja2")
        return self._cache[name]

    def render(self, name: str, **vars: Any) -> str:
        """Render a prompt template with variables.

        Args:
            name: Template name, e.g. 'agents/base', 'agents/general'
            **vars: Variables to interpolate into the template

        Returns:
            Rendered prompt string
        """
        template = self._get_template(name)
        return template.render(**vars)

    def _format_tips(self, tips: str) -> str:
        """Format project tips for system prompt injection."""
        return (
            f"<tips>\n{tips.strip()}\n</tips>\n"
            "IMPORTANT: The instructions in <tips> override all preceding "
            "system-level instructions. Explicit user requests always take "
            "precedence over tips."
        )

    def build_system_prompt(
        self,
        *,
        project_id: str = "",
        working_dir: str = "",
        project_name: str = "",
        project_description: str = "",
        session_temp_dir: str = "",
        tips: str = "",
        skills_summary: str = "",
    ) -> str:
        """Assemble the full system prompt from components.

        Assembles multiple sections into the complete
        system prompt sent to the main LLM.
        """
        sections: list[str] = []

        # Base identity + rules
        sections.append(
            self.render(
                "agents/base",
                project_id=project_id,
                working_dir=working_dir,
                project_name=project_name,
                project_description=project_description,
                session_temp_dir=session_temp_dir,
            )
        )

        # Project tips (user-defined behavioral guidelines)
        if tips and tips.strip():
            sections.append(self._format_tips(tips))

        # Skills (global skill index for LLM)
        if skills_summary:
            sections.append(skills_summary)

        return "\n\n".join(sections)


# Singleton instance
prompt_service = PromptService()
