"""Tool wrapper over `radicacion_prep_service.preparar_radicacion`
(billing-resilience-templates, slice #7).

Orchestrates checklist verification, coherence validation, and packaging in
that exact order — collapsing what would otherwise be three separate agent
tool calls (`obtener_estado_listo_pendiente`/checklist check,
`validar_coherencia_cuenta`, `generar_paquete_evidencias`) into one. Never
transitions `cuenta.estado`; the actual radicación submission still goes
through the existing `POST /cuentas-cobro/{id}/radicar` (`radicar_cuenta`).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from pydantic import BaseModel, Field

from app.schemas.coherence import FindingOut
from app.schemas.radicacion_prep import PreparaRadicacionResponse
from app.services import radicacion_prep_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class PreparaRadicacionInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to prepare for radicación.")


@tool(
    name="preparar_radicacion",
    description=(
        "Orchestrate, IN ORDER, checklist verification, coherence validation, and evidence "
        "packaging for a cuenta de cobro. Halts BEFORE packaging with CHECKLIST_INCOMPLETE if "
        "mandatory requisitos are pending, or with COHERENCE_CHECK_FAILED if the coherence "
        "validator reports any HARD finding (SOFT findings are returned as "
        "advertencias_coherencia, never block). Packaging then runs in 'final' mode: raises "
        "SECRET_DETECTED_IN_PACKAGE on a secret-scan hit, or PACKAGE_PENDIENTE if obligaciones "
        "are still missing evidence — no partial package emitted in either case. On success, "
        "uploads the evidence ZIP to storage and returns its location plus the LISTO/PENDIENTE "
        "status and draft flag. Never transitions the cuenta's estado — actual submission is a "
        "separate call to radicar_cuenta. Args: cuenta_id (UUID of the cuenta de cobro; must "
        "belong to the authenticated user)."
    ),
    input_model=PreparaRadicacionInput,
    output_model=PreparaRadicacionResponse,
    tags=("write",),
)
async def preparar_radicacion(ctx: ToolContext, params: PreparaRadicacionInput) -> PreparaRadicacionResponse:
    resultado = await radicacion_prep_service.preparar_radicacion(ctx.db, ctx.usuario_id, params.cuenta_id)
    return PreparaRadicacionResponse(
        cuenta_cobro_id=resultado.cuenta_cobro_id,
        storage_key=resultado.storage_key,
        filename=resultado.filename,
        size_bytes=resultado.size_bytes,
        listo_para_radicar=resultado.listo_para_radicar,
        pendientes=resultado.pendientes,
        advertencias_coherencia=[FindingOut(**asdict(f)) for f in resultado.advertencias_coherencia],
        es_borrador=resultado.es_borrador,
    )
