"""Contrato service — CRUD and obligaciones management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion
from app.schemas.contrato import (
    ContratoCreate,
    ContratoListItem,
    ContratoResponse,
    ContratoUpdate,
    ObligacionCreate,
    ObligacionResponse,
)

logger = structlog.get_logger("service.contrato")

_ESTADOS_ACTIVOS = {
    EstadoCuentaCobro.ENVIADA,
    EstadoCuentaCobro.APROBADA,
    EstadoCuentaCobro.PAGADA,
}


async def _get_contrato_con_ownership(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> Contrato:
    result = await db.execute(
        select(Contrato)
        .options(selectinload(Contrato.obligaciones))
        .where(
            Contrato.id == contrato_id,
            Contrato.usuario_id == usuario_id,
            Contrato.deleted_at.is_(None),
        )
    )
    contrato = result.scalar_one_or_none()
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))
    return contrato


async def _reload_contrato_response(db: AsyncSession, contrato_id: uuid.UUID) -> ContratoResponse:
    result = await db.execute(
        select(Contrato)
        .options(selectinload(Contrato.obligaciones))
        .where(Contrato.id == contrato_id)
    )
    return ContratoResponse.model_validate(result.scalar_one())


async def crear_contrato(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    data: ContratoCreate,
) -> ContratoResponse:
    """Create a contract with optional obligaciones."""
    if data.fecha_fin <= data.fecha_inicio:
        raise ValidationError("La fecha de fin debe ser posterior a la fecha de inicio.")

    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato=data.numero_contrato,
        objeto=data.objeto,
        valor_total=float(data.valor_total),
        valor_mensual=float(data.valor_mensual),
        fecha_inicio=data.fecha_inicio,
        fecha_fin=data.fecha_fin,
        supervisor_nombre=data.supervisor_nombre,
        entidad=data.entidad,
        dependencia=data.dependencia,
    )
    db.add(contrato)
    await db.flush()

    for ob_data in data.obligaciones:
        db.add(
            Obligacion(
                contrato_id=contrato.id,
                descripcion=ob_data.descripcion,
                tipo=ob_data.tipo,
                orden=ob_data.orden,
            )
        )
    await db.flush()

    await logger.ainfo("contrato_creado", contrato_id=str(contrato.id), usuario_id=str(usuario_id))
    return await _reload_contrato_response(db, contrato.id)


async def listar_contratos(db: AsyncSession, usuario_id: uuid.UUID) -> list[ContratoListItem]:
    """List all active contracts for a user, newest first."""
    result = await db.execute(
        select(Contrato)
        .where(Contrato.usuario_id == usuario_id, Contrato.deleted_at.is_(None))
        .order_by(Contrato.created_at.desc())
    )
    return [ContratoListItem.model_validate(c) for c in result.scalars().all()]


async def obtener_contrato(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> ContratoResponse:
    """Get a contract with its obligaciones."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)
    return await _reload_contrato_response(db, contrato_id)


async def actualizar_contrato(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    data: ContratoUpdate,
) -> ContratoResponse:
    """Partial update of a contract."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    updates = data.model_dump(exclude_unset=True)

    fecha_inicio = updates.get("fecha_inicio", contrato.fecha_inicio)
    fecha_fin = updates.get("fecha_fin", contrato.fecha_fin)
    if fecha_fin <= fecha_inicio:
        raise ValidationError("La fecha de fin debe ser posterior a la fecha de inicio.")

    for field, value in updates.items():
        if field in ("valor_total", "valor_mensual") and value is not None:
            value = float(value)
        setattr(contrato, field, value)

    await db.flush()
    return await _reload_contrato_response(db, contrato_id)


async def eliminar_contrato(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> None:
    """Soft-delete a contract. Blocked if it has active (enviada/aprobada/pagada) cuentas."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    # Block deletion if any cuenta is in an active state
    result = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == contrato_id,
            CuentaCobro.estado.in_([e.value for e in _ESTADOS_ACTIVOS]),
            CuentaCobro.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise ValidationError(
            "No se puede eliminar el contrato: tiene cuentas de cobro en estado enviada, aprobada o pagada."
        )

    result2 = await db.execute(select(Contrato).where(Contrato.id == contrato_id))
    contrato = result2.scalar_one()
    contrato.deleted_at = datetime.now(UTC)
    await db.flush()
    await logger.ainfo("contrato_eliminado", contrato_id=str(contrato_id), usuario_id=str(usuario_id))


async def agregar_obligacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    data: ObligacionCreate,
) -> ObligacionResponse:
    """Add an obligation to a contract."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=data.descripcion,
        tipo=data.tipo,
        orden=data.orden,
    )
    db.add(ob)
    await db.flush()
    await db.refresh(ob)
    return ObligacionResponse.model_validate(ob)


async def eliminar_obligacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    obligacion_id: uuid.UUID,
) -> None:
    """Delete an obligation. Blocked if any activity references it."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    ob_result = await db.execute(
        select(Obligacion).where(
            Obligacion.id == obligacion_id,
            Obligacion.contrato_id == contrato_id,
        )
    )
    ob = ob_result.scalar_one_or_none()
    if ob is None:
        raise NotFoundError("Obligacion", str(obligacion_id))

    # Block if any actividad references this obligacion
    from app.models.actividad import Actividad

    ref = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion_id))
    if ref.scalar_one_or_none() is not None:
        raise ValidationError(
            "No se puede eliminar la obligación: hay actividades de cuentas de cobro que la referencian."
        )

    await db.delete(ob)
    await db.flush()
