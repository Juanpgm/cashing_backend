"""Contrato service unit tests."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.schemas.contrato import (
    ContratoCreate,
    ContratoUpdate,
    ObligacionCreate,
)
from app.services import contrato_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_contrato_create(**overrides) -> ContratoCreate:
    defaults = {
        "numero_contrato": "CTR-2024-001",
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


# ---------------------------------------------------------------------------
# crear_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_contrato_basico(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create()
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert result.numero_contrato == "CTR-2024-001"
    assert result.obligaciones == []
    assert result.usuario_id == test_user["user"].id


@pytest.mark.asyncio
async def test_crear_contrato_con_obligaciones(db: AsyncSession, test_user: dict[str, Any]) -> None:
    obs = [
        ObligacionCreate(descripcion="Elaborar informes técnicos mensuales", tipo=TipoObligacion.ESPECIFICA, orden=1),
        ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL, orden=2),
    ]
    data = _make_contrato_create(obligaciones=obs)
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert len(result.obligaciones) == 2
    assert result.obligaciones[0].orden == 1


@pytest.mark.asyncio
async def test_crear_contrato_fecha_invalida(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create(fecha_inicio=date(2024, 12, 31), fecha_fin=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.crear_contrato(db, test_user["user"].id, data)


@pytest.mark.asyncio
async def test_crear_contrato_fechas_iguales(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create(fecha_inicio=date(2024, 1, 1), fecha_fin=date(2024, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.crear_contrato(db, test_user["user"].id, data)


# ---------------------------------------------------------------------------
# listar_contratos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listar_contratos_vacia(db: AsyncSession, test_user: dict[str, Any]) -> None:
    result = await contrato_service.listar_contratos(db, test_user["user"].id)
    assert result == []


@pytest.mark.asyncio
async def test_listar_contratos_solo_del_usuario(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create()
    await contrato_service.crear_contrato(db, test_user["user"].id, data)

    otro_usuario_id = uuid.uuid4()
    result = await contrato_service.listar_contratos(db, otro_usuario_id)
    assert result == []

    result_own = await contrato_service.listar_contratos(db, test_user["user"].id)
    assert len(result_own) == 1


# ---------------------------------------------------------------------------
# obtener_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_obtener_contrato_existente(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    result = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert result.id == created.id


@pytest.mark.asyncio
async def test_obtener_contrato_no_encontrado(db: AsyncSession, test_user: dict[str, Any]) -> None:
    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, test_user["user"].id, uuid.uuid4())


@pytest.mark.asyncio
async def test_obtener_contrato_otro_usuario(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, uuid.uuid4(), created.id)


# ---------------------------------------------------------------------------
# actualizar_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_actualizar_contrato_parcial(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    update = ContratoUpdate(entidad="Nueva Entidad")
    result = await contrato_service.actualizar_contrato(db, test_user["user"].id, created.id, update)
    assert result.entidad == "Nueva Entidad"
    assert result.numero_contrato == created.numero_contrato


@pytest.mark.asyncio
async def test_actualizar_contrato_fecha_invalida(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    update = ContratoUpdate(fecha_fin=date(2023, 1, 1))
    with pytest.raises(ValidationError):
        await contrato_service.actualizar_contrato(db, test_user["user"].id, created.id, update)


# ---------------------------------------------------------------------------
# eliminar_contrato
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_contrato_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)

    with pytest.raises(NotFoundError):
        await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)


@pytest.mark.asyncio
async def test_eliminar_contrato_bloqueado_por_cuenta_activa(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())

    cc = CuentaCobro(
        contrato_id=created.id,
        mes=1,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.ENVIADA,
    )
    db.add(cc)
    await db.commit()

    with pytest.raises(ValidationError):
        await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)


@pytest.mark.asyncio
async def test_eliminar_contrato_permite_cuenta_borrador(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())

    cc = CuentaCobro(
        contrato_id=created.id,
        mes=1,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cc)
    await db.commit()

    # Should not raise — borrador is not a blocking state
    await contrato_service.eliminar_contrato(db, test_user["user"].id, created.id)


# ---------------------------------------------------------------------------
# agregar_obligacion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agregar_obligacion_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    ob_data = ObligacionCreate(
        descripcion="Elaborar informes técnicos mensuales de avance",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    result = await contrato_service.agregar_obligacion(db, test_user["user"].id, created.id, ob_data)
    assert result.descripcion == ob_data.descripcion
    assert result.contrato_id == created.id


@pytest.mark.asyncio
async def test_agregar_obligacion_contrato_no_encontrado(db: AsyncSession, test_user: dict[str, Any]) -> None:
    ob_data = ObligacionCreate(
        descripcion="Elaborar informes técnicos mensuales de avance",
        tipo=TipoObligacion.ESPECIFICA,
    )
    with pytest.raises(NotFoundError):
        await contrato_service.agregar_obligacion(db, test_user["user"].id, uuid.uuid4(), ob_data)


# ---------------------------------------------------------------------------
# eliminar_obligacion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eliminar_obligacion_ok(db: AsyncSession, test_user: dict[str, Any]) -> None:
    obs = [ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(
        db, test_user["user"].id, _make_contrato_create(obligaciones=obs)
    )
    ob_id = created.obligaciones[0].id
    await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, ob_id)

    refreshed = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert len(refreshed.obligaciones) == 0


@pytest.mark.asyncio
async def test_eliminar_obligacion_no_encontrada(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    with pytest.raises(NotFoundError):
        await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_eliminar_obligacion_bloqueada_por_actividad(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.models.actividad import Actividad

    obs = [ObligacionCreate(descripcion="Asistir a reuniones del equipo de trabajo", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(
        db, test_user["user"].id, _make_contrato_create(obligaciones=obs)
    )
    ob_id = created.obligaciones[0].id

    cc = CuentaCobro(
        contrato_id=created.id, mes=1, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR
    )
    db.add(cc)
    await db.flush()

    actividad = Actividad(
        cuenta_cobro_id=cc.id,
        obligacion_id=ob_id,
        descripcion="Actividad que referencia la obligación para el bloqueo",
    )
    db.add(actividad)
    await db.commit()

    with pytest.raises(ValidationError):
        await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, ob_id)


async def _seed_evidencia_con_enlace(
    db: AsyncSession, contrato_id: uuid.UUID, obligacion_id: uuid.UUID, *, actividad_obligacion_id=None
):
    """CuentaCobro → Actividad → Evidencia + EvidenciaObligacion link to the given obligación."""
    from app.models.actividad import Actividad
    from app.models.evidencia import Evidencia
    from app.models.evidencia_obligacion import EvidenciaObligacion

    cc = CuentaCobro(contrato_id=contrato_id, mes=1, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR)
    db.add(cc)
    await db.flush()
    actividad = Actividad(
        cuenta_cobro_id=cc.id,
        descripcion="Actividad de prueba para enlaces de evidencia",
        obligacion_id=actividad_obligacion_id,
    )
    db.add(actividad)
    await db.flush()
    evidencia = Evidencia(
        actividad_id=actividad.id, storage_key="evidencias/fake/e.pdf", nombre_archivo="e.pdf"
    )
    db.add(evidencia)
    await db.flush()
    db.add(
        EvidenciaObligacion(
            evidencia_id=evidencia.id, obligacion_id=obligacion_id, confianza="alta", status="confirmed"
        )
    )
    await db.flush()
    return actividad


@pytest.mark.asyncio
async def test_eliminar_obligacion_con_enlaces_de_evidencia(db: AsyncSession, test_user: dict[str, Any]) -> None:
    """Delete-one no longer leaves orphaned EvidenciaObligacion rows (FK violation on Postgres)."""
    from app.models.evidencia_obligacion import EvidenciaObligacion
    from sqlalchemy import select

    obs = [ObligacionCreate(descripcion="Obligación con evidencias vinculadas", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create(obligaciones=obs))
    ob_id = created.obligaciones[0].id
    # Actividad deliberately NOT referencing the obligación — the Actividad
    # guard stays intact and is covered by the bloqueada test above.
    await _seed_evidencia_con_enlace(db, created.id, ob_id)

    await contrato_service.eliminar_obligacion(db, test_user["user"].id, created.id, ob_id)

    enlaces = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == ob_id)))
        .scalars()
        .all()
    )
    assert enlaces == []
    refreshed = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert len(refreshed.obligaciones) == 0


@pytest.mark.asyncio
async def test_limpiar_obligaciones_borra_enlaces_y_resetea_extraccion(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """Reset clears obligaciones, evidence links, actividad refs AND obligaciones_extraidas."""
    from app.models.evidencia_obligacion import EvidenciaObligacion
    from sqlalchemy import select

    obs = [ObligacionCreate(descripcion="Obligación a reiniciar", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create(obligaciones=obs))
    ob_id = created.obligaciones[0].id
    contrato_row = await db.get(Contrato, created.id)
    assert contrato_row is not None
    contrato_row.obligaciones_extraidas = True
    actividad = await _seed_evidencia_con_enlace(db, created.id, ob_id, actividad_obligacion_id=ob_id)

    count = await contrato_service.limpiar_obligaciones(db, test_user["user"].id, created.id)

    assert count == 1
    enlaces = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == ob_id)))
        .scalars()
        .all()
    )
    assert enlaces == []
    await db.refresh(actividad)
    assert actividad.obligacion_id is None
    # Vincular-eligible again: the frontend gate is `obligaciones_extraidas !== true`.
    await db.refresh(contrato_row)
    assert contrato_row.obligaciones_extraidas is None
    refreshed = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert len(refreshed.obligaciones) == 0


@pytest.mark.asyncio
async def test_limpiar_obligaciones_bloqueado_por_cuenta_activa(db: AsyncSession, test_user: dict[str, Any]) -> None:
    """Reset is blocked when an actividad referencing an obligación belongs to an ENVIADA cuenta."""
    from app.models.actividad import Actividad

    obs = [ObligacionCreate(descripcion="Obligación de cuenta radicada", tipo=TipoObligacion.GENERAL)]
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create(obligaciones=obs))
    ob_id = created.obligaciones[0].id
    contrato_row = await db.get(Contrato, created.id)
    assert contrato_row is not None
    contrato_row.obligaciones_extraidas = True

    cc = CuentaCobro(contrato_id=created.id, mes=2, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.ENVIADA)
    db.add(cc)
    await db.flush()
    db.add(Actividad(cuenta_cobro_id=cc.id, descripcion="Actividad de cuenta enviada", obligacion_id=ob_id))
    await db.flush()

    with pytest.raises(ValidationError):
        await contrato_service.limpiar_obligaciones(db, test_user["user"].id, created.id)

    # Nothing mutated: obligaciones and the extraction flag are intact.
    refreshed = await contrato_service.obtener_contrato(db, test_user["user"].id, created.id)
    assert len(refreshed.obligaciones) == 1
    await db.refresh(contrato_row)
    assert contrato_row.obligaciones_extraidas is True


@pytest.mark.asyncio
async def test_limpiar_obligaciones_sin_obligaciones_resetea_flag(db: AsyncSession, test_user: dict[str, Any]) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    contrato_row = await db.get(Contrato, created.id)
    assert contrato_row is not None
    contrato_row.obligaciones_extraidas = True

    count = await contrato_service.limpiar_obligaciones(db, test_user["user"].id, created.id)

    assert count == 0
    await db.refresh(contrato_row)
    assert contrato_row.obligaciones_extraidas is None


# ---------------------------------------------------------------------------
# Nuevos campos: ubicación, cargo_supervisor, derivación valor_total, cédula
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_contrato_con_campos_ubicacion(db: AsyncSession, test_user: dict[str, Any]) -> None:
    data = _make_contrato_create(
        pais="Colombia",
        departamento="Cundinamarca",
        ciudad="Bogotá",
        direccion_ejecucion="Carrera 8 # 12-70",
        cargo_supervisor="Director de Área",
    )
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert result.pais == "Colombia"
    assert result.departamento == "Cundinamarca"
    assert result.ciudad == "Bogotá"
    assert result.direccion_ejecucion == "Carrera 8 # 12-70"
    assert result.cargo_supervisor == "Director de Área"


@pytest.mark.asyncio
async def test_crear_contrato_sin_valor_total_deriva_correctamente(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """valor_total should be derived as valor_mensual × months when not provided."""
    from decimal import Decimal

    data = _make_contrato_create(
        valor_total=None,
        valor_mensual="2000000.00",
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 6, 30),
    )
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    # 6 months (Jan–Jun, inclusive: Jan 1 → Jun 30 spans Jan, Feb, Mar, Apr, May, Jun)
    assert result.valor_total == Decimal("12000000.00")


@pytest.mark.asyncio
async def test_crear_contrato_sin_valor_total_un_mes(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """Duration within same month defaults to minimum 1 month."""
    from decimal import Decimal

    data = _make_contrato_create(
        valor_total=None,
        valor_mensual="3000000.00",
        fecha_inicio=date(2025, 3, 1),
        fecha_fin=date(2025, 3, 31),
    )
    result = await contrato_service.crear_contrato(db, test_user["user"].id, data)

    assert result.valor_total == Decimal("3000000.00")


@pytest.mark.asyncio
async def test_crear_contrato_precarga_cedula_usuario(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """documento_proveedor should default to authenticated user's cedula."""
    # Ensure the test user has a cedula set
    user = test_user["user"]
    user.cedula = "9876543210"
    await db.flush()

    data = _make_contrato_create(documento_proveedor=None)
    result = await contrato_service.crear_contrato(db, user.id, data)

    assert result.documento_proveedor == "9876543210"


@pytest.mark.asyncio
async def test_crear_contrato_documento_proveedor_explicito_no_se_sobreescribe(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """Explicit documento_proveedor is preserved even if user has cedula."""
    user = test_user["user"]
    user.cedula = "1111111111"
    await db.flush()

    data = _make_contrato_create(documento_proveedor="9999999999")
    result = await contrato_service.crear_contrato(db, user.id, data)

    assert result.documento_proveedor == "9999999999"


@pytest.mark.asyncio
async def test_actualizar_contrato_campos_ubicacion(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    created = await contrato_service.crear_contrato(db, test_user["user"].id, _make_contrato_create())
    update = ContratoUpdate(ciudad="Medellín", cargo_supervisor="Jefe de Oficina")
    result = await contrato_service.actualizar_contrato(db, test_user["user"].id, created.id, update)

    assert result.ciudad == "Medellín"
    assert result.cargo_supervisor == "Jefe de Oficina"
    assert result.numero_contrato == created.numero_contrato


# ---------------------------------------------------------------------------
# _derivar_valor_total unit tests
# ---------------------------------------------------------------------------


def test_derivar_valor_total_meses_normales() -> None:
    from decimal import Decimal

    from app.services.contrato_service import _derivar_valor_total

    # Jan 1 → Jun 30: 6 calendar months (Jan, Feb, Mar, Apr, May, Jun) × 2,000,000 = 12,000,000
    result = _derivar_valor_total(Decimal("2000000"), date(2025, 1, 1), date(2025, 6, 30))
    assert result == Decimal("12000000.00")


def test_derivar_valor_total_minimo_un_mes() -> None:
    from decimal import Decimal

    from app.services.contrato_service import _derivar_valor_total

    result = _derivar_valor_total(Decimal("5000000"), date(2025, 3, 5), date(2025, 3, 25))
    assert result == Decimal("5000000.00")
