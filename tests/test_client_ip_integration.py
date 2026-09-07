"""Integration tests: `TrustedProxyClientMiddleware` wired into the real app.

Verifies the resolved client IP actually reaches rate limiting (`get_remote_address`
reads `request.client.host`) and the audit log, end-to-end through the FastAPI
app — not just the pure function/middleware unit tested in `test_client_ip.py`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core import audit as audit_module
from app.core.config import settings
from app.core.rate_limit import limiter
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_PDF_MAGIC = b"%PDF-1.4\n"
_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"


@pytest.fixture
def trust_proxy_headers_forced_on() -> Any:
    """Force `TRUST_PROXY_HEADERS=True` for one test, independent of RAILWAY_ENVIRONMENT."""
    previous = settings.TRUST_PROXY_HEADERS
    settings.TRUST_PROXY_HEADERS = True
    yield
    settings.TRUST_PROXY_HEADERS = previous


@pytest.fixture
def rate_limiter_enabled() -> Any:
    """conftest.py disables the limiter globally; re-enable it for one test."""
    previous = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.reset()
    limiter.enabled = previous


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID, numero: str) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Objeto de prueba",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2025,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)

    from app.services import checklist_service

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta


@pytest.mark.asyncio
class TestRateLimitBucketIsolationBehindProxy:
    """Login is 5/minute (`app/api/v1/auth.py`), keyed by `get_remote_address`."""

    async def test_two_x_real_ip_values_get_separate_buckets(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
        trust_proxy_headers_forced_on: None,
        rate_limiter_enabled: None,
    ) -> None:
        login_body = {"email": test_user["user"].email, "password": "TestPass123!"}

        statuses_a = []
        for _ in range(6):
            r = await client.post(
                "/api/v1/auth/login",
                json=login_body,
                headers={"X-Real-IP": "203.0.113.11"},
            )
            statuses_a.append(r.status_code)
        assert 429 in statuses_a, f"expected IP A to exhaust its 5/minute budget: {statuses_a}"

        r_b = await client.post(
            "/api/v1/auth/login",
            json=login_body,
            headers={"X-Real-IP": "203.0.113.22"},
        )
        assert r_b.status_code != 429, "a different X-Real-IP must get its own bucket"

    async def test_forged_xff_leftmost_shares_bucket_with_the_real_rightmost(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
        trust_proxy_headers_forced_on: None,
        rate_limiter_enabled: None,
    ) -> None:
        """Rotating the client-controlled leftmost hop must NOT reset the bucket."""
        login_body = {"email": test_user["user"].email, "password": "TestPass123!"}

        statuses = []
        for i in range(6):
            r = await client.post(
                "/api/v1/auth/login",
                json=login_body,
                # Leftmost hop changes every request (attacker-controlled);
                # the rightmost (proxy-appended, real) hop stays constant.
                headers={"X-Forwarded-For": f"{i}.{i}.{i}.{i}, 203.0.113.55"},
            )
            statuses.append(r.status_code)

        assert 429 in statuses, f"rotating the forged leftmost hop must not dodge the limit: {statuses}"

    async def test_different_real_rightmost_hop_gets_a_separate_bucket(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
        trust_proxy_headers_forced_on: None,
        rate_limiter_enabled: None,
    ) -> None:
        """Same forged leftmost for two different real clients must not merge their buckets."""
        login_body = {"email": test_user["user"].email, "password": "TestPass123!"}

        statuses_a = []
        for _ in range(6):
            r = await client.post(
                "/api/v1/auth/login",
                json=login_body,
                headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.66"},
            )
            statuses_a.append(r.status_code)
        assert 429 in statuses_a

        r_b = await client.post(
            "/api/v1/auth/login",
            json=login_body,
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.77"},
        )
        assert r_b.status_code != 429, "a different real (rightmost) client must not inherit the exhausted budget"


@pytest.mark.asyncio
class TestAuditLogRecordsResolvedIp:
    async def test_audit_log_records_the_resolved_ip_not_the_socket_peer(
        self,
        client: AsyncClient,
        trust_proxy_headers_forced_on: None,
    ) -> None:
        with patch.object(audit_module.logger, "ainfo", new=AsyncMock()) as mock_ainfo:
            response = await client.get("/health", headers={"X-Real-IP": "203.0.113.44"})

        assert response.status_code == 200
        recorded_ips = [call.kwargs.get("ip") for call in mock_ainfo.await_args_list]
        assert "203.0.113.44" in recorded_ips

    async def test_audit_log_falls_back_to_peer_when_flag_is_off(self, client: AsyncClient) -> None:
        """Sanity check: without the flag, a forged header must NOT reach the audit log."""
        previous = settings.TRUST_PROXY_HEADERS
        settings.TRUST_PROXY_HEADERS = False
        try:
            with patch.object(audit_module.logger, "ainfo", new=AsyncMock()) as mock_ainfo:
                response = await client.get("/health", headers={"X-Real-IP": "203.0.113.44"})
        finally:
            settings.TRUST_PROXY_HEADERS = previous

        assert response.status_code == 200
        recorded_ips = [call.kwargs.get("ip") for call in mock_ainfo.await_args_list]
        assert "203.0.113.44" not in recorded_ips


@pytest.mark.asyncio
class TestStreamingUploadStillWorksThroughTheMiddleware:
    async def test_multipart_upload_succeeds_with_the_middleware_installed(
        self,
        client: AsyncClient,
        db: AsyncSession,
        test_user: dict[str, Any],
        trust_proxy_headers_forced_on: None,
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id, "CD-CLIENTIP-01")

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = AsyncMock()
            storage.upload = AsyncMock()
            storage.delete = AsyncMock()
            mock_storage_cls.return_value = storage

            response = await client.post(
                "/api/v1/documentos/upload-batch",
                headers={**test_user["headers"], "X-Real-IP": "203.0.113.99"},
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"files": ("evidencia.pdf", _PDF_MAGIC + b"\xaa" * 4096, "application/pdf")},
            )

        assert response.status_code == 201


class TestTrustProxyHeadersEffectiveAutoDetection:
    def test_railway_environment_present_makes_it_effective_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        previous = settings.TRUST_PROXY_HEADERS
        settings.TRUST_PROXY_HEADERS = None
        try:
            assert settings.trust_proxy_headers_effective is True
        finally:
            settings.TRUST_PROXY_HEADERS = previous

    def test_no_railway_environment_defaults_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        previous = settings.TRUST_PROXY_HEADERS
        settings.TRUST_PROXY_HEADERS = None
        try:
            assert settings.trust_proxy_headers_effective is False
        finally:
            settings.TRUST_PROXY_HEADERS = previous

    def test_explicit_false_overrides_railway_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        previous = settings.TRUST_PROXY_HEADERS
        settings.TRUST_PROXY_HEADERS = False
        try:
            assert settings.trust_proxy_headers_effective is False
        finally:
            settings.TRUST_PROXY_HEADERS = previous

    def test_explicit_true_wins_even_without_railway_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        previous = settings.TRUST_PROXY_HEADERS
        settings.TRUST_PROXY_HEADERS = True
        try:
            assert settings.trust_proxy_headers_effective is True
        finally:
            settings.TRUST_PROXY_HEADERS = previous
