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


def _fake_embedding_response(vectors: list[list[float]]) -> SimpleNamespace:
    return SimpleNamespace(data=[{"embedding": v} for v in vectors])


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
