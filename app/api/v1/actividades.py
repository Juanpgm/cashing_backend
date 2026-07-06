"""Actividades API — work activities within a cuenta de cobro."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.actividad import ActividadCreate, ActividadResponse, ActividadUpdate
from app.services import actividad_service

logger = structlog.get_logger("api.actividades")

router = APIRouter(tags=["actividades"])


# ── Scoped under cuentas_cobro ────────────────────────────────────────────────


@router.post(
    "/cuentas-cobro/{cuenta_cobro_id}/actividades",
    response_model=ActividadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def crear_actividad(
    cuenta_cobro_id: uuid.UUID,
    data: ActividadCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadResponse:
    """Agrega una actividad a la cuenta de cobro (solo en estado borrador)."""
    return await actividad_service.crear_actividad(db, user.id, cuenta_cobro_id, data)


@router.get(
    "/cuentas-cobro/{cuenta_cobro_id}/actividades",
    response_model=list[ActividadResponse],
)
async def listar_actividades(
    cuenta_cobro_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ActividadResponse]:
    """Lista todas las actividades de una cuenta de cobro."""
    return await actividad_service.listar_actividades(db, user.id, cuenta_cobro_id)


# ── Individual actividad operations ───────────────────────────────────────────


@router.patch("/actividades/{actividad_id}", response_model=ActividadResponse)
async def actualizar_actividad(
    actividad_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    data: ActividadUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadResponse:
    return await actividad_service.actualizar_actividad(
        db, user.id, cuenta_cobro_id, actividad_id, data
    )


@router.delete(
    "/actividades/{actividad_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def eliminar_actividad(
    actividad_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    await actividad_service.eliminar_actividad(db, user.id, cuenta_cobro_id, actividad_id)
