"""
Prompt for the ask_user_question tool.
"""

PROMPT = """Ask the user questions only when genuinely blocked and unable to reasonably decide yourself; every question interrupts the user.

- 1-4 questions per call. Each must be high-leverage: its answer should meaningfully change your next step.
- type "single"/"multi": 2-6 concrete, mutually exclusive options. Set recommended: true on at most one option, and never write "(Recommended)" into the label text.
- type "text": for a specific value that cannot be expressed as choices.
- Keep each question short and self-contained; give each option a concise label and a one-line description.

The user can always select "Other" and type a custom answer."""
