"""SECOP document sync must fan out across ALL archive datasets, not just 2025."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import ExternalServiceError
from app.models.secop import SecopContrato
from app.services import secop_service
from app.services.secop_service import _ALL_DOCS_DATASETS, _DS_DOCS_2022, _DS_DOCS_2025
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


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


class TestNuevosDatasetsSchemaGuard:
    """Slice 2 task 2.2/2.4: schema-presence guard for the 3 new datasets
    (cb9c-h8sn/e2u2-swiw/gra4-pcp2) — a dataset whose returned rows lack the
    expected join key is marked unavailable (datasets_con_error), never
    silently dropped."""

    def test_dataset_has_join_key_true_when_key_present(self) -> None:
        from app.services.secop_service import _DS_ADICIONES, _dataset_has_join_key

        rows = [{"id_contrato": "CO1.PCCNTR.1", "tipo": "ADICION EN EL VALOR"}]
        assert _dataset_has_join_key(rows, _DS_ADICIONES) is True

    def test_dataset_has_join_key_false_when_key_missing(self) -> None:
        """Schema drift: rows came back but without the expected join key."""
        from app.services.secop_service import _DS_ADICIONES, _dataset_has_join_key

        rows = [{"algo_distinto": "x"}]
        assert _dataset_has_join_key(rows, _DS_ADICIONES) is False

    def test_dataset_has_join_key_true_when_no_rows(self) -> None:
        """Zero rows is a legitimate 'no data for this contract' result, not schema
        drift — only a populated-but-key-less row indicates a real schema problem."""
        from app.services.secop_service import _DS_ADICIONES, _dataset_has_join_key

        assert _dataset_has_join_key([], _DS_ADICIONES) is True

    def test_modif_procesos_accepts_either_real_field_name(self) -> None:
        """e2u2-swiw's live schema (probed 2026-07-11) exposes `portafolio`/`proceso`,
        NOT `proceso_de_compra`/`id_del_portafolio` as originally assumed in the
        spec/design tables — see tasks.md Slice 2 deviation note."""
        from app.services.secop_service import _DS_MODIF_PROCESOS, _dataset_has_join_key

        rows = [{"portafolio": "CO1.BDOS.1", "proceso": "CO1.REQ.1"}]
        assert _dataset_has_join_key(rows, _DS_MODIF_PROCESOS) is True

    @pytest.mark.asyncio
    async def test_query_dataset_guarded_marks_unavailable_on_schema_drift(self) -> None:
        from app.services.secop_service import _DS_UBICACIONES, _query_dataset_guarded

        failed: list[str] = []
        with patch.object(
            secop_service,
            "_query_socrata",
            new_callable=AsyncMock,
            return_value=[{"unexpected_field": "x"}],
        ):
            rows = await _query_dataset_guarded(_DS_UBICACIONES, "referencia_del_contrato = 'X'", failed)

        assert rows == []
        assert _DS_UBICACIONES in failed

    @pytest.mark.asyncio
    async def test_query_dataset_guarded_returns_rows_on_success(self) -> None:
        from app.services.secop_service import _DS_UBICACIONES, _query_dataset_guarded

        failed: list[str] = []
        good_row = {"referencia_del_contrato": "REF-1", "ubicacion": "COLOMBIA»Cali"}
        with patch.object(
            secop_service,
            "_query_socrata",
            new_callable=AsyncMock,
            return_value=[good_row],
        ):
            rows = await _query_dataset_guarded(_DS_UBICACIONES, "referencia_del_contrato = 'REF-1'", failed)

        assert rows == [good_row]
        assert failed == []

    @pytest.mark.asyncio
    async def test_query_dataset_guarded_appends_on_query_error(self) -> None:
        """A retry-exhausted `ExternalServiceError` also lands in `failed_out`,
        composing with the bounded-backoff contract from Slice 1."""
        from app.services.secop_service import _DS_ADICIONES, _query_dataset_guarded

        failed: list[str] = []
        with patch.object(
            secop_service,
            "_query_socrata",
            side_effect=ExternalServiceError("SECOP API", "HTTP 500"),
        ):
            rows = await _query_dataset_guarded(_DS_ADICIONES, "id_contrato = 'X'", failed)

        assert rows == []
        assert _DS_ADICIONES in failed

    @pytest.mark.asyncio
    async def test_no_matching_key_yields_zero_rows_no_crash(self) -> None:
        """A contract lacking any of a dataset's join keys gets zero rows, no error
        (spec 'Dataset-specific join semantics preserved' — no matching key scenario)."""
        from app.services.secop_service import _fetch_nuevos_datasets_contrato

        with patch.object(secop_service, "_query_socrata", new_callable=AsyncMock, return_value=[]):
            failed: list[str] = []
            result = await _fetch_nuevos_datasets_contrato(
                id_contrato_secop=None,
                proceso_de_compra=None,
                id_del_portafolio=None,
                referencia_del_contrato=None,
                failed_out=failed,
            )

        assert result == {"_adiciones": [], "_modif_procesos": [], "_ubicaciones": []}
        assert failed == []


class TestObtenerAdicionesContrato:
    """Slice 2 task 2.6/2.7 — design D2: the accessor consumed by
    billing-resilience-templates slice #4. Reads cached datos_raw, never queries
    Socrata directly."""

    @pytest.mark.asyncio
    async def test_returns_adiciones_from_cached_datos_raw(self, db: AsyncSession) -> None:
        from app.services.secop_service import obtener_adiciones_contrato

        adiciones_raw = [
            {
                "identificador": "CO1.CTRMOD.1",
                "id_contrato": "CO1.PCCNTR.999",
                "tipo": "ADICION EN EL VALOR",
                "descripcion": "SE ADICIONA EN LA SUMA DE CUATRO MILLONES QUINIENTOS MIL PESOS ($4.500.000) M/CTE",
                "fecharegistro": "2020-08-01T00:00:00.000",
            }
        ]
        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.999",
                cedula_contratista="123456789",
                numero_contrato="CON-ADD-1",
                datos_raw={"_adiciones": adiciones_raw},
            )
        )
        await db.commit()

        with patch.object(secop_service, "_query_socrata") as mock_query:
            result = await obtener_adiciones_contrato(db, "CO1.PCCNTR.999")

        mock_query.assert_not_called()
        assert len(result) == 1
        assert result[0]["id_contrato_secop"] == "CO1.PCCNTR.999"
        assert result[0]["valor_adicion"] == 4_500_000
        # Money crosses the accessor boundary as Decimal, never float
        # (consumed by billing contract-addition-events for invoice math).
        assert isinstance(result[0]["valor_adicion"], Decimal)
        assert result[0]["fecha_efectiva"] is not None

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_adiciones_cached(self, db: AsyncSession) -> None:
        from app.services.secop_service import obtener_adiciones_contrato

        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.NOADD",
                cedula_contratista="123456789",
                numero_contrato="CON-NOADD",
                datos_raw={},
            )
        )
        await db.commit()

        result = await obtener_adiciones_contrato(db, "CO1.PCCNTR.NOADD")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_contrato_not_found(self, db: AsyncSession) -> None:
        from app.services.secop_service import obtener_adiciones_contrato

        result = await obtener_adiciones_contrato(db, "CO1.PCCNTR.MISSING")
        assert result == []

    @pytest.mark.asyncio
    async def test_valor_adicion_none_when_amount_not_parseable(self, db: AsyncSession) -> None:
        """Free-text descriptions don't always contain a parseable peso amount
        (live probe evidence 2026-07-11) — best-effort extraction returns None
        rather than raising or guessing."""
        from app.services.secop_service import obtener_adiciones_contrato

        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.888",
                cedula_contratista="123456789",
                numero_contrato="CON-ADD-2",
                datos_raw={
                    "_adiciones": [
                        {
                            "identificador": "CO1.CTRMOD.2",
                            "id_contrato": "CO1.PCCNTR.888",
                            "tipo": "ADICION EN EL VALOR",
                            "descripcion": "ADICIoN Y PRoRROGA NO. 2 AL CONTRATO SIN MONTO EXPLiCITO",
                            "fecharegistro": "2019-01-02T00:00:00.000",
                        }
                    ]
                },
            )
        )
        await db.commit()

        result = await obtener_adiciones_contrato(db, "CO1.PCCNTR.888")
        assert result[0]["valor_adicion"] is None


class TestNuevosDatasetsNonDestructiveUpsert:
    """Slice 2 task 2.8 — D1: cached rows for the 3 new datasets survive a
    failed/empty refresh (non-destructive upsert)."""

    @pytest.mark.asyncio
    async def test_sincronizar_preserves_cached_adiciones_on_empty_refetch(self, db: AsyncSession) -> None:
        from app.services.secop_service import sincronizar_documentos_secop

        existing_adiciones = [{"id_contrato": "SECOP-CO-010", "tipo": "ADICION EN EL VALOR"}]
        db.add(
            SecopContrato(
                id_contrato_secop="SECOP-CO-010",
                cedula_contratista="123456789",
                numero_contrato="CO-010",
                referencia_del_contrato="CO-010",
                datos_raw={"_adiciones": existing_adiciones},
            )
        )
        await db.commit()

        with patch.object(secop_service, "_query_socrata", new_callable=AsyncMock, return_value=[]):
            await sincronizar_documentos_secop(db, "123456789", confirmar=True)

        result = await db.execute(select(SecopContrato).where(SecopContrato.numero_contrato == "CO-010"))
        refreshed = result.scalar_one()
        assert refreshed.datos_raw.get("_adiciones") == existing_adiciones


class TestObtenerDocumentosConCobertura:
    """Slice 2 task 2.10b: structured `cobertura` block on the documentos
    response, so a legitimately small document count doesn't look like a bug.
    Evidence anchor (2026-07-11): contract 4161.010.26.1.2189.2024 → 2 docs is
    CORRECT behavior for a 2024 contract, must be explainable."""

    @pytest.mark.asyncio
    async def test_2024_contract_flags_gap_with_urlproceso(self, db: AsyncSession) -> None:
        from datetime import date

        from app.services.secop_service import obtener_documentos_con_cobertura

        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.2024X",
                cedula_contratista="123456789",
                numero_contrato="4161.010.26.1.2189.2024",
                referencia_del_contrato="4161.010.26.1.2189.2024",
                fecha_inicio=date(2024, 11, 1),
                datos_raw={"urlproceso": {"url": "https://community.secop.gov.co/Public/Tendering/Index?x=1"}},
            )
        )
        await db.commit()

        with patch.object(secop_service, "_query_socrata", new_callable=AsyncMock, return_value=[]):
            result = await obtener_documentos_con_cobertura(db, "4161.010.26.1.2189.2024", refresh=False)

        assert result.cobertura.gap is True
        assert "2024" in result.cobertura.razon
        assert result.cobertura.url_proceso == "https://community.secop.gov.co/Public/Tendering/Index?x=1"

    @pytest.mark.asyncio
    async def test_non_2024_contract_no_gap_when_datasets_ok(self, db: AsyncSession) -> None:
        from datetime import date

        from app.services.secop_service import obtener_documentos_con_cobertura

        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.2025X",
                cedula_contratista="123456789",
                numero_contrato="CON-2025",
                referencia_del_contrato="CON-2025",
                fecha_inicio=date(2025, 3, 1),
                datos_raw={},
            )
        )
        await db.commit()

        with patch.object(secop_service, "_query_socrata", new_callable=AsyncMock, return_value=[]):
            result = await obtener_documentos_con_cobertura(db, "CON-2025", refresh=False)

        assert result.cobertura.gap is False

    @pytest.mark.asyncio
    async def test_dataset_error_flags_gap_even_outside_2024(self, db: AsyncSession) -> None:
        from datetime import date

        from app.services.secop_service import _DS_DOCS_2022, obtener_documentos_con_cobertura

        db.add(
            SecopContrato(
                id_contrato_secop="CO1.PCCNTR.2025ERR",
                cedula_contratista="123456789",
                numero_contrato="CON-2025-ERR",
                referencia_del_contrato="CON-2025-ERR",
                fecha_inicio=date(2025, 3, 1),
                datos_raw={},
            )
        )
        await db.commit()

        async def fake_query(dataset_id: str, where_clause: str, limit: int = 500) -> list:
            if dataset_id == _DS_DOCS_2022:
                raise ExternalServiceError("SECOP API", "HTTP 500")
            return []

        with patch.object(secop_service, "_query_socrata", side_effect=fake_query):
            result = await obtener_documentos_con_cobertura(db, "CON-2025-ERR", refresh=False)

        assert result.cobertura.gap is True
        assert _DS_DOCS_2022 in result.cobertura.razon


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_probe_nuevos_datasets_schema() -> None:
    """Manual/live probe (skipped in CI by default — run with `-m live`),
    Slice 2 task 2.1.

    Live evidence recorded 2026-07-11 (direct queries against
    www.datos.gov.co/resource/*.json and the Socrata views metadata API):

    - cb9c-h8sn (Adiciones): `id_contrato` IS present (matches design). BUT the
      dataset (real title "SECOP II - Modificaciones a Contratos") has NO
      structured value field — only identificador/id_contrato/tipo/descripcion/
      fecharegistro. `tipo` has a value "ADICION EN EL VALOR" (not just
      "ADICION") that identifies actual value-additions; `valor_adicion` must
      be best-effort-parsed from the free-text `descripcion` (see
      `_extraer_valor_adicion_texto`) and may be `None`.
    - e2u2-swiw (Modificaciones a Procesos): DEVIATION from spec/design —
      real field names are `portafolio` and `proceso`, NOT
      `proceso_de_compra`/`id_del_portafolio` as assumed. `portafolio`
      (format CO1.BDOS.xxx) is the field that matches our own
      `proceso_de_compra`/`id_proceso_secop` values. Also: per its own Socrata
      description, this dataset only holds processes modified in the
      "últimos 8 días" (rolling 8-day window) — most historical contracts
      legitimately return zero rows here, which is expected, not a gap.
    - gra4-pcp2 (Ubicaciones): `referencia_del_contrato` IS present, exactly
      as assumed by spec/design (bonus: `id_contrato`/`proceso_de_compra` also
      present, giving extra fallback keys if ever needed).
    """
    from app.services.secop_service import _DS_ADICIONES, _DS_MODIF_PROCESOS, _DS_UBICACIONES, _query_socrata

    adiciones_rows = await _query_socrata(_DS_ADICIONES, where_clause="1=1", limit=1)
    assert adiciones_rows and "id_contrato" in adiciones_rows[0]
    assert "valor_adicion" not in adiciones_rows[0], (
        "if this ever fails, cb9c-h8sn gained a structured value field — "
        "simplify _extraer_valor_adicion_texto accordingly"
    )

    modif_rows = await _query_socrata(_DS_MODIF_PROCESOS, where_clause="1=1", limit=1)
    if modif_rows:  # rolling 8-day window can legitimately be empty
        assert "portafolio" in modif_rows[0]
        assert "proceso_de_compra" not in modif_rows[0]

    ubicaciones_rows = await _query_socrata(_DS_UBICACIONES, where_clause="1=1", limit=1)
    assert ubicaciones_rows and "referencia_del_contrato" in ubicaciones_rows[0]


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
