"""Tests for microsoft_graph_service — Azure AD authorization-code + PKCE OAuth2 flow.

Mirrors test_google_workspace_service.py's OAuth-flow test shape. Token/state
persistence is delegated to integration_service (already covered by
tests/test_integration_service.py) — this file covers only the
Microsoft-specific PKCE URL building and Graph token exchange.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app.core.exceptions import ExternalServiceError
from app.models.integracion import IntegrationProvider


class TestBuildAuthorizationUrl:
    def test_raises_when_no_client_id_configured(self) -> None:
        from app.services.microsoft_graph_service import build_authorization_url

        with patch("app.services.microsoft_graph_service.settings") as mock_settings:
            mock_settings.AZURE_AD_CLIENT_ID = ""
            with pytest.raises(ExternalServiceError):
                build_authorization_url(uuid.uuid4())

    def test_redirects_to_microsoft_authorize_endpoint_with_scopes_and_pkce(self) -> None:
        from app.services.microsoft_graph_service import build_authorization_url

        with patch("app.services.microsoft_graph_service.settings") as mock_settings:
            mock_settings.AZURE_AD_CLIENT_ID = "test-client-id"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost:8000/api/v1/integraciones/microsoft/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read", "Calendars.Read", "Files.Read"]
            with patch("app.services.integration_service.settings") as mock_integ_settings:
                mock_integ_settings.JWT_SECRET_KEY = "test-secret-key-min-32-chars-xx"
                mock_integ_settings.JWT_ALGORITHM = "HS256"
                mock_integ_settings.GOOGLE_OAUTH_STATE_TTL_SECONDS = 600

                result = build_authorization_url(uuid.uuid4())

        parsed = urlparse(result.authorization_url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "login.microsoftonline.com"
        assert parsed.path == "/common/oauth2/v2.0/authorize"

        query = parse_qs(parsed.query)
        assert query["client_id"] == ["test-client-id"]
        assert query["response_type"] == ["code"]
        scope_values = query["scope"][0].split()
        assert "Mail.Read" in scope_values
        assert "Calendars.Read" in scope_values
        assert "Files.Read" in scope_values
        assert query["code_challenge_method"] == ["S256"]
        assert len(query["code_challenge"][0]) > 0
        assert query["state"][0] == result.state

    def test_each_call_generates_a_distinct_code_challenge(self) -> None:
        from app.services.microsoft_graph_service import build_authorization_url

        with patch("app.services.microsoft_graph_service.settings") as mock_settings:
            mock_settings.AZURE_AD_CLIENT_ID = "test-client-id"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost:8000/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read"]
            with patch("app.services.integration_service.settings") as mock_integ_settings:
                mock_integ_settings.JWT_SECRET_KEY = "test-secret-key-min-32-chars-xx"
                mock_integ_settings.JWT_ALGORITHM = "HS256"
                mock_integ_settings.GOOGLE_OAUTH_STATE_TTL_SECONDS = 600

                r1 = build_authorization_url(uuid.uuid4())
                r2 = build_authorization_url(uuid.uuid4())

        q1 = parse_qs(urlparse(r1.authorization_url).query)
        q2 = parse_qs(urlparse(r2.authorization_url).query)
        assert q1["code_challenge"][0] != q2["code_challenge"][0]


class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_exchanges_code_and_fetches_account_email(self) -> None:
        from app.services.microsoft_graph_service import exchange_code

        token_response = MagicMock()
        token_response.raise_for_status = MagicMock()
        token_response.json.return_value = {
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
            "scope": "Mail.Read Calendars.Read",
        }
        me_response = MagicMock()
        me_response.raise_for_status = MagicMock()
        me_response.json.return_value = {"mail": "user@contoso.com"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=token_response)
        mock_client.get = AsyncMock(return_value=me_response)
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with (
            patch("app.services.microsoft_graph_service.settings") as mock_settings,
            patch("app.services.microsoft_graph_service.httpx.AsyncClient", return_value=mock_client),
        ):
            mock_settings.AZURE_AD_CLIENT_ID = "test-client-id"
            mock_settings.AZURE_AD_CLIENT_SECRET = "test-secret"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost:8000/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read", "Calendars.Read"]

            result = await exchange_code(code="auth-code", code_verifier="verifier-value")

        assert result["access_token"] == "at-123"
        assert result["refresh_token"] == "rt-456"
        assert result["expires_in"] == 3600
        assert result["email"] == "user@contoso.com"
        assert "Mail.Read" in result["scopes"]

    @pytest.mark.asyncio
    async def test_missing_client_id_raises_external_service_error(self) -> None:
        from app.services.microsoft_graph_service import exchange_code

        with patch("app.services.microsoft_graph_service.settings") as mock_settings:
            mock_settings.AZURE_AD_CLIENT_ID = ""
            with pytest.raises(ExternalServiceError):
                await exchange_code(code="auth-code", code_verifier="verifier-value")

    @pytest.mark.asyncio
    async def test_token_endpoint_http_error_raises_external_service_error(self) -> None:
        from app.services.microsoft_graph_service import exchange_code

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=MagicMock())
        )
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with (
            patch("app.services.microsoft_graph_service.settings") as mock_settings,
            patch("app.services.microsoft_graph_service.httpx.AsyncClient", return_value=mock_client),
        ):
            mock_settings.AZURE_AD_CLIENT_ID = "test-client-id"
            mock_settings.AZURE_AD_CLIENT_SECRET = "test-secret"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost:8000/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read"]

            with pytest.raises(ExternalServiceError):
                await exchange_code(code="auth-code", code_verifier="verifier-value")

    @pytest.mark.asyncio
    async def test_email_fetch_failure_defaults_to_empty_string_without_failing_exchange(self) -> None:
        """The token exchange itself succeeded; a Graph /me hiccup shouldn't block connecting."""
        from app.services.microsoft_graph_service import exchange_code

        token_response = MagicMock()
        token_response.raise_for_status = MagicMock()
        token_response.json.return_value = {
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 3600,
            "scope": "Mail.Read",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=token_response)
        mock_client.get = AsyncMock(side_effect=httpx.HTTPStatusError("bad", request=MagicMock(), response=MagicMock()))
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        with (
            patch("app.services.microsoft_graph_service.settings") as mock_settings,
            patch("app.services.microsoft_graph_service.httpx.AsyncClient", return_value=mock_client),
        ):
            mock_settings.AZURE_AD_CLIENT_ID = "test-client-id"
            mock_settings.AZURE_AD_CLIENT_SECRET = "test-secret"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost:8000/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read"]

            result = await exchange_code(code="auth-code", code_verifier="verifier-value")

        assert result["email"] == ""
        assert result["access_token"] == "at-123"


class TestHandleOAuthCallback:
    @pytest.mark.asyncio
    async def test_stores_credentials_with_provider_microsoft_and_returns_status(self) -> None:
        from app.services import microsoft_graph_service

        db = MagicMock()
        usuario_id = uuid.uuid4()

        with (
            patch(
                "app.services.microsoft_graph_service.exchange_code",
                AsyncMock(
                    return_value={
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_in": 3600,
                        "scopes": ["Mail.Read"],
                        "email": "user@contoso.com",
                    }
                ),
            ),
            patch("app.services.integration_service.store_credentials", AsyncMock()) as mock_store,
            patch(
                "app.services.integration_service.get_integration_status",
                AsyncMock(return_value="status-sentinel"),
            ) as mock_status,
        ):
            result = await microsoft_graph_service.handle_oauth_callback(
                db=db, usuario_id=usuario_id, code="c", code_verifier="cv"
            )

        assert result == "status-sentinel"
        mock_store.assert_awaited_once()
        _, kwargs = mock_store.call_args
        assert mock_store.call_args.args[2] == IntegrationProvider.MICROSOFT
        assert kwargs["email"] == "user@contoso.com"
        mock_status.assert_awaited_once_with(db, usuario_id, IntegrationProvider.MICROSOFT)
