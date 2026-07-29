"""Tests for GET /debug/config secret masking.

Regression coverage for the SECRET_FIELDS set in app/api/v1/debug.py::_safe_config
— every OAuth client secret must be masked before the (unauthenticated, dev-only)
/debug/config route returns it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestSafeConfigMasksOAuthSecrets:
    @pytest.mark.asyncio
    async def test_config_masks_google_and_microsoft_oauth_secrets(self, client):
        with patch("app.api.v1.debug.settings") as mock_settings:
            mock_settings.model_fields = {
                "GOOGLE_OAUTH_CLIENT_ID": None,
                "GOOGLE_OAUTH_CLIENT_SECRET": None,
                "AZURE_AD_CLIENT_ID": None,
                "AZURE_AD_CLIENT_SECRET": None,
            }
            mock_settings.GOOGLE_OAUTH_CLIENT_ID = "google-client-id"
            mock_settings.GOOGLE_OAUTH_CLIENT_SECRET = "google-client-secret"
            mock_settings.AZURE_AD_CLIENT_ID = "azure-client-id"
            mock_settings.AZURE_AD_CLIENT_SECRET = "azure-client-secret"
            mock_settings.ENVIRONMENT = "development"
            mock_settings.is_production = False
            mock_settings.is_development = True

            resp = await client.get("/api/v1/debug/config")

        assert resp.status_code == 200
        body = resp.json()
        assert body["GOOGLE_OAUTH_CLIENT_ID"] == "****"
        assert body["GOOGLE_OAUTH_CLIENT_SECRET"] == "****"
        assert body["AZURE_AD_CLIENT_ID"] == "****"
        assert body["AZURE_AD_CLIENT_SECRET"] == "****"
