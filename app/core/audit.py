"""Audit logging for security-sensitive operations."""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.error_response import internal_error_response

logger = structlog.get_logger("audit")


class AuditMiddleware(BaseHTTPMiddleware):
    """Injects trace_id and logs every request with user context."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        # Extract user info from auth header if present (non-blocking)
        user_id = "anonymous"
        if hasattr(request.state, "user_id"):
            user_id = request.state.user_id

        try:
            response = await call_next(request)
        except Exception as exc:
            # Build the redacted error response here and RETURN it (do not
            # re-raise) so it flows out through the normal middleware chain —
            # CORSMiddleware and SecurityHeadersMiddleware still run on it.
            # Re-raising used to let Starlette's ServerErrorMiddleware (which
            # sits OUTSIDE all app middleware) build the response instead,
            # which bypassed CORS/security headers entirely and produced an
            # opaque, browser-blocked cross-origin error with no trace_id.
            await logger.ainfo(
                "request",
                trace_id=trace_id,
                method=request.method,
                path=request.url.path,
                status=500,
                user_id=user_id,
                ip=request.client.host if request.client else "unknown",
                user_agent=request.headers.get("user-agent", ""),
            )
            error_response = internal_error_response(request, exc)
            error_response.headers["X-Trace-Id"] = trace_id
            return error_response

        await logger.ainfo(
            "request",
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            user_id=user_id,
            ip=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent", ""),
        )

        response.headers["X-Trace-Id"] = trace_id
        return response


async def log_audit_event(
    *,
    action: str,
    user_id: str,
    resource: str = "",
    ip: str = "",
    trace_id: str = "",
    success: bool = True,
    detail: str = "",
) -> None:
    """Log a security-relevant event."""
    await logger.ainfo(
        "audit_event",
        action=action,
        user_id=user_id,
        resource=resource,
        ip=ip,
        trace_id=trace_id,
        success=success,
        detail=detail,
    )
