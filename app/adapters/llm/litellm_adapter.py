"""LiteLLM adapter — unified LLM access with tiers, fallback and cost tracking."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.adapters.llm.port import LLMPort
from app.core.config import settings
from app.core.langfuse_client import tracer
from app.core.observability import elapsed_ms
from app.schemas.agent import LLMMessage, LLMResponse, LLMToolCall

logger = structlog.get_logger("llm")

_VALID_REASONING_EFFORTS = {"low", "medium", "high"}


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
        reasoning_effort: str | None = None,
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
        # Groq-specific extra param (litellm passthrough) — only Groq models support
        # it; other providers (Gemini, Ollama, OpenAI) don't, so it must never be
        # sent to them. Decided per-attempted-model, not once at `complete()`, so a
        # fallback to a non-Groq model in the chain never carries it either.
        if reasoning_effort is not None and model.startswith("groq/"):
            kwargs["reasoning_effort"] = reasoning_effort
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
        reasoning_effort: str | None = None,
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

        ``reasoning_effort`` (``"low"``/``"medium"``/``"high"``) is a Groq-specific
        extra param forwarded to reasoning models (e.g. ``groq/openai/gpt-oss-20b``)
        — it is only ever sent when the model actually being called is a Groq
        model (see ``_call_model``); other providers never receive it. Any other
        value raises ``ValueError`` immediately, before any network call.

        Set ``fallback=False`` to try only the requested model — used for vision
        calls, where the text-only fallback models cannot read image parts and
        would just produce a misleading error.
        """
        if reasoning_effort is not None and reasoning_effort not in _VALID_REASONING_EFFORTS:
            raise ValueError(
                f"Invalid reasoning_effort={reasoning_effort!r}; must be one of {sorted(_VALID_REASONING_EFFORTS)}"
            )

        litellm_msgs = self._to_litellm_messages(messages)
        models = self._get_model_chain(model) if fallback else [model or self._default_model]
        last_error: Exception | None = None

        for idx, m in enumerate(models):
            try:
                called_model = self._rewrite_model_for_tools(m, tools)
                await logger.ainfo("llm_request", model=called_model, msg_count=len(messages))
                # reasoning_effort is tuned for the SPECIFIC model the caller asked
                # for (models[0], the primary) — never forwarded on a fallback
                # attempt, regardless of what provider/model it happens to be.
                # A Groq-model-level allowlist ("which Groq models support
                # reasoning_effort") would be more fragile than this: scoping by
                # loop position is simpler and correctly reflects that the caller
                # never tuned this param for whatever model ends up being tried
                # after the primary fails. `_call_model`'s own
                # `model.startswith("groq/")` gate stays as a secondary safety net.
                attempt_reasoning_effort = reasoning_effort if idx == 0 else None
                start = time.perf_counter()
                result = await self._call_model(
                    m,
                    litellm_msgs,
                    temperature,
                    max_tokens,
                    response_format,
                    tools,
                    tool_choice,
                    attempt_reasoning_effort,
                )
                duration_ms = elapsed_ms(start)
                # `idx` IS the fallback depth: 0 means the primary/requested model
                # answered on the first attempt, 1 means one fallback was needed, etc.
                result.fallback_depth = idx
                self._trace_generation(called_model, result, duration_ms, idx)
                await logger.ainfo(
                    "llm_response",
                    model=called_model,
                    tokens=result.total_tokens,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    duration_ms=round(duration_ms, 2),
                    fallback_depth=idx,
                )
                return result
            except Exception as exc:
                last_error = exc
                await logger.awarning("llm_fallback", model=m, error=str(exc))

        raise RuntimeError(f"All LLM models failed. Last error: {last_error}")

    @staticmethod
    def _trace_generation(model: str, result: LLMResponse, duration_ms: float, fallback_depth: int) -> None:
        """Best-effort Langfuse generation record for one successful model attempt.

        Wrapped independently of the LLM call itself: Langfuse being unreachable,
        misconfigured, or raising from a bad SDK response must NEVER surface as a
        chat-turn failure — this is purely observability plumbing riding alongside
        an already-successful completion. `tracer` itself no-ops when
        `LANGFUSE_PUBLIC_KEY` isn't set (see `app.core.langfuse_client`); this
        try/except additionally covers a *configured-but-broken* Langfuse (network
        down, bad keys, SDK exception) so that failure mode degrades the same way.
        """
        try:
            tracer.generation(
                name="llm_complete",
                model=model,
                usage={
                    "input": result.prompt_tokens,
                    "output": result.completion_tokens,
                    "total": result.total_tokens,
                },
                metadata={"fallback_depth": fallback_depth, "duration_ms": round(duration_ms, 2)},
            )
        except Exception as exc:
            logger.warning("langfuse_trace_failed", error=str(exc))

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

    @staticmethod
    def _get_embedding_model_chain(model: str | None) -> list[str]:
        """Ordered embedding models to try: requested/default → configured fallback.

        Mirrors `_get_model_chain`'s dedup logic, scoped to the two embedding-capable
        models (Groq has no embedding API, so it never enters this chain).
        """
        candidates = [model or settings.LLM_EMBEDDING_MODEL, settings.LLM_EMBEDDING_FALLBACK_MODEL]
        seen: set[str] = set()
        chain: list[str] = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                chain.append(c)
        return chain

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Return one embedding vector per input text via litellm's `aembedding`.

        Tries the primary embedding model, then `LLM_EMBEDDING_FALLBACK_MODEL`; raises
        if every model fails so the caller (evidence matcher) can fail-open to
        keyword-only ranking — see `evidence-embeddings` spec, "Fail-open" requirement.
        """
        import litellm

        last_error: Exception | None = None
        for m in self._get_embedding_model_chain(model):
            try:
                kwargs: dict = {"model": m, "input": texts, "timeout": 60}
                api_base = self._api_base_for(m)
                if api_base:
                    kwargs["api_base"] = api_base
                api_key = self._api_key_for(m)
                if api_key:
                    kwargs["api_key"] = api_key
                response = await litellm.aembedding(**kwargs)
                return [item["embedding"] if isinstance(item, dict) else item.embedding for item in response.data]
            except Exception as exc:
                last_error = exc
                await logger.awarning("llm_embed_fallback", model=m, error=str(exc))

        raise RuntimeError(f"All embedding models failed. Last error: {last_error}")


def get_llm(model: str | None = None) -> LLMPort:
    """Factory — returns the configured `LLMPort` implementation.

    `settings.LLM_PROVIDER` selects the implementation: "litellm" (default) wires
    the real Gemini -> Groq -> Ollama fallback chain via `LiteLLMAdapter`; "fake"
    returns `FakeLLMPort` (see `app.adapters.llm.fake_adapter`) — a network-free
    implementation for local dev/E2E runs whose tool-NAME sequence is
    deterministic but whose synthesized argument VALUES are not (see that
    module's docstring for the precise distinction). The import is local to
    avoid paying `app.tools.registry`'s import cost (pulled in by `fake_adapter`)
    on the default "litellm" path, which every request already takes.
    """
    if settings.LLM_PROVIDER == "fake":
        from app.adapters.llm.fake_adapter import FakeLLMPort

        return FakeLLMPort(default_model=model)
    return LiteLLMAdapter(default_model=model)
