"""Common schemas — shared response models."""

from typing import TypeVar

from pydantic import BaseModel

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
    version: str = "0.1.0"


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
