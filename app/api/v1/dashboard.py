"""Dashboard endpoint — aggregated stats for the authenticated user (Phase 8)."""

from __future__ import annotations

from datetime import date

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.models.contrato import Contrato
from app.models.credito import Credito, TipoCredito
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro

logger = structlog.get_logger("api.dashboard")
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return aggregated dashboard stats for the current user.

    Used by the frontend landing page after login. Kept to 3 DB round-trips
    (contratos / cuentas / créditos) via conditional aggregation, because every
    extra query costs a full network round-trip on a remote Postgres (Neon).
    """
    # --- Contratos activos ------------------------------------------------
    contratos_result = await db.execute(
        select(func.count(Contrato.id)).where(
            Contrato.usuario_id == user.id,
            Contrato.deleted_at.is_(None),
        )
    )
    contratos_activos: int = contratos_result.scalar_one() or 0

    # --- Cuentas: pendientes + pagos del mes en UNA sola query ------------
    # Both aggregate over cuentas_cobro joined to the user's contratos, so a single
    # conditional aggregation replaces two separate COUNT round-trips.
    # Use enum members, not string literals: Postgres native enums reject any value
    # not in the type (SQLite silently tolerated the invalid "en_revision").
    now = date.today()
    cuentas_result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            CuentaCobro.estado.in_(
                                [EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.ENVIADA]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pendientes"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                CuentaCobro.estado == EstadoCuentaCobro.APROBADA,
                                func.extract("year", CuentaCobro.created_at) == now.year,
                                func.extract("month", CuentaCobro.created_at) == now.month,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pagos_mes"),
        )
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(Contrato.usuario_id == user.id)
    )
    cuentas_row = cuentas_result.one()
    cuentas_pendientes: int = int(cuentas_row.pendientes or 0)
    total_pagos_mes: int = int(cuentas_row.pagos_mes or 0)

    # --- Créditos: ingreso + consumo + saldo en UNA sola query -----------
    # CONSUMO records store negative cantidad, so SUM(all) IS the balance. The
    # headline saldo (= SUM of the whole ledger) stays the single source of truth,
    # matching /auth/me and /pagos/creditos/balance — now without 3 separate SUMs.
    try:
        creditos_result = await db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                Credito.tipo.in_([TipoCredito.COMPRA, TipoCredito.BONUS]),
                                Credito.cantidad,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("ingreso"),
                func.coalesce(
                    func.sum(
                        case((Credito.tipo == TipoCredito.CONSUMO, Credito.cantidad), else_=0)
                    ),
                    0,
                ).label("consumo"),
                func.coalesce(func.sum(Credito.cantidad), 0).label("saldo"),
            ).where(Credito.usuario_id == user.id)
        )
        creditos_row = creditos_result.one()
        ingreso_total: int = int(creditos_row.ingreso or 0)
        consumo_total: int = int(creditos_row.consumo or 0)  # already negative
        creditos_disponibles: int = int(creditos_row.saldo or 0)
        creditos_detalle = {
            "ingreso": ingreso_total,
            "consumido": abs(consumo_total),
            "saldo": creditos_disponibles,
        }
    except Exception:
        creditos_disponibles = 0
        creditos_detalle = {"ingreso": 0, "consumido": 0, "saldo": 0}

    return {
        "contratos_activos": contratos_activos,
        "cuentas_pendientes": cuentas_pendientes,
        "creditos_disponibles": creditos_disponibles,
        "creditos_detalle": creditos_detalle,
        "total_pagos_mes": total_pagos_mes,
    }
