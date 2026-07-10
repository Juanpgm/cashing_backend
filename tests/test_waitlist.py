"""Waitlist / invite-code gate tests.

The gate is OFF by default (WAITLIST_ENABLED=False) so open registration keeps
working. When enabled, both email registration and first-time Google sign-in
require a valid, non-exhausted invite code.
"""

import asyncio
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import InviteRequiredError
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


# ---------------------------------------------------------------------------
# TOCTOU race guard — _consume_invite_code must be atomic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_invite_code_concurrent_only_one_succeeds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1-use code hit by two concurrent consumers must let exactly ONE through.

    Regression guard for the read-check-increment TOCTOU: the old
    SELECT -> check `disponible` -> increment sequence let two concurrent
    callers both read `usos_actuales < max_usos` as true before either wrote
    back, double-spending a single-use code. The fix uses a single guarded
    UPDATE ... WHERE ... RETURNING/rowcount so only one writer can win the row.
    """
    from tests.conftest import async_session_test

    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="RACE-CODE", max_usos=1, usos_actuales=0, activo=True))
    await db.commit()

    async def _attempt() -> str:
        async with async_session_test() as session:
            try:
                await auth_service._consume_invite_code(session, "RACE-CODE")
            except InviteRequiredError:
                await session.rollback()
                return "fail"
            else:
                await session.commit()
                return "ok"

    results = await asyncio.gather(_attempt(), _attempt())

    assert sorted(results) == ["fail", "ok"], (
        f"expected exactly one success and one InviteRequiredError, got {results}"
    )

    result = await db.execute(select(InviteCode).where(InviteCode.codigo == "RACE-CODE"))
    invite = result.scalar_one()
    assert invite.usos_actuales == 1  # never double-spent past max_usos


@pytest.mark.asyncio
async def test_consume_invite_code_second_sequential_call_fails_when_exhausted(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two SEQUENTIAL calls on a 1-use code: first consumes it, second must fail.

    Simpler, deterministic complement to the concurrent test above — directly
    exercises the atomic UPDATE...WHERE guard rowcount check.
    """
    monkeypatch.setattr(settings, "WAITLIST_ENABLED", True)
    db.add(InviteCode(codigo="SEQ-CODE", max_usos=1, usos_actuales=0, activo=True))
    await db.commit()

    await auth_service._consume_invite_code(db, "SEQ-CODE")
    await db.commit()

    with pytest.raises(InviteRequiredError):
        await auth_service._consume_invite_code(db, "SEQ-CODE")
    await db.rollback()

    result = await db.execute(select(InviteCode).where(InviteCode.codigo == "SEQ-CODE"))
    invite = result.scalar_one()
    assert invite.usos_actuales == 1
