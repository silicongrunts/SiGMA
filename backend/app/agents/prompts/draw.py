"""Prompt for the draw_image tool."""

PROMPT = """Generate an image with the configured draw model and save it under `.SiGMA/draw/` in the current project.

Usage:
- prompt: a detailed visual description — subject, composition, style, colors, labels, and constraints.

Output: "Generated image saved at `{path}`." plus the Markdown snippet ![](path) that displays the image in chat."""
