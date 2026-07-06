"""Tests for PreferenciaUsuario model (Phase 7)."""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def test_create_preferencia(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    from app.models.preferencia_usuario import PreferenciaUsuario

    pref = PreferenciaUsuario(
        usuario_id=test_user["user"].id,
        clave="notificaciones_email",
        valor=True,
    )
    db.add(pref)
    await db.flush()
    assert pref.id is not None


async def test_unique_constraint(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    from sqlalchemy.exc import IntegrityError
    from app.models.preferencia_usuario import PreferenciaUsuario

    pref1 = PreferenciaUsuario(
        usuario_id=test_user["user"].id,
        clave="theme",
        valor="dark",
    )
    db.add(pref1)
    await db.flush()

    pref2 = PreferenciaUsuario(
        usuario_id=test_user["user"].id,
        clave="theme",
        valor="light",
    )
    db.add(pref2)
    with pytest.raises(IntegrityError):
        await db.flush()


def test_model_fields() -> None:
    from app.models.preferencia_usuario import PreferenciaUsuario

    pref = PreferenciaUsuario(
        usuario_id=uuid.uuid4(),
        clave="lang",
        valor="es",
    )
    assert pref.clave == "lang"
    assert pref.valor == "es"


def test_import_preferencia_usuario() -> None:
    from app.models.preferencia_usuario import PreferenciaUsuario
    from app.models import PreferenciaUsuario as PU

    assert PreferenciaUsuario is PU


def test_relationship_on_usuario() -> None:
    from app.models.usuario import Usuario

    assert hasattr(Usuario, "preferencias")

