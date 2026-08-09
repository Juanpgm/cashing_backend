"""Service-level tests for purga_service.purgar_datos_prueba."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.schemas.contrato import ContratoCreate, ObligacionCreate
from app.services import contrato_service, purga_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _make_contrato_create(numero: str, **overrides: Any) -> ContratoCreate:
    defaults: dict[str, Any] = {
        "numero_contrato": numero,
        "objeto": "Prestación de servicios de consultoría tecnológica",
        "valor_total": "36000000.00",
        "valor_mensual": "3000000.00",
        "fecha_inicio": date(2024, 1, 1),
        "fecha_fin": date(2024, 12, 31),
        "supervisor_nombre": "Ana Supervisora",
        "entidad": "Ministerio de TIC",
        "dependencia": "Dirección de Sistemas",
        "obligaciones": [],
    }
    defaults.update(overrides)
    return ContratoCreate(**defaults)


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])
    return storage


@pytest.mark.asyncio
async def test_purgar_datos_prueba_borra_cuentas_y_reinicia_obligaciones(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    usuario_id = test_user["user"].id

    contrato_1 = await contrato_service.crear_contrato(
        db,
        usuario_id,
        _make_contrato_create(
            "CTR-PURGA-001",
            obligaciones=[ObligacionCreate(descripcion="Obligación de prueba número uno", tipo=TipoObligacion.GENERAL)],
        ),
    )
    contrato_2 = await contrato_service.crear_contrato(
        db,
        usuario_id,
        _make_contrato_create(
            "CTR-PURGA-002",
            obligaciones=[ObligacionCreate(descripcion="Obligación de prueba número dos", tipo=TipoObligacion.GENERAL)],
        ),
    )

    row1 = await db.get(Contrato, contrato_1.id)
    row2 = await db.get(Contrato, contrato_2.id)
    assert row1 is not None
    assert row2 is not None
    row1.obligaciones_extraidas = True
    row2.obligaciones_extraidas = True

    cuenta_1 = CuentaCobro(
        contrato_id=contrato_1.id, mes=1, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR
    )
    cuenta_2 = CuentaCobro(
        contrato_id=contrato_2.id, mes=2, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR
    )
    db.add_all([cuenta_1, cuenta_2])
    await db.commit()

    result = await purga_service.purgar_datos_prueba(db, usuario_id, _mock_storage())

    assert result == {"cuentas_borradas": 2, "obligaciones_reiniciadas": 2, "contratos_omitidos": 0}

    cuentas_restantes = (
        (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id.in_([contrato_1.id, contrato_2.id]))))
        .scalars()
        .all()
    )
    assert cuentas_restantes == []

    obligaciones_restantes = (
        (await db.execute(select(Obligacion).where(Obligacion.contrato_id.in_([contrato_1.id, contrato_2.id]))))
        .scalars()
        .all()
    )
    assert obligaciones_restantes == []

    await db.refresh(row1)
    await db.refresh(row2)
    assert row1.obligaciones_extraidas is None
    assert row2.obligaciones_extraidas is None

    # Contratos themselves survive — only their obligaciones/cuentas are reset.
    assert (await db.get(Contrato, contrato_1.id)) is not None
    assert (await db.get(Contrato, contrato_2.id)) is not None


@pytest.mark.asyncio
async def test_purgar_datos_prueba_cuenta_contratos_omitidos_sin_abortar(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """A `ValidationError` from `limpiar_obligaciones` on one contrato must be
    swallowed and counted, not abort the purge for the rest of the user's data."""
    usuario_id = test_user["user"].id
    contrato_ok = await contrato_service.crear_contrato(db, usuario_id, _make_contrato_create("CTR-PURGA-OK"))
    contrato_bloqueado = await contrato_service.crear_contrato(db, usuario_id, _make_contrato_create("CTR-PURGA-BLOQ"))

    real_limpiar = contrato_service.limpiar_obligaciones

    async def _fake_limpiar(db_arg: AsyncSession, usr: Any, contrato_id: Any) -> int:
        if contrato_id == contrato_bloqueado.id:
            raise ValidationError("bloqueado por actividad")
        return await real_limpiar(db_arg, usr, contrato_id)

    with patch("app.services.contrato_service.limpiar_obligaciones", AsyncMock(side_effect=_fake_limpiar)):
        result = await purga_service.purgar_datos_prueba(db, usuario_id, _mock_storage())

    assert result["contratos_omitidos"] == 1
    assert result["obligaciones_reiniciadas"] == 0
    assert result["cuentas_borradas"] == 0

    # Both contratos still exist regardless of which branch they took.
    assert (await db.get(Contrato, contrato_ok.id)) is not None
    assert (await db.get(Contrato, contrato_bloqueado.id)) is not None
