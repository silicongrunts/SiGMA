"""
Global middleware and exception handlers for SiGMA.

Registers exception handlers on the FastAPI app so all errors
return the unified API response format.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.exceptions import SiGMAException
from app.core.response import err
from app.core.logging import get_logger
from app.core.utils import generate_id


async def sigma_exception_handler(_request: Request, exc: SiGMAException) -> JSONResponse:
    """Handle all SiGMA custom exceptions."""
    logger = get_logger("sigma.errors")
    logger.warning(f"SiGMA error: [{exc.code}] {exc.message}")
    return err(exc.message, status_code=exc.status_code)


async def validation_exception_handler(_request: Request, exc) -> JSONResponse:
    """Handle Pydantic validation errors (FastAPI RequestValidationError)."""
    errors = []
    if hasattr(exc, "errors"):
        for e in exc.errors():
            errors.append({
                "field": ".".join(str(loc) for loc in e.get("loc", [])),
                "message": e.get("msg", ""),
            })
    return JSONResponse(
        status_code=422,
        content={
            "request_id": generate_id(),
            "success": False,
            "error": "Validation error",
            "data": {"errors": errors},
        },
    )


async def generic_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions."""
    logger = get_logger("sigma.errors")
    logger.exception(f"Unhandled exception: {exc}")
    return err("Internal server error", status_code=500)


def register_exception_handlers(app) -> None:
    """Register all exception handlers on a FastAPI app."""
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(SiGMAException, sigma_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
