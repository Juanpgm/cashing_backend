"""Tests for vector search utilities (Phase 3)."""
from __future__ import annotations

import json
import unittest

import pytest


class TestEmbeddingCodec:
    """encode_embedding / decode_embedding round-trip."""

    def test_encode_returns_json_string(self) -> None:
        from app.agent.tools.vector_search import encode_embedding

        vec = [0.1, 0.2, 0.3]
        result = encode_embedding(vec)
        assert isinstance(result, str)
        assert json.loads(result) == vec

    def test_decode_returns_list_of_floats(self) -> None:
        from app.agent.tools.vector_search import decode_embedding

        vec = [0.1, 0.2, 0.3]
        encoded = json.dumps(vec)
        result = decode_embedding(encoded)
        assert result == pytest.approx(vec)

    def test_encode_decode_roundtrip(self) -> None:
        from app.agent.tools.vector_search import encode_embedding, decode_embedding

        vec = [float(i) / 100 for i in range(10)]
        assert decode_embedding(encode_embedding(vec)) == pytest.approx(vec)

    def test_decode_none_returns_none(self) -> None:
        from app.agent.tools.vector_search import decode_embedding

        result = decode_embedding(None)
        assert result is None

    def test_encode_embedding_dim(self) -> None:
        """Vector can hold 1536-dim embeddings."""
        from app.agent.tools.vector_search import encode_embedding, EMBEDDING_DIM

        vec = [0.0] * EMBEDDING_DIM
        encoded = encode_embedding(vec)
        parsed = json.loads(encoded)
        assert len(parsed) == EMBEDDING_DIM


class TestGetEmbeddingFromLlm:
    """get_embedding_from_llm — unit (mocked LLM)."""

    def test_returns_list_of_floats(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock
        from app.agent.tools.vector_search import get_embedding_from_llm, EMBEDDING_DIM

        mock_llm = AsyncMock()
        mock_llm.embed = AsyncMock(return_value=[0.5] * EMBEDDING_DIM)

        result = asyncio.run(get_embedding_from_llm("hello world", mock_llm))

        assert isinstance(result, list)
        assert len(result) == EMBEDDING_DIM
        assert all(isinstance(x, float) for x in result)
