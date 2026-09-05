"""Regression: upload-batch rate limit and 429 body shape.

Second production defect found while diagnosing the 2026-09-04 "File type not
allowed" report: `checklist-full-view.tsx :: handleFiles` fires ONE request per
dropped file, in parallel, all against `POST /documentos/upload-batch`, which
carried `@limiter.limit("3/minute")` (app/api/v1/documentos.py) while `/upload`
carries `10/minute` — dropping 4+ files at once 429s from the 4th file onward.

Also: slowapi's default handler returns `{"error": ...}` only. The frontend's
`formatErrorDetail(err.response.data.detail)` reads `detail`, so it misses the
message entirely and the user sees the raw axios string
"Request failed with status code 429".
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.rate_limit import limiter
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"
_PDF_MAGIC = b"%PDF-1.4\n"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-RATELIM-001",
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


class TestUploadBatchRateLimitMatchesSingleUpload:
    async def test_ten_sequential_uploads_are_all_accepted(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """`/upload-batch` must allow the same 10/minute as `/upload`."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)
        limiter.enabled = True
        limiter.reset()
        statuses: list[int] = []
        try:
            with (
                patch(_PATCH_S3) as mock_storage_cls,
                patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            ):
                mock_storage_cls.return_value = _mock_storage()
                for i in range(10):
                    r = await client.post(
                        "/api/v1/documentos/upload-batch",
                        headers=test_user["headers"],
                        params={
                            "tipo": "otros",
                            "cuenta_cobro_id": str(cuenta.id),
                            "requisito_codigo": "EVIDENCIAS",
                        },
                        files={"files": (f"soporte-{i}.pdf", _PDF_MAGIC + bytes([i]) * 2048, "application/pdf")},
                    )
                    statuses.append(r.status_code)
        finally:
            limiter.enabled = False

        assert 429 not in statuses, f"rate-limited within the 10/minute budget: {statuses}"


class TestRateLimit429BodyIncludesDetail:
    async def test_429_body_carries_both_error_and_detail_keys(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """The frontend's error formatter reads `detail`; slowapi's default body
        only carries `error`, so the message is silently dropped client-side."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)
        limiter.enabled = True
        limiter.reset()
        responses = []
        try:
            with (
                patch(_PATCH_S3) as mock_storage_cls,
                patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            ):
                mock_storage_cls.return_value = _mock_storage()
                for i in range(11):
                    r = await client.post(
                        "/api/v1/documentos/upload-batch",
                        headers=test_user["headers"],
                        params={
                            "tipo": "otros",
                            "cuenta_cobro_id": str(cuenta.id),
                            "requisito_codigo": "EVIDENCIAS",
                        },
                        files={"files": (f"soporte-{i}.pdf", _PDF_MAGIC + bytes([i]) * 2048, "application/pdf")},
                    )
                    responses.append(r)
        finally:
            limiter.enabled = False

        rate_limited = [r for r in responses if r.status_code == 429]
        assert rate_limited, f"expected the 11th request to be rate-limited: {[r.status_code for r in responses]}"

        body = rate_limited[0].json()
        assert "detail" in body, f"429 body missing 'detail' key: {body}"
        assert "error" in body, f"429 body missing 'error' key: {body}"
        assert body["detail"] == body["error"]
        assert body["detail"]
