"""Evidencias API — upload, download and delete evidence files for actividades."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import MAX_EVIDENCE_FILES_PER_REQUEST
from app.schemas.evidencia import EvidenciaPresignedResponse, EvidenciaResponse, EvidenciaUploadResponse
from app.services import evidencia_service

logger = structlog.get_logger("api.evidencias")

router = APIRouter(prefix="/evidencias", tags=["evidencias"])


def get_evidencia_storage() -> S3StorageAdapter:
    """Storage adapter scoped to the evidencias bucket."""
    return S3StorageAdapter(bucket=settings.S3_BUCKET_PDFS)


@router.post(
    "/actividades/{actividad_id}",
    response_model=list[EvidenciaUploadResponse],
    status_code=status.HTTP_201_CREATED,
)
async def subir_evidencia(
    actividad_id: uuid.UUID,
    user: CurrentUser,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_evidencia_storage),
) -> list[EvidenciaUploadResponse]:
    """Sube uno o varios archivos de evidencia para una actividad (cualquier formato).

    Accepts multiple files per request (FastAPI still handles a single file sent
    under the `files` field correctly — the list just has one entry). All files
    are validated before any is stored; one invalid file rejects the whole batch.
    """
    if len(files) > MAX_EVIDENCE_FILES_PER_REQUEST:
        raise ValidationError(
            f"Máximo {MAX_EVIDENCE_FILES_PER_REQUEST} archivos por solicitud (se enviaron {len(files)})."
        )
    archivos = [
        (
            f.filename or "upload",
            f.content_type or "application/octet-stream",
            await f.read(),
        )
        for f in files
    ]
    return await evidencia_service.subir_evidencias(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad_id,
        archivos=archivos,
    )


@router.get("/actividades/{actividad_id}", response_model=list[EvidenciaResponse])
async def listar_evidencias(
    actividad_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[EvidenciaResponse]:
    """Lista todas las evidencias de una actividad."""
    return await evidencia_service.listar_evidencias(db, user.id, actividad_id)


@router.get("/{evidencia_id}/download", response_model=EvidenciaPresignedResponse)
async def descargar_evidencia(
    evidencia_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_evidencia_storage),
) -> EvidenciaPresignedResponse:
    """Genera una URL pre-firmada para descargar una evidencia."""
    return await evidencia_service.obtener_url_descarga(db, storage, user.id, evidencia_id)


@router.delete(
    "/{evidencia_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def eliminar_evidencia(
    evidencia_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_evidencia_storage),
) -> None:
    """Elimina una evidencia y su archivo del almacenamiento."""
    await evidencia_service.eliminar_evidencia(db, storage, user.id, evidencia_id)
