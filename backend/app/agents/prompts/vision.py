"""Prompt for the vision_analyze tool."""

PROMPT = """Analyze an image using the configured vision model. \
Use this when the current model cannot inspect image attachments directly. \
Pass the absolute or project-relative image_path from the user's status \
or read tool result and a specific prompt describing what to inspect. \
Supports PNG, JPEG, WebP, and GIF images."""
