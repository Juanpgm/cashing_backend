"""Tests for .zip/.rar archive expansion on cuenta-scoped evidence upload (Req 6).

Mirrors fixtures/conventions from tests/test_evidencia.py and
tests/test_evidencia_dedup.py: local contrato/cuenta_cobro fixtures (no
obligaciones registered, so classification never calls an LLM), AsyncMock
storage double, aiosqlite `db` fixture from conftest.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.services import evidencia_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-ARCH-001",
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
    cc = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=3_000_000)
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def test_zip_con_subcarpetas_preserva_rutas_relativas(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    zip_bytes = _make_zip(
        {
            "carpeta/sub/informe.txt": b"contenido del informe",
            "raiz.txt": b"contenido raiz",
        }
    )
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=_mock_storage(),
        usuario_id=test_user["user"].id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("soportes.zip", "application/zip", zip_bytes)],
    )

    nombres = sorted(r.nombre_archivo for r in resultados)
    assert nombres == ["carpeta/sub/informe.txt", "raiz.txt"]
    assert resultados.avisos == []


async def test_zip_omite_basura_y_vacios_con_avisos(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    zip_bytes = _make_zip(
        {
            "__MACOSX/._valido.txt": b"resource fork junk",
            ".DS_Store": b"mac junk",
            "vacio.txt": b"",
            "valido.txt": b"contenido real",
        }
    )
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=_mock_storage(),
        usuario_id=test_user["user"].id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("soportes.zip", "application/zip", zip_bytes)],
    )

    assert [r.nombre_archivo for r in resultados] == ["valido.txt"]
    assert len(resultados.avisos) == 3  # __MACOSX/..., .DS_Store, vacio.txt


async def test_zip_con_extension_bloqueada_se_omite_sin_romper_el_lote(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    zip_bytes = _make_zip(
        {
            "malware.exe": b"MZ fake pe header",
            "valido.txt": b"contenido real",
        }
    )
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=_mock_storage(),
        usuario_id=test_user["user"].id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("soportes.zip", "application/zip", zip_bytes)],
    )

    assert [r.nombre_archivo for r in resultados] == ["valido.txt"]
    assert any("malware.exe" in aviso for aviso in resultados.avisos)


async def test_rar_sin_soporte_lanza_error_con_mensaje_exacto(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    with (
        patch("app.services.evidencia_service.shutil.which", return_value=None),
        pytest.raises(ValidationError) as exc_info,
    ):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=_mock_storage(),
            usuario_id=test_user["user"].id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("soportes.rar", "application/x-rar-compressed", b"not a real rar")],
        )
    assert exc_info.value.detail == "El soporte para archivos .rar no está disponible en este servidor; usá .zip."


async def test_zip_vacio_o_todo_basura_lanza_error(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    zip_bytes = _make_zip({"__MACOSX/._x.txt": b"junk", ".DS_Store": b"junk"})
    with pytest.raises(ValidationError) as exc_info:
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=_mock_storage(),
            usuario_id=test_user["user"].id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("soportes.zip", "application/zip", zip_bytes)],
        )
    assert exc_info.value.detail == "El comprimido no contiene evidencias válidas."
