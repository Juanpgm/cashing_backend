"""Microsoft Graph service — Azure AD authorization-code + PKCE OAuth2 flow.

Mirrors google_workspace_service.py's OAuth shape for Microsoft 365 (Outlook
Mail, Outlook Calendar, OneDrive via Graph). Token/state persistence and
crypto are NOT re-implemented here — this module builds the PKCE
authorization URL, exchanges the code for tokens, and fetches the connected
account's email, then delegates storage to `integration_service` (provider-
agnostic). See openspec/changes/microsoft-365-integration/design.md D2.

Graph EmailPort/DrivePort/CalendarPort adapter calls (using the credentials
acquired here) are out of scope for this module — see Slice C1's
`app/adapters/microsoft/graph_adapter.py`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ExternalServiceError
from app.models.integracion import IntegrationProvider
from app.schemas.google_workspace import GoogleConnectURLResponse
from app.schemas.integracion import IntegrationStatus
from app.services import integration_service

logger = structlog.get_logger("services.microsoft_graph")

_AUTHORIZE_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for RFC 7636 PKCE, method S256."""
    code_verifier = secrets.token_urlsafe(96)  # 128 chars — satisfies RFC 7636 length requirement
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


def build_authorization_url(usuario_id: uuid.UUID) -> GoogleConnectURLResponse:
    """Build the Microsoft Graph OAuth2 authorization URL for the user to visit.

    Uses PKCE (no client secret needed for the auth request) and embeds the
    code_verifier + usuario_id + provider in a signed state JWT, same pattern
    as Google's `get_authorization_url`.
    """
    if not settings.AZURE_AD_CLIENT_ID:
        raise ExternalServiceError("Microsoft OAuth", "CLIENT_ID no configurado")

    code_verifier, code_challenge = _generate_pkce_pair()
    state = integration_service.encode_oauth_state(usuario_id, code_verifier, IntegrationProvider.MICROSOFT)

    params = {
        "client_id": settings.AZURE_AD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.AZURE_AD_REDIRECT_URI,
        "response_mode": "query",
        "scope": " ".join(settings.MICROSOFT_OAUTH_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = _AUTHORIZE_URL_TEMPLATE.format(tenant=settings.AZURE_AD_TENANT_ID)
    return GoogleConnectURLResponse(authorization_url=f"{authorize_url}?{urlencode(params)}", state=state)


def _describe_http_status_error(exc: httpx.HTTPStatusError) -> str:
    """Extract Azure's `{"error": ..., "error_description": "AADSTS..."}` body for ops triage.

    Falls back to raw response text (truncated) if the body isn't JSON.
    """
    try:
        payload = exc.response.json()
    except (json.JSONDecodeError, ValueError):
        return exc.response.text[:500]
    if isinstance(payload, dict):
        error = payload.get("error", "")
        description = payload.get("error_description", "")
        detail = " ".join(part for part in (error, description) if part)
        if detail:
            return detail[:500]
    return str(payload)[:500]


async def _fetch_account_email(http_client: httpx.AsyncClient, access_token: str) -> str:
    """Best-effort lookup of the connected account's email via Graph `/me`.

    A failure here must not block the connection — the tokens already
    exchanged successfully. Falls back to "" (store_credentials collapses
    unknown-email connections to one row per (usuario, provider), same as
    Google's current behavior).
    """
    try:
        resp = await http_client.get(_GRAPH_ME_URL, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("microsoft_fetch_email_failed", error=str(exc))
        return ""
    email = data.get("mail") or data.get("userPrincipalName") or ""
    return str(email)


async def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    """Exchange an authorization code + PKCE verifier for Graph tokens.

    Returns a dict with access_token/refresh_token/expires_in/scopes/email —
    the shape `handle_oauth_callback` forwards to `integration_service.store_credentials`.
    """
    if not settings.AZURE_AD_CLIENT_ID:
        raise ExternalServiceError("Microsoft OAuth", "No configurado")

    token_url = _TOKEN_URL_TEMPLATE.format(tenant=settings.AZURE_AD_TENANT_ID)
    data = {
        "client_id": settings.AZURE_AD_CLIENT_ID,
        "client_secret": settings.AZURE_AD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.AZURE_AD_REDIRECT_URI,
        "code_verifier": code_verifier,
        "scope": " ".join(settings.MICROSOFT_OAUTH_SCOPES),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            token_resp = await http_client.post(token_url, data=data)
            token_resp.raise_for_status()
            token_data = token_resp.json()
            email = await _fetch_account_email(http_client, token_data["access_token"])
    except httpx.HTTPStatusError as exc:
        detail = _describe_http_status_error(exc)
        logger.error("microsoft_oauth_token_exchange_failed", status=exc.response.status_code, detail=detail)
        raise ExternalServiceError("Microsoft OAuth", f"HTTP {exc.response.status_code}: {detail}") from exc
    except httpx.RequestError as exc:
        logger.error("microsoft_oauth_token_exchange_failed", error=str(exc))
        raise ExternalServiceError("Microsoft OAuth", f"Error intercambiando código: {exc}") from exc
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("microsoft_oauth_token_exchange_failed", error=str(exc))
        raise ExternalServiceError("Microsoft OAuth", f"Respuesta de token malformada: {exc}") from exc

    scope_str = token_data.get("scope", "")
    scopes = scope_str.split() if scope_str else list(settings.MICROSOFT_OAUTH_SCOPES)

    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 3600),
        "scopes": scopes,
        "email": email,
    }


async def handle_oauth_callback(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    code: str,
    code_verifier: str,
) -> IntegrationStatus:
    """Exchange the authorization code for tokens and persist encrypted in `integraciones`."""
    token_data = await exchange_code(code, code_verifier)
    await integration_service.store_credentials(
        db,
        usuario_id,
        IntegrationProvider.MICROSOFT,
        access_token=token_data["access_token"],
        refresh_token=token_data["refresh_token"],
        scopes=token_data["scopes"],
        expires_in=token_data["expires_in"],
        email=token_data["email"],
    )
    logger.info("microsoft_oauth_connected", user_id=str(usuario_id))
    return await integration_service.get_integration_status(db, usuario_id, IntegrationProvider.MICROSOFT)
