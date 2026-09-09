"""Common schemas — shared response models."""

from typing import TypeVar

from pydantic import BaseModel

from app.core.config import APP_VERSION

T = TypeVar("T")


class ErrorResponse(BaseModel):
    detail: str
    trace_id: str | None = None


class PaginatedResponse[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


class HealthResponse(BaseModel):
    status: str = "ok"
    environment: str
    # Default kept only as a safety net for a caller that forgets to pass it
    # explicitly — app.main's /health route always does. See APP_VERSION's
    # docstring in app.core.config for why this can't resolve from package
    # metadata.
    version: str = APP_VERSION


class LLMModelStatus(BaseModel):
    model: str
    reachable: bool
    error: str | None = None
    latency_ms: float | None = None


class LLMHealthResponse(BaseModel):
    status: str  # "ok" | "degraded" | "error"
    is_production: bool
    model_chain: list[str]
    results: list[LLMModelStatus]
