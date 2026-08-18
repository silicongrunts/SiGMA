"""
Prompt for the ``skill_load`` tool.

Instructs the LLM on when and how to load skills.
"""

PROMPT = """\
Load a skill's content by folder ID — the <id> value from the <skills> list in your system prompt.

Usage:
- id: the skill's folder name.
- file_path: optional file inside the skill directory, relative only (no leading /, no ..). Defaults to SKILL.md. Use it to read the skill's reference files, templates, or scripts.

Load a skill before following its instructions, and do not load the same skill more than once per conversation."""
