"""Pure helpers for chat image attachment metadata hidden in message text."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ATTACHMENTS_DIR = ".SiGMA/chat_attachments"
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_CHAT_IMAGE_BYTES = 12 * 1024 * 1024

_ATTACHMENTS_TAG_RE = re.compile(r"<attachments>(.*?)</attachments>\s*", re.DOTALL)
_IMAGE_REFS_TAG_RE = re.compile(r"<image_refs>(.*?)</image_refs>\s*", re.DOTALL)


def render_attachments_tag(attachments: list[dict[str, Any]]) -> str:
    cleaned = [normalize_attachment(item) for item in attachments]
    if not cleaned:
        return ""
    return "\n<attachments>" + json.dumps(cleaned, ensure_ascii=False) + "</attachments>"


def extract_attachments(text: str) -> list[dict[str, Any]]:
    match = _ATTACHMENTS_TAG_RE.search(text or "")
    if not match:
        return []
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    attachments = []
    for item in raw:
        if isinstance(item, dict):
            normalized = normalize_attachment(item)
            if normalized.get("path"):
                attachments.append(normalized)
    return attachments


def strip_attachments_tag(text: str) -> str:
    return _ATTACHMENTS_TAG_RE.sub("", text or "")


def render_image_refs_tag(image_refs: list[dict[str, Any]]) -> str:
    cleaned = [normalize_image_ref(item) for item in image_refs]
    cleaned = [item for item in cleaned if item.get("path")]
    if not cleaned:
        return ""
    return "\n<image_refs>" + json.dumps(cleaned, ensure_ascii=False) + "</image_refs>"


def extract_image_refs(text: str) -> list[dict[str, Any]]:
    match = _IMAGE_REFS_TAG_RE.search(text or "")
    if not match:
        return []
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    refs = []
    for item in raw:
        if isinstance(item, dict):
            normalized = normalize_image_ref(item)
            if normalized.get("path"):
                refs.append(normalized)
    return refs


def strip_image_refs_tag(text: str) -> str:
    return _IMAGE_REFS_TAG_RE.sub("", text or "")


def strip_internal_image_tags(text: str) -> str:
    return strip_image_refs_tag(strip_attachments_tag(text))


def format_attachment_status(attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return ""
    lines = [
        "User uploaded image(s):",
        *[
            f"- {item['path']} ({item.get('mime_type') or 'image'})"
            for item in attachments
            if item.get("path")
        ],
        "Use the vision_analyze tool with a prompt to inspect these images when needed.",
    ]
    return "\n".join(lines)


def normalize_attachment(item: dict[str, Any]) -> dict[str, Any]:
    path = str(item.get("path") or "").strip()
    mime_type = str(item.get("mime_type") or item.get("mime") or "").strip()
    name = str(item.get("name") or Path(path).name).strip()
    size = item.get("size")
    normalized: dict[str, Any] = {
        "path": path,
        "mime_type": mime_type,
        "name": name,
    }
    if isinstance(size, int) and size >= 0:
        normalized["size"] = size
    return normalized


def normalize_image_ref(item: dict[str, Any]) -> dict[str, Any]:
    path = str(item.get("path") or "").strip()
    mime_type = str(item.get("mime_type") or item.get("mime") or "").strip()
    name = str(item.get("name") or Path(path).name).strip()
    source = str(item.get("source") or "").strip()
    text = str(item.get("text") or "").strip()
    normalized: dict[str, Any] = {
        "path": path,
        "mime_type": mime_type,
        "name": name,
    }
    if source:
        normalized["source"] = source
    if text:
        normalized["text"] = text
    return normalized
