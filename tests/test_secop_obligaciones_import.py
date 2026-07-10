"""Tests for bug #6: SECOP-imported contracts silently ending up with zero obligaciones.

Covers:
  - `_mapear_a_contrato_create` attempting the deterministic verbatim extractor
    on `objeto` before falling back to an empty list.
  - `importar_contratos_secop` attempting best-effort LLM extraction (via the
    shared `document_service.extraer_obligaciones_texto`) when verbatim finds
    nothing, without failing the import if the LLM is unavailable.
  - The `requiere_obligaciones` signal on the per-contract import result.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.schemas.agent import LLMResponse
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_GET_LLM = "app.adapters.llm.get_llm"


def _make_row(**kwargs: Any) -> dict:
    defaults = {
        "numero_contrato": "CO1.PCCNTR.OBLIG001",
        "objeto_del_contrato": "Prestación de servicios profesionales de apoyo a la gestión administrativa.",
        "valor_del_contrato": "12000000",
        "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
        "fecha_de_fin_del_contrato": "2024-12-31T00:00:00.000",
        "nombre_entidad": "MinTIC",
        "documento_proveedor": "1016019452",
    }
    defaults.update(kwargs)
    return defaults


class TestMapearAContratoCreateObligaciones:
    def test_short_objeto_yields_no_obligaciones(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        result = _mapear_a_contrato_create(_make_row())
        assert result is not None
        assert result.obligaciones == []

    def test_objeto_with_enumerated_obligaciones_extracts_verbatim(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        objeto = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        result = _mapear_a_contrato_create(_make_row(objeto_del_contrato=objeto))
        assert result is not None
        assert len(result.obligaciones) >= 2
        assert all(ob.tipo == "especifica" for ob in result.obligaciones)


class TestImportarContratosSecopObligaciones:
    @pytest.mark.asyncio
    async def test_new_contract_falls_back_to_llm_when_verbatim_empty(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row()
        llm_response = LLMResponse(
            content="OBLIGACION|especifica|Apoyar la gestión administrativa de la dependencia\n",
            model="test",
            total_tokens=30,
        )

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_response)
            mock_get_llm.return_value = mock_llm

            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert result.importados == 1
        contrato = result.contratos[0]
        assert len(contrato.obligaciones) == 1
        assert contrato.requiere_obligaciones is False
        mock_llm.complete.assert_awaited()

    @pytest.mark.asyncio
    async def test_new_contract_marks_requiere_obligaciones_when_llm_finds_nothing(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIG002")
        llm_response = LLMResponse(content="", model="test", total_tokens=5)

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_response)
            mock_get_llm.return_value = mock_llm

            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert result.importados == 1
        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.requiere_obligaciones is True

    @pytest.mark.asyncio
    async def test_import_does_not_fail_when_llm_extraction_raises(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIG003")

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM, side_effect=RuntimeError("LLM unavailable")),
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert result.importados == 1
        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.requiere_obligaciones is True

    @pytest.mark.asyncio
    async def test_new_contract_skips_llm_when_verbatim_succeeds(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        objeto = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIG004", objeto_del_contrato=objeto)

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert result.importados == 1
        contrato = result.contratos[0]
        assert len(contrato.obligaciones) >= 2
        assert contrato.requiere_obligaciones is False
        mock_get_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_import_completes_when_llm_extraction_times_out(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test: the LLM obligaciones fallback must be bounded so a
        slow/hanging LLM call cannot hang or fail a bulk SECOP import."""
        import app.services.secop_service as secop_service_module
        from app.services.secop_service import importar_contratos_secop

        monkeypatch.setattr(secop_service_module, "SECOP_OBLIGACIONES_LLM_TIMEOUT_S", 0.05)

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIG006")

        async def _slow_extraer(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(1)

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch("app.services.document_service.extraer_obligaciones_texto", new=_slow_extraer),
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert result.importados == 1
        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.requiere_obligaciones is True

    @pytest.mark.asyncio
    async def test_preview_sets_requiere_obligaciones_true(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIG005")

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=False)

        assert len(result.contratos) == 1
        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.requiere_obligaciones is True
