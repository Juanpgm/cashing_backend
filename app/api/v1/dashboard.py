"""Dashboard endpoint — aggregated stats for the authenticated user (Phase 8)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.models.contrato import Contrato
from app.models.credito import Credito, TipoCredito
from app.models.cuenta_cobro import CuentaCobro

logger = structlog.get_logger("api.dashboard")
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return aggregated dashboard stats for the current user.

    Used by the frontend landing page after login.
    """
    # --- Contratos activos ------------------------------------------------
    contratos_result = await db.execute(
        select(func.count(Contrato.id)).where(
            Contrato.usuario_id == user.id,
            Contrato.deleted_at.is_(None),
        )
    )
    contratos_activos: int = contratos_result.scalar_one() or 0

    # --- Cuentas pendientes -----------------------------------------------
    cuentas_result = await db.execute(
        select(func.count(CuentaCobro.id))
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Contrato.usuario_id == user.id,
            CuentaCobro.estado.in_(["borrador", "en_revision"]),
        )
    )
    cuentas_pendientes: int = cuentas_result.scalar_one() or 0

    # --- Créditos disponibles ---------------------------------------------------
    # CONSUMO records store negative cantidad, so a simple SUM gives the real
    # balance without the double-negative error of separating ingreso/consumo.
    try:
        ingreso_result = await db.execute(
            select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
                Credito.usuario_id == user.id,
                Credito.tipo.in_([TipoCredito.COMPRA, TipoCredito.BONUS]),
            )
        )
        consumo_result = await db.execute(
            select(func.coalesce(func.sum(Credito.cantidad), 0)).where(
                Credito.usuario_id == user.id,
                Credito.tipo == TipoCredito.CONSUMO,
            )
        )
        ingreso_total: int = int(ingreso_result.scalar_one() or 0)
        consumo_total: int = int(consumo_result.scalar_one() or 0)  # already negative
        creditos_disponibles: int = max(0, ingreso_total + consumo_total)
        creditos_detalle = {
            "ingreso": ingreso_total,
            "consumido": abs(consumo_total),
            "saldo": creditos_disponibles,
        }
    except Exception:
        creditos_disponibles = 0
        creditos_detalle = {"ingreso": 0, "consumido": 0, "saldo": 0}

    # --- Total pagos del mes (cuentas cobro aprobadas) --------------------
    from datetime import date
    from sqlalchemy import extract

    now = date.today()
    total_pagos_result = await db.execute(
        select(func.count(CuentaCobro.id))
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Contrato.usuario_id == user.id,
            CuentaCobro.estado == "aprobada",
            extract("year", CuentaCobro.created_at) == now.year,
            extract("month", CuentaCobro.created_at) == now.month,
        )
    )
    total_pagos_mes: int = total_pagos_result.scalar_one() or 0

    return {
        "contratos_activos": contratos_activos,
        "cuentas_pendientes": cuentas_pendientes,
        "creditos_disponibles": creditos_disponibles,
        "creditos_detalle": creditos_detalle,
        "total_pagos_mes": total_pagos_mes,
    }
