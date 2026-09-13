"""Tool wrappers over `checklist_service` — required-document checklist per cuenta.

Every handler here first resolves the `CuentaCobro` via
`cuenta_cobro_service._get_cuenta_con_ownership`, which is the same ownership
gate the API routers use (`app/api/v1/checklist.py`) — a user can only ever
inspect or mutate the checklist of their own cuentas de cobro.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.core.exceptions import ValidationError
from app.models.cuenta_cobro import EstadoCuentaCobro
from app.schemas.checklist import ChecklistResponse, RequisitoChecklistItem
from app.services import checklist_service, cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# Checklist rows may only be mutated while the cuenta is still editable —
# mirrors the BORRADOR/RECHAZADA guard `cuenta_cobro_service` already enforces
# for actividades/formato/etc. (e.g. `agregar_actividad`, `actualizar_cuenta_cobro`).
# The legacy `PATCH /checklist/{codigo}` HTTP endpoint does NOT enforce this today
# (confirmed by audit, radicacion-sin-friccion slice 1.6) — this guard is
# deliberately stricter at the agent-tool layer, since an ENVIADA cuenta should
# never have its checklist silently rewritten from a chat turn.
_ESTADOS_CHECKLIST_EDITABLE = (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA)


class ResumenChecklistInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to inspect the checklist for.")


@tool(
    name="resumen_checklist",
    description=(
        "Get the full document checklist for a cuenta de cobro: per-requisito state "
        "(pendiente/detectado/cargado/etc.), linked document (uploaded or SECOP), top SECOP "
        "candidates, and a resumen (counts + whether it's ready to radicar). Read-only — safe "
        "to call repeatedly, idempotently creates the missing checklist rows on first call but "
        "does not auto-link or re-scan SECOP. Args: cuenta_id (UUID of the cuenta de cobro; must "
        "belong to the authenticated user)."
    ),
    input_model=ResumenChecklistInput,
    output_model=ChecklistResponse,
    tags=("read",),
)
async def resumen_checklist(ctx: ToolContext, params: ResumenChecklistInput) -> ChecklistResponse:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)

    if cuenta.requisitos_modo is None:
        return ChecklistResponse(
            cuenta_cobro_id=cuenta.id,
            requisitos_definidos=False,
            items=[],
            resumen={
                "total": 0,
                "cumplidos": 0,
                "pendientes": 0,
                "lista_pendientes": [],
                "lista_pendientes_desc": [],
                "radicacion_lista": False,
            },
            arbol_evidencias=[],
        )

    payload = await checklist_service.construir_checklist_completo(ctx.db, cuenta)
    return ChecklistResponse(**payload)


class DetectarDesdeSecopInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to re-scan SECOP candidates for.")


class CandidatoDetectado(BaseModel):
    secop_documento_id: uuid.UUID
    score: Decimal


class DetectarDesdeSecopOutput(BaseModel):
    candidatos_por_requisito: dict[str, list[CandidatoDetectado]] = Field(
        description="Top-N SECOP document candidates found per requisito code (or custom requisito UUID)."
    )
    requisitos_definidos: bool = Field(
        default=True,
        description=(
            "False when the cuenta has not resolved the post-creation checklist gate yet "
            "(requisitos_modo is NULL) — same signal resumen_checklist reports. When False, "
            "candidatos_por_requisito is always empty and nothing was scanned or materialized; "
            "the agent should prompt the user to call definir_requisitos_checklist first."
        ),
    )


@tool(
    name="detectar_desde_secop",
    description=(
        "Re-scan the cached SECOP documents for this cuenta's contract and (re)score candidates "
        "per checklist requisito. Auto-links the best match when its confidence score clears the "
        "auto-link threshold and the requisito is still pendiente — never overwrites a document "
        "already linked manually. If the cuenta has not resolved the requisitos gate yet "
        "(requisitos_modo unset), does nothing and returns requisitos_definidos=false with an "
        "empty candidatos_por_requisito instead of materializing the standard checklist. Args: "
        "cuenta_id (UUID of the cuenta de cobro; must belong to the authenticated user)."
    ),
    input_model=DetectarDesdeSecopInput,
    output_model=DetectarDesdeSecopOutput,
    tags=("write",),
)
async def detectar_desde_secop(ctx: ToolContext, params: DetectarDesdeSecopInput) -> DetectarDesdeSecopOutput:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)

    # Mirrors resumen_checklist's early return (and app/api/v1/checklist.py's
    # `_checklist_no_definido`): never materialize the standard checklist just
    # because it was scanned before the user chose a requisitos_modo.
    if cuenta.requisitos_modo is None:
        return DetectarDesdeSecopOutput(candidatos_por_requisito={}, requisitos_definidos=False)

    await checklist_service.asegurar_checklist(ctx.db, cuenta)
    resultado = await checklist_service.detectar_desde_secop(ctx.db, cuenta)
    return DetectarDesdeSecopOutput(
        candidatos_por_requisito={
            req_codigo: [CandidatoDetectado(secop_documento_id=doc.id, score=score) for doc, score in candidatos]
            for req_codigo, candidatos in resultado.items()
        }
    )


class AutoVincularDocumentosInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to auto-link uploaded documents for.")


class AutoVincularDocumentosOutput(BaseModel):
    vinculados: int = Field(description="Number of checklist rows newly linked to an uploaded document.")
    requisitos_definidos: bool = Field(
        default=True,
        description=(
            "False when the cuenta has not resolved the post-creation checklist gate yet "
            "(requisitos_modo is NULL) — same signal resumen_checklist reports. When False, "
            "vinculados is always 0 and nothing was linked or materialized; the agent should "
            "prompt the user to call definir_requisitos_checklist first."
        ),
    )


@tool(
    name="auto_vincular_documentos",
    description=(
        "Auto-link already-uploaded documents (DocumentoFuente) to pendiente checklist rows for "
        "this cuenta, using category/type matching. Only touches pendiente rows — manual or "
        "SECOP-detected links are never overwritten. If the cuenta has not resolved the "
        "requisitos gate yet (requisitos_modo unset), does nothing and returns "
        "requisitos_definidos=false with vinculados=0 instead of materializing the standard "
        "checklist. Args: cuenta_id (UUID of the cuenta de cobro; must belong to the "
        "authenticated user)."
    ),
    input_model=AutoVincularDocumentosInput,
    output_model=AutoVincularDocumentosOutput,
    tags=("write",),
)
async def auto_vincular_documentos(
    ctx: ToolContext, params: AutoVincularDocumentosInput
) -> AutoVincularDocumentosOutput:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)

    # Mirrors resumen_checklist's early return (and app/api/v1/checklist.py's
    # `_checklist_no_definido`): never materialize the standard checklist just
    # because auto-link was requested before the user chose a requisitos_modo.
    if cuenta.requisitos_modo is None:
        return AutoVincularDocumentosOutput(vinculados=0, requisitos_definidos=False)

    await checklist_service.asegurar_checklist(ctx.db, cuenta)
    vinculados = await checklist_service.auto_vincular_documentos_fuente(ctx.db, cuenta)
    return AutoVincularDocumentosOutput(vinculados=vinculados)


class MarcarRequisitoInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id owning the checklist row.")
    codigo: str = Field(
        description=(
            "Requisito reference: a standard catalog code (e.g. 'RUT', 'DS_CONSECUTIVO') or the "
            "requisito_cuenta_id UUID (as a string) for a custom requisito."
        )
    )
    modo: Literal["no_aplica", "cumplido_manual", "desvincular"] = Field(
        description=(
            "no_aplica: marks the requisito as not applicable for this cuenta. cumplido_manual: "
            "marks it fulfilled without an attached document. desvincular: removes ALL documents "
            "linked to this row and resets it to pendiente. Exactly one per call — observaciones "
            "is a separate optional field, NOT a fourth modo."
        )
    )
    observaciones: str | None = Field(
        default=None,
        description="Optional free-text note to set/update on the row, combinable with any modo.",
    )


@tool(
    name="marcar_requisito",
    description=(
        "Manually set the state of one checklist requisito for a cuenta de cobro — the agent-tool "
        "twin of `PATCH /checklist/{codigo}`'s no_aplica/cumplido_manual/desvincular actions. Use "
        "this when the user tells you a requisito doesn't apply, was already fulfilled without a "
        "file, or should be unlinked from its current document(s). Only allowed while the cuenta "
        "is BORRADOR or RECHAZADA — rejects with a validation error on any other estado (e.g. "
        "ENVIADA), since the checklist must not change after submission. Args: cuenta_id (UUID of "
        "the cuenta de cobro; must belong to the authenticated user); codigo (catalog code or "
        "custom requisito_cuenta_id); modo ('no_aplica'/'cumplido_manual'/'desvincular', exactly "
        "one per call); observaciones (optional free-text note, combinable with any modo — not a "
        "fourth modo value)."
    ),
    input_model=MarcarRequisitoInput,
    output_model=RequisitoChecklistItem,
    tags=("write",),
)
async def marcar_requisito(ctx: ToolContext, params: MarcarRequisitoInput) -> RequisitoChecklistItem:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)

    if cuenta.estado not in _ESTADOS_CHECKLIST_EDITABLE:
        raise ValidationError(
            f"No se puede modificar el checklist de una cuenta de cobro en estado '{cuenta.estado.value}'. "
            "Solo se permite en borrador o rechazada."
        )

    if params.modo == "no_aplica":
        await checklist_service.marcar_no_aplica(ctx.db, cuenta.id, params.codigo)
    elif params.modo == "cumplido_manual":
        await checklist_service.marcar_cumplido_manual(ctx.db, cuenta.id, params.codigo)
    else:
        await checklist_service.desvincular(ctx.db, cuenta.id, params.codigo)

    if params.observaciones is not None:
        await checklist_service.set_observaciones(ctx.db, cuenta.id, params.codigo, params.observaciones)

    # Rebuild and return only this requisito's item — mirrors the HTTP endpoint's
    # own response shaping (app/api/v1/checklist.py::actualizar_requisito).
    # auto_vincular=False: the user just made an explicit choice; don't override it.
    payload = await checklist_service.construir_checklist_completo(ctx.db, cuenta, auto_vincular=False)
    item = next(
        (
            i
            for i in payload["items"]
            if i["requisito"]["codigo"] == params.codigo
            or str(i["requisito"].get("requisito_cuenta_id")) == params.codigo
        ),
        None,
    )
    if item is None:
        raise ValidationError(f"Requisito {params.codigo} not found in checklist.")
    return RequisitoChecklistItem(**item)
