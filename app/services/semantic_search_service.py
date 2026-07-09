"""Semantic search over obligation embeddings — consumes the pgvector infrastructure.

Dual backend, chosen at runtime by the connection dialect:

- **PostgreSQL (prod):** uses the pgvector ``<=>`` cosine-distance operator, backed
  by the ivfflat index created in migration 008, for efficient nearest-neighbour
  search directly in the database.
- **SQLite (local/tests):** falls back to in-Python cosine similarity over the
  JSON-encoded embeddings, so the feature is exercisable locally without Postgres.

Both paths return results ordered by descending similarity (higher = closer). The
pgvector branch uses a parameterised ``text()`` query because the ``<=>`` operator
and the ``::vector`` cast are not expressible through the plain ORM (the column is
stored as JSON text, not a native ``Vector`` type); the vector literal is built
from our own numeric embedding, never from user input.
"""

from __future__ import annotations

import json
import math
import uuid

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion
from app.schemas.semantic_search import ObligacionSimilar
from app.services import embedding_service

logger = structlog.get_logger("service.semantic_search")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors; 0.0 on mismatch or zero-norm."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def buscar_obligaciones_similares(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    query_text: str,
    top_k: int = 5,
) -> list[ObligacionSimilar]:
    """Return the ``top_k`` obligations of a contract most similar to ``query_text``.

    Verifies the contract belongs to ``usuario_id``, embeds the query, then ranks
    the contract's obligations that already have an embedding by cosine similarity.
    Obligations without an embedding are ignored.
    """
    contrato = await db.get(Contrato, contrato_id)
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))
    if contrato.usuario_id != usuario_id:
        raise ForbiddenError()

    query_emb = await embedding_service.generate_embedding_for_text(query_text)

    if db.bind.dialect.name == "postgresql":
        return await _search_pgvector(db, contrato_id, query_emb, top_k)
    return await _search_python(db, contrato_id, query_emb, top_k)


async def _search_python(
    db: AsyncSession, contrato_id: uuid.UUID, query_emb: list[float], top_k: int
) -> list[ObligacionSimilar]:
    result = await db.execute(
        select(Obligacion).where(
            Obligacion.contrato_id == contrato_id,
            Obligacion.embedding.is_not(None),
        )
    )
    scored: list[tuple[Obligacion, float]] = []
    for ob in result.scalars().all():
        try:
            emb = json.loads(ob.embedding)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        scored.append((ob, _cosine_similarity(query_emb, emb)))

    scored.sort(key=lambda t: t[1], reverse=True)
    return [
        ObligacionSimilar(obligacion_id=ob.id, descripcion=ob.descripcion, score=round(score, 6))
        for ob, score in scored[:top_k]
    ]


async def _search_pgvector(
    db: AsyncSession, contrato_id: uuid.UUID, query_emb: list[float], top_k: int
) -> list[ObligacionSimilar]:
    vector_literal = "[" + ",".join(repr(float(x)) for x in query_emb) + "]"
    sql = text(
        """
        SELECT id, descripcion,
               1 - (embedding::vector <=> (:q)::vector) AS score
        FROM obligaciones
        WHERE contrato_id = :cid AND embedding IS NOT NULL
        ORDER BY embedding::vector <=> (:q)::vector ASC
        LIMIT :k
        """
    )
    result = await db.execute(
        sql, {"q": vector_literal, "cid": str(contrato_id), "k": top_k}
    )
    return [
        ObligacionSimilar(
            obligacion_id=row.id, descripcion=row.descripcion, score=float(row.score)
        )
        for row in result
    ]
