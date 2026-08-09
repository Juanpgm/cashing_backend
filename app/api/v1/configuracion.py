"""Configuración API — self-service account-level actions (test-data purge, etc.)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser, get_pdf_storage
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.schemas.configuracion import PurgarDatosPruebaRequest, PurgarDatosPruebaResponse
from app.services import purga_service

router = APIRouter(prefix="/configuracion", tags=["configuracion"])


@router.post("/purgar-datos-prueba", response_model=PurgarDatosPruebaResponse)
@limiter.limit("3/minute")
async def purgar_datos_prueba(
    request: Request,
    data: PurgarDatosPruebaRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> PurgarDatosPruebaResponse:
    """Reinicia el estado de radicación propio del usuario autenticado para volver a
    probar: elimina todas sus cuentas de cobro (documentos/evidencias/paquetes
    incluidos) y reinicia las obligaciones de sus contratos. Los contratos, los
    documento_fuente y la cuenta de usuario NO se eliminan.

    Requiere `confirmacion: "BORRAR"` en el cuerpo de la solicitud.
    """
    result = await purga_service.purgar_datos_prueba(db, user.id, storage)
    return PurgarDatosPruebaResponse(**result)
