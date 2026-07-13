"""
SiGMA Backend – Application entry-point.

This module creates the FastAPI app, wires up middleware, routers, proxy
routes, lifecycle events, and static file serving.  All substantial logic
lives in dedicated sub-modules under ``app.core``.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging import RequestIDMiddleware, LoggingMiddleware, setup_logging
from app.core.middleware import AuthMiddleware, register_exception_handlers
from app.core.lifecycle import startup_event, shutdown_event
from app.core.proxies import jupyter_ws_proxy, jupyter_proxy, browser_vnc_ws_proxy
from app.core.terminal_ws import terminal_ws_handler
from app.core.static_files import mount_frontend, mount_static
from app.core.config import settings

# ---------------------------------------------------------------------------
# Route modules
# ---------------------------------------------------------------------------
from app.routes import (
    projects, files, compile, chat, annotations,
    git as git_routes, notebooks,
    library as library_routes, browser as browser_routes,
    permissions as permissions_routes, terminal as terminal_routes,
    skills as skills_routes, system as system_routes,
    auth as auth_routes,
)

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SiGMA API",
    description="SiGMA Server API",
    version="0.1.0",
)

# Middleware. Starlette runs middleware in reverse of add order: the LAST
# added runs FIRST. Order below yields inbound execution:
#   CORS (outermost, handles preflight) -> AuthMiddleware -> RequestID -> Logging
app.add_middleware(RequestIDMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Exception handlers
register_exception_handlers(app)

# API routers
app.include_router(projects.router, prefix=settings.API_PREFIX)
app.include_router(files.router, prefix=settings.API_PREFIX)
app.include_router(annotations.router, prefix=settings.API_PREFIX)
app.include_router(compile.router, prefix=settings.API_PREFIX)
app.include_router(chat.router, prefix=settings.API_PREFIX)
app.include_router(git_routes.router, prefix=settings.API_PREFIX)
app.include_router(notebooks.router, prefix=settings.API_PREFIX)
app.include_router(library_routes.router, prefix=settings.API_PREFIX)
app.include_router(browser_routes.router, prefix=settings.API_PREFIX)
app.include_router(permissions_routes.router, prefix=settings.API_PREFIX)
app.include_router(terminal_routes.router, prefix=settings.API_PREFIX)
app.include_router(skills_routes.router, prefix=settings.API_PREFIX)
app.include_router(system_routes.router, prefix=settings.API_PREFIX)
app.include_router(auth_routes.router, prefix=settings.API_PREFIX)

# Proxy routes (WebSocket + HTTP)
app.websocket("/api/v1/jupyter/{path:path}")(jupyter_ws_proxy)
app.api_route(
    "/api/v1/jupyter/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)(jupyter_proxy)
app.websocket("/api/v1/browser/{project_id}/vnc")(browser_vnc_ws_proxy)
app.websocket("/api/v1/terminal/{project_id}")(terminal_ws_handler)

# Static files & frontend
mount_static(app)
mount_frontend(app)

# Lifecycle events
@app.on_event("startup")
async def _on_startup():
    setup_logging(process="web", force=True)
    await startup_event()


@app.on_event("shutdown")
async def _on_shutdown():
    await shutdown_event()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
