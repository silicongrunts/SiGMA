"""
Structured Logging for SiGMA.

Provides:
  - setup_logging(): initialize console and daily file logging
  - get_logger(): factory that binds request_id from context
  - RequestIDMiddleware: generates UUID per HTTP request
  - LoggingMiddleware: logs method, path, status, duration for every request
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging

from app.core.utils import utcnow
from pathlib import Path
import sys
import time
from contextvars import ContextVar
from typing import Literal

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

LogProcess = Literal["web", "worker"]

# Context var for request-scoped request_id
request_id_ctx: ContextVar[str] = ContextVar("request_id_ctx", default="")


def bind_request_id(request_id: str) -> None:
    """Set the request_id for the current async context."""
    request_id_ctx.set(request_id)


def get_request_id() -> str:
    """Get the current request_id (empty string if not in a request)."""
    return request_id_ctx.get()


class _RequestIDFilter(logging.Filter):
    """Inject request_id into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line."""
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Attach any extra fields the caller added
        for key in ("duration_ms", "project_id", "doc_id", "task_id", "agent"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        return json.dumps(log_entry, ensure_ascii=False)


class _ColoredFormatter(logging.Formatter):
    """Human-readable colored output for development."""
    COLORS = {
        "DEBUG": "\033[36m",    # cyan
        "INFO": "\033[32m",     # green
        "WARNING": "\033[33m",  # yellow
        "ERROR": "\033[31m",    # red
        "CRITICAL": "\033[35m", # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        rid = getattr(record, "request_id", "")
        rid_part = f" [{rid[:8]}]" if rid else ""
        msg = super().format(record)
        return f"{color}{record.levelname:8s}{self.RESET}{rid_part} {record.name}: {msg}"


class _MaxLevelFilter(logging.Filter):
    """Allow records below the configured upper bound."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


class _DailyFileHandler(logging.Handler):
    """Write to ``<process>-YYYY-MM-DD.log`` using the system local timezone."""

    def __init__(
        self,
        log_dir: Path,
        process: LogProcess,
        retention_days: int,
    ) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.process = process
        self.retention_days = retention_days
        self._current_date = ""
        self._stream = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_logs()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            if self._stream is None:
                return
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream:
            stream.close()
        super().close()

    def _ensure_stream(self) -> None:
        today = utcnow().date().isoformat()
        if self._stream and today == self._current_date:
            return

        if self._stream:
            self._stream.close()
            self._stream = None

        self._current_date = today
        log_path = self.log_dir / f"{self.process}-{today}.log"
        self._stream = log_path.open("a", encoding="utf-8")

    def _cleanup_old_logs(self) -> None:
        cutoff = utcnow().date() - timedelta(days=self.retention_days - 1)
        for path in self.log_dir.glob(f"{self.process}-*.log"):
            try:
                date_text = path.stem.removeprefix(f"{self.process}-")
                if datetime.strptime(date_text, "%Y-%m-%d").date() < cutoff:
                    path.unlink()
            except (OSError, ValueError):
                # Retention cleanup must never prevent the application from starting.
                continue


def _build_formatter(json_mode: bool) -> logging.Formatter:
    if json_mode:
        return _JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
    return _ColoredFormatter(fmt="%(message)s", datefmt="%H:%M:%S")


def _logging_settings() -> tuple[Path, str, int]:
    from app.core.config import settings

    return settings.SIGMA_DIR / "logs", settings.LOG_LEVEL, settings.LOG_RETENTION_DAYS


def setup_logging(
    json_mode: bool = False,
    level: str | None = None,
    process: LogProcess = "web",
    *,
    log_dir: Path | None = None,
    retention_days: int | None = None,
    force: bool = False,
) -> None:
    """
    Initialize logging for the application.

    Args:
        json_mode: If True, emit JSON lines (production). If False, colored console (dev).
        level: Optional console log level override. Defaults to settings.logging.level.
        process: Process name used for daily log files.
        log_dir: Optional override for tests.
        retention_days: Optional override. Defaults to settings.logging.retention_days.
        force: Reconfigure even if SiGMA logging was already initialized.
    """
    if process not in ("web", "worker"):
        raise ValueError("process must be 'web' or 'worker'")

    root = logging.getLogger()
    configured_process = getattr(root, "_sigma_logging_process", None)
    if configured_process and not force:
        return

    default_log_dir, default_level, default_retention_days = _logging_settings()
    configured_level = level or default_level
    configured_retention_days = retention_days or default_retention_days
    if configured_retention_days <= 0:
        raise ValueError("retention_days must be a positive integer")

    log_level = getattr(logging, configured_level.upper(), logging.INFO)
    formatter = _build_formatter(json_mode)
    request_filter = _RequestIDFilter()

    root.setLevel(logging.DEBUG)

    # Remove existing handlers to avoid duplicates on reload.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(request_filter)
    stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
    root.addHandler(stdout_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(request_filter)
    root.addHandler(stderr_handler)

    file_handler = _DailyFileHandler(
        log_dir or default_log_dir,
        process,
        retention_days=configured_retention_days,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    file_handler.addFilter(request_filter)
    root.addHandler(file_handler)

    root._sigma_logging_process = process  # type: ignore[attr-defined]

    # Quieten noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore", "multipart", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "sigma") -> logging.Logger:
    """
    Get a logger bound to the given name.

    Usage:
        logger = get_logger(__name__)
        logger.info("doing something")
    """
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate a UUID4 request_id for every incoming HTTP request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from app.core.utils import generate_id
        rid = request.headers.get("X-Request-ID") or generate_id()
        bind_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status_code, duration_ms for every request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        logger = get_logger("sigma.http")
        start = time.monotonic()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.exception(
                "Unhandled exception",
                extra={
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code}",
            extra={"duration_ms": duration_ms},
        )
        return response
