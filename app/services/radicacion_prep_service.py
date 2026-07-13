"""Radicación-prep orchestration (billing-resilience-templates, slice #7).

`preparar_radicacion()` runs, IN ORDER: checklist verification, coherence
validation, and packaging. Any HARD coherence finding halts BEFORE packaging
(`COHERENCE_CHECK_FAILED`, surfaced with its findings); the packager's own
gates (`SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE`) propagate unchanged
— this orchestrator introduces NO new error codes (spec: "reuses the codes it
orchestrates").

Never mutates `cuenta.estado` — that stays `cuenta_cobro_service.
radicar_cuenta`'s sole responsibility (the actual state transition). This is a
pre-flight readiness check + package build a caller (agent/API) can run BEFORE
actually submitting, collapsing what would otherwise be three separate calls
(checklist check, coherence check, packaging) into one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import get_storage as _get_storage
from app.core.config import settings
from app.core.exceptions import CHECKLIST_INCOMPLETE, COHERENCE_CHECK_FAILED, ValidationError
from app.services import checklist_service, coherence_validator_service, cuenta_cobro_service, informe_service
from app.services.coherence_validator_service import Finding, Severity

logger = structlog.get_logger("service.radicacion_prep")


@dataclass(frozen=True)
class RadicacionPrepResultado:
    """Result of a successful `preparar_radicacion` run."""

    cuenta_cobro_id: uuid.UUID
    storage_key: str
    filename: str
    size_bytes: int
    listo_para_radicar: bool
    pendientes: int
    advertencias_coherencia: list[Finding]
    # Machine-readable draft flag (task 7.5d) — mirrors `informe_service.ES_BORRADOR`
    # so a caller sees draft status without parsing any generated DOCX.
    es_borrador: bool


async def preparar_radicacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> RadicacionPrepResultado:
    """Orchestrate checklist -> coherence -> packager, in that exact order.

    1. Checklist: `checklist_service.construir_checklist_completo` (the same
       idempotent build `radicar_cuenta`'s own gate uses). Raises
       `ValidationError(code=CHECKLIST_INCOMPLETE)` when mandatory requisitos
       are still pending.
    2. Coherence: `coherence_validator_service.validar_coherencia`. Any HARD
       finding halts BEFORE packaging is ever attempted, raising
       `ValidationError(code=COHERENCE_CHECK_FAILED)` (mirrors
       `cuenta_cobro_service.radicar_cuenta`'s own gate). SOFT findings never
       block — they are returned as `advertencias_coherencia`.
    3. Packaging: `informe_service.generar_zip_evidencias(modo="final")` —
       raises `PACKAGE_PENDIENTE` when obligaciones are incomplete,
       `SECRET_DETECTED_IN_PACKAGE` on a scan hit (Reconciliation Note 2,
       tasks.md). Both propagate unchanged. On success the package is
       uploaded to storage (mirrors the `generar_paquete_evidencias` tool)
       and its location returned alongside the LISTO/PENDIENTE split.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    # 1. Checklist — runs FIRST.
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    resumen = payload["resumen"]
    if not resumen["radicacion_lista"]:
        pendientes_desc = ", ".join(resumen["lista_pendientes"]) or "requisitos pendientes por definir"
        raise ValidationError(
            f"No se puede preparar la radicación: faltan requisitos del checklist. Pendientes: {pendientes_desc}.",
            code=CHECKLIST_INCOMPLETE,
        )

    # 2. Coherence — any HARD finding halts BEFORE packaging is attempted.
    findings = await coherence_validator_service.validar_coherencia(db, usuario_id, cuenta_id)
    hallazgos_duros = [f for f in findings if f.severity == Severity.HARD]
    if hallazgos_duros:
        detalles = "; ".join(f"[{f.rule_id}] {f.mensaje}" for f in hallazgos_duros)
        await logger.awarning(
            "preparar_radicacion.coherencia_bloqueo",
            cuenta_id=str(cuenta_id),
            usuario_id=str(usuario_id),
            hallazgos_duros=len(hallazgos_duros),
        )
        raise ValidationError(
            f"No se puede preparar la radicación: se encontraron inconsistencias. {detalles}",
            code=COHERENCE_CHECK_FAILED,
        )
    advertencias = [f for f in findings if f.severity == Severity.SOFT]

    # 3. Packaging — modo="final": raises PACKAGE_PENDIENTE / SECRET_DETECTED_IN_PACKAGE
    # unchanged; only reached once both prior gates pass.
    contenido, filename = await informe_service.generar_zip_evidencias(db, usuario_id, cuenta_id, modo="final")
    estado = await informe_service.obtener_estado_listo_pendiente(db, usuario_id, cuenta_id)

    storage = _get_storage(settings.S3_BUCKET_EVIDENCIAS)
    storage_key = f"paquetes/{usuario_id}/{cuenta_id}/{filename}"
    await storage.upload(key=storage_key, data=contenido, content_type="application/zip")

    await logger.ainfo(
        "preparar_radicacion_ok",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        advertencias=len(advertencias),
        pendientes=estado.pendientes,
    )

    return RadicacionPrepResultado(
        cuenta_cobro_id=cuenta_id,
        storage_key=storage_key,
        filename=filename,
        size_bytes=len(contenido),
        listo_para_radicar=estado.listo_para_radicar,
        pendientes=estado.pendientes,
        advertencias_coherencia=advertencias,
        es_borrador=informe_service.ES_BORRADOR,
    )
