"""Persistence for the per-cuenta custom requirements set + checklist build mode.

This is the "apply" side of the post-creation gate: it replaces the cuenta's
custom requisitos, records the build mode, and re-materialises the checklist.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.requisito_cuenta import RequisitoCuenta
from app.schemas.requisito_cuenta import (
    RequisitoCuentaItem,
    RequisitosCuentaSet,
)
from app.services import checklist_service, cuenta_cobro_service

logger = structlog.get_logger("service.requisito_cuenta")


async def obtener_set(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> RequisitosCuentaSet:
    """Return the cuenta's active custom requirements + its build mode."""
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    res = await db.execute(
        select(RequisitoCuenta)
        .where(
            RequisitoCuenta.cuenta_cobro_id == cuenta_id,
            RequisitoCuenta.activo.is_(True),
        )
        .order_by(RequisitoCuenta.orden)
    )
    requisitos = [RequisitoCuentaItem.model_validate(r) for r in res.scalars().all()]
    return RequisitosCuentaSet(modo=cuenta.requisitos_modo, requisitos=requisitos)


async def definir_set(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    modo: str,
    requisitos: list[RequisitoCuentaItem],
) -> RequisitosCuentaSet:
    """Replace the cuenta's custom requirements, set the build mode, and
    re-materialise the checklist.

    Previous custom definitions are removed (cascading their checklist rows),
    and standard PENDIENTE rows are cleared so the structure recomputes for the
    chosen mode. Fulfilled rows (cargado/detectado/cumplido/no_aplica) are kept.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    # Drop previous custom definitions — FK CASCADE removes their checklist rows.
    await db.execute(sa_delete(RequisitoCuenta).where(RequisitoCuenta.cuenta_cobro_id == cuenta_id))
    # Clear standard PENDIENTE rows so dropped/added standards recompute per mode.
    await db.execute(
        sa_delete(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo.is_not(None),
            DocumentoCuentaCobro.estado == EstadoRequisito.PENDIENTE,
        )
    )
    await db.flush()

    # Insert the new custom definitions (dedup by codigo within the cuenta).
    vistos: set[str] = set()
    for item in requisitos:
        codigo = (item.codigo or "").strip().upper()
        etiqueta = (item.etiqueta or "").strip()
        if not codigo or not etiqueta or codigo in vistos:
            continue
        vistos.add(codigo)
        db.add(
            RequisitoCuenta(
                cuenta_cobro_id=cuenta_id,
                codigo=codigo,
                etiqueta=etiqueta[:200],
                descripcion=item.descripcion,
                obligatorio=item.obligatorio,
                solo_primera_cuenta=item.solo_primera_cuenta,
                tipo_documento_fuente=item.tipo_documento_fuente,
                keywords_deteccion=item.keywords_deteccion or [],
                orden=item.orden or 500,
                mapea_a_estandar=(item.mapea_a_estandar.upper() if item.mapea_a_estandar else None),
                origen=item.origen or "inferido",
                activo=True,
            )
        )

    cuenta.requisitos_modo = modo
    await db.flush()

    # Materialise the checklist under the new mode.
    await checklist_service.asegurar_checklist(db, cuenta)

    await logger.ainfo(
        "requisitos_cuenta_definidos",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        modo=modo,
        custom=len(vistos),
    )
    return await obtener_set(db, usuario_id, cuenta_id)
