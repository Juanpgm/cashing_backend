"""SECOP document sync must fan out across ALL archive datasets, not just 2025."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError
from app.models.secop import SecopContrato
from app.services import secop_service
from app.services.secop_service import _ALL_DOCS_DATASETS, _DS_DOCS_2022, _DS_DOCS_2025


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


@pytest.mark.asyncio
async def test_sincronizar_datasets_con_error_after_retries_exhausted(db: AsyncSession) -> None:
    """When `_query_socrata` itself exhausts retries for one dataset (raising
    `ExternalServiceError`, per the bounded backoff added in task 1.7), the fan-out
    still returns partial results and lists the exhausted dataset in
    `datasets_con_error` — the retry logic must compose with the existing
    partial-result contract instead of bypassing or breaking it."""
    await _cache_contrato(db, numero="CO-003")

    async def fake_query(dataset_id: str, where_clause: str, limit: int = 500) -> list:
        if dataset_id == _DS_DOCS_2022:
            raise ExternalServiceError("SECOP API", "HTTP 500")
        return []

    with patch.object(secop_service, "_query_socrata", side_effect=fake_query):
        result = await secop_service.sincronizar_documentos_secop(db, "123456789", confirmar=False)

    assert _DS_DOCS_2022 in result.datasets_con_error
    assert result.documentos_encontrados == 0


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_probe_dmgg_8hin_2024_coverage() -> None:
    """Manual/live probe (skipped in CI by default — run with `-m live`).

    Determines whether `_DS_DOCS_2025` (`dmgg-8hin`) actually covers 2024
    documents, per design D4 / spec "2024 archive gap resolved by live-probe".

    Probe evidence recorded 2026-07-11 (see tasks.md Slice 1, task 1.2 for the
    full record): `dmgg-8hin` DOES return rows with `fecha_carga` in 2024
    (count=24565), but ALL of them share the exact same `fecha_carga` value
    (`2024-12-31`) — a single bulk-load batch, not full-year 2024 coverage.
    `_DS_DOCS_2023` (`3skv-9na7`) returned 0 rows for the same 2024 range.
    Conclusion: the `secop_docs_gap_2024` warning is preserved (the gap is
    real for the rest of 2024), but the misleading "no public dataset exists"
    comment on `_DS_DOCS_2025` is corrected.
    """
    from app.services.secop_service import _query_socrata

    rows = await _query_socrata(
        _DS_DOCS_2025,
        where_clause="fecha_carga between '2024-01-01T00:00:00' and '2024-12-31T23:59:59'",
        limit=5,
    )
    assert rows, "expected dmgg-8hin to contain at least the 2024-12-31 bulk-load batch"
