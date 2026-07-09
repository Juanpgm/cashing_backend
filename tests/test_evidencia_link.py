"""Tests for link-type evidencias (evidence records that point to an external URL
instead of an uploaded file) — model, download, and delete behavior."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.services import evidencia_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-LINK-001",
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
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


# ── Model tests ───────────────────────────────────────────────────────────────


async def test_evidencia_model_accepts_link_only_row(db: AsyncSession, actividad: Actividad) -> None:
    """A link-type Evidencia has no storage_key/tipo_archivo/tamano_bytes, only fuente + url."""
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="email",
        url="https://mail.google.com/mail/u/0/#all/abc123",
        nombre_archivo="Informe mensual",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    assert evidencia.id is not None
    assert evidencia.storage_key is None
    assert evidencia.tipo_archivo is None
    assert evidencia.tamano_bytes is None
    assert evidencia.fuente == "email"
    assert evidencia.url.startswith("https://mail.google.com")


# ── Service tests ─────────────────────────────────────────────────────────────


async def test_obtener_url_descarga_link_evidencia_no_storage_call(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """Downloading a link evidencia returns its url directly, without hitting storage."""
    user = test_user["user"]
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="drive",
        url="https://drive.google.com/file/d/xyz",
        nombre_archivo="Contrato firmado",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    storage = _mock_storage()
    result = await evidencia_service.obtener_url_descarga(db, storage, user.id, evidencia.id)

    assert result.presigned_url == "https://drive.google.com/file/d/xyz"
    storage.presigned_url.assert_not_called()


async def test_eliminar_evidencia_link_no_storage_call(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """Deleting a link evidencia removes the row without calling storage.delete."""
    user = test_user["user"]
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="calendar",
        url="https://calendar.google.com/event?eid=abc",
        nombre_archivo="Reunión de seguimiento",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    storage = _mock_storage()
    await evidencia_service.eliminar_evidencia(db, storage, user.id, evidencia.id)

    storage.delete.assert_not_called()
    with pytest.raises(NotFoundError):
        await evidencia_service.obtener_url_descarga(db, storage, user.id, evidencia.id)
