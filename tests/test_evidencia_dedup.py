"""Tests for evidencia upload dedup by content hash (Req 10a, migración 038).

Re-uploading identical bytes must not create a new row or storage object, and
a commit failure right after an upload must not leave an orphaned storage
object behind. Mirrors fixtures/conventions from tests/test_evidencia.py.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.services import evidencia_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CONTENIDO = b"contenido identico de evidencia para dedup"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key.pdf"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


async def _crear_contrato(db: AsyncSession, usuario_id: Any, numero: str) -> Contrato:
    c = Contrato(
        usuario_id=usuario_id,
        numero_contrato=numero,
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


async def _crear_cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
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
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    return await _crear_contrato(db, test_user["user"].id, "CTR-DEDUP-001")


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    return await _crear_cuenta(db, contrato)


async def test_reupload_misma_cuenta_no_duplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(a) Re-uploading identical bytes to the same cuenta → 1 row + 1 storage
    object; the second response marks itself as duplicate."""
    user = test_user["user"]
    storage = _mock_storage()

    r1 = await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )
    r2 = await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id,
        archivos=[("doc-otra-vez.txt", "text/plain", _CONTENIDO)],
    )

    assert r1[0].duplicada is False
    assert r2[0].duplicada is True
    assert r2[0].id == r1[0].id
    assert storage.upload.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 1


async def test_misma_bytes_otra_cuenta_crea_fila_nueva(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(b) Same bytes uploaded to a DIFFERENT cuenta → a new row IS created."""
    user = test_user["user"]
    storage = _mock_storage()

    otro_contrato = await _crear_contrato(db, user.id, "CTR-DEDUP-002")
    otra_cuenta = await _crear_cuenta(db, otro_contrato)

    await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )
    r2 = await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=otra_cuenta.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )

    assert r2[0].duplicada is False
    assert storage.upload.call_count == 2

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 2


async def test_dos_archivos_identicos_en_un_lote_se_guardan_una_vez(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(d) Two identical files in ONE upload batch → stored once."""
    user = test_user["user"]
    storage = _mock_storage()

    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id,
        archivos=[
            ("copia1.txt", "text/plain", _CONTENIDO),
            ("copia2.txt", "text/plain", _CONTENIDO),
        ],
    )

    assert resultados[0].duplicada is False
    assert resultados[1].duplicada is True
    assert resultados[1].id == resultados[0].id
    assert storage.upload.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 1


async def test_commit_fallido_borra_objeto_huerfano_en_storage(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(c) A commit failure right after the storage upload must trigger a
    best-effort `storage.delete` for the just-uploaded object and leave no
    Evidencia row behind — never an orphaned S3-compatible object."""
    user = test_user["user"]
    storage = _mock_storage()

    with (
        patch.object(db, "commit", AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await evidencia_service.subir_evidencias_cuenta(
            db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta_cobro.id,
            archivos=[("doc.txt", "text/plain", _CONTENIDO)],
        )

    storage.upload.assert_called_once()
    uploaded_key = storage.upload.call_args.kwargs["key"]
    storage.delete.assert_called_once_with(key=uploaded_key)

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert rows == []


async def test_subir_evidencia_actividad_reupload_no_duplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """Same dedup contract on the per-actividad single-file upload site
    (`subir_evidencia`) — same actividad, same bytes, second call is marked
    duplicate and no second row/storage object is created."""
    from app.models.actividad import Actividad

    user = test_user["user"]
    storage = _mock_storage()
    a = Actividad(cuenta_cobro_id=cuenta_cobro.id, descripcion="Actividad dedup", fecha_realizacion=date(2024, 3, 15))
    db.add(a)
    await db.commit()
    await db.refresh(a)

    r1 = await evidencia_service.subir_evidencia(
        db=db, storage=storage, usuario_id=user.id, actividad_id=a.id,
        filename="informe.pdf", content_type="application/pdf", data=b"%PDF-1.4 contenido evidencia",
    )
    r2 = await evidencia_service.subir_evidencia(
        db=db, storage=storage, usuario_id=user.id, actividad_id=a.id,
        filename="informe-otra-vez.pdf", content_type="application/pdf", data=b"%PDF-1.4 contenido evidencia",
    )

    assert r1.duplicada is False
    assert r2.duplicada is True
    assert r2.id == r1.id
    assert storage.upload.call_count == 1
