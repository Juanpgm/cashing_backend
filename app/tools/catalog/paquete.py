"""Tool wrappers over the hardened `informe_service` packager.

`generar_paquete_evidencias` builds the real ZIP (real bytes, secret-scanned,
LISTO/PENDIENTE gated) and persists it to storage — an agent/MCP caller can't
stream an HTTP file response, so this tool uploads the result instead of
returning raw bytes. Always packages in "standard" mode (Reconciliation Note 2,
tasks.md): "final" mode is reserved for the slice #7 radicación orchestrator,
which calls `informe_service.generar_zip_evidencias` directly.

`obtener_estado_listo_pendiente` is read-only and never produces a package.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from pydantic import BaseModel, Field

from app.adapters.storage import get_storage as _get_storage
from app.core.config import settings
from app.schemas.paquete import EstadoListoPendienteResponse, ObligacionEstadoOut, PaqueteGeneradoResponse
from app.services import informe_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class GenerarPaqueteEvidenciasInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to package.")


class ObtenerEstadoListoPendienteInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to check.")


@tool(
    name="generar_paquete_evidencias",
    description=(
        "Generate the hardened evidence ZIP package for a cuenta de cobro: fetches real "
        "document bytes from storage, arranges numbered per-obligación folders, runs a "
        "mandatory fail-closed secret scan (raises SECRET_DETECTED_IN_PACKAGE on any hit — "
        "no package emitted), computes a LISTO/PENDIENTE split, and persists the resulting "
        "zip to storage (creates a zip artifact, does not return raw bytes). Args: cuenta_id "
        "(UUID of the cuenta de cobro; must belong to the authenticated user)."
    ),
    input_model=GenerarPaqueteEvidenciasInput,
    output_model=PaqueteGeneradoResponse,
    tags=("write",),
)
async def generar_paquete_evidencias(
    ctx: ToolContext, params: GenerarPaqueteEvidenciasInput
) -> PaqueteGeneradoResponse:
    contenido, filename = await informe_service.generar_zip_evidencias(ctx.db, ctx.usuario_id, params.cuenta_id)
    estado = await informe_service.obtener_estado_listo_pendiente(ctx.db, ctx.usuario_id, params.cuenta_id)

    storage = _get_storage(settings.S3_BUCKET_EVIDENCIAS)
    storage_key = f"paquetes/{ctx.usuario_id}/{params.cuenta_id}/{filename}"
    await storage.upload(key=storage_key, data=contenido, content_type="application/zip")

    return PaqueteGeneradoResponse(
        cuenta_cobro_id=params.cuenta_id,
        storage_key=storage_key,
        filename=filename,
        size_bytes=len(contenido),
        pendientes=estado.pendientes,
        modo="standard",
    )


@tool(
    name="obtener_estado_listo_pendiente",
    description=(
        "Return the LISTO/PENDIENTE split for a cuenta de cobro's obligaciones WITHOUT "
        "producing a package — use this to preview readiness before calling "
        "generar_paquete_evidencias. Read-only. Args: cuenta_id (UUID of the cuenta de "
        "cobro; must belong to the authenticated user)."
    ),
    input_model=ObtenerEstadoListoPendienteInput,
    output_model=EstadoListoPendienteResponse,
    tags=("read",),
)
async def obtener_estado_listo_pendiente(
    ctx: ToolContext, params: ObtenerEstadoListoPendienteInput
) -> EstadoListoPendienteResponse:
    estado = await informe_service.obtener_estado_listo_pendiente(ctx.db, ctx.usuario_id, params.cuenta_id)
    return EstadoListoPendienteResponse(
        cuenta_cobro_id=estado.cuenta_cobro_id,
        obligaciones=[ObligacionEstadoOut(**asdict(o)) for o in estado.obligaciones],
        pendientes=estado.pendientes,
        listo_para_radicar=estado.listo_para_radicar,
    )
