"""
Unified API Response — standard format for all non-streaming endpoints.

Format: {"request_id": str, "success": bool, "error": Optional[str], "data": Any}
"""

from typing import Any, Optional

from app.core.utils import generate_id
from fastapi.responses import JSONResponse


def ok(data: Any = None, request_id: Optional[str] = None) -> dict:
    """Build a success response dict."""
    return {
        "request_id": request_id or generate_id(),
        "success": True,
        "error": None,
        "data": data,
    }


def err(
    message: str,
    status_code: int = 500,
    request_id: Optional[str] = None,
) -> JSONResponse:
    """Build an error JSONResponse."""
    return JSONResponse(
        status_code=status_code,
        content={
            "request_id": request_id or generate_id(),
            "success": False,
            "error": message,
            "data": None,
        },
    )
