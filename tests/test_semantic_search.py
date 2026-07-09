"""Semantic search service tests (SQLite / in-Python cosine fallback path).

The pgvector branch is only reachable on PostgreSQL and is verified against Neon
separately; here we exercise the ranking logic through the SQLite fallback with a
mocked query embedding so the scoring is deterministic.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from httpx import AsyncClient

from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.services import semantic_search_service

PATCH_TARGET = "app.services.embedding_service.generate_embedding_for_text"


async def _make_user(db: AsyncSession) -> Usuario:
    user = Usuario(
        email=f"{uuid.uuid4().hex}@test.com",
        nombre="Test User",
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID | None = None) -> Contrato:
    if usuario_id is None:
        usuario_id = (await _make_user(db)).id
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="001-2024",
        objeto="Servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_ob(
    db: AsyncSession, contrato_id: uuid.UUID, desc: str, embedding: list[float] | None
) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=desc,
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
        embedding=json.dumps(embedding) if embedding is not None else None,
    )
    db.add(ob)
    await db.flush()
    return ob


@pytest.mark.asyncio
async def test_ranks_obligations_by_cosine_similarity(db: AsyncSession) -> None:
    contrato = await _make_contrato(db)
    ob_a = await _make_ob(db, contrato.id, "A", [1.0, 0.0, 0.0])
    ob_b = await _make_ob(db, contrato.id, "B", [0.9, 0.1, 0.0])
    await _make_ob(db, contrato.id, "C", [0.0, 1.0, 0.0])
    await db.commit()

    with patch(PATCH_TARGET, new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        results = await semantic_search_service.buscar_obligaciones_similares(
            db, contrato.usuario_id, contrato.id, "consultoría", top_k=2
        )

    assert len(results) == 2
    assert results[0].obligacion_id == ob_a.id  # exact match ranks first
    assert results[1].obligacion_id == ob_b.id  # near match second
    assert results[0].score > results[1].score


@pytest.mark.asyncio
async def test_skips_obligations_without_embedding(db: AsyncSession) -> None:
    contrato = await _make_contrato(db)
    ob_with = await _make_ob(db, contrato.id, "has-emb", [1.0, 0.0, 0.0])
    await _make_ob(db, contrato.id, "no-emb", None)
    await db.commit()

    with patch(PATCH_TARGET, new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        results = await semantic_search_service.buscar_obligaciones_similares(
            db, contrato.usuario_id, contrato.id, "algo", top_k=5
        )

    assert [r.obligacion_id for r in results] == [ob_with.id]


@pytest.mark.asyncio
async def test_empty_when_no_embeddings(db: AsyncSession) -> None:
    contrato = await _make_contrato(db)
    await _make_ob(db, contrato.id, "no-emb", None)
    await db.commit()

    with patch(PATCH_TARGET, new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        results = await semantic_search_service.buscar_obligaciones_similares(
            db, contrato.usuario_id, contrato.id, "algo", top_k=5
        )

    assert results == []


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_ranks_and_requires_auth(
    client: AsyncClient, db: AsyncSession, test_user: dict
) -> None:
    contrato = await _make_contrato(db, usuario_id=test_user["user"].id)
    ob_a = await _make_ob(db, contrato.id, "A", [1.0, 0.0, 0.0])
    await _make_ob(db, contrato.id, "B", [0.0, 1.0, 0.0])
    await db.commit()

    url = f"/api/v1/contratos/{contrato.id}/obligaciones/similares?q=consultoria&top_k=1"

    # Unauthenticated → 401/403
    unauth = await client.get(url)
    assert unauth.status_code in (401, 403)

    with patch(PATCH_TARGET, new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        resp = await client.get(url, headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["obligacion_id"] == str(ob_a.id)


@pytest.mark.asyncio
async def test_endpoint_forbids_other_users_contract(
    client: AsyncClient, db: AsyncSession, test_user: dict
) -> None:
    other = await _make_user(db)
    contrato = await _make_contrato(db, usuario_id=other.id)
    await _make_ob(db, contrato.id, "A", [1.0, 0.0, 0.0])
    await db.commit()

    url = f"/api/v1/contratos/{contrato.id}/obligaciones/similares?q=x"
    with patch(PATCH_TARGET, new=AsyncMock(return_value=[1.0, 0.0, 0.0])):
        resp = await client.get(url, headers=test_user["headers"])
    assert resp.status_code == 403
