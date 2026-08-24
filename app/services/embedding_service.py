"""Embedding service — generates and stores 1536-dim Gemini embedding vectors for obligations."""

from __future__ import annotations

import json
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.obligacion import Obligacion

logger = structlog.get_logger("service.embedding")

EMBEDDING_DIM = 1536


async def _call_embedding_api(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via the configured (Gemini) embedding model.

    Uses ``settings.LLM_EMBEDDING_MODEL`` (default ``gemini/gemini-embedding-001``)
    at ``EMBEDDING_DIM`` dimensions so obligation and query embeddings stay in the
    same 1536-dim space. Falls back to zero vectors when the API is unavailable
    (dev/test with no key). litellm reads ``GEMINI_API_KEY`` from the environment.
    """
    try:
        import litellm  # type: ignore[import-untyped]

        resp = await litellm.aembedding(
            model=settings.LLM_EMBEDDING_MODEL,
            input=texts,
            dimensions=EMBEDDING_DIM,
            api_key=settings.GEMINI_API_KEY or None,
        )
        return [item["embedding"] for item in resp["data"]]
    except Exception as exc:
        await logger.awarning("embedding_api_failed", error=str(exc), fallback="zeros")
        return [[0.0] * EMBEDDING_DIM for _ in texts]


async def generate_embeddings_for_contrato(
    db: AsyncSession,
    contrato_id: uuid.UUID,
) -> int:
    """Generate and persist embeddings for all obligations of a contract.

    Skips obligations that already have embeddings.
    Returns the count of obligations updated.
    """
    result = await db.execute(
        select(Obligacion).where(
            Obligacion.contrato_id == contrato_id,
            Obligacion.embedding.is_(None),
        )
    )
    obligations = result.scalars().all()

    if not obligations:
        return 0

    texts = [ob.descripcion for ob in obligations]
    embeddings = await _call_embedding_api(texts)

    for ob, emb in zip(obligations, embeddings):
        ob.embedding = json.dumps(emb)
        db.add(ob)

    await db.flush()
    await logger.ainfo(
        "embeddings_generated",
        contrato_id=str(contrato_id),
        count=len(obligations),
    )
    return len(obligations)


async def generate_embedding_for_text(text: str) -> list[float]:
    """Generate an embedding for a single text string (for semantic search queries)."""
    embeddings = await _call_embedding_api([text])
    return embeddings[0]
