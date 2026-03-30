"""Auth API endpoints — register, login, refresh, me."""

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.audit import log_audit_event
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateUserRequest,
    UserResponse,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse, status_code=201)
@limiter.limit("5/minute")
async def register(
    request: Request,
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await auth_service.register(db, data)
    await log_audit_event(
        action="register",
        user_id=str(user.id),
        ip=request.client.host if request.client else "",
    )
    return user


@router.post("/login", response_model=TokenResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    tokens = await auth_service.login(db, data.email, data.password)
    await log_audit_event(
        action="login",
        user_id=data.email,
        ip=request.client.host if request.client else "",
    )
    return tokens


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("10/minute")
async def refresh(
    request: Request,
    data: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    return await auth_service.refresh_tokens(db, data.refresh_token)


@router.get("/me", response_model=UserResponse)
async def get_me(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> UserResponse:
    return await auth_service.get_user_by_id(db, user.id)


@router.put("/me", response_model=UserResponse)
async def update_me(
    data: UpdateUserRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    return await auth_service.update_user(db, user.id, data)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ")
    await auth_service.logout(db, token)
    await log_audit_event(
        action="logout",
        user_id=str(user.id),
        ip=request.client.host if request.client else "",
    )
    return Response(status_code=204)
