"""User preferences service — get/update key-value preferences (Phase 7)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.preferencia_usuario import PreferenciaUsuario
from app.schemas.preferencia import PreferenciasResponse, PreferenciasUpdateRequest

# Defaults mirror the initial state of the frontend settings form
# (cashing-frontend/app/configuracion/page.tsx) so a first-time GET returns
# exactly what the UI would already show before the user changes anything.
DEFAULT_PREFERENCIAS: dict[str, object] = {
    "notificaciones_email": True,
    "idioma": "es",
    "timezone": "America/Bogota",
}


async def get_preferencias(db: AsyncSession, usuario_id: uuid.UUID) -> PreferenciasResponse:
    """Return the user's preferences, filling in defaults for any key without a stored row.

    Never raises NotFoundError — a user with no preference rows yet simply gets the
    defaults, so the frontend never sees a 404 for a valid authenticated user.
    """
    result = await db.execute(
        select(PreferenciaUsuario).where(PreferenciaUsuario.usuario_id == usuario_id)
    )
    stored = {row.clave: row.valor for row in result.scalars().all()}
    merged = {**DEFAULT_PREFERENCIAS, **stored}
    return PreferenciasResponse(**merged)


async def update_preferencias(
    db: AsyncSession, usuario_id: uuid.UUID, data: PreferenciasUpdateRequest
) -> PreferenciasResponse:
    """Partially update preferences — only fields explicitly provided are written.

    Each provided field is upserted as its own row (clave/valor), matching the
    key-value shape of PreferenciaUsuario.
    """
    update_data = data.model_dump(exclude_unset=True)

    for clave, valor in update_data.items():
        result = await db.execute(
            select(PreferenciaUsuario).where(
                PreferenciaUsuario.usuario_id == usuario_id,
                PreferenciaUsuario.clave == clave,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            db.add(PreferenciaUsuario(usuario_id=usuario_id, clave=clave, valor=valor))
        else:
            row.valor = valor

    await db.flush()

    return await get_preferencias(db, usuario_id)
