"""Shared core utilities — identifiers, time, path safety, image parsing.

This module groups four small, dependency-light helper families that are
imported across every backend layer.  Keeping them in one place keeps the
``core`` surface easy to scan while preserving each helper's original
contract.

Sections
--------
* Identifiers — ``generate_id``
* Time — ``utcnow``, ``parse_iso``, ``to_iso``
* Path safety — ``is_within``, ``sanitize_filename``
* Image parsing — ``detect_image_media_type``, ``image_dimensions``
"""

from __future__ import annotations

import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from app.core.exceptions import FileSystemError


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def generate_id() -> str:
    """Generate a 16-char hex ID (64-bit entropy, no hyphens).

    Used as the standard ID format throughout the application:

    * Short enough to be token-efficient in LLM contexts.
    * Long enough (64-bit) for zero practical collision risk.
    * Clean hex string with no hyphens.
    """
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
#
# Strategy: **naive UTC everywhere internally.**
#   * ``utcnow()`` returns a naive UTC datetime (no ``tzinfo``).
#   * SQLite DateTime columns strip timezone info on round-trip, so naive
#     datetimes eliminate the aware-vs-naive ``TypeError`` that occurs when
#     subtracting a DB-read datetime from an aware one.
#   * ``to_iso()`` appends ``+00:00`` at API output boundaries so the
#     frontend can correctly interpret timestamps as UTC.
#   * ``parse_iso()`` parses ISO strings (with or without timezone) and
#     returns naive UTC — safe for arithmetic with ``utcnow()``.


def utcnow() -> datetime:
    """Return the current time as a naive UTC datetime.

    All internal datetime arithmetic should use this function so that
    naive - naive comparisons never raise ``TypeError``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 datetime string and return a naive UTC datetime.

    Handles both timezone-aware strings (e.g. ``2026-05-28T12:00:00+00:00``)
    and naive strings (e.g. ``2026-05-28 12:00:00``).  Always returns naive
    UTC for safe arithmetic with ``utcnow()``.
    """
    if dt_str is None:
        return None
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Format a datetime as an ISO 8601 string with UTC timezone.

    Naive datetimes (the norm inside SiGMA) get ``+00:00`` appended so
    the frontend ``new Date()`` interprets them as UTC.
    """
    if dt is None:
        return None
    s = dt.isoformat()
    if dt.tzinfo is None and '+' not in s and not s.endswith('Z'):
        s += '+00:00'
    return s


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
#
# Centralises two operations that every filesystem-touching module needs.
# Using these consistently eliminates the ``str.startswith()`` prefix-bypass
# class of path-traversal vulnerabilities.


def is_within(child: Path, parent: Path) -> bool:
    """Return ``True`` if *child* is *parent* itself or a descendant.

    Both arguments are resolved internally so that symlinks and ``..``
    components are eliminated before the containment check.  This makes
    the function safe to call even when the caller has not resolved the
    paths first.

    Uses ``Path.is_relative_to()`` (Python 3.9+) which correctly handles
    prefix-ambiguity — e.g. ``/data/abc`` vs ``/data/abcd``.
    """
    child_resolved = child.resolve()
    parent_resolved = parent.resolve()
    return child_resolved == parent_resolved or child_resolved.is_relative_to(parent_resolved)


def sanitize_filename(filename: str) -> str:
    """Return a safe basename from a client-provided *filename*.

    Only pure basenames are accepted — any path component (``/`` or ``\\``),
    traversal sequence (``..``), hidden-file prefix (``.``), or empty name
    causes a rejection.

    Raises ``FileSystemError`` on invalid input.
    """
    if not filename or not filename.strip():
        raise FileSystemError("Filename cannot be empty", code="INVALID_REQUEST")
    if "/" in filename or "\\" in filename:
        raise FileSystemError(f"Filename must not contain path separators: {filename}", code="INVALID_REQUEST")
    if ".." in filename:
        raise FileSystemError(f"Invalid filename: {filename}", code="INVALID_REQUEST")
    if filename.startswith("."):
        raise FileSystemError(f"Hidden filenames not allowed: {filename}", code="INVALID_REQUEST")
    if filename in (".", ".."):
        raise FileSystemError(f"Invalid filename: {filename}", code="INVALID_REQUEST")
    return filename


# ---------------------------------------------------------------------------
# Image parsing
# ---------------------------------------------------------------------------


JPEG_SOF_MARKERS = frozenset((
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
))


def detect_image_media_type(raw: bytes) -> str | None:
    """Return the MIME type inferred from image magic bytes.

    Supports PNG, JPEG, WebP, and GIF. Returns None for unrecognized data.
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image/gif"
    return None


def image_dimensions(header: bytes) -> Optional[Tuple[int, int]]:
    """Read width and height from an image binary header without full decode.

    Supports PNG, JPEG, and GIF. Returns None for unrecognized or malformed
    data.
    """
    if len(header) < 10:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
        return struct.unpack(">II", header[16:24])
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", header[6:10])
    if header[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(header):
            if header[i] != 0xFF:
                break
            while i < len(header) and header[i] == 0xFF:
                i += 1
            if i >= len(header):
                break
            marker = header[i]
            i += 1
            if marker == 0x00:
                continue
            if marker in JPEG_SOF_MARKERS:
                if i + 7 > len(header):
                    break
                h, w = struct.unpack(">HH", header[i + 3:i + 7])
                return w, h
            if marker == 0xD9:
                break
            if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01):
                continue
            if i + 2 > len(header):
                break
            seg_len = struct.unpack(">H", header[i:i + 2])[0]
            if seg_len < 2:
                break
            i += seg_len
    return None
