"""User preferences API endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.preferencia import PreferenciasResponse, PreferenciasUpdateRequest
from app.services import preferencia_service

router = APIRouter(prefix="/usuarios", tags=["usuarios"])


@router.get("/preferencias", response_model=PreferenciasResponse)
async def get_preferencias(
    user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> PreferenciasResponse:
    return await preferencia_service.get_preferencias(db, user.id)


@router.patch("/preferencias", response_model=PreferenciasResponse)
async def update_preferencias(
    data: PreferenciasUpdateRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PreferenciasResponse:
    return await preferencia_service.update_preferencias(db, user.id, data)
