"""Admin user-management endpoints."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, require_role
from app.core.database import get_db
from app.schemas.auth import AdminUpdateUserRequest, UserResponse
from app.services import auth_service

router = APIRouter(prefix="/users", tags=["users"])

_require_admin = require_role(["admin"])


@router.get("/", response_model=list[UserResponse], dependencies=[_require_admin])
async def list_users(
    _user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    """List all non-deleted users. Admin only."""
    return await auth_service.list_users(db)


@router.patch("/{user_id}", response_model=UserResponse, dependencies=[_require_admin])
async def update_user(
    user_id: uuid.UUID,
    data: AdminUpdateUserRequest,
    _user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Update a user's active status, role, or reset their login lockout. Admin only."""
    return await auth_service.admin_update_user(db, user_id, data)
