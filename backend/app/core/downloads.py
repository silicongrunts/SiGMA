"""HTTP download header helpers."""

from __future__ import annotations

from urllib.parse import quote


_SAFE_FALLBACK_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._-"
)


def _download_filename(filename: str) -> str:
    cleaned = "".join(
        "_" if char in {"/", "\\", "\x00", "\r", "\n"} else char
        for char in filename.strip()
    )
    return cleaned or "download"


def _ascii_filename_fallback(filename: str) -> str:
    fallback = "".join(
        char if char in _SAFE_FALLBACK_CHARS else "_"
        for char in filename
    )
    return fallback or "download"


def content_disposition_header(filename: str, disposition: str = "attachment") -> str:
    """Return a Latin-1-safe Content-Disposition value for a download name."""
    download_filename = _download_filename(filename)
    fallback = _ascii_filename_fallback(download_filename)
    encoded = quote(download_filename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def download_headers(filename: str) -> dict[str, str]:
    """Return headers for a file download with Unicode filename support."""
    return {"Content-Disposition": content_disposition_header(filename)}
