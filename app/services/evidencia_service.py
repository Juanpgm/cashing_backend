"""Evidencia service — upload, list and manage evidence files for actividades."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.port import StoragePort
from app.core.exceptions import NotFoundError, ValidationError
from app.core.file_validation import (
    sanitize_filename,
    validate_evidence_file,
    validate_file_extension,
    validate_file_size,
    validate_mime_type,
)
from app.models.actividad import Actividad
from app.models.evidencia import Evidencia
from app.schemas.evidencia import EvidenciaPresignedResponse, EvidenciaResponse, EvidenciaUploadResponse

logger = structlog.get_logger("service.evidencia")

_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB


async def _get_actividad_owned(
    db: AsyncSession, actividad_id: uuid.UUID, usuario_id: uuid.UUID
) -> Actividad:
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

    await _get_actividad_owned(db, actividad_id, usuario_id)

    key = f"evidencias/{usuario_id}/{actividad_id}/{uuid.uuid4()}_{filename}"
    await storage.upload(key=key, data=data, content_type=content_type)

    evidencia = Evidencia(
        actividad_id=actividad_id,
        storage_key=key,
        nombre_archivo=filename,
        tipo_archivo=content_type,
        tamano_bytes=len(data),
    )
    db.add(evidencia)
    await db.commit()
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

    await _get_actividad_owned(db, actividad_id, usuario_id)

    resultados: list[EvidenciaUploadResponse] = []
    for filename, content_type, data in archivos:
        # Sanitize before building the storage key — the filename itself is
        # attacker-controlled and evidence allows arbitrary extensions/characters.
        safe_filename = sanitize_filename(filename)
        key = f"evidencias/{usuario_id}/{actividad_id}/{uuid.uuid4()}_{safe_filename}"
        await storage.upload(key=key, data=data, content_type=content_type)

        evidencia = Evidencia(
            actividad_id=actividad_id,
            storage_key=key,
            nombre_archivo=filename,
            tipo_archivo=content_type,
            tamano_bytes=len(data),
        )
        db.add(evidencia)
        await db.commit()
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


async def listar_evidencias(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    actividad_id: uuid.UUID,
) -> list[EvidenciaResponse]:
    await _get_actividad_owned(db, actividad_id, usuario_id)
    result = await db.execute(
        select(Evidencia)
        .where(Evidencia.actividad_id == actividad_id)
        .order_by(Evidencia.created_at.asc())
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
    result = await db.execute(
        select(Evidencia).where(Evidencia.id == evidencia_id)
    )
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
