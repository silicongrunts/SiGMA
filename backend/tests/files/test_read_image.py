"""Tests for image reading support in the Read tool."""

import base64
import struct
import pytest

from app.agents.tools.file_tools import _read_file, _read_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png_bytes(width: int, height: int) -> bytes:
    """Minimal PNG file (signature + IHDR chunk) sufficient for _image_dimensions."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">II", width, height)
    return sig + b"\x00\x00\x00\r" + b"IHDR" + ihdr_data


def _jpeg_bytes(width: int, height: int) -> bytes:
    """Minimal JPEG file (SOI + SOF0) sufficient for _image_dimensions."""
    return (
        b"\xff\xd8"
        b"\xff\xc0"
        b"\x00\x0b"
        b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x11\x00"
    )


# ---------------------------------------------------------------------------
# _read_image — success cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_image_png(sandbox):
    """PNG file returns image dict with correct media type."""
    img = _png_bytes(800, 600)
    p = sandbox / "test.png"
    p.write_bytes(img)

    result = await _read_image("proj", "sess", "test.png", ".png")

    assert isinstance(result, dict)
    assert result["type"] == "image"
    assert result["media_type"] == "image/png"
    assert result["image_base64"] == base64.b64encode(img).decode("ascii")
    assert "800" in result["text"] and "600" in result["text"]


@pytest.mark.asyncio
async def test_read_image_jpeg(sandbox):
    """JPEG file returns image dict with jpeg media type."""
    img = _jpeg_bytes(1280, 720)
    p = sandbox / "photo.jpg"
    p.write_bytes(img)

    result = await _read_image("proj", "sess", "photo.jpg", ".jpg")

    assert isinstance(result, dict)
    assert result["type"] == "image"
    assert result["media_type"] == "image/jpeg"


# ---------------------------------------------------------------------------
# _read_image — error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_image_not_found(sandbox):
    """Missing file returns error string."""
    result = await _read_image("proj", "sess", "missing.png", ".png")
    assert isinstance(result, str)
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_read_image_corrupted(sandbox):
    """Non-image bytes are rejected as an unsupported/invalid image file
    (magic-byte sniffing fails before any dimension parsing happens)."""
    p = sandbox / "bad.png"
    p.write_bytes(b"not a real image")

    result = await _read_image("proj", "sess", "bad.png", ".png")
    assert isinstance(result, str)
    assert result == (
        "Error: Unsupported or invalid image file: bad.png. "
        "Only PNG and JPG images are supported."
    )


@pytest.mark.asyncio
async def test_read_image_rejects_extension_content_mismatch(sandbox):
    """A JPEG payload named .png is rejected instead of sent with the wrong MIME."""
    p = sandbox / "wrong.png"
    p.write_bytes(_jpeg_bytes(640, 480))

    result = await _read_image("proj", "sess", "wrong.png", ".png")
    assert isinstance(result, str)
    assert "extension" in result.lower()
    assert "detected image/jpeg" in result


@pytest.mark.asyncio
async def test_read_image_directory_returns_tool_error(sandbox):
    """Directory-like image paths return an error string, not an uncaught exception."""
    (sandbox / "folder.png").mkdir()

    result = await _read_image("proj", "sess", "folder.png", ".png")
    assert isinstance(result, str)
    assert "not a regular file" in result.lower()


@pytest.mark.asyncio
async def test_read_image_exactly_4k(sandbox):
    """Image at exactly 3840x2160 is accepted."""
    img = _png_bytes(3840, 2160)
    p = sandbox / "4k.png"
    p.write_bytes(img)

    result = await _read_image("proj", "sess", "4k.png", ".png")
    assert isinstance(result, dict)
    assert result["type"] == "image"


@pytest.mark.asyncio
async def test_read_image_absolute_path(tmp_path):
    """Absolute path image read works without file_service."""
    img = _png_bytes(100, 100)
    p = tmp_path / "abs.png"
    p.write_bytes(img)

    result = await _read_image("proj", "sess", str(p), ".png")
    assert isinstance(result, dict)
    assert result["type"] == "image"


@pytest.mark.asyncio
async def test_read_image_absolute_not_found():
    """Absolute path to missing file returns error."""
    result = await _read_image("proj", "sess", "/nonexistent/path/test.png", ".png")
    assert isinstance(result, str)
    assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# _read_file — image dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_file_dispatches_image(tmp_path, monkeypatch):
    """_read_file with image extension calls _read_image when model supports it."""
    from app.core.config import settings, ModelSettings

    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
        reuse="supervisor",
    ))

    img = _png_bytes(200, 200)
    p = tmp_path / "pic.png"
    p.write_bytes(img)

    from app.services.file_service import file_service
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    result = await _read_file("proj", "sess", "pic.png", model_role="supervisor")
    assert isinstance(result, dict)
    assert result["type"] == "image"


@pytest.mark.asyncio
async def test_read_file_image_rejected_when_model_no_vision(tmp_path, monkeypatch):
    """_read_file with image extension returns a vision-tool hint when model lacks vision."""
    from app.core.config import settings, ModelSettings

    # supervisor does NOT reuse vision → no image support
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="gpt-test", provider="openai", api_key="sk-test",
    ))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(
        model="gpt-vision", provider="openai", api_key="sk-test",
    ))

    img = _png_bytes(200, 200)
    p = tmp_path / "pic.png"
    p.write_bytes(img)

    from app.services.file_service import file_service
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    result = await _read_file("proj", "sess", "pic.png", model_role="supervisor")
    assert isinstance(result, str)
    assert "Image file: pic.png" in result
    assert "vision_analyze" in result
    assert "<image_refs>" in result


@pytest.mark.asyncio
async def test_read_file_text_unchanged(sandbox):
    """Text files are unaffected by image support.

    Note: read now prepends `cat -n` line numbers, so a single-line file
    becomes `1\\t<content>`. The assertion checks the text payload is intact.
    """
    p = sandbox / "hello.txt"
    p.write_text("hello world")

    result = await _read_file("proj", "sess", "hello.txt")
    assert result == "1\thello world"


@pytest.mark.asyncio
async def test_read_file_tracks_image_in_read_state(sandbox):
    """Reading an image records a full read in the per-session cache."""
    from app.agents.tools.read_state import read_state_cache

    img = _png_bytes(100, 100)
    p = sandbox / "tracked.png"
    p.write_bytes(img)

    await _read_image("proj", "sess", "tracked.png", ".png")
    assert read_state_cache.get("sess", str(p)) is not None
