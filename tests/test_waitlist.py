"""Waitlist / invite-code gate tests.

The gate is OFF by default (WAITLIST_ENABLED=False) so open registration keeps
working. When enabled, both email registration and first-time Google sign-in
require a valid, non-exhausted invite code.
"""

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.invite_code import InviteCode
from app.services import auth_service


# ---------------------------------------------------------------------------
# Gate disabled (default) — open registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_open_when_waitlist_disabled(client: AsyncClient) -> None:
    """With the gate off, registering without an invite code succeeds."""
    assert settings.WAITLIST_ENABLED is False  # default
    payload = {
        "email": "open@example.com",
        "password": "StrongPass1!",
        "nombre": "Open Signup",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Gate enabled — email registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_requires_code_when_enabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on + no code → 403."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    payload = {
        "email": "nocode@example.com",
        "password": "StrongPass1!",
        "nombre": "No Code",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_register_rejects_unknown_code(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on + unknown code → 403."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    payload = {
        "email": "badcode@example.com",
        "password": "StrongPass1!",
        "nombre": "Bad Code",
        "invite_code": "DOES-NOT-EXIST",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_register_accepts_valid_code_and_consumes_use(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on + valid code → 201 and the code's usage counter increments."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="LAUNCH-2026", max_usos=1, usos_actuales=0, activo=True))
    await db.commit()

    payload = {
        "email": "invited@example.com",
        "password": "StrongPass1!",
        "nombre": "Invited User",
        "invite_code": "LAUNCH-2026",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201

    result = await db.execute(select(InviteCode).where(InviteCode.codigo == "LAUNCH-2026"))
    invite = result.scalar_one()
    assert invite.usos_actuales == 1


@pytest.mark.asyncio
async def test_register_rejects_exhausted_code(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on + fully-used code → 403 and no extra consumption."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="FULL-CODE", max_usos=2, usos_actuales=2, activo=True))
    await db.commit()

    payload = {
        "email": "late@example.com",
        "password": "StrongPass1!",
        "nombre": "Too Late",
        "invite_code": "FULL-CODE",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_register_rejects_inactive_code(
    client: AsyncClient, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate on + deactivated code → 403 even if it has remaining uses."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="OFF-CODE", max_usos=5, usos_actuales=0, activo=False))
    await db.commit()

    payload = {
        "email": "inactive@example.com",
        "password": "StrongPass1!",
        "nombre": "Inactive Code",
        "invite_code": "OFF-CODE",
    }
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Gate enabled — Google sign-in (new users only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_new_user_requires_code_when_enabled(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-time Google sign-in with the gate on and no code → InviteRequiredError."""
    from app.core.exceptions import InviteRequiredError

    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    claims = {"uid": "g-123", "email": "gnew@example.com", "name": "G New"}
    with patch("app.core.firebase_admin.verify_firebase_token", return_value=claims):
        with pytest.raises(InviteRequiredError):
            await auth_service.google_auth(db, "fake-token", invite_code=None)


@pytest.mark.asyncio
async def test_google_new_user_accepts_valid_code(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-time Google sign-in with a valid code succeeds and consumes the code."""
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="G-INVITE", max_usos=1, usos_actuales=0, activo=True))
    await db.commit()

    claims = {"uid": "g-456", "email": "gok@example.com", "name": "G Ok"}
    with patch("app.core.firebase_admin.verify_firebase_token", return_value=claims):
        tokens = await auth_service.google_auth(db, "fake-token", invite_code="G-INVITE")
    assert tokens.access_token

    result = await db.execute(select(InviteCode).where(InviteCode.codigo == "G-INVITE"))
    assert result.scalar_one().usos_actuales == 1
