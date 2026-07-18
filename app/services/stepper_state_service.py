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

Batch B4a: predicates 1-4 only (schema + the four step-1..4 predicate
functions + their unit tests). The aggregate `obtener_stepper_state`, steps
5-7, the contiguous-prefix resume algorithm, and the router land in batch B4b
— see apply-progress.md.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CHECKLIST_INCOMPLETE
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, PosicionCuota
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.schemas.stepper_state import StepState
from app.services import checklist_service


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
