"""Tests for evidencia service and API upload/download endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import evidencia_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def test_subir_evidencia(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
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


async def test_listar_evidencias(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
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


async def test_obtener_url_descarga(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
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


async def test_eliminar_evidencia(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
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


# ── subir_evidencias_cuenta (cuenta-scoped upload, no pre-existing actividad) ──


@pytest.fixture
async def obligacion_informes(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales de consultoria y asesoria especializada",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
        etiqueta="OB1",
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # `contrato` was already loaded (and its lazy="selectin" `obligaciones` populated
    # as empty) by the `contrato` fixture before this Obligacion existed — set the
    # in-memory collection directly so later queries in this same session/test see it
    # (same pattern as tests/test_generar_actividades_agente.py).
    contrato.obligaciones = [ob]
    return ob


def _txt_file(text: str) -> bytes:
    return text.encode("utf-8")


async def test_subir_evidencias_cuenta_clasifica_por_keyword(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[
            (
                "informe.txt",
                "text/plain",
                _txt_file("Informe tecnico mensual de consultoria y asesoria especializada entregado."),
            )
        ],
    )
    assert len(resultados) == 1
    assert resultados[0].clasificado is True
    assert resultados[0].obligacion_id == obligacion_informes.id
    assert resultados[0].obligacion_etiqueta == "OB1"


async def test_subir_evidencias_cuenta_sin_match_comparte_un_stub(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[
            ("foto1.txt", "text/plain", _txt_file("contenido totalmente ajeno sin relacion alguna")),
            ("foto2.txt", "text/plain", _txt_file("otro contenido igualmente ajeno y distinto")),
        ],
    )
    assert len(resultados) == 2
    assert all(r.clasificado is False and r.obligacion_id is None for r in resultados)
    # Both unclassified files must share the SAME stub Actividad, not one each.
    assert resultados[0].actividad_id == resultados[1].actividad_id

    count_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_cobro.id))
    assert len(count_result.scalars().all()) == 1


async def test_subir_evidencias_cuenta_reutiliza_actividad_misma_obligacion(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    texto = "Informe tecnico mensual de consultoria y asesoria especializada entregado."
    r1 = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("informe1.txt", "text/plain", _txt_file(texto))],
    )
    r2 = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("informe2.txt", "text/plain", _txt_file(texto))],
    )
    assert r1[0].actividad_id == r2[0].actividad_id

    count_result = await db.execute(
        select(Actividad).where(
            Actividad.cuenta_cobro_id == cuenta_cobro.id, Actividad.obligacion_id == obligacion_informes.id
        )
    )
    assert len(count_result.scalars().all()) == 1


async def test_subir_evidencias_cuenta_rechaza_lote_si_un_archivo_invalido(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    storage = _mock_storage()
    with pytest.raises(ValidationError):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("ok.txt", "text/plain", _txt_file("contenido valido")),
                ("malware.exe", "application/octet-stream", b"MZ..."),
            ],
        )
    storage.upload.assert_not_called()
    result = await db.execute(select(Evidencia))
    assert result.scalars().all() == []
    result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_cobro.id))
    assert result.scalars().all() == []


async def test_subir_evidencias_cuenta_otro_usuario_falla(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ForbiddenError

    other_user_id = uuid.uuid4()
    storage = _mock_storage()
    with pytest.raises(ForbiddenError):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=other_user_id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("spy.txt", "text/plain", _txt_file("contenido"))],
        )
