"""Shared builder for safe, redacted 500 responses.

Used by AuditMiddleware, SecurityHeadersMiddleware (both of which must catch
unhandled exceptions and RETURN this response instead of re-raising, so the
error flows out through the normal middleware chain — CORS, security headers,
X-Trace-Id — like any other response) and by app.main's generic exception
handler (the last-resort catch for anything raised outside those middlewares,
e.g. inside routing itself).

Building the response in one place guarantees the redaction rule (generic
Spanish message + trace_id in production, full exception detail outside
production) is applied identically everywhere, and that the full detail is
ALWAYS logged server-side regardless of environment.
"""

from __future__ import annotations

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import settings

logger = structlog.get_logger("app")


def internal_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Build the redacted 500 JSON response for an unhandled exception.

    Always logs the full exception detail server-side. The client-facing
    ``detail`` is a generic Spanish message in production, or
    ``f"{type(exc).__name__}: {exc}"`` outside production.
    """
    trace_id = getattr(request.state, "trace_id", None)
    logger.error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        exc_msg=str(exc),
        path=request.url.path,
        trace_id=trace_id,
    )
    detail = "Error interno del servidor" if settings.is_production else f"{type(exc).__name__}: {exc}"
    return JSONResponse(
        status_code=500,
        content={"detail": detail, "trace_id": trace_id},
    )
