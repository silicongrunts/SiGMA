"""
Prompt for the ask_user_question tool.
"""

PROMPT = """Ask the user questions only when you are genuinely blocked and cannot reasonably decide yourself. Every question interrupts the user — keep the count minimal.

Ask when you need:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.


How to ask well:
- One focused question beats several;
- Every question must be high-leverage — its answer should meaningfully change your next step.
- type "single" / "multi": options must be mutually exclusive and concrete (2-6). Set `recommended: true` on at most one option, only if you genuinely recommend it. Never write "(Recommended)" into the label text.
- type "text": for a specific value you cannot express as choices.
- Keep each question short and self-contained; give each option a concise label and a one-line description.

Notice:
Users will always be able to select "Other" to provide custom text input
"""
