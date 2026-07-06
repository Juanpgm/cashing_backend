"""Cobertura service unit tests — semáforo obligación↔evidencia (Modo Simple)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.cobertura import EstadoCobertura
from app.services import cobertura_service


async def _make_user(db: AsyncSession, *, email: str = "u@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Test User",
        cedula="123456789",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="001-2024",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_obligacion(db: AsyncSession, contrato_id: uuid.UUID, orden: int) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=f"Obligación contractual {orden}",
        tipo=TipoObligacion.ESPECIFICA,
        orden=orden,
    )
    db.add(ob)
    await db.flush()
    return ob


async def _make_cuenta(db: AsyncSession, contrato_id: uuid.UUID) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=3,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cuenta)
    await db.flush()
    return cuenta


async def _make_actividad(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    obligacion_id: uuid.UUID | None,
    *,
    justificacion: str | None = None,
) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta_id,
        obligacion_id=obligacion_id,
        descripcion="Actividad realizada en el período de cobro",
        justificacion=justificacion,
    )
    db.add(act)
    await db.flush()
    return act


async def _make_evidencia(db: AsyncSession, actividad_id: uuid.UUID) -> Evidencia:
    ev = Evidencia(
        actividad_id=actividad_id,
        storage_key=f"evidencias/{uuid.uuid4()}.pdf",
        nombre_archivo="soporte.pdf",
        tipo_archivo="application/pdf",
        tamano_bytes=1024,
    )
    db.add(ev)
    await db.flush()
    return ev


@pytest.mark.asyncio
async def test_obligacion_sin_actividad_es_rojo(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)

    assert resp.resumen.total == 1
    assert resp.resumen.sin_evidencia == 1
    item = resp.obligaciones[0]
    assert item.estado == EstadoCobertura.SIN_EVIDENCIA
    assert item.color == "rojo"
    assert item.fuerza == 0.0
    assert resp.listo_para_generar is False


@pytest.mark.asyncio
async def test_actividad_sin_evidencia_sigue_rojo(db: AsyncSession) -> None:
    """Regla «sin soporte = rojo»: aunque haya actividad y justificación, sin evidencia es rojo."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    await _make_actividad(db, cuenta.id, ob.id, justificacion="Justificación presente")
    await db.commit()

    resp = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)

    item = resp.obligaciones[0]
    assert item.estado == EstadoCobertura.SIN_EVIDENCIA
    assert item.color == "rojo"
    assert item.num_actividades == 1
    assert item.num_evidencias == 0


@pytest.mark.asyncio
async def test_evidencia_sin_justificacion_es_amarillo(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    act = await _make_actividad(db, cuenta.id, ob.id, justificacion=None)
    await _make_evidencia(db, act.id)
    await db.commit()

    resp = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)

    item = resp.obligaciones[0]
    assert item.estado == EstadoCobertura.DEBIL
    assert item.color == "amarillo"
    assert resp.resumen.debiles == 1


@pytest.mark.asyncio
async def test_evidencia_y_justificacion_es_verde(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    act = await _make_actividad(db, cuenta.id, ob.id, justificacion="Cumple la obligación")
    await _make_evidencia(db, act.id)
    await _make_evidencia(db, act.id)
    await db.commit()

    resp = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)

    item = resp.obligaciones[0]
    assert item.estado == EstadoCobertura.CUBIERTA
    assert item.color == "verde"
    assert item.num_evidencias == 2
    assert item.fuerza == 1.0
    assert resp.resumen.cubiertas == 1
    assert resp.resumen.porcentaje_cubierto == 100.0
    assert resp.listo_para_generar is True


@pytest.mark.asyncio
async def test_resumen_mixto_y_orden(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob1 = await _make_obligacion(db, contrato.id, 1)  # verde
    ob2 = await _make_obligacion(db, contrato.id, 2)  # amarillo
    await _make_obligacion(db, contrato.id, 3)  # rojo (sin actividad)
    cuenta = await _make_cuenta(db, contrato.id)

    a1 = await _make_actividad(db, cuenta.id, ob1.id, justificacion="ok")
    await _make_evidencia(db, a1.id)
    a2 = await _make_actividad(db, cuenta.id, ob2.id, justificacion=None)
    await _make_evidencia(db, a2.id)
    await db.commit()

    resp = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)

    assert resp.resumen.total == 3
    assert resp.resumen.cubiertas == 1
    assert resp.resumen.debiles == 1
    assert resp.resumen.sin_evidencia == 1
    # Ordenadas por 'orden'
    assert [o.orden for o in resp.obligaciones] == [1, 2, 3]
    assert resp.listo_para_generar is False


@pytest.mark.asyncio
async def test_cobertura_not_found(db: AsyncSession) -> None:
    user = await _make_user(db)
    await db.commit()
    with pytest.raises(NotFoundError):
        await cobertura_service.calcular_cobertura(db, user.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_cobertura_otro_usuario(db: AsyncSession) -> None:
    owner = await _make_user(db, email="owner@test.com")
    other = await _make_user(db, email="other@test.com")
    contrato = await _make_contrato(db, owner.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with pytest.raises(ForbiddenError):
        await cobertura_service.calcular_cobertura(db, other.id, cuenta.id)
