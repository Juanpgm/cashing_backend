"""Self-service purge of one user's own radicación test data.

Reuses the already-tested cascades in `cuenta_cobro_service.eliminar_cuenta_cobro`
(cuentas de cobro + everything scoped to them) and `contrato_service.limpiar_obligaciones`
(obligaciones reset) — never reimplements their delete logic. Contratos,
documento_fuente rows, and the user account itself are never touched.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.port import StoragePort
from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.services import contrato_service, cuenta_cobro_service

logger = structlog.get_logger("service.purga")


async def purgar_datos_prueba(db: AsyncSession, usuario_id: uuid.UUID, storage: StoragePort) -> dict[str, int]:
    """Reset ONE user's own radicación state for re-testing: delete all their
    cuentas de cobro (reusing eliminar_cuenta_cobro's tested cascade) and
    reset obligaciones on all their contratos (reusing limpiar_obligaciones).
    Contratos, documento_fuente, and the user account itself survive.
    """
    cuenta_ids = [
        row[0]
        for row in (
            await db.execute(
                select(CuentaCobro.id)
                .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
                .where(
                    Contrato.usuario_id == usuario_id,
                    CuentaCobro.deleted_at.is_(None),
                    Contrato.deleted_at.is_(None),
                )
            )
        ).all()
    ]
    cuentas_borradas = 0
    for cuenta_id in cuenta_ids:
        await cuenta_cobro_service.eliminar_cuenta_cobro(db, usuario_id, cuenta_id, storage)
        cuentas_borradas += 1

    contrato_ids = [
        row[0]
        for row in (
            await db.execute(
                select(Contrato.id).where(Contrato.usuario_id == usuario_id, Contrato.deleted_at.is_(None))
            )
        ).all()
    ]
    obligaciones_reiniciadas = 0
    contratos_omitidos = 0
    for contrato_id in contrato_ids:
        try:
            obligaciones_reiniciadas += await contrato_service.limpiar_obligaciones(db, usuario_id, contrato_id)
        except ValidationError:
            # Robustness only — the cuenta deletes above should have already
            # cleared every blocker, but a contrato omitted here is reported
            # rather than aborting the rest of the purge.
            contratos_omitidos += 1

    await logger.ainfo(
        "datos_prueba_purgados",
        usuario_id=str(usuario_id),
        cuentas_borradas=cuentas_borradas,
        obligaciones_reiniciadas=obligaciones_reiniciadas,
        contratos_omitidos=contratos_omitidos,
    )

    return {
        "cuentas_borradas": cuentas_borradas,
        "obligaciones_reiniciadas": obligaciones_reiniciadas,
        "contratos_omitidos": contratos_omitidos,
    }
