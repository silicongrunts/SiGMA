"""PNG/JPEG/GIF header parsing in app.core.utils.image_dimensions.

Moved here from tests/ai/test_compaction_service.py: these tests pin the
core utility's byte-level format parsing, not compaction behavior (the
compaction-side token-estimation tests that build on it remain in
tests/ai/). Header builders are self-contained in this file.
"""

import struct

from app.core.utils import image_dimensions


def _png_header(width: int, height: int) -> bytes:
    """Build a minimal PNG header (signature + IHDR chunk) for testing."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height)
    ihdr = ihdr_data
    return sig + b"\x00\x00\x00\r" + b"IHDR" + ihdr


def _jpeg_header(width: int, height: int) -> bytes:
    """Build a minimal JPEG header (SOI + SOF0) for testing."""
    return (
        b"\xff\xd8"                         # SOI
        b"\xff\xc0"                         # SOF0 marker
        b"\x00\x0b"                         # segment length (11 bytes)
        b"\x08"                             # precision
        + struct.pack(">HH", height, width)
        + b"\x01\x11\x00"                   # channels
    )


def _jpeg_with_app_segment(width: int, height: int, app_payload_size: int) -> bytes:
    app_len = app_payload_size + 2
    return (
        b"\xff\xd8"
        b"\xff\xe1"
        + struct.pack(">H", app_len)
        + b"x" * app_payload_size
        + _jpeg_header(width, height)[2:]
    )


def _gif_header(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height)


def test_image_dimensions_png():
    assert image_dimensions(_png_header(1920, 1080)) == (1920, 1080)
    assert image_dimensions(_png_header(800, 600)) == (800, 600)


def test_image_dimensions_jpeg():
    assert image_dimensions(_jpeg_header(1280, 720)) == (1280, 720)


def test_image_dimensions_jpeg_skips_app_segments():
    assert image_dimensions(_jpeg_with_app_segment(1280, 720, 512)) == (1280, 720)


def test_image_dimensions_gif():
    assert image_dimensions(_gif_header(400, 300)) == (400, 300)


def test_image_dimensions_unrecognised():
    assert image_dimensions(b"\x00\x01\x02\x03") is None
    assert image_dimensions(b"") is None
