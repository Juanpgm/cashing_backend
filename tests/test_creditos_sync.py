"""Credits single-source-of-truth tests.

The `creditos` ledger (SUM of cantidad) is the source of truth; the denormalized
`usuarios.creditos_disponibles` cache must stay in sync on every mutation (so the
credit gate is correct), and balance display must always reflect the ledger.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.credito import Credito, TipoCredito
from app.models.usuario import Usuario
from app.services import credito_service


async def _make_user(db: AsyncSession, *, cache: int = 0) -> Usuario:
    user = Usuario(
        email=f"{uuid.uuid4().hex}@test.com",
        nombre="Test",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=cache,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_agregar_creditos_sincroniza_cache(db: AsyncSession) -> None:
    user = await _make_user(db, cache=0)
    await credito_service.agregar_creditos(db, user.id, 10, TipoCredito.COMPRA, "test")

    await db.refresh(user)
    assert user.creditos_disponibles == 10  # cache synced
    balance = (await credito_service.obtener_balance(db, user.id)).balance
    assert balance == 10  # ledger


@pytest.mark.asyncio
async def test_consumir_creditos_sincroniza_cache(db: AsyncSession) -> None:
    user = await _make_user(db, cache=0)
    await credito_service.agregar_creditos(db, user.id, 10, TipoCredito.COMPRA, "topup")
    await credito_service.consumir_creditos(db, user.id, 3, "accion")

    await db.refresh(user)
    assert user.creditos_disponibles == 7
    assert (await credito_service.obtener_balance(db, user.id)).balance == 7


@pytest.mark.asyncio
async def test_reconciliar_corrige_drift(db: AsyncSession) -> None:
    """A user whose cache drifted from the ledger gets fixed by reconciliar."""
    user = await _make_user(db, cache=999)  # wrong cache
    db.add(Credito(usuario_id=user.id, cantidad=30, tipo=TipoCredito.BONUS, referencia="x"))
    await db.commit()

    fixed = await credito_service.reconciliar_creditos(db, user.id)
    assert fixed == 30
    await db.refresh(user)
    assert user.creditos_disponibles == 30


@pytest.mark.asyncio
async def test_auth_me_muestra_saldo_del_ledger_aunque_cache_drifte(
    client: AsyncClient, db: AsyncSession
) -> None:
    """/auth/me must show the real ledger balance even if the cache is stale."""
    from app.core.security import create_access_token

    user = await _make_user(db, cache=5)  # stale cache
    db.add(Credito(usuario_id=user.id, cantidad=30, tipo=TipoCredito.BONUS, referencia="signup"))
    await db.commit()

    token = create_access_token(subject=str(user.id), role=user.rol)
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["creditos_disponibles"] == 30  # ledger, not the stale 5


@pytest.mark.asyncio
async def test_topup_se_refleja_en_me_y_balance(client: AsyncClient, db: AsyncSession) -> None:
    """A Wompi-style top-up via agregar_creditos shows up on both read paths."""
    from app.core.security import create_access_token

    user = await _make_user(db, cache=0)
    await credito_service.agregar_creditos(db, user.id, 50, TipoCredito.COMPRA, "wompi")

    token = create_access_token(subject=str(user.id), role=user.rol)
    headers = {"Authorization": f"Bearer {token}"}

    me = await client.get("/api/v1/auth/me", headers=headers)
    bal = await client.get("/api/v1/creditos/balance", headers=headers)
    assert me.json()["creditos_disponibles"] == 50
    assert bal.json()["balance"] == 50
