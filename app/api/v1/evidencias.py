"""Evidencias API — upload, download and delete evidence files for actividades."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser, get_pdf_storage
from app.core.config import settings
from app.core.database import get_db
from app.schemas.evidencia import EvidenciaPresignedResponse, EvidenciaResponse, EvidenciaUploadResponse
from app.services import evidencia_service

logger = structlog.get_logger("api.evidencias")

router = APIRouter(prefix="/evidencias", tags=["evidencias"])


def get_evidencia_storage() -> S3StorageAdapter:
    """Storage adapter scoped to the evidencias bucket."""
    return S3StorageAdapter(bucket=settings.S3_BUCKET_PDFS)


@router.post(
    "/actividades/{actividad_id}",
    response_model=EvidenciaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def subir_evidencia(
    actividad_id: uuid.UUID,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_evidencia_storage),
) -> EvidenciaUploadResponse:
    """Sube un archivo de evidencia para una actividad."""
    data = await file.read()
    return await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad_id,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        data=data,
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
