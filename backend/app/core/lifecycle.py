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

# Module-level singleton
jupyter_service: JupyterService = JupyterService(base_dir=str(settings.USERDATA_DIR))


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

    # Repair interrupted destructive operations only after eligible active
    # projects have reached the current schema. Barriered projects are not
    # migration candidates and recovery can remove unreadable databases after
    # draining live work.
    try:
        await project_service.reconcile_lifecycle()
    except Exception as exc:
        logger.warning("Project lifecycle reconciliation failed: %s", exc, exc_info=True)

    from app.core.project_registry import iter_project_ids
    from app.services.ai_service import ai_service
    for project_id in iter_project_ids():
        try:
            await ai_service.reconcile_deleting_sessions(project_id)
        except Exception as exc:
            logger.warning("Session deletion reconciliation failed for %s: %s", project_id, exc, exc_info=True)

    from app.services.file_deletion_service import file_deletion_service
    for project_id in iter_project_ids():
        try:
            await file_deletion_service.recover_deletions(project_id)
        except Exception:
            logger.warning("File deletion recovery failed for %s", project_id, exc_info=True)

    from app.services.annotation_service import annotation_service
    for project_id in iter_project_ids():
        try:
            await annotation_service.recover_transactions(project_id)
        except Exception as exc:
            logger.warning("Annotation transaction recovery failed for %s: %s", project_id, exc, exc_info=True)

    # ---- Fail tasks left active by a previous run ----
    from app.services import task_runtime
    try:
        await task_runtime.startup_reconcile()
    except Exception as e:
        logger.warning("Startup task reconciliation failed: %s", e, exc_info=True)
    try:
        task_runtime.start_stranded_sweep()
    except Exception as e:
        logger.warning("Failed to start stranded task sweep: %s", e, exc_info=True)

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

    # ---- RAG service ----
    from app.services.rag_service import rag_service
    try:
        await rag_service.start()
    except Exception as e:
        logger.warning("Failed to initialize RAG service: %s", e, exc_info=True)

    # ---- Git repo upkeep (stale index locks, generated .gitignore rules) ----
    from app.services.git_service import git_service
    try:
        await asyncio.to_thread(git_service.startup_maintenance)
    except Exception as e:
        logger.warning("Git startup maintenance failed: %s", e, exc_info=True)

    # ---- Library task handler registration ----
    # These imports register the library background task handlers via their
    # module-level registries; no service-level start/stop is needed since
    # the background runner owns the execution lifecycle.
    from importlib import import_module

    import_module("app.services.document_processing_service")
    import_module("app.services.index_builder")

    # ---- Terminal session reaper ----
    from app.services.terminal_service import terminal_service
    try:
        await terminal_service.start_reaper()
    except Exception as e:
        logger.warning("Failed to start terminal reaper: %s", e, exc_info=True)

    # ---- Library background runner and maintenance loop ----
    # Started last: the library task handlers register themselves at the
    # document-processing / index-builder imports above.
    from app.services.background_task_service import (
        background_task_service,
        library_task_runner,
    )
    library_task_runner.start()
    background_task_service.start_maintenance()

    logger.info("SiGMA startup complete")


async def shutdown_event():
    """Run once when the FastAPI application shuts down."""
    # ---- Streaming chat/annotation tasks (producers first) ----
    from app.services import task_runtime
    try:
        await task_runtime.shutdown_all()
    except Exception as e:
        logger.warning("Failed to shut down task runtime: %s", e, exc_info=True)
    try:
        await task_runtime.stop_stranded_sweep()
    except Exception as e:
        logger.warning("Failed to stop stranded task sweep: %s", e, exc_info=True)

    # ---- Library maintenance loop and background runner ----
    from app.services.background_task_service import (
        background_task_service,
        library_task_runner,
    )
    try:
        await background_task_service.stop_maintenance()
    except Exception as e:
        logger.warning("Failed to stop library maintenance loop: %s", e, exc_info=True)
    try:
        await library_task_runner.stop()
    except Exception as e:
        logger.warning("Failed to stop library background runner: %s", e, exc_info=True)

    # ---- Project databases ----
    from app.database.manager import get_db_manager
    try:
        await (await get_db_manager()).close_all()
    except Exception as e:
        logger.warning("Failed to close project databases: %s", e, exc_info=True)

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

    # ---- Pending auto-snapshot timers ----
    from app.services.snapshot_service import snapshot_service
    try:
        await snapshot_service.shutdown()
    except Exception as e:
        logger.warning("Failed to cancel pending auto-snapshot timers: %s", e, exc_info=True)

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
