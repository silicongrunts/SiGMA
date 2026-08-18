"""
Prompt for the submit_plan_for_approval tool.
"""

PROMPT = """Submit a complete implementation plan (Markdown) for user approval.

The loop pauses until the user responds. On approval the plan is saved to internal session storage and the result contains its path; on rejection the result contains the user's feedback — revise the plan and submit again."""
