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

    import app.models  # noqa: F401 — register all models
    from app.core.database import Base, engine

    # Run Alembic migrations (handles ALTER TABLE for existing columns)
    try:
        proc = await asyncio.create_subprocess_exec(
            "alembic", "upgrade", "head",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        log = structlog.get_logger("startup")
        if proc.returncode == 0:
            await log.ainfo("alembic_upgrade_ok", output=stdout.decode().strip())
        else:
            await log.awarning("alembic_upgrade_failed", stderr=stderr.decode().strip())
    except Exception as exc:
        structlog.get_logger("startup").warning("alembic_upgrade_error", error=str(exc))

    # create_all handles any new tables not covered by migrations
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        structlog.get_logger("startup").info("database_ready")
    except Exception as exc:
        structlog.get_logger("startup").warning(
            "database_unavailable",
            error=str(exc),
            note="App starting without DB — some endpoints will fail",
        )

    # Initialise agent graph
    try:
        from app.services.agent_service import initialise_graph

        initialise_graph()
        structlog.get_logger("startup").info("agent_graph_ready")
    except Exception as exc:
        structlog.get_logger("startup").warning("agent_graph_init_failed", error=str(exc))

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
        content={"detail": http_exc.detail, "trace_id": trace_id},
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

# --- MCP Server (dev only) ---
if settings.is_development:
    from fastapi_mcp import FastApiMCP
    mcp = FastApiMCP(app, name="CashIn MCP", description="CashIn backend tools via MCP")
    mcp.mount_http(app, mount_path="/mcp")


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(environment=settings.ENVIRONMENT)


# Developer Test UI (static file served at /test-ui)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/test-ui", include_in_schema=False)
async def test_ui() -> RedirectResponse:
    return RedirectResponse(url="/static/test_ui.html")
