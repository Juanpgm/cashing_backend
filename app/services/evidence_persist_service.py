"""Evidence persist service — turns discovered evidence into real DB rows.

`evidence_discovery_service.descubrir_evidencias` only *proposes* justifications
and evidence links, it never writes to the database. This module is the write
path: given the `ObligacionJustificada` list the discovery agent produced, it
upserts one `Actividad` per obligación (linked by `obligacion_id`) and creates
one `Evidencia` per `EvidenceLink`, plus a CONFIRMED `EvidenciaObligacion`
row for the same (evidencia, obligación) pair — that link table is the
classification source of truth `cobertura_service` reads from, so the
obligación stops being SIN_EVIDENCIA.

Each `Evidencia` keeps `url` as a reference to the discovered item, but when the
link carries provider file ids (email `message_id`+`attachment_id`, or drive
`file_id` — Problema A / `EvidenceLink.file_id` etc.) the real bytes are
downloaded via `EmailPort.get_attachment`/`DrivePort.download_file` and stored
in `get_evidencia_storage()`, so `storage_key` is set and the evidence packages
real files (`informe_service.generar_zip_evidencias`), not just a link. Generic
web links (no provider file id) stay link-only, same as before. Download
failures are fail-open — logged and skipped, never abort the whole persist call.

Idempotent on rows: re-persisting the same discovery result does not duplicate
Actividad/Evidencia rows. Text is REPLACED on regeneration: a new discovery run
overwrites the existing descripcion/justificacion of the matched Actividad.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.file_validation import sanitize_filename
from app.models.actividad import Actividad, JustificacionOrigen
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import ConfianzaEnlace, EstadoEnlace, EvidenciaObligacion, FuenteEnlace
from app.models.obligacion import Obligacion
from app.schemas.google_workspace import EvidenceLink, EvidencePersistSummary, ObligacionJustificada

logger = structlog.get_logger("service.evidence_persist")

_NOMBRE_ARCHIVO_MAX_LEN = 255


async def _descargar_bytes_link(db: AsyncSession, usuario_id: uuid.UUID, link: EvidenceLink) -> bytes | None:
    """Download the real file behind a discovered `EvidenceLink`, or `None`.

    `None` means either "this link has no provider file id" (a generic web
    link — nothing to fetch) or "the download failed" (logged, fail-open: one
    bad attachment must not sink the whole persist call). `link.provider`
    selects Google vs. Microsoft — mirrors the adapter selection already used
    in `evidence_discovery_service`/`drive_fetch_node`.
    """
    from app.models.integracion import IntegrationProvider

    es_microsoft = link.provider == IntegrationProvider.MICROSOFT.value
    try:
        if link.source == "email" and link.message_id and link.attachment_id:
            from app.adapters.email.gmail_adapter import GmailAdapter
            from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

            email_adapter = MicrosoftGraphAdapter(db) if es_microsoft else GmailAdapter(db)
            return await email_adapter.get_attachment(usuario_id, link.message_id, link.attachment_id)
        if link.source == "drive" and link.file_id:
            from app.adapters.drive.drive_adapter import DriveAdapter
            from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

            drive_adapter = MicrosoftGraphAdapter(db) if es_microsoft else DriveAdapter(db)
            return await drive_adapter.download_file(usuario_id, link.file_id)
    except Exception as exc:
        await logger.awarning(
            "evidencia_link_download_failed", source=link.source, link=link.link[:200], error=str(exc)
        )
    return None


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


async def _find_actividad(db: AsyncSession, cuenta_id: uuid.UUID, obligacion_id: uuid.UUID) -> Actividad | None:
    """Find the Actividad to attach evidence to for this cuenta + obligación.

    An obligación can legitimately have MORE THAN ONE Actividad on the same cuenta
    (e.g. `/cruzar` creates one Actividad per matched document candidate) — using
    `scalar_one_or_none()` here raised `MultipleResultsFound` in that case (a latent
    500). Deterministic tie-break instead: prefer the Actividad still awaiting a
    justificación (empty), else the most recently created one.
    """
    result = await db.execute(
        select(Actividad)
        .where(
            Actividad.cuenta_cobro_id == cuenta_id,
            Actividad.obligacion_id == obligacion_id,
        )
        .order_by(Actividad.created_at.desc())
    )
    rows = list(result.scalars().all())
    if not rows:
        return None
    sin_justificacion = [a for a in rows if not (a.justificacion or "").strip()]
    return sin_justificacion[0] if sin_justificacion else rows[0]


def _descripcion_fallback(ob: ObligacionJustificada) -> str:
    """Deterministic `descripcion` fallback — NEVER the obligación text, NEVER the
    justificación. Used only when the discovery agent didn't produce an `actividad`
    (e.g. an older client posting a payload without that field)."""
    if ob.evidencias:
        titulos = ", ".join(e.titulo for e in ob.evidencias[:3])
        return f"Actividades del período soportadas en {len(ob.evidencias)} evidencia(s): {titulos}."
    return "Actividad registrada sin evidencia asociada en el período consultado."


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
    An existing Actividad's descripcion/justificacion are REPLACED when the new
    discovery produced text (regeneration = the user wants a fresh draft); an
    empty generation never blanks existing text. Each EvidenceLink becomes an
    Evidencia (url always set); when the link carries a provider file id its
    bytes are snapshotted into storage too (`storage_key` set) — see
    `_descargar_bytes_link`. Re-persisting the same link on the same actividad
    is a no-op (deduped by url).

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
    evidencias_con_archivo = 0
    evidencias_descarga_fallida = 0

    for ob in obligaciones:
        obligacion_uuid = _parse_obligacion_id(ob.obligacion_id)

        actividad: Actividad | None = None
        if obligacion_uuid is not None:
            actividad = await _find_actividad(db, cuenta_id, obligacion_uuid)

        if actividad is None:
            actividad = Actividad(
                cuenta_cobro_id=cuenta_id,
                obligacion_id=obligacion_uuid,
                descripcion=ob.actividad.strip() or _descripcion_fallback(ob),
                justificacion=ob.justificacion,
                justificacion_origen=ob.origen or JustificacionOrigen.SEED.value,
            )
            db.add(actividad)
            await db.flush()
            actividades_creadas += 1
        elif ob.justificacion.strip() or ob.actividad.strip():
            # Regenerar REEMPLAZA el texto existente (pedido de producto): quien
            # vuelve a dar "Generar" quiere descartar la redacción anterior.
            # Solo se pisa cuando la nueva generación trae texto real, Y el seed
            # nunca pisa una justificación real ya persistida (llm/manual) — B3.
            incoming_origen = ob.origen or JustificacionOrigen.SEED.value
            existing_origen = actividad.justificacion_origen or JustificacionOrigen.SEED.value
            existing_texto = (actividad.justificacion or "").strip()
            puede_reemplazar = (
                incoming_origen in (JustificacionOrigen.LLM.value, JustificacionOrigen.MANUAL.value)
                or existing_origen in (JustificacionOrigen.SEED.value, JustificacionOrigen.SIN_LABORES.value)
                or not existing_texto
            )
            if puede_reemplazar:
                actividad.descripcion = ob.actividad.strip() or _descripcion_fallback(ob)
                actividad.justificacion = ob.justificacion
                actividad.justificacion_origen = incoming_origen
                actividades_actualizadas += 1
            else:
                await logger.ainfo(
                    "evidencias_persistidas_seed_no_pisa_real",
                    actividad_id=str(actividad.id),
                    cuenta_id=str(cuenta_id),
                )

        existing_urls = await _existing_evidencia_urls(db, actividad.id)

        for link in ob.evidencias:
            if link.link in existing_urls:
                evidencias_omitidas += 1
                continue

            storage_key: str | None = None
            tipo_archivo: str | None = None
            tamano_bytes: int | None = None
            tiene_archivo_provider = bool(
                (link.source == "email" and link.attachment_id) or (link.source == "drive" and link.file_id)
            )
            if tiene_archivo_provider:
                contenido = await _descargar_bytes_link(db, usuario_id, link)
                if contenido is not None:
                    from app.services.informe_service import get_evidencia_storage

                    safe_filename = sanitize_filename(link.titulo or link.link)
                    key = f"evidencias/{usuario_id}/{actividad.id}/{uuid.uuid4()}_{safe_filename}"
                    try:
                        storage = get_evidencia_storage()
                        await storage.upload(  # type: ignore[attr-defined]
                            key=key, data=contenido, content_type=link.mime_type or "application/octet-stream"
                        )
                        storage_key = key
                        tipo_archivo = link.mime_type or None
                        tamano_bytes = len(contenido)
                        evidencias_con_archivo += 1
                    except Exception as exc:
                        await logger.awarning("evidencia_link_upload_failed", link=link.link[:200], error=str(exc))
                        evidencias_descarga_fallida += 1
                else:
                    evidencias_descarga_fallida += 1

            evidencia = Evidencia(
                id=uuid.uuid4(),
                actividad_id=actividad.id,
                fuente=link.source,
                url=link.link,
                nombre_archivo=(link.titulo or link.link)[:_NOMBRE_ARCHIVO_MAX_LEN],
                storage_key=storage_key,
                tipo_archivo=tipo_archivo,
                tamano_bytes=tamano_bytes,
            )
            db.add(evidencia)
            existing_urls.add(link.link)
            evidencias_creadas += 1

            # evidencia_obligacion is the classification source of truth
            # (cobertura_service counts CONFIRMED links only) - the discovery
            # matcher already decided this evidencia belongs to `obligacion_uuid`,
            # so persisting it here IS a confirmed classification. Without this,
            # obligaciones covered only via Google-discovery stay SIN_EVIDENCIA.
            if obligacion_uuid is not None:
                db.add(
                    EvidenciaObligacion(
                        evidencia_id=evidencia.id,
                        obligacion_id=obligacion_uuid,
                        confianza=ConfianzaEnlace.ALTA.value,
                        status=EstadoEnlace.CONFIRMED.value,
                        source=FuenteEnlace.AI.value,
                        reasoning="Evidencia descubierta y vinculada automáticamente por el agente de descubrimiento.",
                    )
                )

    await db.commit()

    await logger.ainfo(
        "evidencias_persistidas",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        actividades_creadas=actividades_creadas,
        actividades_actualizadas=actividades_actualizadas,
        evidencias_creadas=evidencias_creadas,
        evidencias_omitidas=evidencias_omitidas,
        evidencias_con_archivo=evidencias_con_archivo,
        evidencias_descarga_fallida=evidencias_descarga_fallida,
    )

    return EvidencePersistSummary(
        actividades_creadas=actividades_creadas,
        actividades_actualizadas=actividades_actualizadas,
        evidencias_creadas=evidencias_creadas,
        evidencias_omitidas=evidencias_omitidas,
    )
