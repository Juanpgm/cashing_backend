"""Tool wrappers over the evidence discovery/persist services.

`descubrir_evidencias` only *proposes* evidence + justificaciones (Gmail/Drive/
Calendar, read-only against Google); `persistir_evidencias` is the write path
that turns a discovery result into real Actividad/Evidencia rows. They are
separate tools because the discovery result is meant to be reviewed (by a human
or an agent) before being persisted — see `evidence_persist_service` docstring.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.google_workspace import (
    EvidenceDiscoveryRequest,
    EvidenceDiscoveryResponse,
    EvidencePersistSummary,
    ObligacionJustificada,
)
from app.services import evidence_discovery_service, evidence_persist_service
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
    return await evidence_discovery_service.descubrir_evidencias(ctx.db, ctx.usuario_id, params)


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
