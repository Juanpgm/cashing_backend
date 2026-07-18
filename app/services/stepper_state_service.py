"""Stepper-state service — aggregate, read-only readiness for the `/radicar`
wizard (radicacion-stepper, work unit B4).

`obtener_stepper_state` is READ-ONLY: no credits, no writes, no side effects,
and it NEVER calls `preparar_radicacion` or `crear_cuenta_cobro`. It composes
existing read-only services/queries only:

- `cuenta_cobro_service._get_cuenta_con_ownership` (load + ownership check,
  same helper `radicacion_prep_service` already reuses across modules).
- `checklist_service.listar_catalogo` / `es_nivel_contrato` (catalog reads).
- `informe_service.obtener_estado_listo_pendiente` (the same read-only
  LISTO/PENDIENTE split `generar_zip_evidencias` uses internally).
- `requisito_inference_service.obtener_plantilla_organismo_por_contrato`
  (read-only organism-template lookup).
- Direct `DocumentoFuente` reads for the two predicates
  (contract-level mandatory docs, cuenta-level SS planilla) that must be
  derivable BEFORE the checklist gate (`asegurar_checklist`) has ever run —
  see the "Deviations" note in apply-progress.md for why these two predicates
  do not depend on `DocumentoCuentaCobro` rows.

Batch B4b (this file's remainder): steps 5-7, the contiguous-prefix resume
algorithm (`_resumen`), and the public `obtener_stepper_state` composition
entrypoint the router delegates to. Batch B4a shipped the schema + the four
step-1..4 predicate functions above — see apply-progress.md for the split
rationale.

Deliberate scope decision (documented in apply-progress.md, Batch B4b): step
7's `complete` predicate is `EstadoListoPendiente.listo_para_radicar` ONLY —
it does NOT check physical package existence in storage. `StoragePort` has no
exists/list capability yet; adding one is B5's job (`GET /paquete`). Until B5
lands, steps 6 and 7 report the same boolean here — this does not affect the
contiguous-prefix algorithm or the double-charge invariant, the two
guarantees this unit is tested against.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CHECKLIST_INCOMPLETE, PACKAGE_PENDIENTE
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, PosicionCuota
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.schemas.stepper_state import StepperStateResponse, StepState
from app.services import checklist_service, cuenta_cobro_service, informe_service, requisito_inference_service
from app.services.informe_service import EstadoListoPendiente

_TOTAL_STEPS = 7


async def _step1_contrato(db: AsyncSession, cuenta: CuentaCobro, contrato: Contrato) -> StepState:
    """contract exists AND nivel-contrato mandatory docs satisfied AND
    contrato.obligaciones non-empty."""
    obligaciones_count = len(contrato.obligaciones)

    catalogo = await checklist_service.listar_catalogo(db)
    is_first = cuenta.posicion == PosicionCuota.PRIMERA
    codigos_necesarios: dict[TipoDocumentoFuente, str] = {}
    for req in catalogo:
        if not req.obligatorio or not req.tipo_documento_fuente:
            continue
        if not checklist_service.es_nivel_contrato(req.codigo):
            continue
        if req.solo_primera_cuenta and not is_first:
            continue
        try:
            tipo = TipoDocumentoFuente(req.tipo_documento_fuente)
        except ValueError:
            continue
        codigos_necesarios[tipo] = req.codigo

    docs_ok = True
    if codigos_necesarios:
        result = await db.execute(
            select(DocumentoFuente.tipo).where(
                DocumentoFuente.contrato_id == contrato.id,
                DocumentoFuente.cuenta_cobro_id.is_(None),
                DocumentoFuente.tipo.in_(list(codigos_necesarios.keys())),
            )
        )
        tipos_presentes = {row[0] for row in result.all()}
        docs_ok = all(tipo in tipos_presentes for tipo in codigos_necesarios)

    complete = obligaciones_count > 0 and docs_ok
    return StepState(
        step=1,
        key="contrato",
        complete=complete,
        blocking=not complete,
        code=None,
        detail={"obligaciones": obligaciones_count},
    )


async def _step2_cuota(db: AsyncSession, cuenta: CuentaCobro) -> StepState:
    """cuota exists (numero_cuota set) AND SS planilla requisito attached.

    The SS-planilla check reads the cuenta-scoped `DocumentoFuente` directly
    (not `DocumentoCuentaCobro`) because checklist rows don't exist until the
    step-3 gate (`requisitos_modo`) has been resolved — see module docstring.
    """
    result = await db.execute(
        select(DocumentoFuente.id)
        .where(
            DocumentoFuente.cuenta_cobro_id == cuenta.id,
            DocumentoFuente.tipo == TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        )
        .limit(1)
    )
    tiene_planilla = result.scalar_one_or_none() is not None
    complete = cuenta.numero_cuota is not None and tiene_planilla
    return StepState(
        step=2,
        key="cuota",
        complete=complete,
        blocking=not complete,
        code=None,
        detail={"numero_cuota": cuenta.numero_cuota},
    )


def _step3_checklist(cuenta: CuentaCobro) -> StepState:
    """checklist defined = the mode gate has been resolved (`requisitos_modo`
    is not NULL) — mirrors `GET /checklist`'s own gate (see
    `app/api/v1/checklist.py::obtener_checklist`)."""
    complete = cuenta.requisitos_modo is not None
    return StepState(
        step=3,
        key="checklist",
        complete=complete,
        blocking=not complete,
        code=None if complete else CHECKLIST_INCOMPLETE,
        detail=None,
    )


def _step4_justificaciones(contrato: Contrato, actividades: list[Actividad]) -> StepState:
    """>= 1 Actividad per obligation."""
    obligaciones = contrato.obligaciones
    obligacion_ids_con_actividad = {a.obligacion_id for a in actividades if a.obligacion_id is not None}
    total = len(obligaciones)
    cubiertas = sum(1 for o in obligaciones if o.id in obligacion_ids_con_actividad)
    complete = total > 0 and cubiertas == total
    return StepState(
        step=4,
        key="justificaciones",
        complete=complete,
        blocking=not complete,
        code=None,
        detail={"obligaciones": total, "con_actividad": cubiertas},
    )


async def _step5_formato(db: AsyncSession, usuario_id: uuid.UUID, contrato: Contrato) -> StepState:
    """organism structure OR standard fallback — always available, never blocks."""
    plantilla = await requisito_inference_service.obtener_plantilla_organismo_por_contrato(db, usuario_id, contrato)
    return StepState(
        step=5,
        key="formato",
        complete=True,
        blocking=False,
        code=None,
        detail={"plantilla_ingerida": plantilla is not None},
    )


def _step6_evidencias(estado: EstadoListoPendiente) -> StepState:
    """checklist satisfied = every obligación has evidence (pendientes == 0)."""
    complete = estado.pendientes == 0
    return StepState(
        step=6,
        key="evidencias",
        complete=complete,
        blocking=not complete,
        code=None,
        detail={"pendientes": estado.pendientes},
    )


def _step7_paquete(estado: EstadoListoPendiente) -> StepState:
    """package readiness — see module docstring for the storage-check deferral."""
    complete = estado.listo_para_radicar
    return StepState(
        step=7,
        key="paquete",
        complete=complete,
        blocking=not complete,
        code=None if complete else PACKAGE_PENDIENTE,
        detail={"pendientes": estado.pendientes},
    )


def _resumen(steps: list[StepState]) -> tuple[int, int]:
    """Contiguous-prefix rule: furthest_completed_step = largest N such that
    steps 1..N are ALL complete. current_step = furthest + 1, clamped to 7."""
    furthest = 0
    for step_state in steps:
        if not step_state.complete:
            break
        furthest = step_state.step
    current = min(furthest + 1, _TOTAL_STEPS)
    return furthest, current


async def obtener_stepper_state(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> StepperStateResponse:
    """Read-only aggregate readiness for the 7-step stepper. Never mutates
    anything, never charges credits, never calls `preparar_radicacion`."""
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    contrato = cuenta.contrato

    estado = await informe_service.obtener_estado_listo_pendiente(db, usuario_id, cuenta_id)

    steps = [
        await _step1_contrato(db, cuenta, contrato),
        await _step2_cuota(db, cuenta),
        _step3_checklist(cuenta),
        _step4_justificaciones(contrato, cuenta.actividades),
        await _step5_formato(db, usuario_id, contrato),
        _step6_evidencias(estado),
        _step7_paquete(estado),
    ]

    furthest, current = _resumen(steps)

    return StepperStateResponse(
        cuenta_cobro_id=cuenta.id,
        contrato_id=contrato.id,
        mes=cuenta.mes,
        anio=cuenta.anio,
        fecha_transaccion=cuenta.fecha_transaccion,
        numero_cuota=cuenta.numero_cuota,
        posicion_cuota=cuenta.posicion.value,
        current_step=current,
        furthest_completed_step=furthest,
        steps=steps,
    )
