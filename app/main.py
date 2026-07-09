"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import IntegrityError

from app.api.router import api_v1_router
from app.core.audit import AuditMiddleware
from app.core.config import settings
from app.core.exceptions import DomainError, domain_to_http
from app.core.rate_limit import limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.schemas.common import HealthResponse

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.is_development else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle."""
    import asyncio

    from sqlalchemy import inspect as sa_inspect

    import app.models  # noqa: F401 — register all models
    from app.core.database import Base, engine

    log = structlog.get_logger("startup")

    # 1. Base schema — create_all is the source of truth (idempotent).
    #    Migrations do NOT build the base schema; they only carry ALTER deltas.
    db_ready = False
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        db_ready = True
        log.info("database_ready")
    except Exception as exc:
        log.warning(
            "database_unavailable",
            error=str(exc),
            note="App starting without DB — some endpoints will fail",
        )

    # 2. Alembic — reconcile the migration version WITHOUT re-running the base schema.
    #    If the DB is unversioned (schema just built by create_all), STAMP head so the
    #    version table matches reality. If it is already versioned, UPGRADE to apply any
    #    pending ALTER deltas. Running `upgrade` on a create_all DB replays migration 001
    #    and collides with existing tables — that was the startup traceback.
    if db_ready:
        try:
            from sqlalchemy import text

            def _is_versioned(sync_conn: object) -> bool:
                # Versioned only if alembic_version exists AND holds a revision row.
                # A create_all-built DB (or one where migration 001 failed) may have an
                # EMPTY alembic_version table — that must be stamped, not upgraded.
                if not sa_inspect(sync_conn).has_table("alembic_version"):
                    return False
                row = sync_conn.execute(  # type: ignore[attr-defined]
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                ).first()
                return row is not None

            async with engine.connect() as conn:
                is_versioned = await conn.run_sync(_is_versioned)
            action = "upgrade" if is_versioned else "stamp"
            proc = await asyncio.create_subprocess_exec(
                "alembic", action, "head",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                await log.ainfo("alembic_ok", action=action, output=stdout.decode().strip())
            else:
                await log.awarning(
                    "alembic_failed", action=action, stderr=stderr.decode().strip()
                )
        except Exception as exc:
            log.warning("alembic_error", error=str(exc))

    # Initialise agent graph
    try:
        from app.services.agent_service import initialise_graph

        initialise_graph()
        structlog.get_logger("startup").info("agent_graph_ready")
    except Exception as exc:
        structlog.get_logger("startup").warning("agent_graph_init_failed", error=str(exc))

    # SECOP relies on a Socrata app token; without it datos.gov.co throttles hard
    # and document datasets fail silently (only a few docs come back).
    from app.core.config import settings as _settings

    if not _settings.SECOP_APP_TOKEN:
        structlog.get_logger("startup").warning(
            "secop_app_token_missing",
            note="SECOP_APP_TOKEN is empty — Socrata will throttle; SECOP imports may be partial.",
        )

    # MCP streamable-http session manager: mounting via app.mount() does NOT
    # propagate the ASGI lifespan protocol into the mounted sub-app (Starlette's
    # Router only enters its OWN lifespan_context, never a mounted route's), so
    # the session manager backing /mcp must be started/stopped here explicitly.
    # See app/mcp/server.py module docstring for the full explanation.
    if settings.MCP_ENABLED:
        from app.mcp.server import get_mcp_server

        async with get_mcp_server().session_manager.run():
            yield
    else:
        yield


app = FastAPI(
    title="CashIn Backend",
    description="AI Agent-first backend for Colombian contractor billing automation",
    version="0.1.0",
    lifespan=lifespan,
)

# --- Middleware (order matters: last added = first executed) ---

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# --- Exception handlers ---


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    http_exc = domain_to_http(exc)
    trace_id = getattr(request.state, "trace_id", None)
    return JSONResponse(
        status_code=http_exc.status_code,
        content={"detail": http_exc.detail, "code": exc.code, "trace_id": trace_id},
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", None)
    structlog.get_logger("app").warning(
        "integrity_error",
        exc_msg=str(exc.orig),
        path=request.url.path,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=409,
        content={"detail": "El registro ya existe o viola una restricción de unicidad.", "trace_id": trace_id},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", None)
    structlog.get_logger("app").error(
        "unhandled_exception",
        exc_type=type(exc).__name__,
        exc_msg=str(exc),
        path=request.url.path,
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "trace_id": trace_id},
    )


# --- Routes ---

app.include_router(api_v1_router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(environment=settings.ENVIRONMENT)


# Developer Test UI (static file served at /test-ui)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/test-ui", include_in_schema=False)
async def test_ui() -> RedirectResponse:
    return RedirectResponse(url="/static/test_ui.html")


# --- MCP Server: curated tool registry (app.tools.registry.TOOL_REGISTRY), see app/mcp/server.py ---
#
# Mounted last, at an EMPTY path (`path=""` is explicitly valid for Starlette's
# Mount — it forwards the full, unmodified request path to the sub-app), and
# not at "/mcp": FastMCP's own streamable-http route already IS "/mcp"
# internally (see app/mcp/server.py). Mounting at "/mcp" too would either
# double the path to "/mcp/mcp", or — if the internal route were rooted at
# "/" instead — produce a same-path Mount whose empty remaining_path doesn't
# exactly match the sub-app's Route("/"), triggering Starlette's
# redirect_slashes middleware (a 307 to "/mcp/") on every call. Mounting at
# "" and registering it LAST means it only ever receives requests that didn't
# match any route above (health, static, api routes), and forwards them
# untouched so "/mcp" lands on the sub-app's own "/mcp" route with no redirect.
if settings.MCP_ENABLED:
    from app.mcp.server import mcp_asgi_app

    app.mount("", mcp_asgi_app())
