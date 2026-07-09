"""Dashboard endpoint: correct pending count (enum members) and ledger-derived saldo."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contrato import Contrato
from app.models.credito import Credito, TipoCredito
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro


@pytest.mark.asyncio
async def test_dashboard_pending_count_and_saldo(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    contrato = Contrato(
        usuario_id=user.id, numero_contrato="001", objeto="x",
        valor_total=1, valor_mensual=1, fecha_inicio=date(2024, 1, 1), fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    db.add(CuentaCobro(contrato_id=contrato.id, mes=1, anio=2024, valor=1, estado=EstadoCuentaCobro.BORRADOR))
    db.add(CuentaCobro(contrato_id=contrato.id, mes=2, anio=2024, valor=1, estado=EstadoCuentaCobro.APROBADA))
    db.add(Credito(usuario_id=user.id, cantidad=30, tipo=TipoCredito.BONUS, referencia="signup"))
    await db.commit()

    resp = await client.get("/api/v1/dashboard", headers=test_user["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["cuentas_pendientes"] == 1  # only the BORRADOR one
    assert data["creditos_disponibles"] == 30  # ledger
