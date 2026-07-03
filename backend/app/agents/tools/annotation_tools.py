"""
Annotation tools — create, read, delete, reply to, and list file annotations.

5 tools: annotation_new, annotation_rm, annotation_get, annotation_reply, annotation_list.
"""

import json
import re
from html import escape as _html_escape

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import (
    PROMPT_ANNOTATION_NEW, PROMPT_ANNOTATION_RM, PROMPT_ANNOTATION_GET,
    PROMPT_ANNOTATION_REPLY, PROMPT_ANNOTATION_LIST,
)
from app.core.exceptions import FileMissingError
from app.core.logging import get_logger
from app.services.annotation_service import annotation_service, serialize_annotation
from app.services.file_service import file_service

logger = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────

_DIFF_RE = re.compile(
    r"<diff>\s*<before>(.*?)</before>\s*<after>(.*?)</after>\s*</diff>",
    re.DOTALL,
)
_LOOSE_DIFF_RE = re.compile(r"<diff\b[^>]*>(.*?)</diff>", re.DOTALL)


def _xml_escape(text: str) -> str:
    """Escape text for safe inclusion in XML output."""
    return _html_escape(text, quote=True)


def _parse_ids(value) -> list:
    """Normalize a string-or-array parameter to a list of non-empty strings."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        if value.strip().startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v).strip() for v in parsed if str(v).strip()]
            except (json.JSONDecodeError, ValueError):
                pass
        return [v.strip() for v in value.split(",") if v.strip()]
    try:
        return [str(v).strip() for v in value if str(v).strip()]
    except TypeError:
        return []


def _validate_diff_structure(content: str) -> str | None:
    """Check that all <diff> blocks have proper <before> and <after> children.

    Returns an error message on failure, or None on success.
    """
    for m in _LOOSE_DIFF_RE.finditer(content):
        block = m.group(1)
        before_open = block.count("<before>")
        before_close = block.count("</before>")
        after_open = block.count("<after>")
        after_close = block.count("</after>")

        if before_open == 0:
            return "Error: <diff> block is missing <before> tag"
        if after_open == 0:
            return "Error: <diff> block is missing <after> tag"
        if before_open > 1:
            return "Error: <diff> block contains multiple <before> tags — only one is allowed"
        if after_open > 1:
            return "Error: <diff> block contains multiple <after> tags — only one is allowed"
        if before_open != before_close:
            return "Error: <diff> block has unclosed <before> tag"
        if after_open != after_close:
            return "Error: <diff> block has unclosed <after> tag"
    return None


def validate_diffs(content: str, file_text: str) -> str | None:
    """Validate all <diff> blocks in *content* against *file_text*.

    Returns an error message on failure, or None on success.
    """
    # Structural validation (missing tags, extra tags, unclosed tags)
    struct_err = _validate_diff_structure(content)
    if struct_err:
        return struct_err

    matches = list(_DIFF_RE.finditer(content))
    if not matches:
        return None

    for m in matches:
        before = m.group(1)
        after = m.group(2)

        if not before.strip():
            return "Error: <diff> block has empty <before> — nothing to match"
        if not after.strip():
            return "Error: <diff> block has empty <after> — no replacement specified"

        count = file_text.count(before)
        if count == 0:
            return (
                f"Error: <diff> <before> not found in file. "
                f"Ensure the text matches exactly (whitespace, indentation, etc.). "
                f"Snippet: {_xml_escape(before[:100])}"
            )
        if count > 1:
            return (
                f"Error: <diff> <before> appears {count} times in file. "
                f"Provide more surrounding context to make it unique. "
                f"Snippet: {_xml_escape(before[:100])}"
            )

    return None


async def _read_project_file(project_id: str, file_name: str) -> str:
    """Read a project file. Raises FileMissingError if not found."""
    try:
        return await file_service.read_file(project_id, file_name)
    except FileMissingError:
        raise
    except Exception as e:
        raise FileMissingError(f"{file_name}: {e}")


# ── Tool implementations ────────────────────────────────────────────

async def _annotation_new(
    project_id: str,
    file_name: str,
    file_content: str,
    annotation_content: str,
) -> str:
    """Create a new annotation anchored to a text range in a project file."""
    # 0. Validate annotation_content is not empty
    if not annotation_content or not annotation_content.strip():
        return "Error: annotation_content cannot be empty"

    # 1. Read file (file_service.read_file enforces sandbox via safe_join)
    try:
        full_text = await _read_project_file(project_id, file_name)
    except FileMissingError:
        return f"Error: file not found: {file_name}"

    # 2. Locate file_content in file — must be unique
    if not file_content:
        return "Error: file_content cannot be empty"

    count = full_text.count(file_content)
    if count == 0:
        return (
            f"Error: file_content not found in {file_name}. "
            "Ensure the text matches exactly (whitespace, indentation, etc.)."
        )
    if count > 1:
        return (
            f"Error: file_content appears {count} times in {file_name}. "
            "Provide more surrounding context to make it unique."
        )

    from_pos = full_text.index(file_content)
    to_pos = from_pos + len(file_content)

    # 3. Validate diff blocks against file content
    diff_err = validate_diffs(annotation_content, full_text)
    if diff_err:
        return diff_err

    # 4. Create annotation via service
    try:
        result = await annotation_service.add_annotation(
            project_id=project_id,
            file_path=file_name,
            from_pos=from_pos,
            to_pos=to_pos,
            text=annotation_content,
            role="assistant",
        )
        return f"Annotation created successfully, ID: {result['id']}"
    except Exception as e:
        logger.exception("Failed to create annotation")
        return f"Failed to create annotation: {e}"


async def _annotation_rm(project_id: str, id) -> str:
    """Delete one or more annotations by ID."""
    ids = _parse_ids(id)
    if not ids:
        return "Error: no annotation IDs provided"

    parts = []
    for short_id in ids:
        success, err = await annotation_service.delete_annotation_by_prefix(project_id, short_id)
        if success:
            parts.append(f"Annotation {short_id} deleted")
        else:
            parts.append(f"Error for {short_id}: {err}")

    return "\n".join(parts)


async def _annotation_get(project_id: str, id) -> str:
    """Retrieve the full thread of one or more annotations."""
    ids = _parse_ids(id)
    if not ids:
        return "Error: no annotation IDs provided"

    parts = []
    for short_id in ids:
        annotation, err = await annotation_service.resolve_annotation(project_id, short_id)
        if err:
            parts.append(f"Error for {short_id}: {err}")
            continue

        data = serialize_annotation(annotation)
        parts.append("<annotation>")
        parts.append(f"<id>{data['id']}</id>")
        parts.append(f"<file_content>{_xml_escape(data.get('originalText', ''))}</file_content>")

        for reply in data.get("thread", []):
            role = reply.get("role", "unknown")
            text = reply.get("content", "")
            parts.append("<reply>")
            parts.append(f"<role>{role}</role>")
            parts.append(f"<text>{_xml_escape(text)}</text>")
            parts.append("</reply>")

        parts.append("</annotation>")

    return "\n".join(parts) if parts else "No annotations found."


async def _annotation_reply(
    project_id: str,
    id: str,
    reply_content: str,
) -> str:
    """Reply to an existing annotation."""
    annotation, err = await annotation_service.resolve_annotation(project_id, id)
    if err:
        return f"Error: {err}"

    file_name = annotation.file_path
    resolved_id = annotation.id

    # Read file and validate diff blocks
    try:
        full_text = await _read_project_file(project_id, file_name)
    except FileMissingError:
        return f"Error: annotation's file not found: {file_name}"

    diff_err = validate_diffs(reply_content, full_text)
    if diff_err:
        return diff_err

    # Persist reply via the service (owns UnitOfWork + unique-seq enforcement).
    result = await annotation_service.reply_annotation(
        project_id=project_id,
        file_path=file_name,
        anno_id=resolved_id,
        content=reply_content,
        role="assistant",
    )
    if not result.get("success"):
        return f"Error: {result.get('error', 'failed to add reply')}"

    return f"Reply added to annotation {id}"


async def _annotation_list(project_id: str, file_name) -> str:
    """List all annotations on one or more files."""
    names = _parse_ids(file_name)
    if not names:
        return "Error: no file names provided"

    all_parts = []
    for name in names:
        # Verify file exists (enforces sandbox)
        try:
            await _read_project_file(project_id, name)
        except FileMissingError:
            all_parts.append(f"Error: file not found: {name}")
            continue

        annotations = await annotation_service.list_annotations_by_file(project_id, name)

        if not annotations:
            all_parts.append(f"No annotations on {name}")
            continue

        parts = []
        for anno in annotations:
            data = serialize_annotation(anno)
            anno_id = data["id"]
            thread = data.get("thread", [])
            first_text = thread[0]["content"] if thread else ""
            summary = first_text[:80] + ("..." if len(first_text) > 80 else "")
            parts.append("<annotation>")
            parts.append(f"<id>{anno_id}</id>")
            parts.append(f"<summary>{_xml_escape(summary)}</summary>")
            parts.append("</annotation>")

        all_parts.append(f"--- {name} ({len(annotations)} annotations) ---")
        all_parts.extend(parts)

    return "\n".join(all_parts)


# ── Register tools ───────────────────────────────────────────────────

tool_registry.register(ToolDefinition(
    name="annotation_new",
    description="Create a new annotation on a project file. Anchors the annotation to a text range identified by file_content.",
    prompt=PROMPT_ANNOTATION_NEW,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Project-relative file path only",
            },
            "file_content": {
                "type": "string",
                "description": "A text snippet from the file that uniquely identifies the annotation range (must appear exactly once)",
            },
            "annotation_content": {
                "type": "string",
                "description": "Annotation text, optionally containing <diff> blocks",
            },
        },
        "required": ["file_path", "file_content", "annotation_content"],
    },
    call=lambda file_path, file_content, annotation_content, project_id="": _annotation_new(
        project_id, file_path, file_content, annotation_content,
    ),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="annotation_rm",
    description="Delete one or more annotations by ID.",
    prompt=PROMPT_ANNOTATION_RM,
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Annotation ID(s) (provide at least first 8 chars for prefix match)",
            },
        },
        "required": ["id"],
    },
    call=lambda id, project_id="": _annotation_rm(project_id, id),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="annotation_get",
    description="Retrieve the full conversation thread of an annotation.",
    prompt=PROMPT_ANNOTATION_GET,
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Annotation ID(s) (provide at least first 8 chars for prefix match)",
            },
        },
        "required": ["id"],
    },
    call=lambda id, project_id="": _annotation_get(project_id, id),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="annotation_reply",
    description="Reply to an existing annotation. Supports <diff> blocks for suggesting code changes.",
    prompt=PROMPT_ANNOTATION_REPLY,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Annotation ID (provide at least first 8 chars for prefix match)"},
            "reply_content": {
                "type": "string",
                "description": "Reply text, optionally containing <diff> blocks",
            },
        },
        "required": ["id", "reply_content"],
    },
    call=lambda id, reply_content, project_id="": _annotation_reply(
        project_id, id, reply_content,
    ),
    requires_project_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="annotation_list",
    description="List all annotations on a project file.",
    prompt=PROMPT_ANNOTATION_LIST,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "Project-relative file path(s) only",
            },
        },
        "required": ["file_path"],
    },
    call=lambda file_path, project_id="": _annotation_list(project_id, file_path),
    requires_project_id=True,
    is_read_only=True,
))
