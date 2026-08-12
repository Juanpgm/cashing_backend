"""Evidencia service — upload, list and manage evidence files for actividades."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import structlog
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm import get_llm
from app.adapters.storage.port import StoragePort
from app.agent.nodes.evidence_matcher import clasificar_evidencia, confidence_bucket, evidence_matcher_node
from app.agent.state import AgentState
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.core.file_validation import (
    sanitize_filename,
    validate_evidence_file,
    validate_file_extension,
    validate_file_size,
    validate_mime_type,
)
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion, FuenteEnlace
from app.models.obligacion import Obligacion
from app.schemas.evidencia import (
    EvidenciaClasificadaResponse,
    EvidenciaPresignedResponse,
    EvidenciaResponse,
    EvidenciaUploadResponse,
)
from app.services import cuenta_cobro_service

logger = structlog.get_logger("service.evidencia")

_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
_TEXTO_EXTRAIDO_MAX_CHARS = 20_000
_PLACEHOLDER_DESCRIPCION = "Pendiente de redactar — evidencia adjunta: {nombre_archivo}"


async def _extraer_texto_seguro(filename: str, data: bytes) -> str:
    """Best-effort text extraction for an uploaded evidence file.

    Reuses the document_service ladder (native text → OCR) so the justification
    LLM can ground on the file's content. Extraction failure NEVER breaks the
    upload — it stores ``""`` so the row is marked as attempted (NULL means
    never-attempted, which is what the discovery backfill keys on).

    ponytail: failed extractions are not retried (stored as ""); add a
    retry/error marker column if retries ever matter.

    Passes ``relaxed_ocr=True``: scanned evidence (a photographed government
    form, a screenshot) commonly recovers concatenated OCR text that fails
    ``document_service``'s strict spacing heuristic — that heuristic exists to
    escalate the CONTRATO parsing flow to the vision model, not to discard
    otherwise-classifiable evidence text.
    """
    from app.services.document_service import extraer_texto_documento

    try:
        texto, _avisos = await extraer_texto_documento(data, filename, relaxed_ocr=True)
    except Exception as exc:
        logger.warning("evidencia_extraccion_failed", filename=filename, error=str(exc))
        return ""
    if not texto or not texto.strip():
        return ""
    return texto.strip()[:_TEXTO_EXTRAIDO_MAX_CHARS]


async def _get_actividad_owned(db: AsyncSession, actividad_id: uuid.UUID, usuario_id: uuid.UUID) -> Actividad:
    """Verify that actividad belongs to the authenticated user (via cuenta_cobro → contrato)."""
    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro

    result = await db.execute(
        select(Actividad)
        .join(CuentaCobro, Actividad.cuenta_cobro_id == CuentaCobro.id)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Actividad.id == actividad_id,
            Contrato.usuario_id == usuario_id,
        )
    )
    a = result.scalar_one_or_none()
    if a is None:
        raise NotFoundError("Actividad", str(actividad_id))
    return a


def _enlazar_si_actividad_tiene_obligacion(db: AsyncSession, evidencia: Evidencia, actividad: Actividad) -> None:
    """A user manually attaching a file to an actividad that already targets a
    specific obligación IS a user classification, not an AI suggestion — write
    a CONFIRMED/user `EvidenciaObligacion` link so `cobertura_service` (which
    counts CONFIRMED links exclusively) sees this evidence. `confianza` is an
    AI-confidence field and stays NULL for user-sourced links. No-op when the
    actividad has no `obligacion_id` (e.g. the default free-form actividad).
    """
    if actividad.obligacion_id is None:
        return
    db.add(
        EvidenciaObligacion(
            evidencia_id=evidencia.id,
            obligacion_id=actividad.obligacion_id,
            confianza=None,
            status=EstadoEnlace.CONFIRMED.value,
            source=FuenteEnlace.USER.value,
        )
    )


async def _buscar_evidencia_duplicada_actividad(
    db: AsyncSession, actividad_id: uuid.UUID, sha256: str
) -> Evidencia | None:
    """Find an existing stored (storage_key set) Evidencia with the same
    content hash under the same actividad — re-uploading identical bytes must
    not create a second row or storage object (Req 10a)."""
    result = await db.execute(
        select(Evidencia).where(
            Evidencia.actividad_id == actividad_id,
            Evidencia.sha256 == sha256,
            Evidencia.storage_key.isnot(None),
        )
    )
    return result.scalars().first()


async def _buscar_evidencia_duplicada_cuenta(db: AsyncSession, cuenta_id: uuid.UUID, sha256: str) -> Evidencia | None:
    """Same dedup as `_buscar_evidencia_duplicada_actividad` but scoped to a
    CuentaCobro (subir_evidencias_cuenta has no actividad_id yet at call time —
    evidence can land on any stub actividad under the cuenta)."""
    result = await db.execute(
        select(Evidencia)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(
            Actividad.cuenta_cobro_id == cuenta_id,
            Evidencia.sha256 == sha256,
            Evidencia.storage_key.isnot(None),
        )
    )
    return result.scalars().first()


async def _commit_or_limpiar_storage(db: AsyncSession, storage: StoragePort, key: str) -> None:
    """Commit the pending Evidencia insert; if the commit itself raises,
    best-effort delete the just-uploaded storage object so a failed commit
    never leaves an orphaned S3-compatible object behind, then roll back and
    re-raise. Upload-then-insert ordering is kept (not swapped to
    insert-then-upload): a committed row pointing at a missing object is worse
    than a transient orphan cleaned up here.
    """
    try:
        await db.commit()
    except Exception:
        try:
            await storage.delete(key=key)
        except Exception:
            logger.warning("evidencia_orphan_storage_cleanup_failed", key=key)
        await db.rollback()
        raise


async def subir_evidencia(
    db: AsyncSession,
    storage: StoragePort,
    usuario_id: uuid.UUID,
    actividad_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
) -> EvidenciaUploadResponse:
    """Validate, upload to S3-compatible storage and persist Evidencia record."""
    if not validate_file_extension(filename):
        raise ValidationError(f"Tipo de archivo no permitido: {filename}")
    if not validate_file_size(len(data)):
        raise ValidationError(f"Archivo demasiado grande ({len(data)} bytes). Máximo 10MB.")
    if not validate_mime_type(data, content_type):
        raise ValidationError("Tipo MIME no coincide con la extensión del archivo.")

    actividad = await _get_actividad_owned(db, actividad_id, usuario_id)

    sha256 = hashlib.sha256(data).hexdigest()
    duplicada = await _buscar_evidencia_duplicada_actividad(db, actividad_id, sha256)
    if duplicada is not None:
        logger.info("evidencia_duplicada_detectada", id=str(duplicada.id), actividad_id=str(actividad_id))
        try:
            presigned = await storage.presigned_url(key=duplicada.storage_key, expires_in=3600)  # type: ignore[arg-type]
        except Exception:
            presigned = None
        return EvidenciaUploadResponse(
            id=duplicada.id,
            actividad_id=duplicada.actividad_id,
            storage_key=duplicada.storage_key,
            nombre_archivo=duplicada.nombre_archivo,
            tipo_archivo=duplicada.tipo_archivo,
            tamano_bytes=duplicada.tamano_bytes,
            presigned_url=presigned,
            created_at=duplicada.created_at,
            duplicada=True,
        )

    key = f"evidencias/{usuario_id}/{actividad_id}/{uuid.uuid4()}_{filename}"
    await storage.upload(key=key, data=data, content_type=content_type)

    evidencia = Evidencia(
        id=uuid.uuid4(),
        actividad_id=actividad_id,
        storage_key=key,
        nombre_archivo=filename,
        tipo_archivo=content_type,
        tamano_bytes=len(data),
        texto_extraido=await _extraer_texto_seguro(filename, data),
        sha256=sha256,
    )
    db.add(evidencia)
    _enlazar_si_actividad_tiene_obligacion(db, evidencia, actividad)
    await _commit_or_limpiar_storage(db, storage, key)
    await db.refresh(evidencia)

    try:
        presigned = await storage.presigned_url(key=key, expires_in=3600)
    except Exception:
        presigned = None

    logger.info(
        "evidencia_uploaded",
        id=str(evidencia.id),
        actividad_id=str(actividad_id),
        filename=filename,
        size=len(data),
    )
    return EvidenciaUploadResponse(
        id=evidencia.id,
        actividad_id=evidencia.actividad_id,
        storage_key=evidencia.storage_key,
        nombre_archivo=evidencia.nombre_archivo,
        tipo_archivo=evidencia.tipo_archivo,
        tamano_bytes=evidencia.tamano_bytes,
        presigned_url=presigned,
        created_at=evidencia.created_at,
    )


async def subir_evidencias(
    db: AsyncSession,
    storage: StoragePort,
    usuario_id: uuid.UUID,
    actividad_id: uuid.UUID,
    archivos: list[tuple[str, str, bytes]],
) -> list[EvidenciaUploadResponse]:
    """Validate and upload MULTIPLE evidence files (any format) for an actividad.

    `archivos` is a list of (filename, content_type, data) tuples — one entry per
    uploaded file, in request order. Unlike `subir_evidencia` (single file, strict
    document allowlist), this uses the permissive evidence validation
    (`validate_evidence_file`): any format is accepted except a small blocklist of
    executables/scripts, since evidence is photos/videos/emails/etc. of any shape.

    ALL files are validated up front before ANY of them is stored or persisted —
    if one file in the batch is invalid, the whole request is rejected (422) and
    nothing is written, so the caller never ends up with a half-uploaded batch.
    """
    if not archivos:
        raise ValidationError("Debe incluir al menos un archivo.")

    for filename, content_type, data in archivos:
        validate_evidence_file(filename=filename, size=len(data), content_type=content_type, content=data)

    actividad = await _get_actividad_owned(db, actividad_id, usuario_id)

    resultados: list[EvidenciaUploadResponse] = []
    for filename, content_type, data in archivos:
        sha256 = hashlib.sha256(data).hexdigest()
        duplicada = await _buscar_evidencia_duplicada_actividad(db, actividad_id, sha256)
        if duplicada is not None:
            # Also covers within-batch duplicates: each iteration commits
            # before the next runs, so a repeat hash later in this SAME
            # request already finds the row this loop just inserted.
            logger.info("evidencia_duplicada_detectada", id=str(duplicada.id), actividad_id=str(actividad_id))
            try:
                presigned = await storage.presigned_url(
                    key=duplicada.storage_key,  # type: ignore[arg-type]
                    expires_in=3600,
                )
            except Exception:
                presigned = None
            resultados.append(
                EvidenciaUploadResponse(
                    id=duplicada.id,
                    actividad_id=duplicada.actividad_id,
                    storage_key=duplicada.storage_key,
                    nombre_archivo=duplicada.nombre_archivo,
                    tipo_archivo=duplicada.tipo_archivo,
                    tamano_bytes=duplicada.tamano_bytes,
                    presigned_url=presigned,
                    created_at=duplicada.created_at,
                    duplicada=True,
                )
            )
            continue

        # Sanitize before building the storage key — the filename itself is
        # attacker-controlled and evidence allows arbitrary extensions/characters.
        safe_filename = sanitize_filename(filename)
        key = f"evidencias/{usuario_id}/{actividad_id}/{uuid.uuid4()}_{safe_filename}"
        try:
            await storage.upload(key=key, data=data, content_type=content_type)
        except Exception as exc:
            # Never leak raw adapter errors (e.g. boto's NoSuchBucket) to the client —
            # same convention as other external-dependency failures (Google/Wompi/SECOP).
            # The raw exception text is logged server-side only; the client gets a
            # safe, generic message with no adapter internals in it.
            logger.error(
                "evidencia_storage_upload_failed",
                key=key,
                actividad_id=str(actividad_id),
                usuario_id=str(usuario_id),
                error=str(exc),
            )
            raise ExternalServiceError(
                "Storage", "No se pudo guardar el archivo de evidencia. Intentá de nuevo más tarde."
            ) from exc

        evidencia = Evidencia(
            id=uuid.uuid4(),
            actividad_id=actividad_id,
            storage_key=key,
            nombre_archivo=filename,
            tipo_archivo=content_type,
            tamano_bytes=len(data),
            texto_extraido=await _extraer_texto_seguro(filename, data),
            sha256=sha256,
        )
        db.add(evidencia)
        _enlazar_si_actividad_tiene_obligacion(db, evidencia, actividad)
        await _commit_or_limpiar_storage(db, storage, key)
        await db.refresh(evidencia)

        try:
            presigned = await storage.presigned_url(key=key, expires_in=3600)
        except Exception:
            presigned = None

        logger.info(
            "evidencia_uploaded",
            id=str(evidencia.id),
            actividad_id=str(actividad_id),
            filename=filename,
            size=len(data),
        )
        resultados.append(
            EvidenciaUploadResponse(
                id=evidencia.id,
                actividad_id=evidencia.actividad_id,
                storage_key=evidencia.storage_key,
                nombre_archivo=evidencia.nombre_archivo,
                tipo_archivo=evidencia.tipo_archivo,
                tamano_bytes=evidencia.tamano_bytes,
                presigned_url=presigned,
                created_at=evidencia.created_at,
            )
        )
    return resultados


async def _find_or_create_actividad_stub(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    obligacion_id: uuid.UUID | None,
    nombre_archivo: str,
) -> Actividad:
    """Reuse an existing Actividad for this (cuenta, obligación) pair, or create a
    lightweight placeholder one so an Evidencia has somewhere to attach to before
    any real Actividad exists (Evidencias step now runs before Justificaciones).

    Reuses WHATEVER Actividad already matches — a prior stub, or a real one with
    user/agent-written content — and NEVER touches its `descripcion`, so a second
    upload for the same obligación never clobbers real content with the placeholder.
    `obligacion_id=None` shares a single "sin clasificar" stub per cuenta.
    """
    stmt = select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id)
    stmt = stmt.where(
        Actividad.obligacion_id.is_(None) if obligacion_id is None else Actividad.obligacion_id == obligacion_id
    )
    result = await db.execute(stmt.limit(1))
    actividad = result.scalars().first()
    if actividad is not None:
        return actividad

    actividad = Actividad(
        cuenta_cobro_id=cuenta_id,
        obligacion_id=obligacion_id,
        descripcion=_PLACEHOLDER_DESCRIPCION.format(nombre_archivo=nombre_archivo),
    )
    db.add(actividad)
    await db.flush()
    await db.refresh(actividad)
    return actividad


async def _avanzar_progreso(db: AsyncSession, job: ClasificacionEvidenciasJob | None) -> None:
    """Increment the background job's per-file progress counter as each
    evidencia finishes — the staleness/progress signal that distinguishes a
    slow-but-alive run from a stuck one (RES-002: `EVIDENCE_JOB_STALE_SECONDS`
    reads `job.updated_at`, which this commit refreshes). No-op when called
    outside a background job (`job=None`, e.g. the synchronous test/direct-
    caller path)."""
    if job is None:
        return
    job.procesadas += 1
    await db.commit()


async def _clasificar_y_enlazar_lote(
    db: AsyncSession,
    obligaciones: list[Obligacion],
    evidencias: list[Evidencia],
    job: ClasificacionEvidenciasJob | None = None,
) -> None:
    """Batch-match freshly uploaded evidencias against the contrato's obligaciones
    (embeddings + keyword blend, at most ONE LLM call per obligación via
    `evidence_matcher_node`) and persist `EvidenciaObligacion` links.

    Every evidencia resolves to either >=1 confidence-bucketed link or an
    explicit "no obligación applies" link with reasoning — never silently
    unclassified (evidence-classification-pipeline: Every evidence resolves to
    a suggestion or explicit non-match). Independent of, and additive to, the
    single-best-guess `clasificar_evidencia` call already used above to pick
    each file's stub Actividad — this is the new multi-obligación source of
    truth (the `evidencia_obligacion` link table).

    `job` (optional): when provided (the background-job path), `job.procesadas`
    is incremented after each evidencia finishes — see `_avanzar_progreso`.
    """
    con_texto = [ev for ev in evidencias if (ev.texto_extraido or "").strip()]
    sin_texto = [ev for ev in evidencias if not (ev.texto_extraido or "").strip()]
    for ev in sin_texto:
        await marcar_evidencia_sin_obligacion(db, ev.id, "Sin texto extraído para clasificar.")
        await _avanzar_progreso(db, job)

    if not obligaciones or not con_texto:
        for ev in con_texto:
            await marcar_evidencia_sin_obligacion(db, ev.id, "El contrato no tiene obligaciones registradas.")
            await _avanzar_progreso(db, job)
        return

    evidence_raw: list[dict[str, Any]] = [
        {"id": str(ev.id), "content": (ev.texto_extraido or "")[:2000]} for ev in con_texto
    ]
    obligaciones_extraidas: list[dict[str, str | int]] = [
        {"id": str(ob.id), "descripcion": ob.descripcion} for ob in obligaciones
    ]
    state: AgentState = {"obligaciones_extraidas": obligaciones_extraidas, "evidence_raw": evidence_raw}
    result = await evidence_matcher_node(state)
    matched: dict[str, list[dict[str, Any]]] = result.get("matched_evidence") or {}
    scores: dict[str, dict[str, float]] = result.get("matched_evidence_scores") or {}

    sugerencias_por_evidencia: dict[str, list[dict[str, Any]]] = {ev["id"]: [] for ev in evidence_raw}
    for ob_id, evs in matched.items():
        for ev_dict in evs:
            ev_id = ev_dict["id"]
            score = scores.get(ob_id, {}).get(ev_id, 0.0)
            sugerencias_por_evidencia[ev_id].append(
                {"obligacion_id": uuid.UUID(ob_id), "confianza": confidence_bucket(score), "score": score}
            )

    for ev_id, sugerencias in sugerencias_por_evidencia.items():
        if sugerencias:
            await guardar_enlaces_evidencia(db, uuid.UUID(ev_id), sugerencias)
        else:
            await marcar_evidencia_sin_obligacion(
                db, uuid.UUID(ev_id), "Ninguna obligación coincide con el contenido de esta evidencia."
            )
        await _avanzar_progreso(db, job)


async def subir_evidencias_cuenta(
    db: AsyncSession,
    storage: StoragePort,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    archivos: list[tuple[str, str, bytes]],
    background_tasks: BackgroundTasks | None = None,
) -> list[EvidenciaClasificadaResponse]:
    """Upload evidence files scoped to a CuentaCobro, BEFORE any Actividad exists.

    The Evidencias step now runs before Justificaciones, so there is no
    `actividad_id` to upload against yet (unlike `subir_evidencias`, per-actividad).
    Each file's extracted text is classified against the contract's obligaciones
    (`clasificar_evidencia`) and attached to a find-or-create "stub" Actividad for
    the matched obligación — or a single shared unclassified stub
    (`obligacion_id=None`) when nothing matches confidently. Zero schema changes:
    this reuses the existing `Evidencia.actividad_id` FK and `Actividad.obligacion_id`
    (already nullable).

    ALL files are validated up front (same all-or-nothing contract as
    `subir_evidencias`) before anything is written.

    `background_tasks` (evidence-classification-jobs: Fast upload response):
    when provided (the real request path, wired from the API route), the
    multi-obligación link-table classification (`_clasificar_y_enlazar_lote`)
    is ENQUEUED via `evidence_classification_service.encolar_clasificacion`
    instead of running inline, so the HTTP response returns before it
    completes. `None` (the default — used by callers/tests that don't care
    about the background aspect) preserves the prior synchronous behavior.
    """
    if not archivos:
        raise ValidationError("Debe incluir al menos un archivo.")

    for filename, content_type, data in archivos:
        validate_evidence_file(filename=filename, size=len(data), content_type=content_type, content=data)

    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    obligaciones = list(cuenta.contrato.obligaciones)

    # ONE cached LLM instance for the whole batch — `clasificar_evidencia` only
    # calls it when a file has 2+ keyword-candidate obligaciones.
    llm = get_llm(model="groq/llama-3.1-8b-instant") if obligaciones else None

    # Cache found/created Actividad stubs within this batch call, keyed by
    # obligacion_id (None = unclassified sentinel) — avoids duplicate rows/queries
    # when multiple files in the same request match the same obligación.
    actividad_cache: dict[uuid.UUID | None, Actividad] = {}

    resultados: list[EvidenciaClasificadaResponse] = []
    evidencias_creadas: list[Evidencia] = []
    for filename, content_type, data in archivos:
        sha256 = hashlib.sha256(data).hexdigest()
        duplicada = await _buscar_evidencia_duplicada_cuenta(db, cuenta_id, sha256)
        if duplicada is not None:
            # Also covers within-batch duplicates: each iteration commits
            # before the next runs, so a repeat hash later in this SAME
            # request already finds the row this loop just inserted.
            logger.info("evidencia_duplicada_detectada", id=str(duplicada.id), cuenta_id=str(cuenta_id))
            actividad_dup = await db.get(Actividad, duplicada.actividad_id)
            ob_id_dup = actividad_dup.obligacion_id if actividad_dup is not None else None
            etiqueta_dup = next((ob.etiqueta or None for ob in obligaciones if ob.id == ob_id_dup), None)
            try:
                presigned = await storage.presigned_url(
                    key=duplicada.storage_key,  # type: ignore[arg-type]
                    expires_in=3600,
                )
            except Exception:
                presigned = None
            resultados.append(
                EvidenciaClasificadaResponse(
                    id=duplicada.id,
                    actividad_id=duplicada.actividad_id,
                    obligacion_id=ob_id_dup,
                    obligacion_etiqueta=etiqueta_dup,
                    nombre_archivo=duplicada.nombre_archivo,
                    tipo_archivo=duplicada.tipo_archivo,
                    tamano_bytes=duplicada.tamano_bytes,
                    presigned_url=presigned,
                    clasificado=ob_id_dup is not None,
                    created_at=duplicada.created_at,
                    duplicada=True,
                )
            )
            continue

        texto = await _extraer_texto_seguro(filename, data)
        matched_ob = await clasificar_evidencia(texto, obligaciones, llm=llm) if texto and obligaciones else None
        ob_id = matched_ob.id if matched_ob is not None else None

        actividad = actividad_cache.get(ob_id)
        if actividad is None:
            actividad = await _find_or_create_actividad_stub(db, cuenta_id, ob_id, filename)
            actividad_cache[ob_id] = actividad

        safe_filename = sanitize_filename(filename)
        key = f"evidencias/{usuario_id}/{actividad.id}/{uuid.uuid4()}_{safe_filename}"
        await storage.upload(key=key, data=data, content_type=content_type)

        evidencia = Evidencia(
            actividad_id=actividad.id,
            storage_key=key,
            nombre_archivo=filename,
            tipo_archivo=content_type,
            tamano_bytes=len(data),
            texto_extraido=texto,
            sha256=sha256,
        )
        db.add(evidencia)
        await _commit_or_limpiar_storage(db, storage, key)
        await db.refresh(evidencia)
        evidencias_creadas.append(evidencia)

        try:
            presigned = await storage.presigned_url(key=key, expires_in=3600)
        except Exception:
            presigned = None

        logger.info(
            "evidencia_clasificada",
            id=str(evidencia.id),
            obligacion_id=str(ob_id) if ob_id else None,
            filename=filename,
        )
        resultados.append(
            EvidenciaClasificadaResponse(
                id=evidencia.id,
                actividad_id=actividad.id,
                obligacion_id=ob_id,
                obligacion_etiqueta=(matched_ob.etiqueta or None) if matched_ob is not None else None,
                nombre_archivo=evidencia.nombre_archivo,
                tipo_archivo=evidencia.tipo_archivo,
                tamano_bytes=evidencia.tamano_bytes,
                presigned_url=presigned,
                clasificado=matched_ob is not None,
                created_at=evidencia.created_at,
            )
        )

    # Multi-obligación batched classification (evidencia_obligacion link table) —
    # additive to, and independent of, the single-best-guess stub assignment above
    # (evidence-classification-pipeline: Every evidence resolves to a suggestion
    # or explicit non-match). Backgrounded when a real request provides
    # `background_tasks` (evidence-classification-jobs: Fast upload response);
    # otherwise runs inline for backward-compatible direct/test callers.
    if background_tasks is not None:
        from app.services import evidence_classification_service

        try:
            await evidence_classification_service.encolar_clasificacion(db, background_tasks, cuenta_id)
        except Exception as exc:
            # RES-003: the upload itself already succeeded (files committed
            # above) — an enqueue failure (e.g. a job-row unique-constraint
            # race between two concurrent uploads) must NOT turn a successful
            # upload into a 500. The user can still trigger classification
            # later via the retry button (RES-002).
            logger.warning("clasificacion_enqueue_failed", cuenta_cobro_id=str(cuenta_id), error=str(exc))
    else:
        await _clasificar_y_enlazar_lote(db, obligaciones, evidencias_creadas)

    return resultados


async def listar_evidencias(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    actividad_id: uuid.UUID,
) -> list[EvidenciaResponse]:
    await _get_actividad_owned(db, actividad_id, usuario_id)
    result = await db.execute(
        select(Evidencia).where(Evidencia.actividad_id == actividad_id).order_by(Evidencia.created_at.asc())
    )
    return [EvidenciaResponse.model_validate(e) for e in result.scalars().all()]


async def obtener_url_descarga(
    db: AsyncSession,
    storage: StoragePort,
    usuario_id: uuid.UUID,
    evidencia_id: uuid.UUID,
) -> EvidenciaPresignedResponse:
    """Return a presigned download URL for an evidence file.

    Download safety (stored-XSS from any-format uploads, e.g. .html/.svg): the
    presigned URL returned here points DIRECTLY at the S3-compatible bucket
    (Cloudflare R2 in prod, MinIO in dev) — a different origin than this API/the
    frontend app. Even if a browser renders an uploaded .html/.svg inline when
    opened from that URL, it executes in the storage origin's security context,
    not ours: it has no access to the app's cookies, auth tokens, or localStorage.
    That cross-origin boundary is the actual mitigation here, not a
    Content-Disposition header — `StoragePort.presigned_url()` does not currently
    expose a way to force `attachment` (S3 `generate_presigned_url` supports
    `ResponseContentDisposition`, but wiring it through would mean widening the
    shared `StoragePort` protocol used by every other presigned-download call
    site in the app — cuenta_cobro PDFs, documentos, profile photos — for a risk
    this endpoint doesn't actually have). If evidence downloads are ever proxied
    through OUR own origin instead of a direct presigned URL, forcing
    `Content-Disposition: attachment` there becomes mandatory.
    """
    result = await db.execute(select(Evidencia).where(Evidencia.id == evidencia_id))
    evidencia = result.scalar_one_or_none()
    if evidencia is None:
        raise NotFoundError("Evidencia", str(evidencia_id))

    # Verify ownership
    await _get_actividad_owned(db, evidencia.actividad_id, usuario_id)

    if evidencia.storage_key is None:
        # Link evidence (Gmail/Drive/Calendar) — the url IS the destination, no
        # storage round-trip needed and nothing to actually "expire".
        return EvidenciaPresignedResponse(
            id=evidencia.id,
            nombre_archivo=evidencia.nombre_archivo,
            presigned_url=evidencia.url or "",
            expires_in_seconds=0,
        )

    presigned = await storage.presigned_url(key=evidencia.storage_key, expires_in=3600)
    return EvidenciaPresignedResponse(
        id=evidencia.id,
        nombre_archivo=evidencia.nombre_archivo,
        presigned_url=presigned,
        expires_in_seconds=3600,
    )


async def eliminar_evidencia(
    db: AsyncSession,
    storage: StoragePort,
    usuario_id: uuid.UUID,
    evidencia_id: uuid.UUID,
) -> None:
    result = await db.execute(select(Evidencia).where(Evidencia.id == evidencia_id))
    evidencia = result.scalar_one_or_none()
    if evidencia is None:
        raise NotFoundError("Evidencia", str(evidencia_id))

    await _get_actividad_owned(db, evidencia.actividad_id, usuario_id)

    if evidencia.storage_key is not None:
        try:
            await storage.delete(key=evidencia.storage_key)
        except Exception:
            logger.warning("storage_delete_failed", key=evidencia.storage_key)

    await db.delete(evidencia)
    await db.commit()
    logger.info("evidencia_deleted", id=str(evidencia_id))


async def guardar_enlaces_evidencia(
    db: AsyncSession,
    evidencia_id: uuid.UUID,
    sugerencias: list[dict[str, Any]],
) -> list[EvidenciaObligacion]:
    """Persist matcher suggestions as `evidencia_obligacion` link rows.

    `alta` confidence auto-confirms the link (status=confirmed, source=ai);
    `media`/`baja` persist as `proposed`, awaiting explicit user confirmation
    (evidence-obligation-links: Suggest-and-confirm auto-link threshold).

    Each item in `sugerencias` is a dict with `obligacion_id`, `confianza`, and
    optionally `score`/`reasoning`. Upserts by explicit SELECT on
    `(evidencia_id, obligacion_id)` — never via a lazy relationship, matching the
    `expire_on_commit=False` cache gotcha this module already guards against.
    """
    links: list[EvidenciaObligacion] = []
    for sugerencia in sugerencias:
        confianza = sugerencia["confianza"]
        obligacion_id = sugerencia["obligacion_id"]
        status = EstadoEnlace.CONFIRMED.value if confianza == "alta" else EstadoEnlace.PROPOSED.value

        result = await db.execute(
            select(EvidenciaObligacion).where(
                EvidenciaObligacion.evidencia_id == evidencia_id,
                EvidenciaObligacion.obligacion_id == obligacion_id,
            )
        )
        link = result.scalar_one_or_none()
        if link is None:
            link = EvidenciaObligacion(
                evidencia_id=evidencia_id,
                obligacion_id=obligacion_id,
                source=FuenteEnlace.AI.value,
            )
            db.add(link)
        link.confianza = confianza
        link.score = sugerencia.get("score")
        link.reasoning = sugerencia.get("reasoning")
        link.status = status
        links.append(link)

    await db.commit()
    for link in links:
        await db.refresh(link)

    logger.info("evidencia_enlaces_guardados", evidencia_id=str(evidencia_id), n_links=len(links))
    return links


async def marcar_evidencia_sin_obligacion(
    db: AsyncSession,
    evidencia_id: uuid.UUID,
    reasoning: str,
) -> EvidenciaObligacion:
    """Record "no obligación applies" for an evidence — distinct from zero links.

    Persisted as a single row with `obligacion_id=None`, `status=no_aplica`, and
    the reasoning explaining the non-match (evidence-obligation-links: "No
    obligación applies" state). Upserts by explicit SELECT, never a lazy
    relationship.
    """
    result = await db.execute(
        select(EvidenciaObligacion).where(
            EvidenciaObligacion.evidencia_id == evidencia_id,
            EvidenciaObligacion.obligacion_id.is_(None),
            EvidenciaObligacion.status == EstadoEnlace.NO_APLICA.value,
        )
    )
    link = result.scalar_one_or_none()
    if link is None:
        link = EvidenciaObligacion(
            evidencia_id=evidencia_id,
            obligacion_id=None,
            status=EstadoEnlace.NO_APLICA.value,
            source=FuenteEnlace.AI.value,
        )
        db.add(link)
    link.reasoning = reasoning
    await db.commit()
    await db.refresh(link)

    logger.info("evidencia_marcada_sin_obligacion", evidencia_id=str(evidencia_id))
    return link


async def _get_evidencia_de_cuenta(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    evidencia_id: uuid.UUID,
) -> tuple[Evidencia, uuid.UUID]:
    """Load an Evidencia scoped to `cuenta_id`, verifying the user owns the
    cuenta (via its contrato) — returns (evidencia, contrato_id) so callers
    can validate obligación ids without a second ownership round-trip."""
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    result = await db.execute(
        select(Evidencia)
        .join(Actividad, Actividad.id == Evidencia.actividad_id)
        .where(Evidencia.id == evidencia_id, Actividad.cuenta_cobro_id == cuenta_id)
    )
    evidencia = result.scalar_one_or_none()
    if evidencia is None:
        raise NotFoundError("Evidencia", str(evidencia_id))
    return evidencia, cuenta.contrato_id


async def reclasificar_evidencia(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    evidencia_id: uuid.UUID,
    *,
    add: list[uuid.UUID] | None = None,
    remove: list[uuid.UUID] | None = None,
    confirm: list[uuid.UUID] | None = None,
    no_aplica: bool = False,
) -> list[EvidenciaObligacion]:
    """Full user authority over one evidencia's obligación links
    (evidence-reclassification capability): add a link the AI missed, remove
    any existing link, confirm a pending suggestion, or mark the evidencia as
    having no applicable obligación — each independently, no approval gate.

    Validates the evidencia belongs to `cuenta_id` and every referenced
    obligación belongs to that cuenta's contrato before writing anything.
    Operations apply in a fixed order (remove -> add -> confirm -> no_aplica)
    so a caller combining `no_aplica=True` with add/confirm ends up with the
    "no obligación applies" state winning, matching its "clears any pending
    suggestions" contract.
    """
    add = add or []
    remove = remove or []
    confirm = confirm or []

    evidencia, contrato_id = await _get_evidencia_de_cuenta(db, usuario_id, cuenta_id, evidencia_id)

    referenciadas = {*add, *confirm}
    if referenciadas:
        ob_result = await db.execute(
            select(Obligacion.id).where(Obligacion.contrato_id == contrato_id, Obligacion.id.in_(referenciadas))
        )
        validas = {row[0] for row in ob_result.all()}
        faltantes = referenciadas - validas
        if faltantes:
            raise NotFoundError("Obligacion", ", ".join(str(i) for i in faltantes))

    async def _find_link(obligacion_id: uuid.UUID) -> EvidenciaObligacion | None:
        result = await db.execute(
            select(EvidenciaObligacion).where(
                EvidenciaObligacion.evidencia_id == evidencia.id,
                EvidenciaObligacion.obligacion_id == obligacion_id,
            )
        )
        return result.scalar_one_or_none()

    async def _limpiar_no_aplica() -> None:
        """Delete any `no_aplica` row(s) for this evidencia — a real link (from
        `add`/`confirm`) contradicts "no obligación applies" (REL-001: the two
        states must never coexist). Explicit SELECT/DELETE, never a lazy
        relationship, matching this module's `expire_on_commit=False` cache
        gotcha."""
        result = await db.execute(
            select(EvidenciaObligacion).where(
                EvidenciaObligacion.evidencia_id == evidencia.id,
                EvidenciaObligacion.obligacion_id.is_(None),
                EvidenciaObligacion.status == EstadoEnlace.NO_APLICA.value,
            )
        )
        for row in result.scalars().all():
            await db.delete(row)

    if remove:
        for obligacion_id in remove:
            link = await _find_link(obligacion_id)
            if link is not None:
                await db.delete(link)
        await db.commit()

    if add:
        for obligacion_id in add:
            link = await _find_link(obligacion_id)
            if link is None:
                link = EvidenciaObligacion(evidencia_id=evidencia.id, obligacion_id=obligacion_id)
                db.add(link)
            link.status = EstadoEnlace.CONFIRMED.value
            link.source = FuenteEnlace.USER.value
        await _limpiar_no_aplica()
        await db.commit()

    if confirm:
        for obligacion_id in confirm:
            link = await _find_link(obligacion_id)
            if link is None:
                raise NotFoundError("EvidenciaObligacion", str(obligacion_id))
            link.status = EstadoEnlace.CONFIRMED.value
        await _limpiar_no_aplica()
        await db.commit()

    if no_aplica:
        vigentes_result = await db.execute(
            select(EvidenciaObligacion).where(
                EvidenciaObligacion.evidencia_id == evidencia.id,
                EvidenciaObligacion.obligacion_id.isnot(None),
            )
        )
        for vigente in vigentes_result.scalars().all():
            await db.delete(vigente)
        await db.commit()
        await marcar_evidencia_sin_obligacion(db, evidencia.id, "Marcado manualmente por el usuario: no aplica.")

    final_result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == evidencia.id))
    logger.info(
        "evidencia_reclasificada",
        evidencia_id=str(evidencia.id),
        add=len(add),
        remove=len(remove),
        confirm=len(confirm),
        no_aplica=no_aplica,
    )
    return list(final_result.scalars().all())
