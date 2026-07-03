"""
Prompt for the ``skill_load`` tool.

Instructs the LLM on when and how to load skills.
"""

PROMPT = """\
## skill_load

Load the full content of a skill by its folder ID.  Use this tool when the user's
task matches or is partially relevant to one of the available skills listed in the
<skills> section of your system prompt.

**Usage:**
- `id` — the folder name of the skill (as shown in <skills><id>…</id>).
- `file_path` — optional relative path to a specific file inside the skill directory.
  Defaults to `SKILL.md`.  Use this to read reference files, templates, or scripts
  bundled with the skill.

**Rules:**
1. Always load a skill with `skill_load` **before** following its instructions.
2. Do not load the same skill more than once per conversation.
3. After loading, follow the Markdown body of the skill strictly.
4. If `file_path` is provided, it must be a **relative** path (no leading `/`, no `..`).
"""
