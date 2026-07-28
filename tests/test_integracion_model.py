"""Tests for the Integracion model — multi-provider credential store uniqueness.

Task A1.1 (RED): unique (usuario_id, provider, email); empty-email connections
collapse to one row per (usuario_id, provider) — see design.md D5.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.models.integracion import Integracion, IntegrationProvider
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _integracion(
    usuario_id: Any,
    provider: IntegrationProvider = IntegrationProvider.GOOGLE,
    email: str = "",
) -> Integracion:
    return Integracion(
        usuario_id=usuario_id,
        provider=provider,
        access_token_encrypted="enc-access",
        refresh_token_encrypted="enc-refresh",
        scopes="scope-a scope-b",
        email=email,
    )


async def test_unique_usuario_provider_email_rejects_duplicate(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    db.add(_integracion(user.id, email="a@example.com"))
    await db.commit()

    db.add(_integracion(user.id, email="a@example.com"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_empty_email_collapses_to_one_row_per_usuario_provider(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """Two unknown-email connections for the same (usuario, provider) collapse
    under the same key — the second commit violates the unique constraint."""
    user = test_user["user"]
    db.add(_integracion(user.id))  # email="" default
    await db.commit()

    db.add(_integracion(user.id))  # email="" again — same (usuario_id, provider, "")
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_different_provider_same_email_is_allowed(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    db.add(_integracion(user.id, provider=IntegrationProvider.GOOGLE, email="a@example.com"))
    await db.commit()

    db.add(_integracion(user.id, provider=IntegrationProvider.MICROSOFT, email="a@example.com"))
    await db.commit()  # must not raise — different provider is a distinct key
