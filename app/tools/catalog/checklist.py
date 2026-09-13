"""Tool wrappers over `checklist_service` — required-document checklist per cuenta.

Every handler here first resolves the `CuentaCobro` via
`cuenta_cobro_service._get_cuenta_con_ownership`, which is the same ownership
gate the API routers use (`app/api/v1/checklist.py`) — a user can only ever
inspect or mutate the checklist of their own cuentas de cobro.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.checklist import ChecklistResponse
from app.services import checklist_service, cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool


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
