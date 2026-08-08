"""Evidence auto service — P1 one-action fused evidencias flow.

Collapses discovery → persistencia → justificación (previously 2-3 manual clicks
across `evidencias-tab.tsx`'s "Descubrir evidencias"/"Guardar todo" and
`step-6-justificacion.tsx`'s "Generar justificaciones") into one backend call.

Idempotency guards this orchestrator adds on top of the reused services:
- Evidence rows: `evidence_persist_service.persistir_evidencias` already dedupes by
  URL — re-running never duplicates `Evidencia` rows (unchanged, reused as-is).
- Actividad/justificación text: the manual flow intentionally REPLACES text on every
  regeneration (a user clicking "Generar" again wants a fresh draft). A repeat
  AUTO-fire must NOT do that — it would silently discard a justificación the user
  never asked to regenerate. Guarded by `_cuenta_tiene_justificacion`: once the
  cuenta has at least one real (non-sentinel) justificación, later auto calls still
  persist newly discovered evidence links but skip writing new actividad/
  justificación text for that cuenta.
- Empty discovery and "no provider connected" both resolve to a clean no-op
  (`omitido=True`, no persist call) — neither is an error; the manual buttons
  remain the fallback.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExternalServiceError
from app.models.actividad import Actividad
from app.schemas.google_workspace import EvidenceDiscoveryRequest, PaqueteEvidenciasAutoResponse
from app.services import evidence_discovery_service, evidence_persist_service
from app.services.informe_constants import SENTINEL_SIN_EVIDENCIAS

logger = structlog.get_logger("services.evidence_auto")


async def _cuenta_tiene_justificacion(db: AsyncSession, cuenta_id: uuid.UUID) -> bool:
    """True once the cuenta has at least one real (non-sentinel) justificación.

    `SENTINEL_SIN_EVIDENCIAS` (the deterministic "no evidence found" text) never
    counts — a cuenta that only has sentinel text should still get a real
    justificación written the next time evidence turns up.
    """
    result = await db.execute(
        select(Actividad.id)
        .where(
            Actividad.cuenta_cobro_id == cuenta_id,
            Actividad.justificacion.is_not(None),
            Actividad.justificacion != "",
            Actividad.justificacion != SENTINEL_SIN_EVIDENCIAS,
        )
        .limit(1)
    )
    return result.first() is not None


async def auto_evidencias(
    db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID
) -> PaqueteEvidenciasAutoResponse:
    """One call: discovery → persistencia → justificación for a CuentaCobro.

    Ownership of `cuenta_id` is verified by the reused services (discovery via
    `_resolve_contrato_id`, persist via `_verify_cuenta_owned`) — no duplicate
    check needed here.
    """
    req = EvidenceDiscoveryRequest(cuenta_id=cuenta_id)
    try:
        discovery = await evidence_discovery_service.descubrir_evidencias(db, usuario_id, req)
    except ExternalServiceError:
        # No Google/Microsoft connected: nothing to auto-discover. Not an error —
        # the auto-fire runs without explicit user action and must not surface a
        # hard failure; the manual buttons stay available to connect + retry.
        await logger.ainfo("auto_evidencias_sin_proveedor", cuenta_id=str(cuenta_id))
        return PaqueteEvidenciasAutoResponse(descubiertas=0, persistidas=0, justificadas=0, omitido=True)

    if discovery.total_evidencias == 0:
        return PaqueteEvidenciasAutoResponse(descubiertas=0, persistidas=0, justificadas=0, omitido=True)

    obligaciones = discovery.obligaciones
    if await _cuenta_tiene_justificacion(db, cuenta_id):
        # Non-destructive repeat: keep evidence links (still worth linking), drop
        # actividad/justificación text so persistir_evidencias' regenerate branch
        # (`elif ob.justificacion.strip() or ob.actividad.strip()`) never fires.
        obligaciones = [ob.model_copy(update={"actividad": "", "justificacion": ""}) for ob in obligaciones]

    persisted = await evidence_persist_service.persistir_evidencias(db, usuario_id, cuenta_id, obligaciones)
    justificadas = persisted.actividades_creadas + persisted.actividades_actualizadas

    await logger.ainfo(
        "auto_evidencias_done",
        cuenta_id=str(cuenta_id),
        descubiertas=discovery.total_evidencias,
        persistidas=persisted.evidencias_creadas,
        justificadas=justificadas,
    )

    return PaqueteEvidenciasAutoResponse(
        descubiertas=discovery.total_evidencias,
        persistidas=persisted.evidencias_creadas,
        justificadas=justificadas,
        omitido=False,
    )
