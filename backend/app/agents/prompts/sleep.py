"""
Prompt for the sleep tool.
"""

PROMPT = """Wait for a specified duration.

Use when the user asks you to wait, or when you need to pause between steps. Prefer this over bash(sleep ...) — it holds no shell process.

Usage:
- `duration` is in seconds (default 5), must be non-negative, and is capped at 300; the result notes when a cap was applied.

Output: "Slept for {N} seconds." on success, or "Error: duration must be non-negative, got {value}" on a negative duration."""
