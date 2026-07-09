"""LiteLLM adapter — unified LLM access with tiers, fallback and cost tracking."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.schemas.agent import LLMMessage, LLMResponse, LLMToolCall

logger = structlog.get_logger("llm")


class LiteLLMAdapter:
    """Wraps LiteLLM for async completions with automatic fallback."""

    def __init__(self, default_model: str | None = None) -> None:
        self._default_model = default_model or settings.LLM_DEFAULT_MODEL

    def _get_model_chain(self, model: str | None) -> list[str]:
        """Return ordered list of models to try (primary → fallback → local/production-fallback).

        In production (settings.is_production), LLM_LOCAL_MODEL (Ollama) is replaced by
        LLM_PRODUCTION_FALLBACK_MODEL when set, or silently dropped when not set.
        Prevents Railway containers from hanging on a connection to a non-existent Ollama instance.

        Chain: requested model → LLM_FALLBACK_MODEL → LLM_LOCAL_MODEL (dev)
                                                     → LLM_PRODUCTION_FALLBACK_MODEL (prod)
        Duplicates are removed to avoid retrying the same model.
        """
        primary = model or self._default_model
        local_candidate = settings.LLM_LOCAL_MODEL

        if settings.is_production:
            local_candidate = settings.LLM_PRODUCTION_FALLBACK_MODEL  # may be ""

        candidates = [primary, settings.LLM_FALLBACK_MODEL, local_candidate]
        seen: set[str] = set()
        chain: list[str] = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                chain.append(c)
        return chain

    @staticmethod
    def _to_litellm_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id is not None:
                entry["tool_call_id"] = m.tool_call_id
            if m.tool_calls is not None:
                entry["tool_calls"] = m.tool_calls
            result.append(entry)
        return result

    @staticmethod
    def _rewrite_model_for_tools(model: str, tools: list[dict[str, Any]] | None) -> str:
        """Rewrite ``ollama/<model>`` to ``ollama_chat/<model>`` when tools are requested.

        LiteLLM's ``ollama/`` provider talks to Ollama's `/api/generate` endpoint,
        which has weak/absent native tool-calling support; ``ollama_chat/`` uses
        `/api/chat`, which properly returns structured ``tool_calls``. Only rewrite
        when tools are actually being sent so plain completions keep using the
        originally configured provider prefix.
        """
        if tools and model.startswith("ollama/") and not model.startswith("ollama_chat/"):
            return "ollama_chat/" + model[len("ollama/") :]
        return model

    @staticmethod
    def _api_key_for(model: str) -> str | None:
        """Resolve the provider API key from settings.

        LiteLLM otherwise only reads keys from ``os.environ``; ours live in
        Settings (loaded from .env files), so we must pass them explicitly.
        """
        if model.startswith("gemini/"):
            return settings.GEMINI_API_KEY or None
        if model.startswith("groq/"):
            return settings.GROQ_API_KEY or None
        if model.startswith(("openai/", "gpt-")):
            return settings.OPENAI_API_KEY or None
        if model.startswith("mistral/"):
            return settings.MISTRAL_API_KEY or None
        return None

    @staticmethod
    def _api_base_for(model: str) -> str | None:
        """Resolve provider-specific api_base overrides."""
        if model.startswith(("ollama/", "ollama_chat/")):
            return settings.OLLAMA_BASE_URL
        return None

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=1, max=4),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        import litellm

        model = self._rewrite_model_for_tools(model, tools)

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": 120,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        api_base = self._api_base_for(model)
        if api_base:
            kwargs["api_base"] = api_base
        api_key = self._api_key_for(model)
        if api_key:
            kwargs["api_key"] = api_key

        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]  # type: ignore[union-attr]
        usage = response.usage  # type: ignore[union-attr]
        return LLMResponse(
            content=choice.message.content or "",
            model=model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            tool_calls=self._parse_tool_calls(choice.message),
        )

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[LLMToolCall] | None:
        """Parse litellm's ``choice.message.tool_calls`` into ``LLMToolCall`` list.

        ``arguments`` normally arrives from litellm as a JSON object string, but some
        providers may already hand back a native dict, or a syntactically valid JSON
        string that decodes to something other than an object (e.g. an array or a
        scalar). Any of these malformed/unexpected shapes degrades to ``{}`` with a
        warning rather than raising, since a badly-formed tool call is a model error
        the caller should be able to surface, not a crash.
        """
        raw_calls = getattr(message, "tool_calls", None)
        if not raw_calls:
            return None

        parsed: list[LLMToolCall] = []
        for call in raw_calls:
            fn = call.function
            if isinstance(fn.arguments, dict):
                arguments: Any = fn.arguments
            else:
                try:
                    arguments = json.loads(fn.arguments) if fn.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    logger.warning("llm_tool_call_bad_arguments", name=fn.name, raw=fn.arguments)
                    arguments = {}
            if not isinstance(arguments, dict):
                logger.warning("llm_tool_call_non_dict_arguments", name=fn.name, raw=fn.arguments)
                arguments = {}
            parsed.append(LLMToolCall(id=call.id, name=fn.name, arguments=arguments))
        return parsed

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        fallback: bool = True,
    ) -> LLMResponse:
        """Complete with automatic fallback through model chain.

        When ``response_format`` is a Pydantic model class (or a json_schema
        dict), LiteLLM requests structured output; the response ``content`` is
        then a JSON string the caller can validate with ``model_validate_json``.

        When ``tools`` is set, they are forwarded to the provider (OpenAI
        function-calling schema — see ``app.tools.llm_schema.to_openai_tools``)
        and ``LLMResponse.tool_calls`` is populated if the model requests one or
        more invocations. See ``_rewrite_model_for_tools`` for the Ollama
        provider caveat.

        Set ``fallback=False`` to try only the requested model — used for vision
        calls, where the text-only fallback models cannot read image parts and
        would just produce a misleading error.
        """
        litellm_msgs = self._to_litellm_messages(messages)
        models = self._get_model_chain(model) if fallback else [model or self._default_model]
        last_error: Exception | None = None

        for m in models:
            try:
                called_model = self._rewrite_model_for_tools(m, tools)
                await logger.ainfo("llm_request", model=called_model, msg_count=len(messages))
                result = await self._call_model(
                    m, litellm_msgs, temperature, max_tokens, response_format, tools, tool_choice
                )
                await logger.ainfo(
                    "llm_response",
                    model=called_model,
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

        stream_kwargs: dict = {
            "model": target_model,
            "messages": litellm_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "timeout": 120,
        }
        if target_model.startswith("ollama/"):
            stream_kwargs["api_base"] = settings.OLLAMA_BASE_URL
        api_key = self._api_key_for(target_model)
        if api_key:
            stream_kwargs["api_key"] = api_key

        response = await litellm.acompletion(**stream_kwargs)
        async for chunk in response:  # type: ignore[union-attr]
            delta = chunk.choices[0].delta  # type: ignore[union-attr]
            if delta and delta.content:
                yield delta.content


def get_llm(model: str | None = None) -> LiteLLMAdapter:
    """Factory — returns the LLM adapter."""
    return LiteLLMAdapter(default_model=model)
