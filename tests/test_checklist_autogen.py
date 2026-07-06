"""Tests for the checklist autogen endpoint (POST .../checklist/{codigo}/generar).

Covers the `permite_autogen` flow: generating an informe from the cuenta's data,
attaching it as a DocumentoFuente, and flipping the checklist row to `cargado`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import ForbiddenError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import checklist_autogen_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

# Patch path for storage — lazily resolved inside document_service.
_PATCH_S3 = "app.services.document_service._get_storage"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-AUTOGEN-001",
        objeto="Servicios profesionales para autogen",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía",
        dependencia="TI",
        supervisor_nombre="Carlos Supervisor",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligaciones(db: AsyncSession, contrato: Contrato) -> list[Obligacion]:
    obs = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación contractual #{i + 1} con texto suficientemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(2)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    contrato.obligaciones = list(obs)
    return obs


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato, obligaciones: list[Obligacion]) -> CuentaCobro:
    """Cuenta with the checklist gate resolved (estandar) and activities registered."""
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)

    for i, ob in enumerate(obligaciones):
        db.add(
            Actividad(
                cuenta_cobro_id=cc.id,
                obligacion_id=ob.id,
                descripcion=f"Actividad realizada {i + 1}",
                justificacion=f"Justificación {i + 1}",
                fecha_realizacion=date(2024, 5, 10 + i),
            )
        )
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def cuenta_sin_actividades(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=6,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_generar_informe_actividades_marca_cargado(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    with patch(_PATCH_S3, return_value=_fake_storage()):
        r = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=test_user["headers"],
        )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["requisito"]["codigo"] == "INFORME_ACTIVIDADES"
    assert item["estado"] == "cargado"
    assert item["documento_fuente"] is not None
    assert item["documento_fuente"]["tipo"] == "informe_actividades"


async def test_generar_informe_supervision_marca_cargado(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    with patch(_PATCH_S3, return_value=_fake_storage()):
        r = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/INFORME_SUPERVISION/generar",
            headers=test_user["headers"],
        )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["estado"] == "cargado"
    assert item["documento_fuente"]["tipo"] == "informe_supervision"


async def test_generar_sin_actividades_falla(
    client: AsyncClient, test_user: dict[str, Any], cuenta_sin_actividades: CuentaCobro
) -> None:
    with patch(_PATCH_S3, return_value=_fake_storage()):
        r = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta_sin_actividades.id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=test_user["headers"],
        )
    assert r.status_code == 422, r.text
    # The row must not be broken: a follow-up GET still lists it as pendiente.
    g = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_sin_actividades.id}/checklist",
        headers=test_user["headers"],
    )
    assert g.status_code == 200
    item = next(i for i in g.json()["items"] if i["requisito"]["codigo"] == "INFORME_ACTIVIDADES")
    assert item["estado"] == "pendiente"


async def test_generar_codigo_no_autogenerable_falla(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RUT/generar",
        headers=test_user["headers"],
    )
    assert r.status_code == 422, r.text


async def test_generar_sin_gate_definido_falla(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato
) -> None:
    # Cuenta whose checklist gate is unresolved (requisitos_modo IS NULL).
    cc = CuentaCobro(
        contrato_id=contrato.id, mes=7, anio=2024, valor=1_000_000, estado=EstadoCuentaCobro.BORRADOR
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    r = await client.post(
        f"/api/v1/cuentas-cobro/{cc.id}/checklist/INFORME_ACTIVIDADES/generar",
        headers=test_user["headers"],
    )
    assert r.status_code == 422, r.text


async def test_regenerar_es_idempotente(client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro) -> None:
    with patch(_PATCH_S3, return_value=_fake_storage()):
        r1 = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=test_user["headers"],
        )
        r2 = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=test_user["headers"],
        )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r2.json()["estado"] == "cargado"


async def test_ownership_otro_usuario_falla(db: AsyncSession, cuenta: CuentaCobro) -> None:
    fake_user_id = uuid.uuid4()
    with patch(_PATCH_S3, return_value=_fake_storage()), pytest.raises(ForbiddenError):
        await checklist_autogen_service.generar_y_vincular(db, fake_user_id, cuenta.id, "INFORME_ACTIVIDADES")


async def test_es_autogenerable() -> None:
    assert checklist_autogen_service.es_autogenerable("INFORME_ACTIVIDADES")
    assert checklist_autogen_service.es_autogenerable("INFORME_SUPERVISION")
    assert not checklist_autogen_service.es_autogenerable("RUT")
