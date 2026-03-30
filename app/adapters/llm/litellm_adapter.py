"""LiteLLM adapter — unified LLM access with tiers, fallback and cost tracking."""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.schemas.agent import LLMMessage, LLMResponse

logger = structlog.get_logger("llm")


class LiteLLMAdapter:
    """Wraps LiteLLM for async completions with automatic fallback."""

    def __init__(self, default_model: str | None = None) -> None:
        self._default_model = default_model or settings.LLM_DEFAULT_MODEL

    def _get_model_chain(self, model: str | None) -> list[str]:
        """Return ordered list of models to try (primary → fallback)."""
        primary = model or self._default_model
        chain = [primary]
        if primary != settings.LLM_FALLBACK_MODEL:
            chain.append(settings.LLM_FALLBACK_MODEL)
        return chain

    @staticmethod
    def _to_litellm_messages(messages: list[LLMMessage]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=1, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call_model(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        import litellm

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]  # type: ignore[union-attr]
        usage = response.usage  # type: ignore[union-attr]
        return LLMResponse(
            content=choice.message.content or "",
            model=model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        )

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """Complete with automatic fallback through model chain."""
        litellm_msgs = self._to_litellm_messages(messages)
        models = self._get_model_chain(model)
        last_error: Exception | None = None

        for m in models:
            try:
                await logger.ainfo("llm_request", model=m, msg_count=len(messages))
                result = await self._call_model(m, litellm_msgs, temperature, max_tokens)
                await logger.ainfo(
                    "llm_response",
                    model=m,
                    tokens=result.total_tokens,
                )
                return result
            except Exception as exc:
                last_error = exc
                await logger.awarning("llm_fallback", model=m, error=str(exc))

        raise RuntimeError(f"All LLM models failed. Last error: {last_error}")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """Stream tokens from the LLM."""
        import litellm

        litellm_msgs = self._to_litellm_messages(messages)
        target_model = model or self._default_model
        await logger.ainfo("llm_stream_start", model=target_model)

        response = await litellm.acompletion(
            model=target_model,
            messages=litellm_msgs,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in response:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta  # type: ignore[union-attr]
            if delta and delta.content:
                yield delta.content


def get_llm(model: str | None = None) -> LiteLLMAdapter:
    """Factory — returns the LLM adapter."""
    return LiteLLMAdapter(default_model=model)
