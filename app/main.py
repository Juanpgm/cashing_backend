"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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


# --- Routes ---

app.include_router(api_v1_router)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    return HealthResponse(environment=settings.ENVIRONMENT)
