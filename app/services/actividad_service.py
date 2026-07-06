"""Actividad service — CRUD for activities within cuentas de cobro."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError, ValidationError
from app.models.actividad import Actividad
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion
from app.schemas.actividad import ActividadCreate, ActividadResponse, ActividadUpdate

logger = structlog.get_logger("service.actividad")

_ESTADOS_EDITABLES = {EstadoCuentaCobro.BORRADOR}


async def _get_cuenta_cobro_owned(
    db: AsyncSession, cuenta_cobro_id: uuid.UUID, usuario_id: uuid.UUID
) -> CuentaCobro:
    from app.models.contrato import Contrato

    result = await db.execute(
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            CuentaCobro.id == cuenta_cobro_id,
            Contrato.usuario_id == usuario_id,
            CuentaCobro.deleted_at.is_(None),
        )
    )
    cc = result.scalar_one_or_none()
    if cc is None:
        raise NotFoundError("CuentaCobro", str(cuenta_cobro_id))
    return cc


async def _get_actividad(
    db: AsyncSession,
    actividad_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
) -> Actividad:
    result = await db.execute(
        select(Actividad).options(selectinload(Actividad.evidencias)).where(
            Actividad.id == actividad_id,
            Actividad.cuenta_cobro_id == cuenta_cobro_id,
        )
    )
    a = result.scalar_one_or_none()
    if a is None:
        raise NotFoundError("Actividad", str(actividad_id))
    return a


async def listar_actividades(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
) -> list[ActividadResponse]:
    await _get_cuenta_cobro_owned(db, cuenta_cobro_id, usuario_id)
    result = await db.execute(
        select(Actividad)
        .options(selectinload(Actividad.evidencias))
        .where(Actividad.cuenta_cobro_id == cuenta_cobro_id)
        .order_by(Actividad.created_at.asc())
    )
    return [ActividadResponse.model_validate(a) for a in result.scalars().all()]


async def crear_actividad(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    data: ActividadCreate,
) -> ActividadResponse:
    cc = await _get_cuenta_cobro_owned(db, cuenta_cobro_id, usuario_id)
    if cc.estado not in _ESTADOS_EDITABLES:
        raise ValidationError(
            f"No se pueden agregar actividades a una cuenta de cobro en estado '{cc.estado}'."
        )

    if data.obligacion_id:
        ob = await db.get(Obligacion, data.obligacion_id)
        if ob is None:
            raise NotFoundError("Obligacion", str(data.obligacion_id))

    actividad = Actividad(
        cuenta_cobro_id=cuenta_cobro_id,
        obligacion_id=data.obligacion_id,
        descripcion=data.descripcion,
        justificacion=data.justificacion,
        fecha_realizacion=data.fecha_realizacion,
    )
    db.add(actividad)
    await db.commit()
    await db.refresh(actividad)
    logger.info("actividad_created", id=str(actividad.id), cuenta_cobro_id=str(cuenta_cobro_id))
    return ActividadResponse.model_validate(actividad)


async def actualizar_actividad(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    actividad_id: uuid.UUID,
    data: ActividadUpdate,
) -> ActividadResponse:
    cc = await _get_cuenta_cobro_owned(db, cuenta_cobro_id, usuario_id)
    if cc.estado not in _ESTADOS_EDITABLES:
        raise ValidationError(
            f"No se puede editar actividades de una cuenta de cobro en estado '{cc.estado}'."
        )

    a = await _get_actividad(db, actividad_id, cuenta_cobro_id)
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(a, field, value)
    await db.commit()
    await db.refresh(a)
    return ActividadResponse.model_validate(a)


async def eliminar_actividad(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    actividad_id: uuid.UUID,
) -> None:
    cc = await _get_cuenta_cobro_owned(db, cuenta_cobro_id, usuario_id)
    if cc.estado not in _ESTADOS_EDITABLES:
        raise ValidationError(
            f"No se puede eliminar actividades de una cuenta de cobro en estado '{cc.estado}'."
        )
    a = await _get_actividad(db, actividad_id, cuenta_cobro_id)
    await db.delete(a)
    await db.commit()
    logger.info("actividad_deleted", id=str(actividad_id))
