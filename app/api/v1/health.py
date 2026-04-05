"""LLM health diagnostics endpoint."""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter

from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.core.config import settings
from app.schemas.common import LLMHealthResponse, LLMModelStatus

logger = structlog.get_logger("api.health")
router = APIRouter(prefix="/health", tags=["health"])

_PROBE_MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]


@router.get("/llm", response_model=LLMHealthResponse)
async def llm_health() -> LLMHealthResponse:
    """Probe each LLM in the configured chain.

    No authentication required — this is an ops/diagnostics endpoint.
    Use after Railway deploys to confirm LLM access works before processing real documents.

    Returns:
        status "ok" — all models reachable
        status "degraded" — at least one model reachable
        status "error" — no models reachable (check GROQ_API_KEY, quota, ENVIRONMENT)
    """
    adapter = LiteLLMAdapter()
    primary = settings.LLM_EXTRACTION_MODEL or None
    chain = adapter._get_model_chain(primary)
    results: list[LLMModelStatus] = []

    for model_name in chain:
        import litellm

        kwargs: dict[str, object] = {
            "model": model_name,
            "messages": _PROBE_MESSAGES,
            "temperature": 0.0,
            "max_tokens": 5,
            "timeout": 15,
        }
        if model_name.startswith("ollama/"):
            kwargs["api_base"] = settings.OLLAMA_BASE_URL

        t_start = time.monotonic()
        try:
            await litellm.acompletion(**kwargs)
            latency_ms = round((time.monotonic() - t_start) * 1000, 1)
            results.append(LLMModelStatus(model=model_name, reachable=True, latency_ms=latency_ms))
            await logger.ainfo("llm_health_probe_ok", model=model_name, latency_ms=latency_ms)
        except Exception as exc:
            latency_ms = round((time.monotonic() - t_start) * 1000, 1)
            results.append(
                LLMModelStatus(model=model_name, reachable=False, error=str(exc), latency_ms=latency_ms)
            )
            await logger.awarning("llm_health_probe_failed", model=model_name, error=str(exc))

    reachable = sum(1 for r in results if r.reachable)
    status = "ok" if reachable == len(results) else ("degraded" if reachable > 0 else "error")
    return LLMHealthResponse(
        status=status,
        is_production=settings.is_production,
        model_chain=chain,
        results=results,
    )
