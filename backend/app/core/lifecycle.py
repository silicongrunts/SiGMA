"""
Application lifecycle events (startup / shutdown).

All heavy service initialisation and teardown lives here so that
main.py stays small and declarative.
"""

import asyncio

from app.core.config import settings
from app.services.jupyter_service import JupyterService, set_jupyter

from app.core.logging import get_logger
logger = get_logger(__name__)

# Module-level singletons
jupyter_service: JupyterService = JupyterService(base_dir=str(settings.USERDATA_DIR))
stream_server = None  # set at startup


async def startup_event():
    """Run once when the FastAPI application starts."""
    from app.services.notebook_service import init_notebook_service

    # ---- Notebook / Jupyter ----
    init_notebook_service(settings)
    set_jupyter(jupyter_service)

    # ---- Ensure base directories exist ----
    settings.USERDATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.SIGMA_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Database migration (all projects) ----
    from app.database.manager import get_db_manager
    db_mgr = await get_db_manager()
    # Sweep leftover .<id>.import.tmp dirs from interrupted zip uploads
    # before they can confuse later scans or leak disk.
    from app.services.project_service import project_service
    try:
        swept = project_service.cleanup_interrupted_imports()
        if swept:
            logger.info("Cleaned up %d interrupted import(s)", swept)
    except Exception as exc:
        logger.warning("Interrupted-import cleanup failed: %s", exc, exc_info=True)
    await db_mgr.migrate_all_projects()

    # ---- Stream Server (TCP relay for Huey Worker → SSE) ----
    global stream_server
    from app.workers.stream_server import stream_server as _ss
    stream_server = _ss
    try:
        await stream_server.start()
        logger.info("StreamServer started on %s:%s", stream_server.host, stream_server.port)
    except Exception as e:
        logger.warning("Failed to start StreamServer: %s", e, exc_info=True)

    # ---- Browser service ----
    from app.services.browser_service import get_browser_service
    try:
        await get_browser_service().on_startup()
    except Exception as e:
        logger.warning("Failed to start browser service on startup: %s", e, exc_info=True)

    # ---- Tab Reaper (idle tab auto-close) ----
    from app.agents.tools.browser_tab_reaper import get_tab_reaper
    try:
        get_tab_reaper().start()
    except Exception as e:
        logger.warning("Failed to start tab reaper: %s", e, exc_info=True)

    # ---- Browser Thread (persistent event loop for Playwright) ----
    from app.agents.tools.browser_thread import get_browser_thread
    try:
        await asyncio.to_thread(get_browser_thread)
        logger.info("Browser thread started")
    except Exception as e:
        logger.warning("Failed to start browser thread: %s", e, exc_info=True)

    # ---- Document processing service (library) ----
    from app.services.document_processing_service import document_processing_service
    try:
        await document_processing_service.start()
    except Exception as e:
        logger.warning("Failed to start document processing service: %s", e, exc_info=True)

    # ---- RAG service ----
    from app.services.rag_service import rag_service
    try:
        await rag_service.start()
    except Exception as e:
        logger.warning("Failed to initialize RAG service: %s", e, exc_info=True)

    # ---- Index builder ----
    from app.services.index_builder import index_builder
    index_builder.start()

    # ---- Terminal session reaper ----
    from app.services.terminal_service import terminal_service
    try:
        await terminal_service.start_reaper()
    except Exception as e:
        logger.warning("Failed to start terminal reaper: %s", e, exc_info=True)

    logger.info("SiGMA startup complete")


async def shutdown_event():
    """Run once when the FastAPI application shuts down."""
    # ---- Stream server ----
    global stream_server
    if stream_server:
        try:
            await stream_server.stop()
        except Exception as e:
            logger.warning("Failed to stop StreamServer: %s", e, exc_info=True)

    # ---- Index builder ----
    from app.services.index_builder import index_builder
    try:
        index_builder.stop()
    except Exception as e:
        logger.warning("Failed to stop index builder: %s", e, exc_info=True)

    # ---- Jupyter ----
    jupyter_service.stop()

    # ---- Browser ----
    from app.services.browser_service import get_browser_service
    try:
        await get_browser_service().on_shutdown()
    except Exception as e:
        logger.warning("Failed to stop browser service: %s", e, exc_info=True)

    # ---- Browser Thread (Playwright + daemon thread) ----
    try:
        from app.agents.tools.browser_thread import _browser_thread
        if _browser_thread is not None:
            _browser_thread.shutdown()
            logger.info("Browser thread stopped")
    except Exception as e:
        logger.warning("Failed to stop browser thread: %s", e, exc_info=True)

    # ---- Tab Reaper ----
    try:
        from app.agents.tools.browser_tab_reaper import get_tab_reaper
        get_tab_reaper().stop()
    except Exception:
        logger.debug("Failed to stop tab reaper", exc_info=True)

    # ---- Document processing service ----
    from app.services.document_processing_service import document_processing_service
    try:
        await document_processing_service.stop()
    except Exception as e:
        logger.warning("Failed to stop document processing service: %s", e, exc_info=True)

    # ---- RAG service ----
    from app.services.rag_service import rag_service
    try:
        await rag_service.stop()
    except Exception:
        logger.debug("Failed to stop RAG service", exc_info=True)

    # ---- Terminal PTY sessions ----
    from app.services.terminal_service import terminal_service
    try:
        await terminal_service.kill_all()
    except Exception as e:
        logger.warning("Failed to kill terminal sessions: %s", e, exc_info=True)

    logger.info("SiGMA shutdown complete")
