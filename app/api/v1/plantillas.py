"""Plantillas API — CRUD for HTML document templates + rendering."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.models.plantilla import TipoPlantilla
from app.schemas.plantilla import (
    PlantillaCreate,
    PlantillaRenderRequest,
    PlantillaRenderResponse,
    PlantillaResponse,
    PlantillaUpdate,
)
from app.services import plantilla_service

logger = structlog.get_logger("api.plantillas")

router = APIRouter(prefix="/plantillas", tags=["plantillas"])


@router.post("/", response_model=PlantillaResponse, status_code=status.HTTP_201_CREATED)
async def crear_plantilla(
    data: PlantillaCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlantillaResponse:
    """Crea una nueva plantilla HTML para generación de documentos."""
    return await plantilla_service.crear_plantilla(db, user.id, data)


@router.get("/", response_model=list[PlantillaResponse])
async def listar_plantillas(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    tipo: TipoPlantilla | None = None,
) -> list[PlantillaResponse]:
    """Lista las plantillas activas del usuario. Filtrable por tipo."""
    return await plantilla_service.listar_plantillas(db, user.id, tipo=tipo)


@router.get("/{plantilla_id}", response_model=PlantillaResponse)
async def obtener_plantilla(
    plantilla_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlantillaResponse:
    return await plantilla_service.obtener_plantilla(db, user.id, plantilla_id)


@router.patch("/{plantilla_id}", response_model=PlantillaResponse)
async def actualizar_plantilla(
    plantilla_id: uuid.UUID,
    data: PlantillaUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlantillaResponse:
    return await plantilla_service.actualizar_plantilla(db, user.id, plantilla_id, data)


@router.delete("/{plantilla_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def eliminar_plantilla(
    plantilla_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Desactiva (soft-delete) una plantilla."""
    await plantilla_service.eliminar_plantilla(db, user.id, plantilla_id)


@router.post("/{plantilla_id}/render", response_model=PlantillaRenderResponse)
async def renderizar_plantilla(
    plantilla_id: uuid.UUID,
    req: PlantillaRenderRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlantillaRenderResponse:
    """Renderiza la plantilla con los datos provistos. Devuelve HTML y PDF (base64) si disponible."""
    return await plantilla_service.renderizar_plantilla(db, user.id, plantilla_id, req)
