"""FastAPI application entry point."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import IntegrityError

from app.api.router import api_v1_router
from app.core.audit import AuditMiddleware
from app.core.client_ip import TrustedProxyClientMiddleware
from app.core.config import APP_VERSION, settings
from app.core.error_response import internal_error_response
from app.core.exceptions import DomainError, domain_to_http
from app.core.rate_limit import limiter
from app.core.security_headers import SecurityHeadersMiddleware
from app.schemas.common import HealthResponse


def _wrapper_log_level() -> int:
    """DEBUG in development, INFO in production.

    `make_filtering_bound_logger(0)` (NOTSET) used to emit DEBUG-level logs
    unconditionally, including in production — noisy (e.g. every
    `agent_chat_llm_turn`/`llm_request` call) and a needless verbose-log-volume
    cost on a paid log sink. Kept as a standalone function (not inlined into
    `structlog.configure` below) so it's unit-testable without re-triggering
    the module-level `configure()` call, which is process-global.
    """
    return logging.DEBUG if settings.is_development else logging.INFO


# cache_logger_on_first_use=True is a real production perf optimization (skips
# re-resolving the processor chain on every log call) but has bitten this repo's
# test suite three separate times (radicacion-sin-friccion 0.3's contextvars
# concurrency test, 0.4's SECOP_APP_TOKEN warning test, and again after 0.4's own
# fix-pass): whichever module-level `structlog.get_logger(name)` proxy gets its
# FIRST real log call anywhere across the ~2450-test suite caches that binding for
# the rest of the run, and a later `structlog.testing.capture_logs()` in an
# unrelated test file can silently fail to intercept it depending on suite order.
# `app` is never imported by production/Railway with pytest on sys.path, so this
# is production-safe: the optimization stays on everywhere except under pytest.
_RUNNING_UNDER_PYTEST = "pytest" in sys.modules

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.is_development else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(_wrapper_log_level()),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=not _RUNNING_UNDER_PYTEST,
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
    from sqlalchemy import text as _text

    # pgvector is needed only for semantic-search QUERIES (the `embedding` column is
    # Text, so create_all itself does NOT need the type). Create it in its own
    # best-effort block so a role without CREATE EXTENSION privilege — e.g. a
    # locked-down GCP Cloud SQL user — still boots with a fully provisioned schema;
    # only semantic search degrades, instead of the whole app starting with no schema.
    if engine.dialect.name == "postgresql":
        try:
            async with engine.begin() as conn:
                await conn.execute(_text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            log.warning("pgvector_extension_skipped", error=str(exc))

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

    # 1b. Widen alembic_version before alembic touches it. Alembic's default
    #     version_num is VARCHAR(32), but several revision ids here are longer
    #     (e.g. "029_contexto_usuario_evidencia_texto", 36 chars). SQLite ignores the
    #     length; Postgres enforces it and the stamp/upgrade fails with a truncation
    #     error, leaving the DB unversioned. Pre-creating the table makes alembic reuse
    #     it instead of creating the narrow default.
    if db_ready:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    _text(
                        "CREATE TABLE IF NOT EXISTS alembic_version ("
                        "version_num VARCHAR(255) NOT NULL, "
                        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                    )
                )
                # If the table already existed (any DB provisioned before this fix),
                # CREATE IF NOT EXISTS is a no-op and version_num is still alembic's
                # default VARCHAR(32) — widen it so long revision ids don't truncate on
                # stamp/upgrade. Widen ONLY when actually narrower than 255 so we don't
                # take an ACCESS EXCLUSIVE lock on every boot (multi-replica rolling
                # deploys on Cloud SQL would otherwise serialize on it each startup).
                if engine.dialect.name == "postgresql":
                    current_len = (
                        await conn.execute(
                            _text(
                                "SELECT character_maximum_length FROM information_schema.columns "
                                "WHERE table_name = 'alembic_version' AND column_name = 'version_num'"
                            )
                        )
                    ).scalar()
                    if current_len is not None and current_len < 255:
                        await conn.execute(
                            _text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)")
                        )
        except Exception as exc:
            log.warning("alembic_version_prepare_failed", error=str(exc))

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
                "alembic",
                action,
                "head",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                await log.ainfo("alembic_ok", action=action, output=stdout.decode().strip())
            else:
                await log.awarning("alembic_failed", action=action, stderr=stderr.decode().strip())
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
    version=APP_VERSION,
    lifespan=lifespan,
)

# --- Middleware (order matters: last added = first executed) ---

# /mcp is dispatched by a path-scoped ASGI middleware instead of a Mount:
# a catch-all Mount("") full-matched every unclaimed path, which disabled
# Starlette's trailing-slash redirect app-wide (routes defined as "/x/"
# returned the MCP sub-app's plain-text 404 when called as "/x") and
# replaced FastAPI's JSON 404. Added FIRST so it runs innermost, i.e. /mcp
# requests still traverse CORS/audit/security-headers like everything else.
# See app/mcp/server.py::MCPDispatchMiddleware.
if settings.MCP_ENABLED:
    from app.mcp.server import MCPDispatchMiddleware

    app.add_middleware(MCPDispatchMiddleware)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
    # Without this, browsers strip Content-Disposition from cross-origin
    # responses before JS ever sees it (frontend :3000 / backend :8000 are
    # different origins) — every file-download helper that reads this header
    # to name the saved file silently falls back to a broken filename.
    expose_headers=["Content-Disposition"],
)

# Resolves the real client IP behind Railway's proxy (see
# app/core/client_ip.py). Added LAST so it is OUTERMOST — Starlette's
# add_middleware wraps last-added outermost, and this must run BEFORE
# SecurityHeadersMiddleware/AuditMiddleware/CORSMiddleware/the slowapi rate
# limiter so they all observe the resolved `request.client.host`, not the raw
# socket peer (the proxy's own address).
app.add_middleware(TrustedProxyClientMiddleware)

# Rate limiting
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Same body as slowapi's default handler, PLUS a `detail` key.

    The frontend's error formatter reads `err.response.data.detail` (matching
    every other error response in this API — see `domain_error_handler`
    below), but slowapi's built-in handler returns `{"error": ...}` only, so a
    429 was silently dropped client-side and the user saw the raw axios
    string instead of a useful message.

    The `_inject_headers` call below is kept for forward-compatibility with
    slowapi's own default handler, but it is a no-op today: `limiter` is
    built in `app/core/rate_limit.py` without `headers_enabled=True`, so no
    `Retry-After` / `X-RateLimit-*` header is actually added to the response.
    Enabling that is tracked separately since it would affect every
    `@limiter.limit(...)` route, not just this handler:
    https://github.com/Juanpgm/cashing_backend/issues/53
    """
    message = f"Rate limit exceeded: {exc.detail}"
    response = JSONResponse({"error": message, "detail": message}, status_code=429)
    return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)


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
    # Last-resort handler for exceptions raised outside AuditMiddleware and
    # SecurityHeadersMiddleware (e.g. in routing itself). Delegates to the
    # same shared builder those middlewares use so the redaction rule is
    # applied identically everywhere.
    return internal_error_response(request, exc)


# --- Routes ---

app.include_router(api_v1_router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(environment=settings.ENVIRONMENT, version=APP_VERSION)


# Developer Test UI (static file served at /test-ui)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/test-ui", include_in_schema=False)
async def test_ui() -> RedirectResponse:
    return RedirectResponse(url="/static/test_ui.html")


# NOTE: /mcp is served by MCPDispatchMiddleware (registered in the middleware
# section above), NOT by app.mount(). A catch-all Mount("") here broke
# trailing-slash redirects for the whole API — see the middleware comment.
