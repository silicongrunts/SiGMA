"""
Prompt for the sleep tool.
"""

PROMPT = """Wait for a specified duration.

Use this when the user tells you to sleep or rest, when you have nothing to do,
or when you're waiting for something.

Prefer this over bash(sleep ...) — it doesn't hold a shell process.

`duration` is in seconds, must be non-negative. Values above 300 are silently
capped at 300 (the return string notes when this happens).

Output: "Slept for {N} seconds." on success, or "Error: duration must be
non-negative, got {value}" if a negative duration was supplied."""
