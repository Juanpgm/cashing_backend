"""Integration tests for GET/DELETE /integraciones/{provider}/connect|callback|status|revoke.

Generalized per design.md D1 — a validated `{provider}` path parameter dispatches
to Google or Microsoft OAuth. Covers the regression requirement (existing
/google/* URLs keep resolving identically) alongside the new /microsoft/* routes.
`test_calendar`/`test_drive` stay Google-only in this slice (see apply-progress.md
deviation note — those need MicrosoftGraphAdapter, Slice C1).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from app.models.integracion import IntegrationProvider
from app.services import integration_service


class TestProviderPathValidation:
    @pytest.mark.asyncio
    async def test_unknown_provider_on_connect_fails_cleanly(self, client, test_user):
        resp = await client.get("/api/v1/integraciones/dropbox/connect", headers=test_user["headers"])
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_static_second_segment_does_not_mis_route_as_provider(self, client, test_user):
        """`/integraciones/evidencias/status` must fail validation, never silently mis-route."""
        resp = await client.get("/api/v1/integraciones/evidencias/status", headers=test_user["headers"])
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_provider_on_status_fails_cleanly(self, client, test_user):
        resp = await client.get("/api/v1/integraciones/dropbox/status", headers=test_user["headers"])
        assert resp.status_code == 422


class TestConnect:
    @pytest.mark.asyncio
    async def test_google_connect_still_resolves(self, client, test_user):
        with patch("app.services.google_workspace_service.settings") as mock_settings:
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = "client-id"
            mock_settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
            mock_settings.GOOGLE_OAUTH_REDIRECT_URI = "http://localhost/callback"
            mock_settings.GOOGLE_OAUTH_SCOPES = ["scope-a"]
            resp = await client.get("/api/v1/integraciones/google/connect", headers=test_user["headers"])

        assert resp.status_code == 200
        assert "accounts.google.com" in resp.json()["authorization_url"]

    @pytest.mark.asyncio
    async def test_microsoft_connect_resolves_with_pkce(self, client, test_user):
        with patch("app.services.microsoft_graph_service.settings") as mock_settings:
            mock_settings.AZURE_AD_CLIENT_ID = "client-id"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.AZURE_AD_REDIRECT_URI = "http://localhost/callback"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read", "Calendars.Read", "Files.Read"]
            resp = await client.get("/api/v1/integraciones/microsoft/connect", headers=test_user["headers"])

        assert resp.status_code == 200
        body = resp.json()
        assert "login.microsoftonline.com" in body["authorization_url"]
        assert "code_challenge=" in body["authorization_url"]


class TestCallback:
    @pytest.mark.asyncio
    async def test_callback_with_provider_error_redirects_with_reason(self, client):
        """User clicked Cancel on the provider's consent screen (`error=access_denied`)."""
        with patch("app.api.v1.integraciones.gws.google_handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get(
                "/api/v1/integraciones/google/callback",
                params={"error": "access_denied"},
            )

        assert resp.status_code == 303
        assert "google=error" in resp.headers["location"]
        assert "reason=access_denied" in resp.headers["location"]
        mock_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_missing_code_and_state_redirects_with_reason(self, client):
        with patch("app.api.v1.integraciones.mgs.handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get("/api/v1/integraciones/microsoft/callback")

        assert resp.status_code == 303
        assert "microsoft=error" in resp.headers["location"]
        assert "reason=missing_code_or_state" in resp.headers["location"]
        mock_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_google_callback_still_resolves(self, client, test_user):
        state = integration_service.encode_oauth_state(test_user["user"].id, "cv", IntegrationProvider.GOOGLE)

        with patch("app.api.v1.integraciones.gws.google_handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get(
                "/api/v1/integraciones/google/callback",
                params={"code": "auth-code", "state": state},
            )

        assert resp.status_code == 303
        assert "google=connected" in resp.headers["location"]
        mock_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_microsoft_callback_exchanges_code(self, client, test_user):
        state = integration_service.encode_oauth_state(test_user["user"].id, "cv", IntegrationProvider.MICROSOFT)

        with patch("app.api.v1.integraciones.mgs.handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get(
                "/api/v1/integraciones/microsoft/callback",
                params={"code": "auth-code", "state": state},
            )

        assert resp.status_code == 303
        assert "microsoft=connected" in resp.headers["location"]
        mock_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tampered_state_rejected_before_token_exchange(self, client):
        with patch("app.api.v1.integraciones.gws.google_handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get(
                "/api/v1/integraciones/google/callback",
                params={"code": "auth-code", "state": "tampered.state.value"},
            )

        assert resp.status_code == 303
        assert "google=error" in resp.headers["location"]
        mock_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_provider_mismatch_rejected_before_token_exchange(self, client, test_user):
        """A state minted for google must not authorize a microsoft callback (or vice versa)."""
        state = integration_service.encode_oauth_state(test_user["user"].id, "cv", IntegrationProvider.GOOGLE)

        with patch("app.api.v1.integraciones.mgs.handle_oauth_callback", AsyncMock()) as mock_callback:
            resp = await client.get(
                "/api/v1/integraciones/microsoft/callback",
                params={"code": "auth-code", "state": state},
            )

        assert resp.status_code == 303
        assert "microsoft=error" in resp.headers["location"]
        mock_callback.assert_not_awaited()


class TestStatus:
    @pytest.mark.asyncio
    async def test_google_status_still_resolves_unconnected(self, client, test_user):
        resp = await client.get("/api/v1/integraciones/google/status", headers=test_user["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "google"
        assert body["connected"] is False

    @pytest.mark.asyncio
    async def test_google_status_derives_flags_from_granted_scopes_when_connected(self, client, test_user, db):
        await integration_service.store_credentials(
            db,
            test_user["user"].id,
            IntegrationProvider.GOOGLE,
            access_token="at",
            refresh_token="rt",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
            ],
        )
        await db.commit()

        resp = await client.get("/api/v1/integraciones/google/status", headers=test_user["headers"])

        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "google"
        assert body["connected"] is True
        assert body["mail_enabled"] is True
        assert body["calendar_enabled"] is True
        assert body["drive_enabled"] is False

    @pytest.mark.asyncio
    async def test_microsoft_status_resolves_unconnected(self, client, test_user):
        resp = await client.get("/api/v1/integraciones/microsoft/status", headers=test_user["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["provider"] == "microsoft"
        assert body["connected"] is False

    @pytest.mark.asyncio
    async def test_microsoft_status_derives_flags_from_granted_scopes(self, client, test_user, db):
        await integration_service.store_credentials(
            db,
            test_user["user"].id,
            IntegrationProvider.MICROSOFT,
            access_token="at",
            refresh_token="rt",
            scopes=["Mail.Read", "Calendars.Read"],
        )
        await db.commit()

        resp = await client.get("/api/v1/integraciones/microsoft/status", headers=test_user["headers"])

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is True
        assert body["mail_enabled"] is True
        assert body["calendar_enabled"] is True
        assert body["drive_enabled"] is False


class TestRevoke:
    @pytest.mark.asyncio
    async def test_google_revoke_not_connected_returns_404(self, client, test_user):
        resp = await client.delete("/api/v1/integraciones/google/revoke", headers=test_user["headers"])
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revoking_microsoft_does_not_affect_google(self, client, test_user, db):
        await integration_service.store_credentials(
            db,
            test_user["user"].id,
            IntegrationProvider.GOOGLE,
            access_token="at-g",
            refresh_token="rt-g",
            scopes=["scope"],
        )
        await integration_service.store_credentials(
            db,
            test_user["user"].id,
            IntegrationProvider.MICROSOFT,
            access_token="at-m",
            refresh_token="rt-m",
            scopes=["Mail.Read"],
        )
        await db.commit()

        resp = await client.delete("/api/v1/integraciones/microsoft/revoke", headers=test_user["headers"])
        assert resp.status_code == 200

        google_status = await client.get("/api/v1/integraciones/google/status", headers=test_user["headers"])
        microsoft_status = await client.get("/api/v1/integraciones/microsoft/status", headers=test_user["headers"])
        assert google_status.json()["connected"] is True
        assert microsoft_status.json()["connected"] is False


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnecting_same_microsoft_account_updates_not_duplicates(self, client, test_user, db):
        from app.models.integracion import Integracion
        from sqlalchemy import select

        usuario_id = test_user["user"].id
        with patch(
            "app.services.microsoft_graph_service.exchange_code",
            AsyncMock(
                return_value={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3600,
                    "scopes": ["Mail.Read"],
                    "email": "user@contoso.com",
                }
            ),
        ):
            state = integration_service.encode_oauth_state(usuario_id, "cv", IntegrationProvider.MICROSOFT)
            resp1 = await client.get(
                "/api/v1/integraciones/microsoft/callback",
                params={"code": "auth-code-1", "state": state},
            )
        assert resp1.status_code == 303

        with patch(
            "app.services.microsoft_graph_service.exchange_code",
            AsyncMock(
                return_value={
                    "access_token": "at-2",
                    "refresh_token": "rt-2",
                    "expires_in": 3600,
                    "scopes": ["Mail.Read"],
                    "email": "user@contoso.com",
                }
            ),
        ):
            state2 = integration_service.encode_oauth_state(usuario_id, "cv", IntegrationProvider.MICROSOFT)
            resp2 = await client.get(
                "/api/v1/integraciones/microsoft/callback",
                params={"code": "auth-code-2", "state": state2},
            )
        assert resp2.status_code == 303

        result = await db.execute(
            select(Integracion).where(
                Integracion.usuario_id == usuario_id, Integracion.provider == IntegrationProvider.MICROSOFT
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1

        from cryptography.fernet import Fernet

        f = Fernet(integration_service.settings.TOKEN_ENCRYPTION_KEY.encode())
        stored = rows[0]
        assert f.decrypt(stored.access_token_encrypted.encode()).decode() == "at-2"
        assert f.decrypt(stored.refresh_token_encrypted.encode()).decode() == "rt-2"
        assert stored.email == "user@contoso.com"
