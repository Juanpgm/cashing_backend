"""Tests for `stepper_state_service` (radicacion-stepper, work unit B4 —
highest attention: the cross-repo `StepperStateResponse` contract + the
contiguous-prefix resume algorithm + the double-charge invariant).

Batch B4a: unit tests for the four step-1..4 `complete` predicates, calling
the predicate functions directly.

Batch B4b: the aggregate `obtener_stepper_state` (steps 5-7, contiguous-prefix
resume, double-charge guard) + `GET /cuentas-cobro/{id}/stepper-state` + the
full `StepperStateResponse` contract-shape test (SECOP-drift mitigation).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.month_scoping import calcular_ventana_mes
from app.core.security import create_access_token, hash_password
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.stepper_state import StepperStateResponse
from app.services import stepper_state_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_KNOWN_CODES = {
    "CHECKLIST_INCOMPLETE",
    "COHERENCE_CHECK_FAILED",
    "PACKAGE_PENDIENTE",
    "SECRET_DETECTED_IN_PACKAGE",
    "CUOTA_POSITION_CONFLICT",
}

# No module-wide `pytestmark = pytest.mark.asyncio`: this file intentionally
# mixes async tests (DB-backed) with plain sync tests (pure predicate
# functions, e.g. `_step3_checklist`) — `asyncio_mode = "auto"` (pyproject.toml)
# already auto-detects async tests without needing the marker, and applying it
# module-wide would incorrectly mark the sync tests too.


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-STEPPER-STATE-001",
        objeto="Servicios profesionales para pruebas de stepper-state",
        valor_total=24_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # The `contrato` fixture already refreshed `contrato.obligaciones` (empty) via
    # its own `db.refresh(c)` BEFORE this row existed; within the same session's
    # identity map, later eager-load options on `contrato` won't overwrite an
    # already-populated (stale) collection. Force a real reload here so every
    # test sees the current DB state through `contrato.obligaciones`.
    await db.refresh(contrato)
    return ob


async def _make_cuenta(
    db: AsyncSession,
    contrato: Contrato,
    *,
    mes: int = 1,
    anio: int = 2024,
    numero_cuota: int | None = 1,
    posicion: PosicionCuota = PosicionCuota.PRIMERA,
    requisitos_modo: str | None = None,
    fecha_transaccion: date | None = None,
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        numero_cuota=numero_cuota,
        posicion=posicion,
        requisitos_modo=requisitos_modo,
        fecha_transaccion=fecha_transaccion,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _add_documento_contrato(
    db: AsyncSession,
    *,
    usuario_id: Any,
    contrato_id: Any,
    tipo: TipoDocumentoFuente,
    cuenta_cobro_id: Any | None = None,
) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        storage_key=f"documentos/{tipo.value}.pdf",
        nombre=f"{tipo.value}.pdf",
        tipo=tipo,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _add_actividad_con_evidencia(
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
    *,
    con_evidencia: bool = True,
    justificacion: str = "Justificación",
) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion=justificacion,
        fecha_realizacion=date(2024, 1, 10),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    if con_evidencia:
        ev = Evidencia(
            actividad_id=act.id,
            storage_key=f"evidencias/{act.id}/foto.jpg",
            nombre_archivo="foto.jpg",
            tipo_archivo="image/jpeg",
            tamano_bytes=12,
        )
        db.add(ev)
        await db.commit()
        # Same identity-map staleness pattern as above, now on `act.evidencias`
        # (already cached empty by the `db.refresh(act)` call before this row
        # existed) — force a real reload.
        await db.refresh(act)
    # `_make_cuenta`'s own `db.refresh(cc)` already loaded `cuenta.actividades`
    # (empty) via the same identity-map staleness pattern as `contrato.obligaciones`
    # above — force a real reload so the newly added actividad is visible.
    await db.refresh(cuenta)
    return act


async def _contrato_completo(db: AsyncSession, contrato: Contrato, usuario_id: Any) -> None:
    """Satisfy step 1's completeness predicate: a contract-level CONTRATO
    upload (narrowed rule — see `_step1_contrato`'s docstring; RPC/CEDULA/
    RUT/ACTA_INICIO are no longer required here, only at the step-3
    checklist gate)."""
    await _add_documento_contrato(db, usuario_id=usuario_id, contrato_id=contrato.id, tipo=TipoDocumentoFuente.CONTRATO)


async def _cuenta_completo(db: AsyncSession, cuenta: CuentaCobro, usuario_id: Any) -> None:
    """Satisfy step 2's SS-planilla predicate (cuenta-scoped upload)."""
    await _add_documento_contrato(
        db,
        usuario_id=usuario_id,
        contrato_id=cuenta.contrato_id,
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        cuenta_cobro_id=cuenta.id,
    )


# ── B4a — one unit test per step-1..4 predicate (direct calls) ─────────────


async def test_step1_predicate_incomplete_without_contrato_doc_or_obligaciones(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """No CONTRATO doc, no obligaciones, flag unset — genuinely nothing to
    show step 1 was ever processed. Deliberately does NOT use the
    `obligacion` fixture (see the OR-satisfier tests below) — that fixture's
    presence alone now satisfies step 1, per the live-reproduced fix."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert result.step == 1
    assert result.key == "contrato"
    assert result.complete is False
    assert result.blocking is True
    _ = user


async def test_step1_predicate_complete_with_only_contrato_doc(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    """Only the CONTRATO doc is attached (no RPC/CEDULA/RUT/ACTA_INICIO) —
    step 1 must still report complete, per the narrowed product rule."""
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert result.complete is True
    assert result.blocking is False


async def test_step1_predicate_complete_with_obligaciones_extraidas_no_contrato_doc(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    """Live-reproduced bug: a contrato imported via the SECOP 'Vincular' flow
    extracts obligaciones (sets `obligaciones_extraidas = True`) without ever
    persisting a CONTRATO-typed `DocumentoFuente` — step 1 must still report
    complete, not stay permanently stuck."""
    cuenta = await _make_cuenta(db, contrato)
    contrato.obligaciones_extraidas = True
    await db.commit()
    await db.refresh(contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert result.complete is True
    assert result.blocking is False


async def test_step1_predicate_complete_with_obligaciones_but_null_flag(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    """Live-reproduced bug (Postgres, real data): 'Reiniciar obligaciones'
    resets `obligaciones_extraidas` to None (`contrato_service.py`), and the
    manual Vincular recovery path that follows repopulates `Obligacion` rows
    WITHOUT ever setting the flag back to True. Step 1 must key off
    obligaciones presence, not just the flag, or this contrato — despite
    having real obligaciones and no CONTRATO doc, exactly like the live
    reproduction — stays permanently stuck."""
    cuenta = await _make_cuenta(db, contrato)
    contrato.obligaciones_extraidas = None
    await db.commit()
    await db.refresh(contrato, attribute_names=["obligaciones_extraidas"])
    await db.refresh(contrato)

    result = await stepper_state_service._step1_contrato(db, cuenta, contrato)

    assert contrato.obligaciones_extraidas is None
    assert result.complete is True
    assert result.blocking is False


async def test_step2_predicate_incomplete_without_numero_cuota(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, numero_cuota=None)

    result = stepper_state_service._step2_cuota(cuenta)

    assert result.step == 2
    assert result.key == "cuota"
    assert result.complete is False


async def test_step2_predicate_complete_with_numero_cuota_alone(db: AsyncSession, contrato: Contrato) -> None:
    """Live-reproduced deadlock (restores an already-archived design decision
    — `radicar-stepper-simplificacion` design.md, 'move SS complete from
    _step2_cuota to _step3_checklist' — that never actually landed in this
    function): SS-planilla is NOT required for step 2 anymore. The only place
    it's attachable is the checklist's generic per-requisito upload (step 3)
    — requiring it here made step 3 permanently unreachable whenever it was
    still missing (`furthest_completed_step` could never pass 1), since
    checklist needs step 2 complete to even be navigated to."""
    cuenta = await _make_cuenta(db, contrato, numero_cuota=1)

    result = stepper_state_service._step2_cuota(cuenta)

    assert result.complete is True


async def test_step3_predicate_incomplete_when_mode_not_chosen(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, requisitos_modo=None)

    result = await stepper_state_service._step3_checklist(db, cuenta)

    assert result.step == 3
    assert result.key == "checklist"
    assert result.complete is False
    assert result.code == "CHECKLIST_INCOMPLETE"


async def test_step3_predicate_incomplete_when_mode_chosen_but_no_ss_planilla(
    db: AsyncSession, contrato: Contrato
) -> None:
    """The mode gate alone is no longer sufficient — SS-planilla completeness
    moved here from step 2 (same design decision as above)."""
    cuenta = await _make_cuenta(db, contrato, requisitos_modo="estandar")

    result = await stepper_state_service._step3_checklist(db, cuenta)

    assert result.complete is False
    assert result.code == "CHECKLIST_INCOMPLETE"


async def test_step3_predicate_complete_when_mode_chosen_and_ss_planilla_attached(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, requisitos_modo="estandar")
    await _cuenta_completo(db, cuenta, user.id)  # attaches the cuenta-scoped SEGURIDAD_SOCIAL doc

    result = await stepper_state_service._step3_checklist(db, cuenta)

    assert result.complete is True
    assert result.code is None


async def test_step3_predicate_complete_when_ss_requisito_cumplido_manual(db: AsyncSession, contrato: Contrato) -> None:
    """Parity with `computar_resumen` (live-reproduced 2026-08-11): the checklist
    UI offers "Marcar cumplido"/"No aplica" on the SS row and radicación honors
    them — step 3 must too, or the row shows green while the wizard stays
    blocked with a generic hint."""
    from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito

    cuenta = await _make_cuenta(db, contrato, requisitos_modo="estandar")
    db.add(
        DocumentoCuentaCobro(
            cuenta_cobro_id=cuenta.id,
            requisito_codigo="SEGURIDAD_SOCIAL",
            estado=EstadoRequisito.CUMPLIDO_MANUAL,
        )
    )
    await db.commit()

    result = await stepper_state_service._step3_checklist(db, cuenta)

    assert result.complete is True
    assert result.code is None


async def test_step6_predicate_incomplete_without_actividad_per_obligacion(
    contrato: Contrato, obligacion: Obligacion
) -> None:
    result = stepper_state_service._step6_justificaciones(contrato, [])

    assert result.step == 6
    assert result.key == "justificaciones"
    assert result.complete is False


async def test_step6_predicate_complete_with_one_actividad_per_obligacion(
    db: AsyncSession, contrato: Contrato, obligacion: Obligacion
) -> None:
    cuenta = await _make_cuenta(db, contrato)
    act = await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=False)

    result = stepper_state_service._step6_justificaciones(contrato, [act])

    assert result.complete is True


# ── B4b — exact response shape (SECOP-drift mitigation) ────────────────────


async def test_stepper_state_response_shape_exact_keys(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert isinstance(result, StepperStateResponse)
    payload = result.model_dump()
    assert set(payload.keys()) == {
        "cuenta_cobro_id",
        "contrato_id",
        "mes",
        "anio",
        "fecha_transaccion",
        "numero_cuota",
        "posicion_cuota",
        "current_step",
        "furthest_completed_step",
        "steps",
        "ventana_inicio",
        "ventana_fin",
        "ventana_advertencia",
    }


async def test_stepper_state_steps_length_7_ordered(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert len(result.steps) == 7
    assert [s.step for s in result.steps] == [1, 2, 3, 4, 5, 6, 7]
    for step_state in result.steps:
        payload = step_state.model_dump()
        assert set(payload.keys()) == {"step", "key", "complete", "blocking", "code", "detail"}


async def test_stepper_state_codes_are_known_domain_codes_or_null(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)  # nothing satisfied — every step incomplete

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    for step_state in result.steps:
        assert step_state.code is None or step_state.code in _KNOWN_CODES


async def test_stepper_state_numero_cuota_presence_signals_cuota_exists(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, numero_cuota=1)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.numero_cuota == 1


async def test_stepper_state_endpoint_returns_the_documented_shape(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato)

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/stepper-state", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cuenta_cobro_id"] == str(cuenta.id)
    assert body["contrato_id"] == str(contrato.id)
    assert len(body["steps"]) == 7


# ── B4b — steps 5-7 via the aggregate ───────────────────────────────────────


async def test_step5_formato_always_non_blocking(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Step 5 never hard-blocks — a standard-format fallback always exists."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[4].key == "formato"
    assert result.steps[4].blocking is False
    assert result.steps[4].complete is True
    assert result.steps[4].detail == {"plantilla_ingerida": False, "plantillas": []}


async def test_step5_formato_lists_every_plantilla_per_tipo(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Detail lists ALL ingested plantillas for the contrato's entidad (not
    just informe_actividades) with tipo/formato/clonable/campos_total."""
    from app.core.text_match import normalize
    from app.models.plantilla_organismo import PlantillaOrganismo

    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    db.add(
        PlantillaOrganismo(
            usuario_id=user.id,
            entidad=contrato.entidad,
            entidad_normalizada=normalize(contrato.entidad),
            tipo_documento="cuenta_cobro",
            formato="docx",
            estructura_json={"clonable": True, "campos": [{"campo": "cuota.valor"}, {"campo": "cuota.mes"}]},
        )
    )
    db.add(
        PlantillaOrganismo(
            usuario_id=user.id,
            entidad=contrato.entidad,
            entidad_normalizada=normalize(contrato.entidad),
            tipo_documento="informe_actividades",
            formato="pdf",
            estructura_json={"columnas": [], "secciones": []},
        )
    )
    await db.commit()

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    detail = result.steps[4].detail
    assert detail is not None
    assert detail["plantilla_ingerida"] is True
    assert detail["plantillas"] == [
        {"tipo_documento": "cuenta_cobro", "formato": "docx", "clonable": True, "campos_total": 2},
        {"tipo_documento": "informe_actividades", "formato": "pdf", "clonable": False, "campos_total": 0},
    ]


async def test_step4_evidencias_incomplete_when_pendientes_greater_than_zero(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # H12: a real justificación now counts as coverage — this test intends "no
    # support at all", so the actividad must carry neither evidencia nor texto.
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=False, justificacion="")

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[3].key == "evidencias"
    assert result.steps[3].complete is False


async def test_step4_evidencias_complete_when_pendientes_is_zero(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=True)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[3].complete is True


async def test_step7_paquete_incomplete_when_not_listo_para_radicar(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # H12: same as step-4 test above — "not listo" now requires no justificación.
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=False, justificacion="")

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[6].key == "paquete"
    assert result.steps[6].complete is False
    assert result.steps[6].code == "PACKAGE_PENDIENTE"


async def test_step7_paquete_complete_when_listo_para_radicar(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=True)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[6].complete is True
    assert result.steps[6].code is None


# ── B4b — contiguous-prefix resume / current_step clamp ─────────────────────


async def test_resume_non_contiguous_completion_does_not_advance_past_first_gap(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    """Steps 1-2 complete, step 3 (checklist mode) NOT chosen, but step 4
    (evidencias) happens to already be satisfiable. furthest_completed_step
    must stop at 2 — never skip the gap at step 3."""
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato, requisitos_modo=None)
    await _cuenta_completo(db, cuenta, user.id)
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=True)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[0].complete is True
    assert result.steps[1].complete is True
    assert result.steps[2].complete is False  # the gap
    assert result.furthest_completed_step == 2
    assert result.current_step == 3


async def test_resume_current_step_is_furthest_completed_plus_one(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato)
    await _cuenta_completo(db, cuenta, user.id)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.furthest_completed_step == 2
    assert result.current_step == 3


async def test_resume_all_seven_complete_clamps_current_step_to_7(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, obligacion: Obligacion
) -> None:
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato, requisitos_modo="estandar")
    await _cuenta_completo(db, cuenta, user.id)
    await _add_actividad_con_evidencia(db, cuenta, obligacion, con_evidencia=True)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.furthest_completed_step == 7
    assert result.current_step == 7


async def test_resume_no_progress_lands_on_step_1(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.furthest_completed_step == 0
    assert result.current_step == 1


# ── B4b — double-charge guard (the critical invariant) ──────────────────────


async def test_resume_with_existing_cuota_never_calls_crear_cuenta_cobro(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a cuota already exists (numero_cuota != null, credits already
    charged at creation), calling stepper-state — simulating a step-2 resume —
    must issue ZERO crear_cuenta_cobro calls and leave the credit balance
    unchanged. This is the backend half of the No-Double-Credit-Charge invariant
    (frontend half is F3.3)."""
    from app.services import cuenta_cobro_service

    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, numero_cuota=1)
    saldo_antes = user.creditos_disponibles

    spy = AsyncMock(wraps=cuenta_cobro_service.crear_cuenta_cobro)
    monkeypatch.setattr(cuenta_cobro_service, "crear_cuenta_cobro", spy)

    await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    spy.assert_not_called()
    await db.refresh(user)
    assert user.creditos_disponibles == saldo_antes


# ── B7 Fix 1 — zero-obligation contract must not be permanently blocked ────


async def test_zero_obligation_contract_aggregate_is_internally_consistent(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """A contract with zero Obligacion rows must not be permanently blocked at
    steps 1/4 while steps 6/7 report complete=True vacuously for the same
    reason (no obligaciones to satisfy). Predicates must mirror the real
    radicación gates (`construir_checklist_completo`, `generar_zip_evidencias`,
    `generar_actividades_agente`'s "obligaciones OR texto_contrato" branch),
    none of which hard-require obligaciones_count > 0. `contrato` here has NO
    `obligacion` fixture dependency — zero Obligacion rows by construction."""
    user = test_user["user"]
    await _contrato_completo(db, contrato, user.id)
    cuenta = await _make_cuenta(db, contrato, requisitos_modo="estandar")
    await _cuenta_completo(db, cuenta, user.id)

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.steps[0].key == "contrato"
    assert result.steps[0].complete is True
    assert result.steps[0].detail == {"obligaciones": 0}
    assert result.steps[3].key == "evidencias"
    assert result.steps[3].complete is True  # evidencias — already vacuous pre-fix
    assert result.steps[5].key == "justificaciones"
    assert result.steps[5].complete is True
    assert result.steps[5].detail == {"obligaciones": 0, "con_actividad": 0}
    assert result.steps[6].complete is True  # paquete — already vacuous pre-fix
    assert result.furthest_completed_step == 7
    assert result.current_step == 7


# ── B7 Fix 2 — wire calcular_ventana_mes into stepper-state ─────────────────


async def test_stepper_state_includes_ventana_fields_from_month_scoping(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """`calcular_ventana_mes` had zero production callers before this fix — the
    frontend must consume the server-computed window from stepper-state
    instead of duplicating the rule client-side (the SECOP-drift precedent
    this design was built to avoid)."""
    user = test_user["user"]
    # mes/anio inside the contrato fixture's vigencia (2024) — H5 makes a month
    # outside vigencia set advertencia=True, covered by its own test below.
    cuenta = await _make_cuenta(db, contrato, mes=4, anio=2024, fecha_transaccion=date(2024, 4, 15))

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    esperado = calcular_ventana_mes(
        4, 2024, date(2024, 4, 15), vigencia_inicio=contrato.fecha_inicio, vigencia_fin=contrato.fecha_fin
    )
    assert result.ventana_inicio == esperado.fecha_inicio
    assert result.ventana_fin == esperado.fecha_fin
    assert result.ventana_advertencia == esperado.advertencia
    assert result.ventana_advertencia is False


async def test_stepper_state_ventana_advertencia_true_when_fecha_transaccion_before_month(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, mes=4, anio=2024, fecha_transaccion=date(2024, 3, 1))

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.ventana_advertencia is True
    assert result.ventana_inicio == date(2024, 4, 1)
    assert result.ventana_fin == date(2024, 4, 30)


async def test_stepper_state_ventana_advertencia_true_when_mes_fuera_de_vigencia(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """H5: cuota month entirely outside the contract vigencia (fixture: 2024)
    → server-driven advertencia, never a rejection."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, mes=4, anio=2025, fecha_transaccion=date(2025, 4, 15))

    result = await stepper_state_service.obtener_stepper_state(db, user.id, cuenta.id)

    assert result.ventana_advertencia is True
    assert result.ventana_inicio == date(2025, 4, 1)
    assert result.ventana_fin == date(2025, 4, 15)


# ── B7 Fix 3 — cross-tenant negative test ────────────────────────────────────


async def test_stepper_state_cross_tenant_returns_403(
    client: AsyncClient, db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato)

    otro = Usuario(
        email="otro-stepper-state@example.com",
        nombre="Otro Usuario",
        cedula="987654321",
        password_hash=hash_password("OtroPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    token = create_access_token(subject=str(otro.id), role=otro.rol)

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/stepper-state",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 403


# ── B7 Fix 4 — soft-deleted Contrato must not leak through ──────────────────


async def test_stepper_state_soft_deleted_contrato_returns_404(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato)
    contrato.deleted_at = datetime.now(UTC)
    db.add(contrato)
    await db.commit()

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/stepper-state", headers=test_user["headers"])

    assert r.status_code == 404
