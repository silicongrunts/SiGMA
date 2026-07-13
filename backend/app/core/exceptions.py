"""
SiGMA Exception Hierarchy.

All custom exceptions inherit from SiGMAException, which carries:
  - message: human-readable description
  - code: machine-readable error code (for frontend i18n / error mapping)
  - status_code: HTTP status code
  - details: optional dict with extra context
"""

from typing import Optional, Dict, Any


class SiGMAException(Exception):
    """Base exception for all SiGMA errors."""

    def __init__(
        self,
        message: str = "An error occurred",
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Service-layer exceptions
# ---------------------------------------------------------------------------

class ServiceException(SiGMAException):
    """Base for business-logic errors."""
    def __init__(self, message: str = "Service error", code: str = "SERVICE_ERROR",
                 status_code: int = 400, **kw):
        super().__init__(message, code, status_code, **kw)


class ProjectNotFoundError(ServiceException):
    def __init__(self, project_id: str = ""):
        super().__init__(
            message=f"Project not found: {project_id}" if project_id else "Project not found",
            code="PROJECT_NOT_FOUND", status_code=404,
        )


class DocumentNotFoundError(ServiceException):
    def __init__(self, doc_id: str = ""):
        super().__init__(
            message=f"Document not found: {doc_id}" if doc_id else "Document not found",
            code="DOCUMENT_NOT_FOUND", status_code=404,
        )


class AnnotationNotFoundError(ServiceException):
    def __init__(self, annotation_id: str = ""):
        super().__init__(
            message=f"Annotation not found: {annotation_id}" if annotation_id else "Annotation not found",
            code="ANNOTATION_NOT_FOUND", status_code=404,
        )


class FileSystemError(SiGMAException):
    """Filesystem operation failed (path invalid, I/O error, permission, etc.)."""
    def __init__(self, message: str = "File system error", code: str = "FILE_SYSTEM_ERROR",
                 status_code: int = 400):
        super().__init__(message, code, status_code)


class FileMissingError(FileSystemError):
    """A path that was expected to exist on disk is absent (HTTP 404)."""
    def __init__(self, path: str = ""):
        super().__init__(
            message=f"File not found: {path}" if path else "File not found",
            code="FILE_NOT_FOUND", status_code=404,
        )


class BinaryFileError(FileSystemError):
    def __init__(self, path: str = ""):
        super().__init__(
            message=f"Cannot open binary file: {path}" if path else "Cannot open binary file",
            code="BINARY_FILE", status_code=400,
        )


class FileAlreadyExistsError(FileSystemError):
    def __init__(self, path: str = ""):
        super().__init__(
            message=f"File already exists: {path}" if path else "File already exists",
            code="FILE_ALREADY_EXISTS", status_code=409,
        )


class InvalidPathError(FileSystemError):
    def __init__(self, path: str = ""):
        super().__init__(
            message=f"Invalid path: {path}" if path else "Invalid path",
            code="INVALID_PATH", status_code=400,
        )


class ValidationError(SiGMAException):
    """Input validation failure."""
    def __init__(self, message: str = "Validation error", details: Optional[Dict] = None):
        super().__init__(message, "VALIDATION_ERROR", 422, details=details)


# ---------------------------------------------------------------------------
# Database exceptions
# ---------------------------------------------------------------------------

class DatabaseException(SiGMAException):
    def __init__(self, message: str = "Database error", code: str = "DATABASE_ERROR",
                 status_code: int = 500, **kw):
        super().__init__(message, code, status_code, **kw)


class SessionNotFoundError(DatabaseException):
    def __init__(self, session_id: str = ""):
        super().__init__(
            message=f"Session not found: {session_id}" if session_id else "Session not found",
            code="SESSION_NOT_FOUND", status_code=404,
        )


class ConcurrencyError(DatabaseException):
    def __init__(self, message: str = "Concurrent write conflict"):
        super().__init__(message, "CONCURRENCY_ERROR", 409)


class DatabaseIncompatibleError(DatabaseException):
    """Database has an incompatible Alembic revision (e.g. post-squash or version mismatch)."""
    def __init__(self, message: str):
        super().__init__(message, code="DATABASE_INCOMPATIBLE", status_code=422)


# ---------------------------------------------------------------------------
# LLM exceptions
# ---------------------------------------------------------------------------

class LLMException(SiGMAException):
    def __init__(self, message: str = "LLM call failed", code: str = "LLM_ERROR",
                 status_code: int = 502, **kw):
        super().__init__(message, code, status_code, **kw)


class LLMTimeoutError(LLMException):
    def __init__(self, timeout: float = 0):
        super().__init__(
            message=f"LLM call timed out after {timeout}s" if timeout else "LLM call timed out",
            code="LLM_TIMEOUT", status_code=504,
        )


class LLMRateLimitError(LLMException):
    def __init__(self):
        super().__init__("LLM rate limit exceeded", "LLM_RATE_LIMIT", 429)


class LLMResponseError(LLMException):
    def __init__(self, message: str = "LLM returned an invalid response"):
        super().__init__(message, "LLM_RESPONSE_ERROR", 502)


# ---------------------------------------------------------------------------
# Configuration exception
# ---------------------------------------------------------------------------

class ConfigurationError(SiGMAException):
    def __init__(self, message: str = "Configuration error"):
        super().__init__(message, "CONFIGURATION_ERROR", 500)


class RAGIndexModelMismatchError(ServiceException):
    """The on-disk RAG index was built with a different embedding model."""
    def __init__(self, current_model: str = "", indexed_model: str = ""):
        details = {"current_model": current_model, "indexed_model": indexed_model}
        super().__init__(
            "Embedding model changed. Rebuild the library index before searching or indexing.",
            "RAG_INDEX_MODEL_MISMATCH",
            status_code=409,
            details=details,
        )


# ---------------------------------------------------------------------------
# LaTeX / Compilation exceptions
# ---------------------------------------------------------------------------

class LaTeXCompilationError(ServiceException):
    """LaTeX compilation failed."""
    def __init__(self, message: str = "LaTeX compilation failed",
                 log: str = "", code: str = "COMPILATION_FAILED"):
        self.log = log
        super().__init__(message, code, status_code=400, details={"log": log})


class SyncTeXError(ServiceException):
    """SyncTeX forward/reverse lookup failed."""
    def __init__(self, message: str = "SyncTeX error"):
        super().__init__(message, "SYNCTEX_ERROR", status_code=400)


# ---------------------------------------------------------------------------
# Task / Agent exceptions
# ---------------------------------------------------------------------------

class TaskActiveError(ServiceException):
    """Another task is already running in this session."""
    def __init__(self, task_id: str = ""):
        super().__init__(
            message="Another task is already running in this session",
            code="TASK_ACTIVE", status_code=409,
            details={"task_id": task_id} if task_id else {},
        )


# ---------------------------------------------------------------------------
# Jupyter exceptions
# ---------------------------------------------------------------------------

class JupyterNotInitializedError(ServiceException):
    """Jupyter/Notebook service has not been initialized."""
    def __init__(self, detail: str = "Jupyter service not initialized"):
        super().__init__(detail, "JUPYTER_NOT_INITIALIZED", status_code=500)


class JupyterKernelError(ServiceException):
    """Jupyter kernel operation failed."""
    def __init__(self, kernel_id: str = "", status_code: int = 500, detail: str = ""):
        msg = f"Kernel operation failed: {kernel_id}" + (f" — {detail}" if detail else "")
        super().__init__(msg, "JUPYTER_KERNEL_ERROR", status_code=status_code,
                         details={"kernel_id": kernel_id})


# ---------------------------------------------------------------------------
# Document / Library exceptions
# ---------------------------------------------------------------------------

class SourceFileNotFoundError(ServiceException):
    """Document's source file is missing on disk."""
    def __init__(self, identifier: str = ""):
        super().__init__(
            message=f"Source file not found: {identifier}" if identifier else "Source file not found",
            code="SOURCE_FILE_NOT_FOUND", status_code=404,
        )


class DuplicateTitleError(ServiceException):
    """A resource with the same title already exists."""
    def __init__(self, detail: str = ""):
        super().__init__(
            message=detail or "Duplicate title",
            code="DUPLICATE_TITLE", status_code=409,
        )


class MoveFailedError(ServiceException):
    """Move operation failed."""
    def __init__(self, detail: str = ""):
        super().__init__(
            message=detail or "Move operation failed",
            code="MOVE_FAILED", status_code=400,
        )


class DocumentProcessingError(SiGMAException):
    """Base exception for document processing pipeline failures."""
    def __init__(self, message: str = "Document processing failed",
                 code: str = "DOCUMENT_PROCESSING_ERROR", status_code: int = 500,
                 doc_id: str = "", stage: str = ""):
        details = {}
        if doc_id:
            details["doc_id"] = doc_id
        if stage:
            details["stage"] = stage
        super().__init__(message, code, status_code, details=details)


class DocumentConversionError(DocumentProcessingError):
    """Docling or binary file conversion returned empty/corrupt result."""
    def __init__(self, file_path: str = "", doc_id: str = ""):
        msg = f"Document conversion failed: {file_path}" if file_path else "Document conversion failed"
        super().__init__(msg, code="DOCUMENT_CONVERSION_ERROR", status_code=422,
                         doc_id=doc_id, stage="conversion")


class AIExtractionError(DocumentProcessingError):
    """AI field extraction exhausted retries. Non-fatal — document still indexes."""
    def __init__(self, doc_id: str = "", attempts: int = 3):
        super().__init__(
            f"AI field extraction failed after {attempts} attempts",
            code="AI_EXTRACTION_ERROR", status_code=502,
            doc_id=doc_id, stage="ai_extraction",
        )


# ---------------------------------------------------------------------------
# Permission exception
# ---------------------------------------------------------------------------

class PermissionDeniedError(ServiceException):
    """Permission denied for the requested operation."""
    def __init__(self, detail: str = "Permission denied"):
        super().__init__(detail, "PERMISSION_DENIED", status_code=403)


# ---------------------------------------------------------------------------
# Authentication exception
# ---------------------------------------------------------------------------

class AuthenticationError(SiGMAException):
    """Authentication is required or the provided credentials are invalid."""
    def __init__(self, detail: str = "Authentication required"):
        super().__init__(detail, "AUTHENTICATION_ERROR", status_code=401)


# ---------------------------------------------------------------------------
# Skill exceptions
# ---------------------------------------------------------------------------

class SkillError(ServiceException):
    """Base skill operation error."""
    def __init__(self, message: str = "Skill error", code: str = "SKILL_ERROR", status_code: int = 400):
        super().__init__(message, code, status_code)


class SkillNotFoundError(SkillError):
    """Skill directory not found."""
    def __init__(self, skill_id: str = ""):
        super().__init__(
            message=f"Skill not found: {skill_id}" if skill_id else "Skill not found",
            code="SKILL_NOT_FOUND", status_code=404,
        )


# ---------------------------------------------------------------------------
# Browser automation exceptions
# ---------------------------------------------------------------------------

class BrowserException(ServiceException):
    """Browser automation error."""
    def __init__(self, message: str = "Browser operation failed",
                 code: str = "BROWSER_ERROR", status_code: int = 500):
        super().__init__(message, code, status_code)


class BrowserNotConnectedError(BrowserException):
    """Browser not connected — Chrome may not be running."""
    def __init__(self, message: str = "Browser not connected. Is Chrome running?"):
        super().__init__(message, "BROWSER_NOT_CONNECTED", 503)


class ElementRefStaleError(BrowserException):
    """Element reference is stale — page may have changed."""
    def __init__(self, ref: str = ""):
        super().__init__(
            f"Element ref '{ref}' not found — the page may have changed. "
            "Run browser_snapshot to get fresh refs.",
            "ELEMENT_REF_STALE", 400,
        )
