"""Tool wrappers over the evidence discovery/persist services.

`descubrir_evidencias` only *proposes* evidence + justificaciones (Gmail/Drive/
Calendar, read-only against Google); `persistir_evidencias` is the write path
that turns a discovery result into real Actividad/Evidencia rows. They are
separate tools because the discovery result is meant to be reviewed (by a human
or an agent) before being persisted — see `evidence_persist_service` docstring.

`subir_evidencias_desde_chat` is a THIRD, independent path: it bridges chat
attachments (`ToolContext.attachments`, same mechanism as `importar_documento`)
into `evidencia_service.subir_evidencias_cuenta` — the same pipeline
`POST /cuentas-cobro/{id}/evidencias/subir` uses — for users who attach files
directly in chat instead of connecting Gmail/Drive/Calendar.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.adapters.storage import get_storage as _get_storage
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.schemas.evidencia import EvidenciasCuentaSubidaResponse
from app.schemas.google_workspace import (
    EvidenceDiscoveryRequest,
    EvidenceDiscoveryResponse,
    EvidencePersistSummary,
    ObligacionJustificada,
)
from app.services import evidence_discovery_service, evidence_persist_service, evidencia_service
from app.tools.context import ToolContext
from app.tools.registry import tool


@tool(
    name="descubrir_evidencias",
    description=(
        "Explore Gmail, Drive, and Calendar for evidence supporting a set of contractual "
        "obligaciones (either sent directly or loaded from a contrato_id) and generate a "
        "justificación per obligación with supporting links. Read-only against the DB — this "
        "does not create Actividad/Evidencia rows, it only proposes them (call "
        "persistir_evidencias to write them). Requires the user's Google account to be "
        "connected. Args: see EvidenceDiscoveryRequest (obligaciones or contrato_id, "
        "fecha_inicio, fecha_fin, optional supervisor_email/entidad hints)."
    ),
    input_model=EvidenceDiscoveryRequest,
    output_model=EvidenceDiscoveryResponse,
    tags=("read",),
    consumes_credits=settings.CREDITS_PER_EVIDENCE_COLLECTION,
)
async def descubrir_evidencias(ctx: ToolContext, params: EvidenceDiscoveryRequest) -> EvidenceDiscoveryResponse:
    return await evidence_discovery_service.descubrir_evidencias(ctx.db, ctx.usuario_id, params, refresh=params.refresh)


class PersistirEvidenciasInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to attach the persisted activities/evidence to.")
    obligaciones: list[ObligacionJustificada] = Field(
        default_factory=list,
        description="Justified obligaciones as returned by descubrir_evidencias.obligaciones.",
    )


@tool(
    name="persistir_evidencias",
    description=(
        "Persist a descubrir_evidencias result into real DB rows: upserts one Actividad per "
        "obligación (never overwriting a justificación the user already wrote by hand) and "
        "creates one link-type Evidencia per evidence link found. Idempotent — re-persisting "
        "the same result does not duplicate rows. Args: cuenta_id (UUID of the cuenta de cobro; "
        "must belong to the authenticated user); obligaciones (the justified obligaciones list "
        "from descubrir_evidencias)."
    ),
    input_model=PersistirEvidenciasInput,
    output_model=EvidencePersistSummary,
    tags=("write",),
)
async def persistir_evidencias(ctx: ToolContext, params: PersistirEvidenciasInput) -> EvidencePersistSummary:
    return await evidence_persist_service.persistir_evidencias(
        ctx.db, ctx.usuario_id, params.cuenta_id, params.obligaciones
    )


class SubirEvidenciasDesdeChatInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to attach the uploaded evidence to.")
    filenames: list[str] = Field(
        description=(
            "Names of files the user attached in this chat turn, exactly as attached (must match "
            "an attachment from this turn — never invent a filename)."
        )
    )


@tool(
    name="subir_evidencias_desde_chat",
    description=(
        "Sube uno o varios archivos adjuntados por el usuario en ESTE turno del chat como "
        "evidencia de una cuenta de cobro (antes de que existan actividades). Cada archivo se "
        "clasifica automáticamente contra las obligaciones del contrato y se adjunta a una "
        "actividad. Usa esta herramienta cuando el usuario adjunte soportes directamente en el "
        "chat; si en cambio no adjuntó nada pero tiene Gmail/Drive/Calendar conectado, preferí "
        "descubrir_evidencias + persistir_evidencias. Los 'filenames' deben coincidir exactamente "
        "con los nombres de archivos adjuntados en este turno — no inventes nombres. Args: "
        "cuenta_id (UUID de la cuenta de cobro; debe pertenecer al usuario autenticado); "
        "filenames (lista de nombres de archivos adjuntados en este turno)."
    ),
    input_model=SubirEvidenciasDesdeChatInput,
    output_model=EvidenciasCuentaSubidaResponse,
    tags=("write", "chat_only"),
)
async def subir_evidencias_desde_chat(
    ctx: ToolContext, params: SubirEvidenciasDesdeChatInput
) -> EvidenciasCuentaSubidaResponse:
    archivos: list[tuple[str, str, bytes]] = []
    for filename in params.filenames:
        attachment = ctx.attachments.get(filename)
        if attachment is None:
            raise NotFoundError("Attachment", filename)
        archivos.append((attachment.filename, attachment.content_type, attachment.data))

    storage = _get_storage(settings.S3_BUCKET_PDFS)
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=ctx.db,
        storage=storage,
        usuario_id=ctx.usuario_id,
        cuenta_id=params.cuenta_id,
        archivos=archivos,
        background_tasks=None,
    )
    return EvidenciasCuentaSubidaResponse(resultados=resultados, avisos=resultados.avisos)
