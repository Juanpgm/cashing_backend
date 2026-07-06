"""Tests for evidencia service and API upload/download endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.actividad import Actividad
from app.models.evidencia import Evidencia
from app.services import evidencia_service

pytestmark = pytest.mark.asyncio

_PDF_MAGIC = b"%PDF-1.4 sample pdf content here"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-EVI-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="SENA",
        dependencia="Sistemas",
        supervisor_nombre="Pedro",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def actividad(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=cuenta_cobro.id,
        descripcion="Reunión de seguimiento",
        fecha_realizacion=date(2024, 3, 15),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key.pdf"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


# ── Service tests ──────────────────────────────────────────────────────────────


async def test_subir_evidencia(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    result = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="informe.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    assert result.nombre_archivo == "informe.pdf"
    assert result.tamano_bytes == len(_PDF_MAGIC)
    assert result.presigned_url == "https://s3.example.com/presigned"


async def test_subir_evidencia_extension_invalida(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    storage = _mock_storage()
    with pytest.raises(ValidationError):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            filename="virus.exe",
            content_type="application/octet-stream",
            data=b"MZ...",
        )


async def test_listar_evidencias(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    for i in range(2):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            filename=f"doc{i}.pdf",
            content_type="application/pdf",
            data=_PDF_MAGIC,
        )
    evidencias = await evidencia_service.listar_evidencias(db, user.id, actividad.id)
    assert len(evidencias) == 2


async def test_obtener_url_descarga(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="doc.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    result = await evidencia_service.obtener_url_descarga(db, storage, user.id, uploaded.id)
    assert result.presigned_url == "https://s3.example.com/presigned"
    assert result.nombre_archivo == "doc.pdf"


async def test_eliminar_evidencia(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.core.exceptions import NotFoundError

    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="del.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    await evidencia_service.eliminar_evidencia(db, storage, user.id, uploaded.id)
    storage.delete.assert_called_once()

    with pytest.raises(NotFoundError):
        await evidencia_service.obtener_url_descarga(db, storage, user.id, uploaded.id)


async def test_evidencia_actividad_otro_usuario_falla(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.core.exceptions import NotFoundError

    # Use a random unknown UUID as the "other user"
    other_user_id = uuid.uuid4()
    storage = _mock_storage()
    with pytest.raises(NotFoundError):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=other_user_id,
            actividad_id=actividad.id,
            filename="spy.pdf",
            content_type="application/pdf",
            data=_PDF_MAGIC,
        )
