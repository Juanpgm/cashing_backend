"""SECOP public contracting data endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.secop import (
    SecopConsultaCompletaResponse,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopImportResult,
    SecopProcesoResponse,
)
from app.services import secop_service

router = APIRouter(prefix="/secop", tags=["secop"])


@router.get("/contratos", response_model=list[SecopContratoResponse])
async def buscar_contratos(
    user: CurrentUser,
    cedula: str = Query(..., description="Número de cédula del contratista", pattern=r"^\d{5,15}$"),
    refresh: bool = Query(False, description="Forzar recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> list[SecopContratoResponse]:
    """Busca contratos de prestación de servicios asociados a una cédula."""
    return await secop_service.buscar_contratos_cedula(db, cedula, refresh=refresh)


@router.get("/procesos/{id_proceso}", response_model=SecopProcesoResponse | None)
async def obtener_proceso(
    id_proceso: str,
    user: CurrentUser,
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> SecopProcesoResponse | None:
    """Obtiene un proceso de contratación SECOP por su ID."""
    return await secop_service.obtener_proceso(db, id_proceso, refresh=refresh)


@router.get("/documentos/{numero_contrato}", response_model=list[SecopDocumentoResponse])
async def buscar_documentos(
    numero_contrato: str,
    user: CurrentUser,
    refresh: bool = Query(False),
    db: AsyncSession = Depends(get_db),
) -> list[SecopDocumentoResponse]:
    """Lista los documentos/archivos asociados a un número de contrato."""
    return await secop_service.buscar_documentos_contrato(db, numero_contrato, refresh=refresh)


@router.post("/importar", response_model=SecopImportResult, status_code=201)
async def importar_contratos(
    user: CurrentUser,
    documento_proveedor: str = Query(
        ...,
        description="Documento del proveedor/contratista (cédula o NIT)",
        pattern=r"^\d{5,15}$",
    ),
    confirmar: bool = Query(
        False,
        description=(
            "false → solo muestra los contratos que se importarían (preview, sin guardar). "
            "true → guarda los contratos en la base de datos."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> SecopImportResult:
    """Busca en SECOP todos los contratos del documento_proveedor.
    Con confirmar=false devuelve una vista previa sin persistir nada.
    Con confirmar=true guarda los contratos nuevos en la base de datos."""
    return await secop_service.importar_contratos_secop(db, documento_proveedor, user.id, confirmar=confirmar)


@router.get("/consulta", response_model=SecopConsultaCompletaResponse)
async def consulta_completa(
    user: CurrentUser,
    cedula: str = Query(..., description="Número de cédula del contratista", pattern=r"^\d{5,15}$"),
    refresh: bool = Query(False, description="Forzar recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> SecopConsultaCompletaResponse:
    """Consulta completa: contratos + proceso + documentos por cédula."""
    return await secop_service.consulta_completa(db, cedula, refresh=refresh)
