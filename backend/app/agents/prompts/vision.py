"""Prompt for the vision_analyze tool."""

PROMPT = """Analyze an image with the configured vision model. Use when the current model cannot inspect images directly.

Usage:
- image_path: absolute or project-relative path to the image (e.g. the path returned by the read tool). Supports PNG, JPEG, WebP, and GIF, up to 12 MB.
- question: what to inspect or answer about the image.

Output: the analysis as text, or an "Error: ..." string on failure (vision model not configured, image missing or unreadable)."""
