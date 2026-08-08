"""Tests for app/services/integration_service.py — shared, provider-agnostic
credential/state helpers (task A1.5-A1.6, A1.9-A1.10).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from app.core.exceptions import NotFoundError, ValidationError
from app.models.integracion import IntegrationProvider
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

# No module-level `pytestmark` — this file mixes sync (JWT helper) and async (DB-backed)
# tests; `asyncio_mode = "auto"` (pyproject.toml) already detects `async def test_*` tests.


class TestFernet:
    def test_fernet_roundtrip(self) -> None:
        from app.services.integration_service import _fernet
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.TOKEN_ENCRYPTION_KEY = key.decode()
            f = _fernet()
            encrypted = f.encrypt(b"secret")
            assert f.decrypt(encrypted) == b"secret"


class TestOAuthState:
    _SECRET = "test-secret-key-min-32-chars-xx"
    _ALGO = "HS256"

    def _make_state(
        self, usuario_id: Any, ttl_seconds: int = 600, cv: str = "cv", provider: str | None = "google"
    ) -> str:
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "sub": str(usuario_id),
            "type": "oauth_state",
            "cv": cv,
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
        }
        if provider is not None:
            claims["provider"] = provider
        return jwt.encode(claims, self._SECRET, algorithm=self._ALGO)

    def test_round_trip_carries_provider(self) -> None:
        import uuid

        from app.services.integration_service import verify_oauth_state

        uid = uuid.uuid4()
        state = self._make_state(uid, cv="verifier-x", provider="microsoft")
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            returned_uid, returned_cv, provider = verify_oauth_state(state)
            assert returned_uid == uid
            assert returned_cv == "verifier-x"
            assert provider == IntegrationProvider.MICROSOFT

    def test_missing_provider_claim_defaults_to_google(self) -> None:
        """Backward compat: states encoded before this change carry no `provider` claim."""
        import uuid

        from app.services.integration_service import verify_oauth_state

        uid = uuid.uuid4()
        state = self._make_state(uid, provider=None)
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            _uid, _cv, provider = verify_oauth_state(state)
            assert provider == IntegrationProvider.GOOGLE

    def test_tampered_token_raises_validation_error(self) -> None:
        import uuid

        from app.services.integration_service import verify_oauth_state

        state = self._make_state(uuid.uuid4()) + "TAMPERED"
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            with pytest.raises(ValidationError):
                verify_oauth_state(state)

    def test_expired_token_raises_validation_error(self) -> None:
        import uuid

        from app.services.integration_service import verify_oauth_state

        state = self._make_state(uuid.uuid4(), ttl_seconds=-1)
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            with pytest.raises(ValidationError):
                verify_oauth_state(state)

    def test_encode_then_verify_round_trip(self) -> None:
        import uuid

        from app.services.integration_service import encode_oauth_state, verify_oauth_state

        uid = uuid.uuid4()
        with patch("app.services.integration_service.settings") as mock_settings:
            mock_settings.JWT_SECRET_KEY = self._SECRET
            mock_settings.JWT_ALGORITHM = self._ALGO
            mock_settings.GOOGLE_OAUTH_STATE_TTL_SECONDS = 600
            state = encode_oauth_state(uid, "my-verifier", IntegrationProvider.MICROSOFT)
            returned_uid, returned_cv, provider = verify_oauth_state(state)
            assert returned_uid == uid
            assert returned_cv == "my-verifier"
            assert provider == IntegrationProvider.MICROSOFT


class TestStoreCredentials:
    async def test_creates_new_row_when_none_exists(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import store_credentials

        user = test_user["user"]
        record = await store_credentials(
            db,
            user.id,
            IntegrationProvider.GOOGLE,
            access_token="access-1",
            refresh_token="refresh-1",
            scopes=["scope-a", "scope-b"],
            email="user@example.com",
        )
        assert record.usuario_id == user.id
        assert record.provider == IntegrationProvider.GOOGLE
        assert record.email == "user@example.com"
        assert record.access_token_encrypted != "access-1"  # Fernet-encrypted, not plaintext

    async def test_reconnect_updates_in_place_no_duplicate(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.models.integracion import Integracion
        from app.services.integration_service import store_credentials
        from sqlalchemy import func, select

        user = test_user["user"]
        await store_credentials(
            db,
            user.id,
            IntegrationProvider.GOOGLE,
            access_token="access-1",
            refresh_token="refresh-1",
            scopes=["scope-a"],
            email="user@example.com",
        )
        await store_credentials(
            db,
            user.id,
            IntegrationProvider.GOOGLE,
            access_token="access-2",
            refresh_token="refresh-2",
            scopes=["scope-b"],
            email="user@example.com",
        )

        count = await db.scalar(select(func.count()).select_from(Integracion))
        assert count == 1

        result = await db.execute(select(Integracion).where(Integracion.usuario_id == user.id))
        record = result.scalar_one()
        assert record.scopes == "scope-b"


class TestGetIntegrationStatus:
    async def test_returns_not_connected_when_no_row(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import get_integration_status

        user = test_user["user"]
        status = await get_integration_status(db, user.id, IntegrationProvider.GOOGLE)
        assert status.connected is False
        assert status.provider == IntegrationProvider.GOOGLE

    async def test_returns_connected_with_derived_flags(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import get_integration_status, store_credentials

        user = test_user["user"]
        await store_credentials(
            db,
            user.id,
            IntegrationProvider.GOOGLE,
            access_token="a",
            refresh_token="r",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
            email="user@example.com",
        )
        status = await get_integration_status(db, user.id, IntegrationProvider.GOOGLE)
        assert status.connected is True
        assert status.mail_enabled is True
        assert status.drive_enabled is True
        assert status.calendar_enabled is False


class TestListIntegrationStatuses:
    async def test_empty_when_no_connections(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import list_integration_statuses

        user = test_user["user"]
        statuses = await list_integration_statuses(db, user.id)
        assert statuses == []

    async def test_one_entry_per_connected_provider(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import list_integration_statuses, store_credentials

        user = test_user["user"]
        await store_credentials(
            db, user.id, IntegrationProvider.GOOGLE, access_token="a", refresh_token="r", scopes=["s"], email="a@x.com"
        )
        await store_credentials(
            db,
            user.id,
            IntegrationProvider.MICROSOFT,
            access_token="a2",
            refresh_token="r2",
            scopes=["s2"],
            email="a@x.com",
        )
        statuses = await list_integration_statuses(db, user.id)
        providers = {s.provider for s in statuses}
        assert providers == {IntegrationProvider.GOOGLE, IntegrationProvider.MICROSOFT}


class TestHasAnyConnectedProvider:
    async def test_false_when_no_connections(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import has_any_connected_provider

        user = test_user["user"]
        assert await has_any_connected_provider(db, user.id) is False

    async def test_true_when_at_least_one_connection(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import has_any_connected_provider, store_credentials

        user = test_user["user"]
        await store_credentials(
            db,
            user.id,
            IntegrationProvider.MICROSOFT,
            access_token="a",
            refresh_token="r",
            scopes=["s"],
            email="a@x.com",
        )
        assert await has_any_connected_provider(db, user.id) is True


class TestRevokeIntegration:
    async def test_raises_not_found_when_no_row(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import revoke_integration

        user = test_user["user"]
        with pytest.raises(NotFoundError):
            await revoke_integration(db, user.id, IntegrationProvider.GOOGLE)

    async def test_deletes_row_when_found(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import (
            get_integration_status,
            revoke_integration,
            store_credentials,
        )

        user = test_user["user"]
        await store_credentials(
            db, user.id, IntegrationProvider.GOOGLE, access_token="a", refresh_token="r", scopes=["s"], email="a@x.com"
        )
        await revoke_integration(db, user.id, IntegrationProvider.GOOGLE)
        status = await get_integration_status(db, user.id, IntegrationProvider.GOOGLE)
        assert status.connected is False


class TestMultipleAccountsSameProvider:
    """Regression guard: 2+ Integracion rows for one (usuario_id, provider), distinguished
    only by `email`, is the multi-account design goal of this whole slice (store_credentials
    already supports it) — `get_integration_status`/`revoke_integration` must not raise
    sqlalchemy.exc.MultipleResultsFound the moment a second account exists.
    """

    async def _store_two_accounts(self, db: AsyncSession, user_id: Any) -> None:
        from app.services.integration_service import store_credentials

        await store_credentials(
            db,
            user_id,
            IntegrationProvider.GOOGLE,
            access_token="a1",
            refresh_token="r1",
            scopes=["s1"],
            email="first@example.com",
        )
        await store_credentials(
            db,
            user_id,
            IntegrationProvider.GOOGLE,
            access_token="a2",
            refresh_token="r2",
            scopes=["s2"],
            email="second@example.com",
        )

    async def test_get_integration_status_does_not_raise_with_two_accounts(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.integration_service import get_integration_status

        user = test_user["user"]
        await self._store_two_accounts(db, user.id)

        status = await get_integration_status(db, user.id, IntegrationProvider.GOOGLE)
        assert status.connected is True

    async def test_revoke_integration_does_not_raise_with_two_accounts(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.integration_service import revoke_integration

        user = test_user["user"]
        await self._store_two_accounts(db, user.id)

        # Must resolve to exactly one row deterministically, not raise MultipleResultsFound.
        await revoke_integration(db, user.id, IntegrationProvider.GOOGLE)

    async def test_email_param_targets_one_specific_account(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        from app.services.integration_service import get_integration_status

        user = test_user["user"]
        await self._store_two_accounts(db, user.id)

        status = await get_integration_status(db, user.id, IntegrationProvider.GOOGLE, email="first@example.com")
        assert status.email == "first@example.com"
