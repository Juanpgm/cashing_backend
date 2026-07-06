"""Vector search tool — pgvector cosine similarity search for obligations.

Uses the `embedding` column (Text JSON) in `obligaciones` via a raw SQL cast
to `vector(1536)` so we do not need pgvector SQLAlchemy types at model level.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger("agent.tools.vector_search")

# Embedding dimension must match the model used (text-embedding-004 = 1536)
EMBEDDING_DIM = 1536


def encode_embedding(embedding: list[float]) -> str:
    """Encode a float list to the JSON text representation stored in DB."""
    return json.dumps(embedding)


def decode_embedding(text_value: str | None) -> list[float] | None:
    """Decode the JSON text representation back to a float list."""
    if text_value is None:
        return None
    try:
        return json.loads(text_value)
    except (json.JSONDecodeError, TypeError):
        return None


async def semantic_search_obligaciones(
    db: AsyncSession,
    query_embedding: list[float],
    contrato_id: UUID | None = None,
    limit: int = 10,
    min_similarity: float = 0.5,
) -> list[dict[str, Any]]:
    """Find obligations semantically similar to a query embedding.

    Uses pgvector cosine distance: 1 - cosine_distance = cosine_similarity.

    Args:
        db: Async SQLAlchemy session.
        query_embedding: The query vector (must be EMBEDDING_DIM floats).
        contrato_id: Optional UUID to restrict search to one contract.
        limit: Maximum number of results.
        min_similarity: Minimum cosine similarity threshold (0.0–1.0).

    Returns:
        List of dicts with keys: id, contrato_id, descripcion, tipo, orden, similarity.
    """
    if len(query_embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"Query embedding must have {EMBEDDING_DIM} dimensions, "
            f"got {len(query_embedding)}."
        )

    query_vector_str = "[" + ",".join(str(f) for f in query_embedding) + "]"

    where_clause = "o.embedding IS NOT NULL"
    params: dict[str, Any] = {
        "query_vector": query_vector_str,
        "limit": limit,
        "min_similarity": min_similarity,
    }

    if contrato_id is not None:
        where_clause += " AND o.contrato_id = :contrato_id"
        params["contrato_id"] = str(contrato_id)

    sql = text(
        f"""
        SELECT
            o.id,
            o.contrato_id,
            o.descripcion,
            o.tipo,
            o.orden,
            1 - (o.embedding::vector({EMBEDDING_DIM}) <=> :query_vector::vector({EMBEDDING_DIM})) AS similarity
        FROM obligaciones o
        WHERE {where_clause}
          AND 1 - (o.embedding::vector({EMBEDDING_DIM}) <=> :query_vector::vector({EMBEDDING_DIM})) >= :min_similarity
        ORDER BY similarity DESC
        LIMIT :limit
        """
    )

    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        await logger.aerror("vector_search_failed", error=str(exc))
        # Fallback: return empty list rather than crashing the node
        return []


async def store_obligacion_embedding(
    db: AsyncSession,
    obligacion_id: UUID,
    embedding: list[float],
) -> None:
    """Persist an embedding vector to the obligaciones table.

    Args:
        db: Async SQLAlchemy session (must be flushed/committed by caller).
        obligacion_id: UUID of the obligation to update.
        embedding: Float list of length EMBEDDING_DIM.
    """
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"Embedding must have {EMBEDDING_DIM} dimensions, got {len(embedding)}."
        )

    encoded = encode_embedding(embedding)
    sql = text(
        "UPDATE obligaciones SET embedding = :embedding WHERE id = :id"
    )
    await db.execute(sql, {"embedding": encoded, "id": str(obligacion_id)})


async def get_embedding_from_llm(text_content: str, llm: Any) -> list[float] | None:
    """Get embedding for text using the configured LLM adapter.

    Falls back to None if the LLM does not support embeddings.

    Args:
        text_content: Text to embed.
        llm: LLM adapter instance from app.adapters.llm.

    Returns:
        List of floats or None if embedding failed.
    """
    try:
        embedding = await llm.embed(text_content, model="text-embedding-004")
        if embedding and len(embedding) == EMBEDDING_DIM:
            return embedding
        return None
    except (AttributeError, NotImplementedError):
        # LLM adapter does not support embed() — skip silently
        return None
    except Exception as exc:
        await logger.awarning("embedding_failed", error=str(exc))
        return None
