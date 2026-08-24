"""Tests for embedding_service._call_embedding_api model/dimension contract."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.core.config import settings
from app.services.embedding_service import EMBEDDING_DIM, _call_embedding_api


@pytest.mark.asyncio
async def test_uses_configured_gemini_model_not_hardcoded_openai() -> None:
    """Regression: _call_embedding_api must use the configured embedding model
    (Gemini) with a 1536-dim request — NOT the hardcoded OpenAI
    `text-embedding-3-small`, which needs OPENAI_API_KEY and silently degraded
    every embedding to zero vectors in prod.
    """
    captured: dict[str, object] = {}

    async def fake_aembedding(**kwargs: object) -> dict[str, list[dict[str, list[float]]]]:
        captured.update(kwargs)
        inputs = kwargs["input"]
        assert isinstance(inputs, list)
        return {"data": [{"embedding": [0.1] * EMBEDDING_DIM} for _ in inputs]}

    with patch("litellm.aembedding", side_effect=fake_aembedding):
        out = await _call_embedding_api(["obligación de prueba"])

    assert captured["model"] == settings.LLM_EMBEDDING_MODEL
    assert str(captured["model"]).startswith("gemini/"), "solo Gemini — no OpenAI"
    assert captured.get("dimensions") == EMBEDDING_DIM
    assert len(out) == 1
    assert len(out[0]) == EMBEDDING_DIM


@pytest.mark.asyncio
async def test_falls_back_to_zero_vectors_on_api_failure() -> None:
    """When the embedding API errors (e.g. no key in dev), the service degrades to
    zero vectors of the right dimension instead of raising."""

    async def boom(**_kwargs: object) -> None:
        raise RuntimeError("no api key")

    with patch("litellm.aembedding", side_effect=boom):
        out = await _call_embedding_api(["a", "b"])

    assert out == [[0.0] * EMBEDDING_DIM, [0.0] * EMBEDDING_DIM]
