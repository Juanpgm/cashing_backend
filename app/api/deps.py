"""API dependencies — DB session, current user, credit checks, storage."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drive.drive_adapter import DriveAdapter
from app.adapters.email.gmail_adapter import GmailAdapter
from app.adapters.storage import get_storage as _get_storage
from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.core.auth import authenticate_bearer
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, InsufficientCreditsError, UnauthorizedError
from app.models.usuario import Usuario

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    """Extract and validate JWT from Authorization header, return active user."""
    if not credentials:
        raise UnauthorizedError("Missing or invalid authorization header")

    user = await authenticate_bearer(credentials.credentials, db)

    # Store user_id in request state for audit logging
    request.state.user_id = str(user.id)

    return user


CurrentUser = Annotated[Usuario, Depends(get_current_user)]


def require_role(allowed_roles: list[str]):  # type: ignore[no-untyped-def]
    """Dependency factory that checks user has one of the allowed roles."""

    async def _check_role(user: CurrentUser) -> Usuario:
        if user.rol.value not in allowed_roles:
            raise ForbiddenError()
        return user

    return Depends(_check_role)


async def require_credits(
    amount: int,
    user: CurrentUser,
) -> Usuario:
    """Check user has enough credits."""
    if user.creditos_disponibles < amount:
        raise InsufficientCreditsError(required=amount, available=user.creditos_disponibles)
    return user


def get_pdf_storage() -> object:
    """Storage adapter scoped to the PDFs bucket."""
    return _get_storage(settings.S3_BUCKET_PDFS)


def get_avatar_storage() -> object:
    """Storage adapter scoped to the avatars bucket."""
    return _get_storage(settings.S3_BUCKET_AVATARS)


async def get_email_adapter(db: AsyncSession = Depends(get_db)) -> GmailAdapter:
    """Gmail adapter — requires user to have connected their Google account."""
    return GmailAdapter(db=db)


async def get_drive_adapter(db: AsyncSession = Depends(get_db)) -> DriveAdapter:
    """Drive adapter — requires user to have connected their Google account."""
    return DriveAdapter(db=db)
