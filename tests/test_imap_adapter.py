"""Tests for ImapAdapter — generic (non-OAuth) IMAP implementation of EmailPort.

imaplib is mocked at the module level (no real mail server). imaplib.IMAP4_SSL
is synchronous/blocking, so the adapter must wrap every call in run_in_executor
— the non-blocking test below proves that structurally, same pattern as
test_microsoft_graph_adapter.py's search_messages test.
"""

from __future__ import annotations

import asyncio
import imaplib
import uuid
from unittest.mock import MagicMock, patch

import pytest
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.imap_account import ImapAccount

pytestmark = pytest.mark.asyncio

RAW_EMAIL = (
    b"From: Contratista <contratista@empresa.com>\r\n"
    b"To: supervisor@empresa.com\r\n"
    b"Subject: Informe mensual de actividades\r\n"
    b"Date: Wed, 10 Apr 2024 10:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Contenido del informe de abril.\r\n"
)


def _fake_account(db_return: ImapAccount | None):
    db = MagicMock()

    async def _execute(*args, **kwargs):
        result = MagicMock()
        scalars_result = MagicMock()
        scalars_result.first.return_value = db_return
        result.scalars.return_value = scalars_result
        result.scalar_one_or_none.return_value = db_return
        return result

    db.execute = _execute
    db.commit = MagicMock(side_effect=lambda: asyncio.sleep(0))
    db.add = MagicMock()
    return db


def _mock_imap_connection(search_result=("OK", [b"1"]), fetch_result=("OK", [(b"1 (RFC822 {100}", RAW_EMAIL)])):
    conn = MagicMock()
    conn.login = MagicMock()
    conn.select = MagicMock(return_value=("OK", [b"1"]))
    conn.search = MagicMock(return_value=search_result)
    conn.fetch = MagicMock(return_value=fetch_result)
    conn.logout = MagicMock()
    return conn


class TestConnectAccount:
    async def test_connect_validates_credentials_then_stores_encrypted(self) -> None:
        from app.adapters.email.imap_adapter import ImapAdapter

        db = _fake_account(None)
        adapter = ImapAdapter(db=db)
        adapter._fernet.encrypt = MagicMock(side_effect=lambda b: b"enc-" + b)

        conn = _mock_imap_connection()
        with patch("app.adapters.email.imap_adapter.imaplib.IMAP4_SSL", return_value=conn):
            await adapter.connect_account(
                uuid.uuid4(),
                host="mail.empresa.com",
                port=993,
                username="user@empresa.com",
                password="secret",
            )

        conn.login.assert_called_once_with("user@empresa.com", "secret")
        conn.logout.assert_called()
        db.add.assert_called_once()

    async def test_connect_with_wrong_credentials_raises_validation_error(self) -> None:
        from app.adapters.email.imap_adapter import ImapAdapter

        db = _fake_account(None)
        adapter = ImapAdapter(db=db)

        conn = MagicMock()
        conn.login = MagicMock(side_effect=imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
        conn.logout = MagicMock()
        with (
            patch("app.adapters.email.imap_adapter.imaplib.IMAP4_SSL", return_value=conn),
            pytest.raises(ValidationError),
        ):
            await adapter.connect_account(
                uuid.uuid4(), host="mail.empresa.com", port=993, username="user@empresa.com", password="wrong"
            )


class TestSearchMessages:
    async def test_search_messages_returns_normalized_results_without_blocking_event_loop(self) -> None:
        from app.adapters.email.imap_adapter import ImapAdapter

        record = MagicMock(spec=ImapAccount)
        record.host = "mail.empresa.com"
        record.port = 993
        record.username = "user@empresa.com"
        record.password_encrypted = "enc-password"
        record.use_ssl = True

        db = _fake_account(record)
        adapter = ImapAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(return_value=b"real-password")

        conn = _mock_imap_connection()
        yielded = False

        async def _yield_marker() -> None:
            nonlocal yielded
            await asyncio.sleep(0)
            yielded = True

        with patch("app.adapters.email.imap_adapter.imaplib.IMAP4_SSL", return_value=conn):
            results, _ = await asyncio.gather(
                adapter.search_messages(uuid.uuid4(), "informe", max_results=10),
                _yield_marker(),
            )

        assert yielded is True
        assert len(results) == 1
        assert results[0].subject == "Informe mensual de actividades"
        assert results[0].sender == "Contratista <contratista@empresa.com>"
        assert "Contenido del informe" in results[0].body_plain

    async def test_no_connected_account_raises_not_found(self) -> None:
        from app.adapters.email.imap_adapter import ImapAdapter

        db = _fake_account(None)
        adapter = ImapAdapter(db=db)

        with pytest.raises(NotFoundError):
            await adapter.search_messages(uuid.uuid4(), "informe")

    async def test_search_raising_imap_error_is_wrapped_as_external_service_error(self) -> None:
        from app.adapters.email.imap_adapter import ImapAdapter

        record = MagicMock(spec=ImapAccount)
        record.host = "mail.empresa.com"
        record.port = 993
        record.username = "user@empresa.com"
        record.password_encrypted = "enc-password"
        record.use_ssl = True

        db = _fake_account(record)
        adapter = ImapAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(return_value=b"real-password")

        conn = _mock_imap_connection()
        conn.search = MagicMock(side_effect=imaplib.IMAP4.error("connection reset"))
        with (
            patch("app.adapters.email.imap_adapter.imaplib.IMAP4_SSL", return_value=conn),
            pytest.raises(ExternalServiceError),
        ):
            await adapter.search_messages(uuid.uuid4(), "informe")
