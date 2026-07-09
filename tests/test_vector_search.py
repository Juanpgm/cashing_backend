"""vector_search tool must work on SQLite (Python cosine) via the dialect branch,
instead of silently returning [] when the raw pgvector query raises."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import vector_search
from app.agent.tools.vector_search import EMBEDDING_DIM
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario


def _vec(*head: float) -> list[float]:
    """A 1536-dim vector with the given leading components, zeros after."""
    v = list(head) + [0.0] * (EMBEDDING_DIM - len(head))
    return v[:EMBEDDING_DIM]


async def _contrato(db: AsyncSession) -> Contrato:
    user = Usuario(
        email=f"{uuid.uuid4().hex}@t.com", nombre="T", password_hash="x",
        rol="contratista", activo=True, creditos_disponibles=0,
    )
    db.add(user)
    await db.flush()
    c = Contrato(
        usuario_id=user.id, numero_contrato="001", objeto="x",
        valor_total=1, valor_mensual=1, fecha_inicio=date(2024, 1, 1), fecha_fin=date(2024, 12, 31),
    )
    db.add(c)
    await db.flush()
    return c


async def _ob(db: AsyncSession, contrato_id: uuid.UUID, desc: str, emb: list[float]) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id, descripcion=desc, tipo=TipoObligacion.ESPECIFICA,
        orden=1, embedding=json.dumps(emb),
    )
    db.add(ob)
    await db.flush()
    return ob


@pytest.mark.asyncio
async def test_semantic_search_python_fallback_ranks(db: AsyncSession) -> None:
    c = await _contrato(db)
    ob_a = await _ob(db, c.id, "match", _vec(1.0))
    await _ob(db, c.id, "orthogonal", _vec(0.0, 1.0))
    await db.commit()

    results = await vector_search.semantic_search_obligaciones(
        db, _vec(1.0), contrato_id=c.id, min_similarity=0.5
    )

    assert len(results) == 1  # the orthogonal one is below threshold
    assert results[0]["id"] == ob_a.id
    assert results[0]["similarity"] > 0.99


@pytest.mark.asyncio
async def test_semantic_search_validates_dimension(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await vector_search.semantic_search_obligaciones(db, [1.0, 2.0, 3.0])
