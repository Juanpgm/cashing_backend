"""Evidencia service — upload, list and manage evidence files for actividades."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.port import StoragePort
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.file_validation import validate_file_extension, validate_file_size, validate_mime_type
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
        raise ValidationError(f"Tipo MIME no coincide con la extensión del archivo.")

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
    """Return a presigned download URL for an evidence file."""
    result = await db.execute(
        select(Evidencia).where(Evidencia.id == evidencia_id)
    )
    evidencia = result.scalar_one_or_none()
    if evidencia is None:
        raise NotFoundError("Evidencia", str(evidencia_id))

    # Verify ownership
    await _get_actividad_owned(db, evidencia.actividad_id, usuario_id)

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

    try:
        await storage.delete(key=evidencia.storage_key)
    except Exception:
        logger.warning("storage_delete_failed", key=evidencia.storage_key)

    await db.delete(evidencia)
    await db.commit()
    logger.info("evidencia_deleted", id=str(evidencia_id))
