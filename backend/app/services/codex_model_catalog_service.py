"""Codex subscription model catalog with a manual remote refresh.

The subscription backend exposes no model discovery endpoint, so chat-model
suggestions come from a static catalog published online by a third-party
Codex client project (GitHub raw first, with a mirror as fallback). A
successful refresh persists under userdata/.SiGMA/chatgpt and is served
unchanged until the user refreshes again. Image-generation models appear in
no remote catalog — the settings UI carries those as built-in suggestions.
"""

import asyncio
import json
import logging
import time

import httpx

from app.core.atomic_file import atomic_write_json
from app.core.config import SIGMA_DIR
from app.core.exceptions import CodexCatalogError

logger = logging.getLogger(__name__)

# Both sources are plain static JSON, no auth. Order matters: the upstream
# GitHub file is authoritative, the mirror exists for networks where raw
#.githubusercontent.com is unreliable.
CATALOG_SOURCES = (
    "https://raw.githubusercontent.com/router-for-me/models/refs/heads/main/codex_client_models.json",
    "https://models.router-for.me/codex_client_models.json",
)
CATALOG_CACHE_FILE = SIGMA_DIR / "chatgpt" / "codex_model_catalog.json"
FETCH_TIMEOUT_SECONDS = 10.0
MAX_MODELS = 200


class CodexModelCatalogService:
    """Fetch, validate, and cache the Codex chat-model catalog."""

    def __init__(self):
        self._refresh_lock = asyncio.Lock()

    def cached_models(self) -> dict:
        """The persisted catalog payload, or an empty one before first refresh."""
        empty = {"models": [], "source": None, "fetched_at": None}
        try:
            data = json.loads(CATALOG_CACHE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return empty
        models = data.get("models")
        if not isinstance(models, list):
            return empty
        return {
            "models": [m for m in models if isinstance(m, str)],
            "source": data.get("source"),
            "fetched_at": data.get("fetched_at"),
        }

    async def refresh(self) -> dict:
        """Fetch the catalog from the first reachable source and cache it.

        Concurrent refreshes collapse into one fetch. A malformed catalog
        rejects its source so a format change never clobbers the cached
        suggestions; a total failure raises CodexCatalogError and leaves the
        previous cache untouched.
        """
        async with self._refresh_lock:
            errors: list[str] = []
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS) as client:
                for source in CATALOG_SOURCES:
                    try:
                        resp = await client.get(source)
                        resp.raise_for_status()
                        models = self._parse(resp.json())
                    except (httpx.HTTPError, ValueError) as exc:
                        errors.append(f"{source}: {exc}")
                        continue
                    data = {
                        "models": models,
                        "source": source,
                        "fetched_at": time.time(),
                    }
                    atomic_write_json(CATALOG_CACHE_FILE, data)
                    logger.info(
                        "Codex model catalog refreshed from %s (%d models)",
                        source, len(models),
                    )
                    return data
            raise CodexCatalogError(
                "Could not fetch the Codex model catalog from any source: "
                + "; ".join(errors)
            )

    @staticmethod
    def _parse(data) -> list[str]:
        """Extract visible model slugs in catalog order, deduplicated."""
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list) or not models:
            raise ValueError("catalog carries no models array")
        slugs: list[str] = []
        for entry in models:
            if not isinstance(entry, dict):
                raise ValueError("catalog entry is not an object")
            if entry.get("visibility") == "hide":
                continue
            slug = entry.get("slug")
            if not isinstance(slug, str) or not slug:
                raise ValueError("catalog entry has no slug")
            slugs.append(slug)
        if not slugs:
            raise ValueError("catalog has no visible models")
        return list(dict.fromkeys(slugs))[:MAX_MODELS]


codex_model_catalog_service = CodexModelCatalogService()
