"""Reusable bearer-token authentication core.

Extracted from `app.api.deps.get_current_user` so the same authentication logic
can be reused by callers that have no FastAPI dependency-injection machinery —
notably the upcoming MCP server, which receives a raw bearer token and needs an
authenticated `Usuario` without going through `HTTPBearer`/`Depends`.

`app.api.deps.get_current_user` delegates to `authenticate_bearer` and keeps
only the FastAPI-specific concerns (missing-credentials check, `request.state`
side effect).
"""

import uuid

from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnauthorizedError
from app.core.security import decode_token
from app.models.token_blacklist import TokenBlacklist
from app.models.usuario import Usuario


async def authenticate_bearer(token: str, db: AsyncSession) -> Usuario:
    """Decode a raw bearer token and return the active `Usuario` it authenticates.

    Steps (identical to the former inline logic in `get_current_user`):
    1. Decode and verify the JWT signature/expiry.
    2. Require `type == "access"` (refresh tokens are not valid credentials here).
    3. Reject if the token's `jti` has been revoked (present in `token_blacklist`).
    4. Resolve the user from the `sub` claim and require `activo` and not soft-deleted.

    Raises `UnauthorizedError` with the same messages as the original
    `get_current_user` implementation on every failure path.
    """
    try:
        payload = decode_token(token)
    except JWTError:
        raise UnauthorizedError("Invalid or expired token") from None

    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid token type")

    jti = payload.get("jti", "")
    blacklisted = await db.execute(select(TokenBlacklist).where(TokenBlacklist.jti == jti))
    if blacklisted.scalar_one_or_none() is not None:
        raise UnauthorizedError("Token has been revoked")

    user_id = payload.get("sub", "")
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise UnauthorizedError("Invalid token subject") from None

    result = await db.execute(select(Usuario).where(Usuario.id == uid))
    user = result.scalar_one_or_none()

    if user is None or not user.activo or user.is_deleted:
        raise UnauthorizedError("User not found or inactive")

    return user
