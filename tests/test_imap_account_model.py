"""Tests for the ImapAccount model — unique (usuario_id, email)."""

from __future__ import annotations

from typing import Any

import pytest
from app.models.imap_account import ImapAccount
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _imap_account(usuario_id: Any, email: str = "user@empresa.com") -> ImapAccount:
    return ImapAccount(
        usuario_id=usuario_id,
        email=email,
        host="mail.empresa.com",
        port=993,
        username="user@empresa.com",
        password_encrypted="enc-password",
    )


async def test_unique_usuario_email_rejects_duplicate(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    db.add(_imap_account(user.id))
    await db.commit()

    db.add(_imap_account(user.id))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_different_email_same_usuario_is_allowed(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    db.add(_imap_account(user.id, email="a@empresa.com"))
    await db.commit()

    db.add(_imap_account(user.id, email="b@empresa.com"))
    await db.commit()  # must not raise — different email is a distinct key


async def test_defaults(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    record = _imap_account(user.id)
    db.add(record)
    await db.commit()
    await db.refresh(record)

    assert record.use_ssl is True
    assert record.port == 993
