"""Configuración API — self-service account-level actions (test-data purge, etc.)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import AdminUser, CurrentUser, get_pdf_storage
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.schemas.configuracion import (
    PurgarDatosPruebaRequest,
    PurgarDatosPruebaResponse,
    PurgarTodoAdminRequest,
    PurgarTodoAdminResponse,
)
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


@router.post("/purgar-todo-admin", response_model=PurgarTodoAdminResponse)
@limiter.limit("1/minute")
async def purgar_todo_admin(
    request: Request,
    data: PurgarTodoAdminRequest,
    user: AdminUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> PurgarTodoAdminResponse:
    """PELIGRO — wipe GLOBAL: borra TODAS las cuentas de cobro, evidencias, documentos
    y obligaciones de TODOS los usuarios de la aplicación (no solo las del que llama).
    Solo accesible con rol admin (`AdminUser`, 403 en caso contrario).

    Requiere `confirmacion: "BORRAR TODO"` en el cuerpo de la solicitud. La cuenta del
    admin que ejecuta la acción nunca se elimina.
    """
    result = await purga_service.purgar_todo_admin(db, storage)
    return PurgarTodoAdminResponse(**result)
