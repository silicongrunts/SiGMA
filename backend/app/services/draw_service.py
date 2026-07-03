"""
Image generation service for chat tools.

Owns prompt-to-file generation and keeps project-relative output paths stable.
"""

import base64
import mimetypes

import httpx

from app.core.atomic_file import atomic_write_bytes
from app.core.exceptions import LLMResponseError
from app.core.utils import generate_id, utcnow
from app.services.file_service import file_service
from app.services.llm_service import llm_service


class DrawService:
    """Generate an image with the configured draw model and save it in project."""

    async def draw_image(self, project_id: str, prompt: str) -> dict:
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return {"error": "Prompt is required"}

        generated = await llm_service.generate_image(clean_prompt)
        image_bytes = await self._read_generated_bytes(generated)
        suffix = self._suffix_for_mime(generated.mime_type)

        root = file_service.get_project_path(project_id)
        draw_dir = file_service.safe_join(root, ".SiGMA", "draw")
        draw_dir.mkdir(parents=True, exist_ok=True)

        name = f"{utcnow().strftime('%Y%m%d-%H%M%S')}-{generate_id()[:6]}{suffix}"
        target = file_service.safe_join(root, ".SiGMA", "draw", name)
        atomic_write_bytes(target, image_bytes, fail_if_exists=True)

        rel_path = str(target.relative_to(root))
        return {"path": rel_path, "prompt": clean_prompt, "mime_type": generated.mime_type}

    async def _read_generated_bytes(self, generated) -> bytes:
        if generated.b64_json:
            return base64.b64decode(generated.b64_json)
        if generated.url:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                response = await client.get(generated.url)
                response.raise_for_status()
                return response.content
        raise LLMResponseError("Image model returned neither base64 data nor URL")

    @staticmethod
    def _suffix_for_mime(mime_type: str) -> str:
        mime = (mime_type or "").lower().split(";", 1)[0].strip()
        if mime == "image/jpeg":
            return ".jpg"
        if mime == "image/png":
            return ".png"
        guessed = mimetypes.guess_extension(mime)
        return guessed or ".png"


draw_service = DrawService()
