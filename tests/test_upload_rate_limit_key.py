"""Upload rate limiting must not be keyed on a client-controlled header.

The upload endpoints are always authenticated, so `upload_rate_limit_key` keys on
`request.state.user_id` (set by the auth dependency before slowapi evaluates this
function). There is no reachable anonymous branch on these endpoints: falling back
to `X-Forwarded-For` would let any caller forge their rate-limit bucket by rotating
the header, since a Railway-style proxy only APPENDS to that header — the leftmost
hop is fully attacker-controlled. The only safe fallback is the socket peer
(`get_remote_address`), same as every other limiter in this app.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.rate_limit import limiter, upload_rate_limit_key
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

_PDF_MAGIC = b"%PDF-1.4\n"
_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"


def _request(
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("203.0.113.9", 54321),
    user_id: str | None = None,
) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/documentos/upload",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "state": {},
    }
    request = Request(scope)
    if user_id is not None:
        request.state.user_id = user_id
    return request


class TestUploadRateLimitKey:
    def test_authenticated_user_is_keyed_by_user_id(self) -> None:
        """Two users behind the same proxy must get independent budgets."""
        uno = _request(headers={"x-forwarded-for": "10.0.0.7"}, user_id="user-a")
        otro = _request(headers={"x-forwarded-for": "10.0.0.7"}, user_id="user-b")

        assert upload_rate_limit_key(uno) == "user:user-a"
        assert upload_rate_limit_key(uno) != upload_rate_limit_key(otro)

    def test_spoofed_forwarded_header_does_not_change_the_key(self) -> None:
        """A caller rotating `X-Forwarded-For` must not get a fresh rate-limit bucket."""
        sin_cabecera = _request()
        con_cabecera_falsa = _request(headers={"x-forwarded-for": "198.51.100.4, 10.0.0.7, 10.0.0.8"})

        assert upload_rate_limit_key(sin_cabecera) == "203.0.113.9"
        assert upload_rate_limit_key(con_cabecera_falsa) == "203.0.113.9"

    def test_falls_back_to_the_socket_peer_without_user_or_forwarded_header(self) -> None:
        assert upload_rate_limit_key(_request()) == "203.0.113.9"

    def test_authenticated_user_key_ignores_a_spoofed_forwarded_header(self) -> None:
        """The user-keyed branch must not be swayed by a client-controlled header either."""
        con_cabecera_falsa = _request(headers={"x-forwarded-for": "6.6.6.6"}, user_id="user-a")

        assert upload_rate_limit_key(con_cabecera_falsa) == "user:user-a"


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


async def _segundo_usuario(db: AsyncSession) -> dict[str, Any]:
    from app.core.security import create_access_token, hash_password
    from app.models.usuario import Usuario

    user = Usuario(
        email="otro@example.com",
        nombre="Otro User",
        cedula="987654321",
        telefono="+573009876543",
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=str(user.id), role=user.rol)
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.mark.asyncio
async def test_one_user_exhausting_the_budget_does_not_block_another(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """End-to-end: user A burns the 10/minute budget, user B must still be served."""
    cuenta_a = await _crear_cuenta(db, test_user["user"].id, "CD-RLKEY-A")
    otro = await _segundo_usuario(db)
    cuenta_b = await _crear_cuenta(db, otro["user"].id, "CD-RLKEY-B")

    enabled_previo = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = AsyncMock()
            storage.upload = AsyncMock()
            storage.delete = AsyncMock()
            mock_storage_cls.return_value = storage

            estados_a = []
            for i in range(11):
                r = await client.post(
                    "/api/v1/documentos/upload-batch",
                    headers=test_user["headers"],
                    params={
                        "tipo": "otros",
                        "cuenta_cobro_id": str(cuenta_a.id),
                        "requisito_codigo": "EVIDENCIAS",
                    },
                    files={"files": (f"a-{i}.pdf", _PDF_MAGIC + bytes([i]) * 2048, "application/pdf")},
                )
                estados_a.append(r.status_code)

            respuesta_b = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=otro["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta_b.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"files": ("b-0.pdf", _PDF_MAGIC + b"\xaa" * 2048, "application/pdf")},
            )
    finally:
        limiter.reset()
        limiter.enabled = enabled_previo

    assert 429 in estados_a, f"expected user A to hit the limit: {estados_a}"
    assert respuesta_b.status_code != 429, "user B must not inherit user A's exhausted budget"
