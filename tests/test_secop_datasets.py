"""SECOP document sync must fan out across ALL archive datasets, not just 2025."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.secop import SecopContrato
from app.services import secop_service
from app.services.secop_service import _ALL_DOCS_DATASETS, _DS_DOCS_2022


async def _cache_contrato(db: AsyncSession, *, numero: str = "CO-001") -> None:
    db.add(
        SecopContrato(
            id_contrato_secop=f"SECOP-{numero}",
            cedula_contratista="123456789",
            numero_contrato=numero,
            datos_raw={},
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_sincronizar_consulta_los_cuatro_datasets(db: AsyncSession) -> None:
    await _cache_contrato(db)
    calls: list[str] = []

    async def fake_query(dataset_id: str, where_clause: str, limit: int = 500) -> list:
        calls.append(dataset_id)
        return []

    with patch.object(secop_service, "_query_socrata", side_effect=fake_query):
        result = await secop_service.sincronizar_documentos_secop(db, "123456789", confirmar=False)

    # The bug was querying only the 2025 dataset; must now hit all four.
    assert set(calls) == set(_ALL_DOCS_DATASETS)
    assert result.documentos_encontrados == 0
    assert result.datasets_con_error == []


@pytest.mark.asyncio
async def test_sincronizar_surfacea_datasets_fallidos(db: AsyncSession) -> None:
    await _cache_contrato(db, numero="CO-002")

    async def fake_query(dataset_id: str, where_clause: str, limit: int = 500) -> list:
        if dataset_id == _DS_DOCS_2022:
            raise RuntimeError("429 throttled")
        return []

    with patch.object(secop_service, "_query_socrata", side_effect=fake_query):
        result = await secop_service.sincronizar_documentos_secop(db, "123456789", confirmar=False)

    # A throttled dataset is reported as partial rather than silently swallowed.
    assert _DS_DOCS_2022 in result.datasets_con_error
