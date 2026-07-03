"""
Frontend static-file serving and noVNC mounting.

Provides ``mount_frontend(app)`` and ``mount_static(app)`` so that
main.py stays declarative.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import get_logger
logger = get_logger(__name__)

# Frontend build output: <ROOT_DIR>/frontend/dist
_react_dist_path = settings.ROOT_DIR / "frontend" / "dist"
frontend_path = (
    _react_dist_path
    if (_react_dist_path / "index.html").exists()
    else None
)

# Base path for noVNC / VNC assets
_novnc_static = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------------------
# Individual route handlers
# ---------------------------------------------------------------------------

async def serve_vnc_client():
    """Serve the VNC client HTML page."""
    vnc_html = _novnc_static / "vnc.html"
    if vnc_html.exists():
        return FileResponse(vnc_html)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


async def health_check():
    return {
        "status": "healthy",
        "engines": {
            "latex": list(settings.LATEX_ENGINES),
        },
    }


async def serve_assets(file: str):
    if frontend_path:
        asset = frontend_path / "assets" / file
        if asset.exists():
            return FileResponse(asset)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


async def serve_root():
    if frontend_path:
        index = frontend_path / "index.html"
        if index.exists():
            return FileResponse(index)
    return JSONResponse(content={
        "message": "SiGMA API is running",
        "note": "Run 'npm run build' in frontend/ to build production assets",
    })


async def serve_frontend(catch_all: str):
    """SPA catch-all -- MUST be registered last."""
    if catch_all.startswith("api/") or catch_all.startswith("ws/"):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if catch_all.startswith("src/") or catch_all.startswith("@"):
        return JSONResponse(status_code=404, content={"detail": "Module not found"})

    if frontend_path:
        index = frontend_path / "index.html"
        if index.exists():
            return FileResponse(index)
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


# ---------------------------------------------------------------------------
# Mount helpers
# ---------------------------------------------------------------------------

def mount_frontend(app: FastAPI) -> None:
    """Register all frontend-serving routes on *app*."""
    app.get("/vnc.html")(serve_vnc_client)
    app.get("/api/health")(health_check)
    app.get("/assets/{file:path}")(serve_assets)
    app.get("/")(serve_root)
    # SPA catch-all -- MUST be last
    app.api_route(
        "/{catch_all:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )(serve_frontend)


def mount_static(app: FastAPI) -> None:
    """Mount noVNC static files (served by backend to avoid Vite MIME issues)."""
    novnc_dir = _novnc_static / "novnc"
    if novnc_dir.exists():
        app.mount(
            "/novnc",
            StaticFiles(directory=str(novnc_dir), html=True),
            name="novnc-static",
        )
