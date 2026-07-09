"""Evidence persist service — turns discovered evidence into real DB rows.

`evidence_discovery_service.descubrir_evidencias` only *proposes* justifications
and evidence links, it never writes to the database. This module is the write
path: given the `ObligacionJustificada` list the discovery agent produced, it
upserts one `Actividad` per obligación (linked by `obligacion_id`) and creates
one link-type `Evidencia` per `EvidenceLink`, so `cobertura_service` picks them
up and the obligación stops being SIN_EVIDENCIA.

Idempotent: re-persisting the same discovery result does not duplicate rows,
and never clobbers a justificación the user already wrote by hand.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion
from app.schemas.google_workspace import EvidencePersistSummary, ObligacionJustificada

logger = structlog.get_logger("service.evidence_persist")

_NOMBRE_ARCHIVO_MAX_LEN = 255


def _parse_obligacion_id(value: str) -> uuid.UUID | None:
    """Parse an obligación id if it's a real UUID; return None for placeholder ids.

    `ObligacionJustificada.obligacion_id` may be a placeholder index (e.g. "0")
    when the discovery request sent free-form obligaciones instead of loading
    them from a contrato_id — those can't be linked to a real Obligacion row.
    """
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


async def _verify_cuenta_owned(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> uuid.UUID:
    """Verify the cuenta belongs to the authenticated user, without leaking existence.

    Selects only the id/contrato_id columns (not the mapped CuentaCobro entity).
    Loading the full entity would instantiate its `actividades` relationship,
    which uses `lazy="selectin"` — a mapper-level default that eagerly (and
    immediately) caches the collection in the session's identity map. Since
    this check runs before any Actividad is created, that would cache a stale
    empty collection and any later `cobertura_service` call in the *same*
    session/request would keep seeing it as empty even after we add rows.

    Returns the cuenta's `contrato_id`, used to validate obligacion ownership.
    """
    result = await db.execute(
        select(CuentaCobro.id, CuentaCobro.contrato_id)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            CuentaCobro.id == cuenta_id,
            CuentaCobro.deleted_at.is_(None),
            Contrato.usuario_id == usuario_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    return row.contrato_id


async def _obligacion_ids_del_contrato(db: AsyncSession, contrato_id: uuid.UUID) -> set[uuid.UUID]:
    """Fetch the set of obligacion ids that legitimately belong to a contrato.

    Single query (no N+1 round-trips to the DB — remote-Postgres round-trips
    are the dominant cost in this codebase, see `perf_db_round_trips`).
    """
    result = await db.execute(select(Obligacion.id).where(Obligacion.contrato_id == contrato_id))
    return {ob_id for (ob_id,) in result.all()}


async def _find_actividad(
    db: AsyncSession, cuenta_id: uuid.UUID, obligacion_id: uuid.UUID
) -> Actividad | None:
    result = await db.execute(
        select(Actividad).where(
            Actividad.cuenta_cobro_id == cuenta_id,
            Actividad.obligacion_id == obligacion_id,
        )
    )
    return result.scalar_one_or_none()


async def _existing_evidencia_urls(db: AsyncSession, actividad_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(Evidencia.url).where(Evidencia.actividad_id == actividad_id, Evidencia.url.is_not(None))
    )
    return {url for (url,) in result.all() if url}


async def persistir_evidencias(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    obligaciones: list[ObligacionJustificada],
) -> EvidencePersistSummary:
    """Persist discovered justificaciones/evidencias for a CuentaCobro.

    For each obligación entry: upsert one Actividad (by cuenta_id + obligacion_id).
    An existing Actividad's justificación is only filled in if it was empty —
    user-written text is never overwritten. Each EvidenceLink becomes a
    link-type Evidencia (storage_key=None, url set); re-persisting the same
    link on the same actividad is a no-op (deduped by url).

    Security: every `obligacion_id` referencing a real UUID must belong to the
    cuenta's own contrato. Without this check a malicious client could pass an
    obligacion_id from ANY other contrato (their own or another user's) and
    have an Actividad created against it on this cuenta. Invalid entries reject
    the whole request (not silently skipped) so client bugs surface instead of
    being hidden.
    """
    contrato_id = await _verify_cuenta_owned(db, usuario_id, cuenta_id)
    obligaciones_validas = await _obligacion_ids_del_contrato(db, contrato_id)

    for ob in obligaciones:
        obligacion_uuid = _parse_obligacion_id(ob.obligacion_id)
        if obligacion_uuid is not None and obligacion_uuid not in obligaciones_validas:
            raise ValidationError(
                f"La obligación '{ob.obligacion_id}' no pertenece al contrato de esta cuenta de cobro."
            )

    actividades_creadas = 0
    actividades_actualizadas = 0
    evidencias_creadas = 0
    evidencias_omitidas = 0

    for ob in obligaciones:
        obligacion_uuid = _parse_obligacion_id(ob.obligacion_id)

        actividad: Actividad | None = None
        if obligacion_uuid is not None:
            actividad = await _find_actividad(db, cuenta_id, obligacion_uuid)

        if actividad is None:
            actividad = Actividad(
                cuenta_cobro_id=cuenta_id,
                obligacion_id=obligacion_uuid,
                descripcion=ob.descripcion or ob.justificacion,
                justificacion=ob.justificacion,
            )
            db.add(actividad)
            await db.flush()
            actividades_creadas += 1
        elif ob.justificacion.strip() and not (actividad.justificacion or "").strip():
            actividad.justificacion = ob.justificacion
            actividades_actualizadas += 1

        existing_urls = await _existing_evidencia_urls(db, actividad.id)

        for link in ob.evidencias:
            if link.link in existing_urls:
                evidencias_omitidas += 1
                continue
            evidencia = Evidencia(
                actividad_id=actividad.id,
                fuente=link.source,
                url=link.link,
                nombre_archivo=(link.titulo or link.link)[:_NOMBRE_ARCHIVO_MAX_LEN],
                storage_key=None,
                tipo_archivo=None,
                tamano_bytes=None,
            )
            db.add(evidencia)
            existing_urls.add(link.link)
            evidencias_creadas += 1

    await db.commit()

    await logger.ainfo(
        "evidencias_persistidas",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        actividades_creadas=actividades_creadas,
        actividades_actualizadas=actividades_actualizadas,
        evidencias_creadas=evidencias_creadas,
        evidencias_omitidas=evidencias_omitidas,
    )

    return EvidencePersistSummary(
        actividades_creadas=actividades_creadas,
        actividades_actualizadas=actividades_actualizadas,
        evidencias_creadas=evidencias_creadas,
        evidencias_omitidas=evidencias_omitidas,
    )
