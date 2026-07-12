"""Adición event log — records contract Adición/prórroga/otrosí events.

Replaces the previous single scalar `Contrato.valor_adicion` (kept for back-compat,
design D4) as the authoritative source for cuota position/final detection and the
coherence validator's R6 rule (`coherence_validator_service`).

CROSS-CHANGE NOTE (task 4.8, 2026-07-12): `secop_service.obtener_adiciones_contrato()`
(landed in `secop-full-acquisition` slice 2) is the SECOP-side source of Adición data —
its `valor_adicion` is best-effort regex-parsed from cb9c-h8sn's free-text
`descripcion` and MAY BE `None` (the dataset has no structured value field). This
module never coerces a missing SECOP value to `0` — `AdicionContrato.valor_adicion`
stays nullable end-to-end, and `registrar_adicion` accepts `None` as-is (an
"unknown-value adición" is still a real, recordable event).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.adicion_contrato import AdicionContrato, TipoAdicion
from app.models.contrato import Contrato


async def _get_contrato_con_ownership(db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID) -> Contrato:
    contrato = await db.get(Contrato, contrato_id)
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))
    if contrato.usuario_id != usuario_id:
        raise ForbiddenError()
    return contrato


async def registrar_adicion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    *,
    tipo: TipoAdicion,
    numero: int,
    fecha_evento: date,
    rpc_nuevo: str | None = None,
    cdp_nuevo: str | None = None,
    valor_adicion: Decimal | None = None,
    nueva_fecha_fin: date | None = None,
    descripcion: str = "",
) -> AdicionContrato:
    """Record a new Adición/prórroga/otrosí event for a contract.

    Never overwrites a prior event — every call inserts a new row, so the full
    history stays queryable in chronological order (spec: "Multiple Adición events
    are preserved").
    """
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    evento = AdicionContrato(
        contrato_id=contrato_id,
        tipo=tipo,
        numero=numero,
        rpc_nuevo=rpc_nuevo,
        cdp_nuevo=cdp_nuevo,
        valor_adicion=valor_adicion,
        nueva_fecha_fin=nueva_fecha_fin,
        descripcion=descripcion,
        fecha_evento=fecha_evento,
    )
    db.add(evento)
    await db.flush()
    await db.refresh(evento)
    return evento


async def listar_adiciones(db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID) -> list[AdicionContrato]:
    """Return every recorded Adición event for a contract, in chronological order
    (`fecha_evento` ascending, `created_at` ascending as a stable tie-break for
    same-day events) — the second event of a day never overwrites the first."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    result = await db.execute(
        select(AdicionContrato)
        .where(AdicionContrato.contrato_id == contrato_id)
        .order_by(AdicionContrato.fecha_evento.asc(), AdicionContrato.created_at.asc())
    )
    return list(result.scalars().all())
