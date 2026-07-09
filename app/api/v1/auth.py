"""Auth API endpoints — register, login, refresh, me."""

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser, get_avatar_storage
from app.core.audit import log_audit_event
from app.core.database import get_db
from app.core.rate_limit import limiter
from app.schemas.auth import (
    GoogleAuthRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateUserRequest,
    UserResponse,
)
from app.services import auth_service

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB

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


@router.post("/google", response_model=TokenResponse)
@limiter.limit("10/minute")
async def google_login(
    request: Request,
    data: GoogleAuthRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate via Firebase Google Sign-in.

    The frontend calls signInWithPopup → gets an ID token → sends it here.
    The backend verifies the token with firebase-admin, upserts the user,
    and returns the same JWT pair as email/password login.
    """
    tokens = await auth_service.google_auth(db, data.id_token, data.invite_code)
    await log_audit_event(
        action="google_login",
        user_id="google",
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


@router.post("/me/photo", response_model=UserResponse)
async def upload_photo(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    storage: S3StorageAdapter = Depends(get_avatar_storage),
) -> UserResponse:
    if file.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=422, detail="Solo se permiten imágenes JPEG, PNG, WebP o GIF")
    data = await file.read()
    if len(data) > _MAX_PHOTO_BYTES:
        raise HTTPException(status_code=422, detail="La imagen no puede superar 5 MB")
    return await auth_service.upload_profile_photo(
        db, user.id, data, file.content_type or "image/jpeg", storage
    )


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
