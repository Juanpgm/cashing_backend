"""Tests for secop_service.buscar_documentos_contrato and importar_contratos_secop (preview mode)."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_secop_contrato(
    numero: str = "CON-001",
    proceso: str = "CO1.BDOS.123",
    referencia: str | None = None,
) -> MagicMock:
    c = MagicMock()
    c.id = uuid.uuid4()
    c.numero_contrato = numero
    c.referencia_del_contrato = referencia or numero
    c.proceso_de_compra = proceso
    c.datos_raw = {}
    c.updated_at = None
    return c


def _make_secop_documento(num_contrato: str = "CON-001") -> MagicMock:
    d = MagicMock()
    d.id = uuid.uuid4()
    d.numero_contrato = num_contrato
    d.proceso = "CO1.BDOS.123"
    d.updated_at = None
    return d


def _contrato_execute_result(contratos: list) -> MagicMock:
    """Mock for db.execute() returning scalars().all() = contratos."""
    r = MagicMock()
    r.scalars.return_value.all.return_value = contratos
    return r


def _proceso_execute_result(proceso=None) -> MagicMock:
    """Mock for db.execute() returning scalar_one_or_none() = proceso."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = proceso
    return r


def _cached_execute_result(docs: list) -> MagicMock:
    """Mock for db.execute() returning scalars().all() = docs."""
    r = MagicMock()
    r.scalars.return_value.all.return_value = docs
    return r


class TestBuscarDocumentosContrato:
    @pytest.mark.asyncio
    async def test_returns_cached_when_fresh(self) -> None:
        """If cached docs exist and are fresh, skip SECOP query."""
        from datetime import datetime, timezone
        from app.services.secop_service import buscar_documentos_contrato

        secop_contrato = _make_secop_contrato()
        fake_doc = _make_secop_documento()
        fake_doc.updated_at = datetime.now(timezone.utc)  # fresh

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([secop_contrato]),   # contrato lookup
            _proceso_execute_result(None),                # proceso FK lookup
            _cached_execute_result([fake_doc]),           # cache check
        ]

        with patch(
            "app.services.secop_service.SecopDocumentoResponse.model_validate",
            return_value=MagicMock(),
        ):
            with patch("app.services.secop_service._is_fresh", return_value=True):
                result = await buscar_documentos_contrato(mock_db, "CON-001", refresh=False)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_queries_secop_when_no_cache(self) -> None:
        """If no cached docs, query SECOP and upsert."""
        from app.services.secop_service import buscar_documentos_contrato

        secop_contrato = _make_secop_contrato()
        fake_doc = _make_secop_documento()

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([secop_contrato]),   # contrato lookup
            _proceso_execute_result(None),                # proceso FK lookup
            _cached_execute_result([]),                   # cache check: empty
            _cached_execute_result([fake_doc]),           # post-refresh re-read
        ]

        secop_row = {"id_documento": "DOC001", "titulo": "Minuta", "n_mero_de_contrato": "CON-001"}

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ):
            with patch(
                "app.services.secop_service._upsert_documento",
                new_callable=AsyncMock,
            ):
                with patch(
                    "app.services.secop_service.SecopDocumentoResponse.model_validate",
                    return_value=MagicMock(),
                ):
                    result = await buscar_documentos_contrato(mock_db, "CON-001", refresh=False)

        assert len(result) == 1
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(self) -> None:
        """If no contrato in DB and no docs from SECOP, returns []."""
        from app.services.secop_service import buscar_documentos_contrato

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([]),                 # contrato lookup: none
            _cached_execute_result([]),                   # cache check: empty
            _cached_execute_result([]),                   # post-refresh re-read
        ]

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await buscar_documentos_contrato(mock_db, "NOEXI-999", refresh=False)

        assert result == []

    @pytest.mark.asyncio
    async def test_multiple_contratos_rows_all_procesos_queried(self) -> None:
        """When a contract has multiple SECOP rows (addenda), ALL proceso_de_compra are queried."""
        from app.services.secop_service import buscar_documentos_contrato

        contrato_original = _make_secop_contrato("CON-001", proceso="CO1.BDOS.111")
        contrato_adicion = _make_secop_contrato("CON-001", proceso="CO1.BDOS.222")

        doc_original = _make_secop_documento("CON-001")
        doc_original.proceso = "CO1.BDOS.111"
        doc_adicion = _make_secop_documento("CON-001")
        doc_adicion.proceso = "CO1.BDOS.222"

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([contrato_original, contrato_adicion]),  # two rows
            _proceso_execute_result(None),                                    # proceso FK
            _cached_execute_result([]),                                       # cache: empty
            _cached_execute_result([doc_original, doc_adicion]),              # post-refresh
        ]

        # _query_docs_datasets called 3 times: CON-001 ref + proceso.111 + proceso.222
        doc_row_1 = {"id_documento": "DOC1", "n_mero_de_contrato": "", "proceso": "CO1.BDOS.111", "_secop_dataset": "dmgg-8hin"}
        doc_row_2 = {"id_documento": "DOC2", "n_mero_de_contrato": "", "proceso": "CO1.BDOS.222", "_secop_dataset": "dmgg-8hin"}

        # Single combined OR query: n_mero_de_contrato = 'CON-001' OR proceso = 'CO1.BDOS.111' OR proceso = 'CO1.BDOS.222'
        combined_docs = [doc_row_1, doc_row_2]

        with patch(
            "app.services.secop_service._query_docs_datasets",
            new_callable=AsyncMock,
            return_value=combined_docs,
        ) as mock_query_docs:
            with patch(
                "app.services.secop_service._query_modificaciones_docs",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch("app.services.secop_service._upsert_documento", new_callable=AsyncMock):
                    with patch(
                        "app.services.secop_service.SecopDocumentoResponse.model_validate",
                        side_effect=lambda d: MagicMock(),
                    ):
                        result = await buscar_documentos_contrato(mock_db, "CON-001", refresh=False)

        # All refs and procesos are combined into one call
        assert mock_query_docs.call_count == 1
        # Combined WHERE should include both proceso IDs
        call_where = mock_query_docs.call_args[0][0]
        assert "CO1.BDOS.111" in call_where
        assert "CO1.BDOS.222" in call_where
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_refresh_true_always_queries_secop(self) -> None:
        """refresh=True should query SECOP even when cache is fresh."""
        from datetime import datetime, timezone
        from app.services.secop_service import buscar_documentos_contrato

        secop_contrato = _make_secop_contrato()
        fresh_doc = _make_secop_documento()
        fresh_doc.updated_at = datetime.now(timezone.utc)

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([secop_contrato]),   # contrato lookup
            _proceso_execute_result(None),                # proceso FK lookup
            _cached_execute_result([fresh_doc]),          # cache: has fresh docs
            _cached_execute_result([fresh_doc]),          # post-refresh re-read
        ]

        new_secop_row = {"id_documento": "DOC999", "n_mero_de_contrato": "CON-001"}

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[new_secop_row],
        ) as mock_query:
            with patch("app.services.secop_service._upsert_documento", new_callable=AsyncMock):
                with patch(
                    "app.services.secop_service.SecopDocumentoResponse.model_validate",
                    return_value=MagicMock(),
                ):
                    with patch("app.services.secop_service._is_fresh", return_value=True):
                        result = await buscar_documentos_contrato(mock_db, "CON-001", refresh=True)

        # Even though cache was fresh, SECOP should have been queried
        assert mock_query.call_count >= 1

    @pytest.mark.asyncio
    async def test_referencia_del_contrato_also_searched(self) -> None:
        """A contract whose numero_contrato differs from referencia should still find docs."""
        from app.services.secop_service import buscar_documentos_contrato

        # Contract stored as '123' in contratos table but SECOP uses 'CO1.PCCNTR.456'
        contrato = _make_secop_contrato("123", proceso="CO1.BDOS.789", referencia="CO1.PCCNTR.456")

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([contrato]),         # lookup by both num + referencia
            _proceso_execute_result(None),                # proceso FK
            _cached_execute_result([]),                   # cache: empty
            _cached_execute_result([_make_secop_documento("CO1.PCCNTR.456")]),  # post-refresh
        ]

        # Single combined OR query covering all refs and proceso
        combined_docs = [
            {"id_documento": "DOC_REF", "n_mero_de_contrato": "CO1.PCCNTR.456", "proceso": None, "_secop_dataset": "dmgg-8hin"},
            {"id_documento": "DOC_PROC", "n_mero_de_contrato": None, "proceso": "CO1.BDOS.789", "_secop_dataset": "dmgg-8hin"},
        ]

        with patch(
            "app.services.secop_service._query_docs_datasets",
            new_callable=AsyncMock,
            return_value=combined_docs,
        ) as mock_query_docs:
            with patch(
                "app.services.secop_service._query_modificaciones_docs",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch("app.services.secop_service._upsert_documento", new_callable=AsyncMock):
                    with patch(
                        "app.services.secop_service.SecopDocumentoResponse.model_validate",
                        side_effect=lambda d: MagicMock(),
                    ):
                        result = await buscar_documentos_contrato(mock_db, "123", refresh=False)

        # All keys combined into exactly one call with OR-joined WHERE clause
        assert mock_query_docs.call_count == 1
        call_where = mock_query_docs.call_args[0][0]
        assert "CO1.PCCNTR.456" in call_where
        assert "CO1.BDOS.789" in call_where

    @pytest.mark.asyncio
    async def test_buscar_documentos_consulta_datasets_historicos(self) -> None:
        """Fan-out queries all 3 archive datasets and deduplicates by id_documento."""
        from app.services.secop_service import buscar_documentos_contrato

        secop_contrato = _make_secop_contrato("CON-001", proceso="CO1.BDOS.123")

        # Simulate each archive dataset returning a distinct doc
        doc_2022 = {"id_documento": "DOC2022", "n_mero_de_contrato": "CON-001", "_secop_dataset": "kgcd-kt7i"}
        doc_2023 = {"id_documento": "DOC2023", "n_mero_de_contrato": "CON-001", "_secop_dataset": "3skv-9na7"}
        doc_2025 = {"id_documento": "DOC2025", "n_mero_de_contrato": "CON-001", "_secop_dataset": "dmgg-8hin"}

        # Single combined OR query returns all docs from all datasets
        all_docs = [doc_2022, doc_2023, doc_2025]

        fake_docs = [_make_secop_documento("CON-001") for _ in range(3)]
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([secop_contrato]),
            _proceso_execute_result(None),
            _cached_execute_result([]),       # empty cache → triggers refresh
            _cached_execute_result(fake_docs),  # post-refresh re-read: 3 docs
        ]

        with patch(
            "app.services.secop_service._query_docs_datasets",
            new_callable=AsyncMock,
            return_value=all_docs,
        ) as mock_fanout:
            with patch(
                "app.services.secop_service._query_modificaciones_docs",
                new_callable=AsyncMock,
                return_value=[],
            ):
                with patch(
                    "app.services.secop_service._upsert_documento",
                    new_callable=AsyncMock,
                ) as mock_upsert:
                    with patch(
                        "app.services.secop_service.SecopDocumentoResponse.model_validate",
                        side_effect=lambda d: MagicMock(),
                    ):
                        result = await buscar_documentos_contrato(mock_db, "CON-001", refresh=False)

        # Combined query: exactly 1 call with all refs+procesos merged
        assert mock_fanout.call_count == 1
        # 3 unique id_documento values → 3 upserts (dedup by seen set)
        assert mock_upsert.call_count == 3
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_buscar_documentos_consulta_modificaciones(self) -> None:
        """u8cx-r425 is queried and synthetic modification PDFs are upserted."""
        from app.services.secop_service import buscar_documentos_contrato

        secop_contrato = _make_secop_contrato("CON-MOD", proceso="CO1.BDOS.999")

        mod_row = {
            "id_documento": "MOD-ABC123",
            "n_mero_de_contrato": "CO1.PCCNTR.99",
            "proceso": None,
            "nombre_archivo": "Modificación: Adición de días",
            "extensi_n": "pdf",
            "descripci_n": "Otrosí No. 1",
            "fecha_carga": None,
            "entidad": None,
            "nit_entidad": None,
            "url_descarga_documento": "https://secop.gov.co/mod.pdf",
            "_secop_dataset": "u8cx-r425",
        }

        fake_mod_doc = _make_secop_documento("CO1.PCCNTR.99")
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _contrato_execute_result([secop_contrato]),
            _proceso_execute_result(None),
            _cached_execute_result([]),             # cache: empty
            _cached_execute_result([fake_mod_doc]), # post-refresh: 1 doc
        ]

        with patch(
            "app.services.secop_service._query_docs_datasets",
            new_callable=AsyncMock,
            return_value=[],  # archive datasets return nothing
        ):
            with patch(
                "app.services.secop_service._query_modificaciones_docs",
                new_callable=AsyncMock,
                return_value=[mod_row],
            ) as mock_mod:
                with patch(
                    "app.services.secop_service._upsert_documento",
                    new_callable=AsyncMock,
                ) as mock_upsert:
                    with patch(
                        "app.services.secop_service.SecopDocumentoResponse.model_validate",
                        side_effect=lambda d: MagicMock(),
                    ):
                        result = await buscar_documentos_contrato(mock_db, "CON-MOD", refresh=False)

        # Modificaciones query was invoked exactly once
        mock_mod.assert_called_once()
        # The modification doc was upserted
        assert mock_upsert.call_count == 1
        assert len(result) == 1


class TestImportarContratosSecopPreview:
    @pytest.mark.asyncio
    async def test_preview_returns_contracts_without_persisting(self) -> None:
        """confirmar=False should return contracts without DB writes."""
        from app.services.secop_service import importar_contratos_secop

        secop_row = {
            "numero_contrato": "CON-2024-001",
            "objeto_del_contrato": "Prestacion de servicios de desarrollo de software",
            "valor_del_contrato": "12000000",
            "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
            "fecha_de_fin_del_contrato": "2024-06-30T00:00:00.000",
            "nombre_entidad": "Alcaldia de Bogota",
            "documento_proveedor": "12345678",
        }

        existing_result = MagicMock()
        existing_result.all.return_value = []  # No existing contracts

        mock_db = AsyncMock()
        mock_db.execute.return_value = existing_result

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ):
            with patch("app.services.secop_service._upsert_contrato", new_callable=AsyncMock):
                result = await importar_contratos_secop(
                    mock_db,
                    usuario_id=uuid.uuid4(),
                    documento_proveedor="12345678",
                    confirmar=False,
                )

        assert result.encontrados_en_secop == 1
        assert len(result.contratos) == 1
        assert result.contratos[0].numero_contrato == "CON-2024-001"
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_duplicate_contracts(self) -> None:
        """Contracts already in DB are upserted (actualizados), not counted as new importados."""
        from app.services.secop_service import importar_contratos_secop

        secop_row = {
            "numero_contrato": "CON-EXISTING",
            "objeto_del_contrato": "Prestacion de servicios profesionales de consultoria",
            "valor_del_contrato": "6000000",
            "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
            "fecha_de_fin_del_contrato": "2024-06-30T00:00:00.000",
            "documento_proveedor": "12345678",
        }

        # Simulate existing contract in the initial query
        existing_result = MagicMock()
        existing_result.all.return_value = [("CON-EXISTING",)]

        mock_db = AsyncMock()
        mock_db.execute.return_value = existing_result

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ):
            with patch("app.services.secop_service._upsert_contrato", new_callable=AsyncMock):
                result = await importar_contratos_secop(
                    mock_db,
                    usuario_id=uuid.uuid4(),
                    documento_proveedor="12345678",
                    confirmar=False,
                )

        # When confirmar=False, the result should show 1 found in SECOP
        assert result.encontrados_en_secop == 1
