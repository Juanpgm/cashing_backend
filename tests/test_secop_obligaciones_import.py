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
from decimal import Decimal
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
    async def test_preview_sets_requiere_obligaciones_true(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
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


class TestObligacionesExtraidasFlag:
    """C.3 (slice 7a): `Contrato.obligaciones_extraidas` distinguishes a SECOP
    import that genuinely found zero obligaciones from a contract the user
    simply hasn't populated yet (manual creation leaves it None). Set at the
    `Contrato(...)` construction (deterministic case) and reconciled after the
    LLM-fallback branch resolves — the 7a.4 under-report guard: obligaciones
    added ONLY by the LLM fallback (after the row already exists) must still
    flip the flag to True, not just the deterministic verbatim pass."""

    @pytest.mark.asyncio
    async def test_deterministic_extraction_sets_true(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        """Verbatim extraction finds obligaciones in `objeto` → True, LLM never called."""
        from app.services.secop_service import importar_contratos_secop

        objeto = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIGEXT001", objeto_del_contrato=objeto)

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        contrato = result.contratos[0]
        assert contrato.obligaciones_extraidas is True

    @pytest.mark.asyncio
    async def test_zero_deterministic_and_zero_llm_fallback_sets_false(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Neither verbatim nor LLM fallback find obligaciones → False."""
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIGEXT002")
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

        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.obligaciones_extraidas is False

    @pytest.mark.asyncio
    async def test_llm_fallback_finding_obligaciones_sets_true(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """The 7a.4 under-report guard: obligaciones added AFTER the Contrato row
        already exists (LLM fallback branch, not the deterministic constructor
        call) must still reconcile the flag to True."""
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.OBLIGEXT003")
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

        contrato = result.contratos[0]
        assert len(contrato.obligaciones) == 1
        assert contrato.obligaciones_extraidas is True

    @pytest.mark.asyncio
    async def test_manually_created_contract_leaves_flag_none(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Manual contract creation (contrato_service.crear_contrato) never sets
        this SECOP-import-only signal — stays None."""
        from datetime import date
        from decimal import Decimal

        from app.schemas.contrato import ContratoCreate
        from app.services.contrato_service import crear_contrato

        data = ContratoCreate(
            numero_contrato="MANUAL-OBLIGEXT-001",
            objeto="Prestación de servicios profesionales de apoyo a la gestión administrativa.",
            valor_mensual=Decimal("1000000"),
            fecha_inicio=date(2024, 1, 1),
            fecha_fin=date(2024, 12, 31),
        )

        contrato = await crear_contrato(db, test_user["user"].id, data)

        assert contrato.obligaciones_extraidas is None


class TestObligacionesExtraidasFlagOnReimport:
    """Bug: re-importing a contract that already exists (the UPSERT/duplicate
    branch of `importar_contratos_secop`) never reconsidered obligaciones — a
    contract first imported with zero obligaciones stayed stuck forever, even
    when a later SECOP snapshot's `objeto` yields obligaciones on verbatim
    extraction. Fix: when the existing contrato currently has empty obligaciones
    and the new import yields obligaciones, add them and flip the flag to True;
    an already-True flag must never be clobbered back to False/None."""

    @pytest.mark.asyncio
    async def test_reimport_flips_false_to_true_when_obligaciones_now_appear(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row_sin_obligaciones = _make_row(numero_contrato="CO1.PCCNTR.REIMPORT001")
        llm_response = LLMResponse(content="", model="test", total_tokens=5)
        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row_sin_obligaciones],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_response)
            mock_get_llm.return_value = mock_llm
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        assert first.contratos[0].obligaciones_extraidas is False

        objeto_con_obligaciones = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row_con_obligaciones = _make_row(
            numero_contrato="CO1.PCCNTR.REIMPORT001", objeto_del_contrato=objeto_con_obligaciones
        )
        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row_con_obligaciones],
        ):
            second = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        contrato = second.contratos[0]
        assert len(contrato.obligaciones) >= 2
        assert contrato.obligaciones_extraidas is True

    @pytest.mark.asyncio
    async def test_reimport_never_clobbers_an_existing_true_flag(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        objeto_con_obligaciones = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row_con_obligaciones = _make_row(
            numero_contrato="CO1.PCCNTR.REIMPORT002", objeto_del_contrato=objeto_con_obligaciones
        )
        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row_con_obligaciones],
        ):
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        assert first.contratos[0].obligaciones_extraidas is True

        row_reimport_sin_obligaciones = _make_row(
            numero_contrato="CO1.PCCNTR.REIMPORT002", valor_del_contrato="15000000"
        )
        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row_reimport_sin_obligaciones],
        ):
            second = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        contrato = second.contratos[0]
        assert contrato.obligaciones_extraidas is True
        assert len(contrato.obligaciones) >= 2

    @pytest.mark.asyncio
    async def test_llm_raises_path_sets_flag_false_not_none(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.REIMPORT003")
        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM, side_effect=RuntimeError("LLM unavailable")),
        ):
            result = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.obligaciones_extraidas is False

    @pytest.mark.asyncio
    async def test_llm_timeout_path_sets_flag_false_not_none(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.services.secop_service as secop_service_module
        from app.services.secop_service import importar_contratos_secop

        monkeypatch.setattr(secop_service_module, "SECOP_OBLIGACIONES_LLM_TIMEOUT_S", 0.05)

        row = _make_row(numero_contrato="CO1.PCCNTR.REIMPORT004")

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

        contrato = result.contratos[0]
        assert contrato.obligaciones == []
        assert contrato.obligaciones_extraidas is False


class TestReimportValorAdicionPreserved:
    """SUGGESTION A-002: `valor_adicion` was unconditionally overwritten on
    every re-import (`existing_contrato.valor_adicion = float(data.valor_adicion)
    if data.valor_adicion else None`) — a re-import whose row lacks a fresh
    valor_adicion signal (falsy/None) wiped a previously-recorded non-null
    value. Fix mirrors the obligaciones "never clobber good data on a later
    empty run" pattern: only overwrite when the new value actually has one."""

    @pytest.mark.asyncio
    async def test_reimport_preserves_valor_adicion_when_new_value_is_falsy(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row_con_adicion = _make_row(numero_contrato="CO1.PCCNTR.VALADIC001", valor_adicion="500000")
        llm_response = LLMResponse(content="", model="test", total_tokens=5)

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row_con_adicion],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_response)
            mock_get_llm.return_value = mock_llm
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        assert first.contratos[0].valor_adicion == Decimal("500000.00")

        row_sin_adicion = _make_row(numero_contrato="CO1.PCCNTR.VALADIC001")
        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row_sin_adicion],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_response)
            mock_get_llm.return_value = mock_llm
            second = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert second.contratos[0].valor_adicion == Decimal("500000.00")


class TestReimportObligacionesRowLock:
    """WARNING A-001/B-001 (corroborated by both judges): the re-import
    obligaciones-insert is a read-then-write with no row lock and no unique
    constraint, so two concurrent re-imports of the same contrato could both
    observe empty obligaciones and both insert, duplicating rows. True
    concurrency isn't unit-testable against a single aiosqlite connection, so
    this pins the two observable layers instead:
      1. the UPSERT branch re-selects the existing contrato with
         `with_for_update()` before the obligaciones read-then-write
         (verified at SQL-construction level, since SQLite silently no-ops
         FOR UPDATE at execution time), and
      2. sequential re-imports that both yield obligaciones never duplicate
         the guard's insert (the guard's correctness is otherwise
         unaffected by adding the lock).
    """

    @pytest.mark.asyncio
    async def test_upsert_branch_locks_existing_contrato_row(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.secop_service import importar_contratos_secop
        from sqlalchemy.dialects import postgresql

        row = _make_row(numero_contrato="CO1.PCCNTR.LOCK001")
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
            await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        captured_selects: list[Any] = []
        original_execute = db.execute

        async def _spy_execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            captured_selects.append(stmt)
            return await original_execute(stmt, *args, **kwargs)

        db.execute = _spy_execute  # type: ignore[method-assign]
        try:
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
                await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        finally:
            db.execute = original_execute  # type: ignore[method-assign]

        locked = [
            stmt
            for stmt in captured_selects
            if hasattr(stmt, "compile") and "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect()))
        ]
        assert locked, "expected the UPSERT branch to re-select existing_contrato with with_for_update()"

    @pytest.mark.asyncio
    async def test_sequential_reimports_do_not_duplicate_obligaciones(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        objeto_con_obligaciones = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row = _make_row(numero_contrato="CO1.PCCNTR.LOCK002", objeto_del_contrato=objeto_con_obligaciones)

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        assert len(first.contratos[0].obligaciones) >= 2
        count_after_first = len(first.contratos[0].obligaciones)

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            second = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert len(second.contratos[0].obligaciones) == count_after_first


class TestReimportLlmFallback:
    """Judgment finding B-002: the re-import UPSERT branch only retried the
    DETERMINISTIC verbatim extractor to decide whether to add obligaciones —
    unlike the new-contract path, it never fell back to the LLM extractor.
    A re-imported contract whose `objeto` never enumerates obligaciones
    verbatim stayed stuck at `obligaciones_extraidas=False` forever, even
    though the LLM could extract them. Fix: wire the same LLM-fallback flow
    (`extraer_obligaciones_texto`, same timeout/cap) into the re-import
    branch, only reached when BOTH the existing contrato and the fresh
    verbatim pass are empty — never touching a contrato that already has
    obligaciones."""

    @pytest.mark.asyncio
    async def test_reimport_llm_fallback_flips_flag_and_inserts_obligaciones(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        row = _make_row(numero_contrato="CO1.PCCNTR.LLMREIMPORT001")
        empty_llm_response = LLMResponse(content="", model="test", total_tokens=5)

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=empty_llm_response)
            mock_get_llm.return_value = mock_llm
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        assert first.contratos[0].obligaciones == []
        assert first.contratos[0].obligaciones_extraidas is False

        found_llm_response = LLMResponse(
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
            mock_llm.complete = AsyncMock(return_value=found_llm_response)
            mock_get_llm.return_value = mock_llm
            second = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        contrato = second.contratos[0]
        assert len(contrato.obligaciones) == 1
        assert contrato.obligaciones_extraidas is True

    @pytest.mark.asyncio
    async def test_reimport_skips_llm_when_existing_contrato_already_has_obligaciones(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.secop_service import importar_contratos_secop

        objeto_con_obligaciones = (
            "Prestación de servicios profesionales. OBLIGACIONES ESPECIFICAS DEL CONTRATISTA: "
            "1. Elaborar informes mensuales de las actividades desarrolladas en el marco del contrato. "
            "2. Apoyar la atención al ciudadano y la gestión documental de la dependencia. "
            "Las demás actividades que le sean asignadas por el supervisor del contrato."
        )
        row = _make_row(numero_contrato="CO1.PCCNTR.LLMREIMPORT002", objeto_del_contrato=objeto_con_obligaciones)

        with patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[row],
        ):
            first = await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)
        assert len(first.contratos[0].obligaciones) >= 2

        with (
            patch(
                "app.services.secop_service._query_socrata",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(_PATCH_GET_LLM) as mock_get_llm,
        ):
            await importar_contratos_secop(db, "1016019452", test_user["user"].id, confirmar=True)

        mock_get_llm.assert_not_called()
