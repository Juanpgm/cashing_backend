"""Tests for the generic (unhandled exception) handler in app.main.

Security hardening: an unhandled exception used to leak
``f"{type(exc).__name__}: {exc}"`` straight to the client, which can expose
internal details (stack-trace-adjacent strings, library names, sometimes
data values embedded in the exception message). In production this must be
replaced by a generic Spanish message; the full detail must still be logged
server-side either way, and the trace_id must still be returned so the user
can reference it in a support request.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.config import settings
from app.main import app as fastapi_app
from app.services import contrato_service
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio


async def _raise_boom(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("boom-secret-internal-detail")


@pytest.fixture
async def raising_client() -> Any:
    """A client that surfaces the actual 500 response body instead of re-raising.

    Starlette's ServerErrorMiddleware sends the error response to the ASGI
    `send` callable and THEN re-raises the exception (by design, so app
    servers can log it) — httpx's ASGITransport re-raises that same exception
    to the caller by default (`raise_app_exceptions=True`), which is exactly
    what you want for catching unhandled bugs in most tests, but here we
    explicitly want to inspect the 500 response body the handler produced.
    """
    transport = ASGITransport(app=fastapi_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_generic_exception_hides_details_in_production(
    raising_client: AsyncClient, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(contrato_service, "listar_contratos", _raise_boom)

    response = await raising_client.get("/api/v1/contratos/", headers=test_user["headers"])

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Error interno del servidor"
    assert "boom-secret-internal-detail" not in body["detail"]
    assert "RuntimeError" not in body["detail"]
    assert body.get("trace_id")


async def test_generic_exception_shows_details_outside_production(
    raising_client: AsyncClient, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(contrato_service, "listar_contratos", _raise_boom)

    response = await raising_client.get("/api/v1/contratos/", headers=test_user["headers"])

    assert response.status_code == 500
    body = response.json()
    assert "boom-secret-internal-detail" in body["detail"]
    assert "RuntimeError" in body["detail"]
    assert body.get("trace_id")


async def test_500_still_carries_cors_and_security_headers_in_production(
    raising_client: AsyncClient, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a 500 raised deep in a route must still flow back out
    through CORSMiddleware and SecurityHeadersMiddleware.

    AuditMiddleware and SecurityHeadersMiddleware used to RE-RAISE unhandled
    exceptions so `app.main`'s generic handler could redact them. But that
    handler is wired via `@app.exception_handler(Exception)`, which Starlette
    dispatches from `ServerErrorMiddleware` — the OUTERMOST layer, sitting
    even above CORSMiddleware. Re-raising therefore skipped CORSMiddleware and
    both header middlewares entirely, producing an opaque, browser-blocked
    cross-origin error with no JSON body and no trace_id. This test exercises
    the full real middleware stack (the app factory) with a matching `Origin`
    header to catch that regression.
    """
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(contrato_service, "listar_contratos", _raise_boom)

    origin = "http://localhost:3000"
    assert origin in settings.CORS_ORIGINS

    headers = {**test_user["headers"], "Origin": origin}
    response = await raising_client.get("/api/v1/contratos/", headers=headers)

    assert response.status_code == 500

    # CORS header must be present so the browser doesn't turn this into an
    # opaque cross-origin error.
    assert response.headers.get("access-control-allow-origin") == origin

    # Security headers must still be applied to error responses.
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    # trace_id must be present in both the body and the header, and no
    # exception detail must leak in production.
    assert response.headers.get("x-trace-id")
    body = response.json()
    assert body["detail"] == "Error interno del servidor"
    assert "boom-secret-internal-detail" not in body["detail"]
    assert "RuntimeError" not in body["detail"]
    assert body.get("trace_id")
