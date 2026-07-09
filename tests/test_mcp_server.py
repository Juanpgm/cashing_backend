"""Tests for the curated MCP server (app.mcp.server / app.mcp.auth).

Driving the full streamable-http wire protocol in-process (real JSON-RPC
framing, SSE responses, a live ASGI transport) would mostly re-test the `mcp`
SDK itself. Instead these tests exercise the two things that are actually
*ours*:

1. `app.mcp.auth.get_request_token` — the isolated, version-sensitive helper
   that recovers the bearer token from FastMCP's request context — tested
   directly with fake `Context`/request objects (no real HTTP needed).
2. The per-tool wrapper built by `app.mcp.server._make_wrapper`, exercised via
   `FastMCP.call_tool()` (the SDK's own dispatch entrypoint) with
   `app.mcp.server.get_request_token` monkeypatched to hand back a fixed
   token — this is the "test the wrapper layer directly (token extraction
   helper mocked)" option the task calls out, since there's no real Starlette
   request available when calling `call_tool()` outside an actual served
   HTTP request.

Plus one minimal HTTP smoke test (`test_mount_smoke.py`-style) verifying
`POST /mcp` on the mounted ASGI app answers with a protocol response rather
than a 404, and two tests asserting the FastAPI app only mounts /mcp when
`MCP_ENABLED` is true.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import date
from types import SimpleNamespace

import app.mcp.server as mcp_server_module
import pytest
from app.core import database
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.mcp.auth import get_request_token
from app.mcp.server import get_mcp_server
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.tools.registry import TOOL_REGISTRY
from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import async_session_test

# --- Helpers -----------------------------------------------------------------


async def _make_user_with_contrato(db: AsyncSession) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email="mcp_server_test@example.com",
        nombre="MCP Server Test User",
        cedula="30303030",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="MCP-0001",
        objeto="Objeto de prueba para el servidor MCP curado",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor="30303030",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


# --- 1. Curated surface -------------------------------------------------------


@pytest.mark.asyncio
async def test_curated_server_exposes_exactly_the_registry() -> None:
    """The curated server must mirror TOOL_REGISTRY exactly — no REST routes leak."""
    mcp = get_mcp_server()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}

    assert tool_names == set(TOOL_REGISTRY.keys())
    for t in tools:
        assert (t.description or "").strip(), f"{t.name} has an empty MCP description"


# --- 2. app.mcp.auth.get_request_token ---------------------------------------


def test_get_request_token_returns_none_outside_a_request() -> None:
    ctx = Context(request_context=None)
    assert get_request_token(ctx) is None


def test_get_request_token_returns_none_without_authorization_header() -> None:
    fake_request = SimpleNamespace(headers={})
    fake_request_context = SimpleNamespace(request=fake_request)
    ctx = Context(request_context=fake_request_context)
    assert get_request_token(ctx) is None


def test_get_request_token_returns_none_for_non_bearer_scheme() -> None:
    fake_request = SimpleNamespace(headers={"authorization": "Basic dXNlcjpwYXNz"})
    fake_request_context = SimpleNamespace(request=fake_request)
    ctx = Context(request_context=fake_request_context)
    assert get_request_token(ctx) is None


def test_get_request_token_extracts_bearer_token() -> None:
    fake_request = SimpleNamespace(headers={"authorization": "Bearer abc.def.ghi"})
    fake_request_context = SimpleNamespace(request=fake_request)
    ctx = Context(request_context=fake_request_context)
    assert get_request_token(ctx) == "abc.def.ghi"


# --- 3. Auth at the tool-call layer (token extraction mocked) ----------------


@pytest.mark.asyncio
async def test_call_tool_without_token_is_an_mcp_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server_module, "get_request_token", lambda ctx: None)
    mcp = get_mcp_server()

    with pytest.raises(ToolError):
        await mcp.call_tool("resumen_checklist", {"cuenta_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_call_tool_with_invalid_token_is_an_mcp_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server_module, "get_request_token", lambda ctx: "not-a-real-jwt")
    mcp = get_mcp_server()

    with pytest.raises(ToolError):
        await mcp.call_tool("resumen_checklist", {"cuenta_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_call_tool_with_valid_token_reaches_business_logic(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    """A valid token authenticates successfully and the call reaches invoke_tool —
    proven here by getting a NotFoundError (wrapped as ToolError) for a
    nonexistent cuenta_id, rather than an auth error."""
    monkeypatch.setattr(database, "async_session_factory", async_session_test)

    user, _contrato = await _make_user_with_contrato(db)
    token = create_access_token(subject=str(user.id), role=user.rol)
    monkeypatch.setattr(mcp_server_module, "get_request_token", lambda ctx: token)

    mcp = get_mcp_server()
    with pytest.raises(ToolError, match=r"not found|NotFoundError"):
        await mcp.call_tool("resumen_checklist", {"cuenta_id": str(uuid.uuid4())})


# --- 4. Round-trip: read + write tools through the MCP layer, write commits --


@pytest.mark.asyncio
async def test_round_trip_write_then_read_through_mcp_layer_commits(
    monkeypatch: pytest.MonkeyPatch, db: AsyncSession
) -> None:
    # Point the MCP wrapper's session factory at the same in-memory SQLite
    # engine the `db` fixture uses, so seeded data is visible and writes can
    # be checked from a brand-new session afterwards.
    monkeypatch.setattr(database, "async_session_factory", async_session_test)

    user, contrato = await _make_user_with_contrato(db)
    token = create_access_token(subject=str(user.id), role=user.rol)
    monkeypatch.setattr(mcp_server_module, "get_request_token", lambda ctx: token)

    mcp = get_mcp_server()

    # Write tool: crear_cuenta_cobro (tags=("write",)) — must commit.
    _unstructured, structured = await mcp.call_tool(
        "crear_cuenta_cobro",
        {"contrato_id": str(contrato.id), "mes": 6, "anio": 2026},
    )
    assert structured["estado"] == "borrador"
    cuenta_id = structured["id"]

    # Visible from a FRESH session — proves the wrapper committed, since the
    # catalog wrapper itself is flush-only.
    async with async_session_test() as fresh_session:
        from app.models.cuenta_cobro import CuentaCobro

        result = await fresh_session.execute(select(CuentaCobro).where(CuentaCobro.id == uuid.UUID(cuenta_id)))
        assert result.scalar_one_or_none() is not None

    # Read tool: resumen_checklist (tags=("read",)) — no requisitos_modo yet.
    _unstructured, checklist_structured = await mcp.call_tool(
        "resumen_checklist",
        {"cuenta_id": cuenta_id},
    )
    assert checklist_structured["requisitos_definidos"] is False
    assert checklist_structured["items"] == []


# --- 5. Dispatch middleware toggled by MCP_ENABLED ---------------------------


def _has_mcp_dispatcher(app) -> bool:  # type: ignore[no-untyped-def]
    """The /mcp dispatcher is a path-scoped ASGI middleware, NOT a Mount —
    a catch-all Mount("") broke trailing-slash redirects app-wide."""
    from app.mcp.server import MCPDispatchMiddleware

    return any(m.cls is MCPDispatchMiddleware for m in app.user_middleware)


def test_mcp_dispatcher_registered_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MCP_ENABLED", True)
    import app.main as main_module

    importlib.reload(main_module)
    try:
        assert _has_mcp_dispatcher(main_module.app)
    finally:
        importlib.reload(main_module)


def test_mcp_dispatcher_absent_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MCP_ENABLED", False)
    import app.main as main_module

    importlib.reload(main_module)
    try:
        assert not _has_mcp_dispatcher(main_module.app)
    finally:
        importlib.reload(main_module)


def test_mcp_dispatch_does_not_break_slash_redirects_or_json_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the catch-all Mount("") bug: with MCP enabled, an API
    route defined with a trailing slash (e.g. /api/v1/contratos/) must still
    resolve when called without it (307 redirect or auth error — anything but
    a plain-text 404), and unmatched paths must keep FastAPI's JSON 404."""
    from starlette.testclient import TestClient

    monkeypatch.setattr(settings, "MCP_ENABLED", True)
    import app.main as main_module

    importlib.reload(main_module)
    try:
        # raise_server_exceptions off + no lifespan needed: routing happens
        # before any handler/DB work for the paths asserted here.
        client = TestClient(main_module.app, raise_server_exceptions=False)

        no_slash = client.get("/api/v1/contratos", follow_redirects=False)
        assert no_slash.status_code != 404, (
            "trailing-slash redirect was swallowed by the MCP dispatch"
        )

        unmatched = client.get("/definitely-not-a-route")
        assert unmatched.status_code == 404
        assert unmatched.headers["content-type"].startswith("application/json"), (
            "unmatched paths must keep FastAPI's JSON 404 envelope"
        )
    finally:
        importlib.reload(main_module)


# --- 6. Minimal HTTP smoke: POST /mcp responds, doesn't 404 ------------------


@pytest.mark.asyncio
async def test_post_mcp_initialize_is_not_a_404() -> None:
    """Smoke-test the exact dispatch shape app.main uses —
    `MCPDispatchMiddleware` wrapping the app — without needing a live
    Postgres/SQLite-backed FastAPI lifespan just to prove the route is wired
    up (see app/mcp/server.py docstring on why the session manager must be
    started explicitly instead of relying on the parent app's lifespan)."""
    from app.mcp.server import MCPDispatchMiddleware
    from httpx import ASGITransport, AsyncClient
    from starlette.applications import Starlette

    mcp = get_mcp_server()
    wrapper_app = MCPDispatchMiddleware(Starlette())

    async with mcp.session_manager.run():
        transport = ASGITransport(app=wrapper_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.0.1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code != 404
