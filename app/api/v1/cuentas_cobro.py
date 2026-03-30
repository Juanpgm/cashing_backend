"""CuentasCobro API — CRUD, state machine, and PDF generation."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser, get_pdf_storage
from app.core.database import get_db
from app.schemas.cuenta_cobro import (
    ActividadCreate,
    ActividadResponse,
    CambiarEstadoRequest,
    CuentaCobroCreate,
    CuentaCobroListItem,
    CuentaCobroResponse,
    GenerarPDFResponse,
    PDFUrlResponse,
)
from app.services import cuenta_cobro_service

logger = structlog.get_logger("api.cuentas_cobro")

router = APIRouter(prefix="/cuentas-cobro", tags=["cuentas-cobro"])


@router.post("/", response_model=CuentaCobroResponse, status_code=status.HTTP_201_CREATED)
async def crear_cuenta_cobro(
    data: CuentaCobroCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """Create a new CuentaCobro (costs 10 credits). Starts in BORRADOR state."""
    return await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


@router.get("/", response_model=list[CuentaCobroListItem])
async def listar_cuentas_cobro(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[CuentaCobroListItem]:
    """List all CuentasCobro for the authenticated user, newest first."""
    return await cuenta_cobro_service.listar_cuentas_cobro(db, user.id)


@router.get("/{cuenta_id}", response_model=CuentaCobroResponse)
async def obtener_cuenta_cobro(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """Get a single CuentaCobro with its activities."""
    return await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta_id)


@router.delete("/{cuenta_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def eliminar_cuenta_cobro(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a CuentaCobro. Only allowed when in BORRADOR state."""
    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta_id)


@router.post("/{cuenta_id}/actividades", response_model=ActividadResponse, status_code=status.HTTP_201_CREATED)
async def agregar_actividad(
    cuenta_id: uuid.UUID,
    data: ActividadCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadResponse:
    """Add an activity to a CuentaCobro. Only allowed in BORRADOR or RECHAZADA states."""
    return await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta_id, data)


@router.patch("/{cuenta_id}/estado", response_model=CuentaCobroResponse)
async def cambiar_estado(
    cuenta_id: uuid.UUID,
    data: CambiarEstadoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """
    Transition a CuentaCobro to a new state.

    Valid transitions:
    - borrador → enviada
    - enviada → aprobada | rechazada
    - rechazada → borrador
    - aprobada → pagada
    """
    return await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta_id, data.estado)


@router.post("/{cuenta_id}/generar-pdf", response_model=GenerarPDFResponse)
async def generar_pdf(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> GenerarPDFResponse:
    """
    Generate a PDF for a CuentaCobro using the user's template (or the default one).
    Uploads the PDF to storage and returns a 1-hour presigned download URL.
    """
    return await cuenta_cobro_service.generar_pdf(db, user.id, cuenta_id, storage)


@router.get("/{cuenta_id}/pdf", response_model=PDFUrlResponse)
async def obtener_url_pdf(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> PDFUrlResponse:
    """Get a fresh 1-hour presigned URL for the stored PDF. Requires PDF to have been generated first."""
    return await cuenta_cobro_service.obtener_url_pdf(db, user.id, cuenta_id, storage)
