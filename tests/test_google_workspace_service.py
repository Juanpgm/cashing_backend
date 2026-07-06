"""Tests for google_workspace_service helper functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError


class TestFernet:
    def test_fernet_roundtrip(self) -> None:
        """_fernet() returns a working Fernet instance that can encrypt/decrypt."""
        from cryptography.fernet import Fernet

        from app.services.google_workspace_service import _fernet

        key = Fernet.generate_key()
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.TOKEN_ENCRYPTION_KEY = key.decode()
            f = _fernet()
            encrypted = f.encrypt(b"secret")
            assert f.decrypt(encrypted) == b"secret"


class TestGetAuthorizationUrl:
    def test_raises_when_no_credentials_configured(self) -> None:
        from app.services.google_workspace_service import get_authorization_url

        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = ""
            mock_settings.GOOGLE_OAUTH_CLIENT_SECRET = ""
            mock_settings.GOOGLE_OAUTH_REDIRECT_URI = "http://localhost/callback"
            mock_settings.GOOGLE_OAUTH_SCOPES = []
            mock_settings.JWT_SECRET_KEY = "test-secret-key-min-32-chars-xx"
            mock_settings.JWT_ALGORITHM = "HS256"
            mock_settings.GOOGLE_OAUTH_STATE_TTL_SECONDS = 600

            with pytest.raises(ExternalServiceError):
                get_authorization_url(uuid.uuid4())


class TestVerifyOauthState:
    _SECRET = "test-secret-key-min-32-chars-xx"
    _ALGO = "HS256"

    _CV = "test-code-verifier-value"

    def _make_state(self, usuario_id: uuid.UUID, ttl_seconds: int = 600, cv: str = "test-cv") -> str:
        now = datetime.now(UTC)
        claims = {
            "sub": str(usuario_id),
            "type": "oauth_state",
            "cv": cv,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
        }
        return jwt.encode(claims, self._SECRET, algorithm=self._ALGO)

    def test_round_trip_returns_same_uuid_and_verifier(self) -> None:
        from app.services.google_workspace_service import verify_oauth_state

        uid = uuid.uuid4()
        state = self._make_state(uid, cv=self._CV)
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            returned_uid, returned_cv = verify_oauth_state(state)
            assert returned_uid == uid
            assert returned_cv == self._CV

    def test_tampered_token_raises_validation_error(self) -> None:
        from app.services.google_workspace_service import verify_oauth_state

        state = self._make_state(uuid.uuid4()) + "TAMPERED"
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            with pytest.raises(ValidationError):
                verify_oauth_state(state)

    def test_expired_token_raises_validation_error(self) -> None:
        from app.services.google_workspace_service import verify_oauth_state

        state = self._make_state(uuid.uuid4(), ttl_seconds=-1)
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            with pytest.raises(ValidationError):
                verify_oauth_state(state)

    def test_wrong_type_raises_validation_error(self) -> None:
        from app.services.google_workspace_service import verify_oauth_state

        uid = uuid.uuid4()
        claims = {"sub": str(uid), "type": "access", "cv": "x", "exp": datetime.now(UTC) + timedelta(minutes=5)}
        state = jwt.encode(claims, self._SECRET, algorithm=self._ALGO)
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            with pytest.raises(ValidationError):
                verify_oauth_state(state)


class TestGetIntegrationStatus:
    @pytest.mark.asyncio
    async def test_returns_not_connected_when_no_token(self) -> None:
        from app.services.google_workspace_service import get_integration_status

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        status = await get_integration_status(mock_db, uuid.uuid4())
        assert status.connected is False

    @pytest.mark.asyncio
    async def test_returns_connected_when_token_exists(self) -> None:
        from app.services.google_workspace_service import get_integration_status

        fake_token = MagicMock()
        fake_token.scopes = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/drive.file"
        fake_token.expires_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_token

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        status = await get_integration_status(mock_db, uuid.uuid4())
        assert status.connected is True
        assert status.gmail_enabled is True
        assert status.drive_enabled is True


class TestRevokeIntegration:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_no_token(self) -> None:
        from app.services.google_workspace_service import revoke_integration

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        with pytest.raises(NotFoundError):
            await revoke_integration(mock_db, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_deletes_token_when_found(self) -> None:
        from app.services.google_workspace_service import revoke_integration

        fake_token = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_token

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        await revoke_integration(mock_db, uuid.uuid4())
        mock_db.delete.assert_called_once_with(fake_token)
        mock_db.commit.assert_called_once()


class TestHandleOAuthCallbackErrorPath:
    @pytest.mark.asyncio
    async def test_raises_when_not_configured(self) -> None:
        from app.services.google_workspace_service import handle_oauth_callback

        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = ""
            with pytest.raises(ExternalServiceError):
                await handle_oauth_callback(AsyncMock(), uuid.uuid4(), "code", "verifier")

    @pytest.mark.asyncio
    async def test_raises_on_token_exchange_failure(self) -> None:
        from app.services.google_workspace_service import handle_oauth_callback

        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = "client_id"
            mock_settings.GOOGLE_OAUTH_CLIENT_SECRET = "client_secret"
            mock_settings.GOOGLE_OAUTH_REDIRECT_URI = "http://localhost/callback"
            mock_settings.GOOGLE_OAUTH_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

            mock_flow = MagicMock()
            mock_flow.fetch_token.side_effect = Exception("HTTP 400")

            with patch("app.services.google_workspace_service.Flow.from_client_config", return_value=mock_flow):
                with pytest.raises(ExternalServiceError):
                    await handle_oauth_callback(AsyncMock(), uuid.uuid4(), "bad_code", "verifier")


class TestSearchEmails:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_messages(self) -> None:
        from app.services.google_workspace_service import search_emails

        mock_adapter = MagicMock()
        mock_adapter.search_messages = AsyncMock(return_value=[])

        with patch("app.services.google_workspace_service.GmailAdapter", return_value=mock_adapter):
            result = await search_emails(AsyncMock(), uuid.uuid4(), "query")

        assert result.total == 0
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_returns_messages(self) -> None:
        from app.services.google_workspace_service import search_emails

        fake_message = MagicMock()
        fake_message.id = "msg1"
        fake_message.thread_id = "thread1"
        fake_message.subject = "Test"
        fake_message.sender = "a@b.com"
        fake_message.recipients = []
        from datetime import datetime, timezone
        fake_message.date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        fake_message.snippet = "a snippet"
        fake_message.body_plain = ""
        fake_message.body_html = ""
        fake_message.attachments = []
        fake_message.labels = []

        mock_adapter = MagicMock()
        mock_adapter.search_messages = AsyncMock(return_value=[fake_message])

        with patch("app.services.google_workspace_service.GmailAdapter", return_value=mock_adapter):
            result = await search_emails(AsyncMock(), uuid.uuid4(), "invoice")

        assert result.total == 1
        assert result.messages[0].subject == "Test"


class TestHandleOAuthCallbackSuccess:
    @pytest.mark.asyncio
    async def test_saves_new_token_when_no_existing(self) -> None:
        from cryptography.fernet import Fernet
        from app.services.google_workspace_service import handle_oauth_callback

        key = Fernet.generate_key()

        mock_creds = MagicMock()
        mock_creds.token = "access_token"
        mock_creds.refresh_token = "refresh_token"
        mock_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

        mock_flow = MagicMock()
        mock_flow.fetch_token.return_value = None
        mock_flow.credentials = mock_creds

        # DB: first execute for GoogleToken lookup → none; second for get_integration_status
        no_token_result = MagicMock()
        no_token_result.scalar_one_or_none.return_value = None

        status_result = MagicMock()
        status_result.scalar_one_or_none.return_value = MagicMock(
            scopes="https://www.googleapis.com/auth/gmail.readonly",
            expires_at=None,
        )

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [no_token_result, status_result]

        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = "client_id"
            mock_settings.GOOGLE_OAUTH_CLIENT_SECRET = "secret"
            mock_settings.GOOGLE_OAUTH_REDIRECT_URI = "http://localhost/callback"
            mock_settings.GOOGLE_OAUTH_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
            mock_settings.TOKEN_ENCRYPTION_KEY = key.decode()

            with patch("app.services.google_workspace_service.Flow.from_client_config", return_value=mock_flow):
                result = await handle_oauth_callback(mock_db, uuid.uuid4(), "valid_code", "test-verifier")

        assert result.connected is True
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()


class TestUploadPdfToDrive:
    @pytest.mark.asyncio
    async def test_uploads_without_sharing(self) -> None:
        from app.services.google_workspace_service import upload_pdf_to_drive

        mock_file = MagicMock()
        mock_file.id = "file123"
        mock_file.name = "factura.pdf"
        mock_file.web_view_link = "https://drive.google.com/file/file123"

        mock_adapter = MagicMock()
        mock_adapter.get_or_create_folder = AsyncMock(return_value="folder123")
        mock_adapter.upload_file = AsyncMock(return_value=mock_file)

        with patch("app.services.google_workspace_service.DriveAdapter", return_value=mock_adapter):
            with patch("app.services.google_workspace_service.build_contract_drive_path", return_value=["CashIn", "Entidad", "CON-001", "2024-03"]):
                result = await upload_pdf_to_drive(
                    AsyncMock(),
                    uuid.uuid4(),
                    b"%PDF-1.4",
                    "factura.pdf",
                    "Entidad Test",
                    "CON-001",
                    2024,
                    3,
                    make_shareable=False,
                )

        assert result.file_id == "file123"
        assert result.share_link is None


class TestSendInvoiceEmail:
    @pytest.mark.asyncio
    async def test_sends_email_and_returns_message_id(self) -> None:
        from app.services.google_workspace_service import send_invoice_email

        mock_adapter = MagicMock()
        mock_adapter.send_message = AsyncMock(return_value="msg_abc123")

        with patch("app.services.google_workspace_service.GmailAdapter", return_value=mock_adapter):
            result = await send_invoice_email(
                AsyncMock(),
                uuid.uuid4(),
                to=["recipient@example.com"],
                subject="Factura marzo 2024",
                body_html="<p>Adjunto factura</p>",
            )

        assert result.message_id == "msg_abc123"
        assert result.sent_to == ["recipient@example.com"]
