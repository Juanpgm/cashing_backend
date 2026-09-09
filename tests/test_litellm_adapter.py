"""Tests for `LiteLLMAdapter.embed()` — wraps litellm's `aembedding` for in-memory
cosine ranking in the evidence matcher (evidence-embeddings capability).

All litellm calls are mocked — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.core.config import settings
from app.schemas.agent import LLMMessage


def _fake_embedding_response(vectors: list[list[float]]) -> SimpleNamespace:
    return SimpleNamespace(data=[{"embedding": v} for v in vectors])


def _fake_completion_response(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    return SimpleNamespace(choices=[choice], usage=usage)


class TestEmbeddingModelDefault:
    def test_default_embedding_model_is_a_working_gemini_model(self) -> None:
        """text-embedding-004 is 404 NotFound on current project keys (deprecated for
        new keys — same class of issue as the known gemini-2.5-flash-vs-flash-lite 404
        pattern). gemini-embedding-001 is empirically confirmed working (dim=3072).
        """
        assert settings.LLM_EMBEDDING_MODEL == "gemini/gemini-embedding-001"


class TestEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_one_vector_per_text(self) -> None:
        adapter = LiteLLMAdapter()
        fake_response = _fake_embedding_response([[0.1, 0.2], [0.3, 0.4]])

        with patch("litellm.aembedding", new=AsyncMock(return_value=fake_response)) as mock_call:
            result = await adapter.embed(["texto obligación", "texto evidencia"])

        assert result == [[0.1, 0.2], [0.3, 0.4]]
        assert mock_call.call_args.kwargs["input"] == ["texto obligación", "texto evidencia"]
        assert mock_call.call_args.kwargs["model"] == settings.LLM_EMBEDDING_MODEL

    @pytest.mark.asyncio
    async def test_embed_single_text_returns_single_vector(self) -> None:
        """Triangulation: a different input size still returns one vector per text."""
        adapter = LiteLLMAdapter()
        fake_response = _fake_embedding_response([[9.9]])

        with patch("litellm.aembedding", new=AsyncMock(return_value=fake_response)):
            result = await adapter.embed(["solo un texto"])

        assert result == [[9.9]]

    @pytest.mark.asyncio
    async def test_embed_falls_back_to_secondary_model_on_primary_failure(self) -> None:
        adapter = LiteLLMAdapter()
        fake_response = _fake_embedding_response([[1.0, 2.0]])

        with patch(
            "litellm.aembedding",
            new=AsyncMock(side_effect=[RuntimeError("primary embedding model down"), fake_response]),
        ) as mock_call:
            result = await adapter.embed(["texto"])

        assert result == [[1.0, 2.0]]
        assert mock_call.call_count == 2
        assert mock_call.call_args_list[0].kwargs["model"] == settings.LLM_EMBEDDING_MODEL
        assert mock_call.call_args_list[1].kwargs["model"] == settings.LLM_EMBEDDING_FALLBACK_MODEL

    @pytest.mark.asyncio
    async def test_embed_raises_when_all_models_fail(self) -> None:
        """No fallback left standing → raise, so the caller (matcher) can fail-open to keyword-only."""
        adapter = LiteLLMAdapter()

        with (
            patch("litellm.aembedding", new=AsyncMock(side_effect=RuntimeError("provider unreachable"))),
            pytest.raises(RuntimeError),
        ):
            await adapter.embed(["texto"])


class TestReasoningEffortPassthrough:
    """`reasoning_effort` is a Groq-specific extra param (litellm passthrough).

    Groq's decommissioned `groq/llama-3.1-8b-instant` was replaced with
    `groq/openai/gpt-oss-20b`, a reasoning model that burns hidden
    `reasoning_tokens` before producing visible content. `reasoning_effort`
    must be forwarded ONLY to Groq models — other providers (Gemini, Ollama,
    OpenAI) don't support this param.
    """

    @pytest.mark.asyncio
    async def test_reasoning_effort_forwarded_for_groq_model(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        fake_response = _fake_completion_response()

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                reasoning_effort="low",
                fallback=False,
            )

        assert mock_call.call_args.kwargs["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_reasoning_effort_not_forwarded_for_non_groq_model(self) -> None:
        """Gemini (and every other non-Groq provider) must never receive this kwarg —
        litellm/the provider could error or silently ignore it, either way it's not
        a param we control the behavior of outside Groq."""
        adapter = LiteLLMAdapter(default_model="gemini/gemini-2.5-flash")
        fake_response = _fake_completion_response()

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                reasoning_effort="low",
                fallback=False,
            )

        assert "reasoning_effort" not in mock_call.call_args.kwargs

    @pytest.mark.asyncio
    async def test_invalid_reasoning_effort_raises_before_any_network_call(self) -> None:
        """Only 'low'/'medium'/'high' are valid — Groq's API rejects 'none' with a
        400. Fail fast in the adapter instead of burning a round trip on a
        guaranteed-bad request."""
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")

        with (
            patch("litellm.acompletion", new=AsyncMock()) as mock_call,
            pytest.raises(ValueError, match="reasoning_effort"),
        ):
            await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                reasoning_effort="none",
                fallback=False,
            )

        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_reasoning_effort_none_default_is_no_regression(self) -> None:
        """Every existing caller that doesn't pass reasoning_effort must see
        byte-identical behavior — no new kwarg sent to litellm."""
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        fake_response = _fake_completion_response()

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            result = await adapter.complete([LLMMessage(role="user", content="hola")], fallback=False)

        assert result.content == "ok"
        assert "reasoning_effort" not in mock_call.call_args.kwargs

    @pytest.mark.asyncio
    async def test_fallback_to_non_groq_model_does_not_forward_reasoning_effort(self, monkeypatch) -> None:
        """The forwarding decision is per-model-in-the-fallback-chain, not fixed once
        at the complete() call: when the Groq primary fails over to a non-Groq
        fallback model, that second attempt must not carry reasoning_effort — and
        the existing fallback-on-exception mechanism must still work unchanged.

        `LLM_FALLBACK_MODEL` is forced to a non-Groq model so this is deterministic
        regardless of the environment's own .env value (which may itself be Groq).
        `_call_model` is internally retried once by tenacity before its exception
        reaches the outer fallback loop, so the Groq attempt needs TWO failures
        (not one) to exhaust and fall through to the next model in the chain.
        """
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        fake_response = _fake_completion_response()

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[RuntimeError("groq down"), RuntimeError("groq down"), fake_response]),
        ) as mock_call:
            result = await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                reasoning_effort="low",
            )

        assert result.content == "ok"
        assert mock_call.call_count == 3
        assert mock_call.call_args_list[0].kwargs["model"] == "groq/openai/gpt-oss-20b"
        assert mock_call.call_args_list[0].kwargs["reasoning_effort"] == "low"
        assert mock_call.call_args_list[1].kwargs["reasoning_effort"] == "low"
        assert mock_call.call_args_list[2].kwargs["model"] == "gemini/gemini-2.5-flash"
        assert "reasoning_effort" not in mock_call.call_args_list[2].kwargs

    @pytest.mark.asyncio
    async def test_fallback_to_different_groq_model_does_not_forward_reasoning_effort(self, monkeypatch) -> None:
        """`reasoning_effort` must be scoped to the PRIMARY model the caller actually
        requested (models[0]), never to "any Groq model that happens to be in the
        fallback chain". The `_call_model`-internal `model.startswith("groq/")` gate
        is provider-level, not model-level — it alone would still forward
        reasoning_effort to a different Groq fallback model (e.g. a non-reasoning
        one), which Groq's API can reject with a 400. `LLM_FALLBACK_MODEL` is forced
        to a DIFFERENT Groq model here so the only thing that can save the fallback
        attempt from carrying reasoning_effort is the loop-position (index) check in
        `complete()`, not the provider-prefix check.
        """
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "groq/llama-3.3-70b-versatile")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        fake_response = _fake_completion_response()

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[RuntimeError("groq down"), RuntimeError("groq down"), fake_response]),
        ) as mock_call:
            result = await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                reasoning_effort="low",
            )

        assert result.content == "ok"
        assert mock_call.call_count == 3
        assert mock_call.call_args_list[0].kwargs["model"] == "groq/openai/gpt-oss-20b"
        assert mock_call.call_args_list[0].kwargs["reasoning_effort"] == "low"
        assert mock_call.call_args_list[1].kwargs["reasoning_effort"] == "low"
        assert mock_call.call_args_list[2].kwargs["model"] == "groq/llama-3.3-70b-versatile"
        assert "reasoning_effort" not in mock_call.call_args_list[2].kwargs
