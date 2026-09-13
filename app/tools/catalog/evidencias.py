"""Tool wrappers over the evidence discovery/persist services.

`descubrir_evidencias` only *proposes* evidence + justificaciones (Gmail/Drive/
Calendar, read-only against Google); `persistir_evidencias` is the write path
that turns a discovery result into real Actividad/Evidencia rows. They are
separate tools because the discovery result is meant to be reviewed (by a human
or an agent) before being persisted — see `evidence_persist_service` docstring.

Discovery-handle bridge (radicacion-sin-friccion 1.7): `descubrir_evidencias`
is the SHARED handler behind `invoke_tool("descubrir_evidencias", ...)` — the
agent chat loop, `POST /integraciones/evidencias/descubrir` (`response_model=
EvidenceDiscoveryResponse`), and the `/mcp` evidence server all dispatch
through this exact function, so its return type can never change shape (REST's
`response_model` and the MCP server's raw JSON passthrough both depend on the
FULL `EvidenceDiscoveryResponse`). Instead, this handler ADDITIVELY stashes its
own result in `evidence_handle_cache` and returns a response whose `handle_id`
field is populated — every consumer keeps getting the full payload it already
expects, and `persistir_evidencias` can redeem the SAME payload from just
`cuenta_id` + `handle_id` without the calling LLM re-emitting it. The
LLM-facing token savings come from `agent_chat_service._compact_descubrir_evidencias`
(the chat loop's compact serializer), which sends the model the summary +
handle_id instead of the full JSON dump.

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
)
from app.services import evidence_discovery_service, evidence_handle_cache, evidence_persist_service, evidencia_service
from app.tools.context import ToolContext
from app.tools.registry import tool


@tool(
    name="descubrir_evidencias",
    description=(
        "Explore Gmail, Drive, and Calendar for evidence supporting a set of contractual "
        "obligaciones (either sent directly or loaded from a contrato_id) and generate a "
        "justificación per obligación with supporting links. Read-only against the DB — this "
        "does not create Actividad/Evidencia rows, it only proposes them. The response's "
        "handle_id lets you call persistir_evidencias(cuenta_id, handle_id) to write them "
        "WITHOUT re-sending the obligaciones/evidencias — never copy that payload into your "
        "persistir_evidencias call, just pass the handle_id back. Requires the user's Google "
        "account to be connected. Args: see EvidenceDiscoveryRequest (obligaciones or "
        "contrato_id, fecha_inicio, fecha_fin, optional supervisor_email/entidad hints)."
    ),
    input_model=EvidenceDiscoveryRequest,
    output_model=EvidenceDiscoveryResponse,
    tags=("read",),
    consumes_credits=settings.CREDITS_PER_EVIDENCE_COLLECTION,
)
async def descubrir_evidencias(ctx: ToolContext, params: EvidenceDiscoveryRequest) -> EvidenceDiscoveryResponse:
    response = await evidence_discovery_service.descubrir_evidencias(
        ctx.db, ctx.usuario_id, params, refresh=params.refresh
    )
    # `response` may be the SAME object `discovery_cache` has stored for a prior
    # call (see evidence_discovery_service's discovery-result cache) — mutating
    # it in place would leak this call's handle_id into that cached instance.
    # `model_copy` returns a fresh instance, leaving the cache untouched.
    handle_id = evidence_handle_cache.store(ctx.usuario_id, params.cuenta_id, response.obligaciones)
    return response.model_copy(update={"handle_id": handle_id})


class PersistirEvidenciasInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to attach the persisted activities/evidence to.")
    handle_id: str = Field(
        description=(
            "The handle_id from a prior descubrir_evidencias call's response — NEVER "
            "re-type or re-send the obligaciones/evidencias list itself, only this handle."
        )
    )


@tool(
    name="persistir_evidencias",
    description=(
        "Persist a descubrir_evidencias result into real DB rows: upserts one Actividad per "
        "obligación (never overwriting a justificación the user already wrote by hand) and "
        "creates one link-type Evidencia per evidence link found. Idempotent — re-persisting "
        "the same result does not duplicate rows, so redeeming the same handle_id twice (e.g. "
        "after an ambiguous network response) is always safe. Args: cuenta_id (UUID of the "
        "cuenta de cobro; must belong to the authenticated user); handle_id (from the "
        "descubrir_evidencias response you want to persist — expires a short while after "
        "discovery; if it's no longer valid, call descubrir_evidencias again and use the new "
        "handle_id, never invent one)."
    ),
    input_model=PersistirEvidenciasInput,
    output_model=EvidencePersistSummary,
    tags=("write",),
)
async def persistir_evidencias(ctx: ToolContext, params: PersistirEvidenciasInput) -> EvidencePersistSummary:
    obligaciones = evidence_handle_cache.redeem(ctx.usuario_id, params.cuenta_id, params.handle_id)
    return await evidence_persist_service.persistir_evidencias(ctx.db, ctx.usuario_id, params.cuenta_id, obligaciones)


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
