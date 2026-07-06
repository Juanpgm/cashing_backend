"""Cobertura service — semáforo de cobertura obligación↔evidencia (Modo Simple).

Calcula, de forma 100% determinista (sin LLM), el estado de cobertura de cada
obligación del contrato dentro de una cuenta de cobro. Es el motor del «semáforo»
y el sustento del discurso de confianza «nunca inventa»: una obligación sólo se
considera CUBIERTA si tiene actividad, justificación y evidencia documental.

Reglas (regla rectora: «sin soporte = rojo, siempre»):
- SIN_EVIDENCIA (rojo): la obligación no tiene actividad vinculada, o ninguna de
  sus actividades tiene evidencia adjunta.
- DEBIL (amarillo): hay evidencia adjunta pero falta justificación que la relacione.
- CUBIERTA (verde): hay actividad vinculada, con justificación y ≥1 evidencia.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.schemas.cobertura import (
    COLOR_POR_ESTADO,
    CoberturaResponse,
    EstadoCobertura,
    ObligacionCobertura,
    ResumenCobertura,
)

logger = structlog.get_logger("service.cobertura")


def _evaluar_obligacion(num_evidencias: int, tiene_justificacion: bool) -> tuple[EstadoCobertura, float, str]:
    """Return (estado, fuerza, detalle) for a single obligation from its support counts."""
    if num_evidencias == 0:
        detalle = (
            "Hay actividad registrada pero sin evidencia documental adjunta."
            if tiene_justificacion
            else "Sin actividad ni evidencia que respalde esta obligación."
        )
        return EstadoCobertura.SIN_EVIDENCIA, 0.0, detalle

    # Hay al menos una evidencia → fuerza base 0.5, refuerzos por justificación y soporte múltiple.
    fuerza = 0.5
    if tiene_justificacion:
        fuerza += 0.3
    if num_evidencias >= 2:
        fuerza += 0.2
    fuerza = round(min(fuerza, 1.0), 2)

    if tiene_justificacion:
        return EstadoCobertura.CUBIERTA, fuerza, "Respaldada con evidencia y justificación."
    return (
        EstadoCobertura.DEBIL,
        fuerza,
        "Evidencia adjunta pero sin justificación que la relacione con la obligación.",
    )


async def calcular_cobertura(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> CoberturaResponse:
    """Compute the coverage matrix (semáforo) for a cuenta de cobro."""
    result = await db.execute(
        select(CuentaCobro)
        .options(
            selectinload(CuentaCobro.actividades).selectinload(Actividad.evidencias),
            selectinload(CuentaCobro.contrato).selectinload(Contrato.obligaciones),
        )
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()

    # Agrupar actividades por obligación vinculada.
    acts_por_obligacion: dict[uuid.UUID, list[Actividad]] = {}
    for act in cuenta.actividades:
        if act.obligacion_id is not None:
            acts_por_obligacion.setdefault(act.obligacion_id, []).append(act)

    obligaciones = sorted(cuenta.contrato.obligaciones, key=lambda o: o.orden)

    items: list[ObligacionCobertura] = []
    cubiertas = debiles = sin_evidencia = 0
    for ob in obligaciones:
        acts = acts_por_obligacion.get(ob.id, [])
        num_evidencias = sum(len(a.evidencias) for a in acts)
        tiene_justificacion = any((a.justificacion or "").strip() for a in acts)

        estado, fuerza, detalle = _evaluar_obligacion(num_evidencias, tiene_justificacion)
        if estado is EstadoCobertura.CUBIERTA:
            cubiertas += 1
        elif estado is EstadoCobertura.DEBIL:
            debiles += 1
        else:
            sin_evidencia += 1

        items.append(
            ObligacionCobertura(
                obligacion_id=ob.id,
                descripcion=ob.descripcion,
                tipo=ob.tipo.value,
                orden=ob.orden,
                estado=estado,
                color=COLOR_POR_ESTADO[estado],
                fuerza=fuerza,
                num_actividades=len(acts),
                num_evidencias=num_evidencias,
                tiene_justificacion=tiene_justificacion,
                detalle=detalle,
            )
        )

    total = len(obligaciones)
    porcentaje = round((cubiertas / total) * 100, 1) if total else 0.0
    resumen = ResumenCobertura(
        total=total,
        cubiertas=cubiertas,
        debiles=debiles,
        sin_evidencia=sin_evidencia,
        porcentaje_cubierto=porcentaje,
    )

    await logger.ainfo(
        "cobertura_calculada",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        total=total,
        cubiertas=cubiertas,
        debiles=debiles,
        sin_evidencia=sin_evidencia,
    )

    return CoberturaResponse(
        cuenta_cobro_id=cuenta_id,
        contrato_id=cuenta.contrato_id,
        resumen=resumen,
        obligaciones=items,
        listo_para_generar=sin_evidencia == 0 and total > 0,
    )
