"""Self-service purge of test data, plus the global admin wipe.

`purgar_datos_prueba` reuses the already-tested cascades in
`cuenta_cobro_service.eliminar_cuenta_cobro` (cuentas de cobro + everything
scoped to them) and `contrato_service.limpiar_obligaciones` (obligaciones
reset) — never reimplements their delete logic. Contratos, documento_fuente
rows, and the user account itself are never touched.

`purgar_todo_admin` wipes ALL users' data. Its cuenta-scoped cascade is the
same delete order `scripts/wipe_cuentas.py` used to own — extracted here as
`wipe_cuentas_globalmente` so the script and the admin endpoint share one
implementation instead of two copies drifting apart.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.port import StoragePort
from app.core.exceptions import ValidationError
from app.models.actividad import Actividad
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
)
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion
from app.models.requisito_cuenta import RequisitoCuenta
from app.services import contrato_service, cuenta_cobro_service

logger = structlog.get_logger("service.purga")

# Child -> parent order — mirrors `eliminar_cuenta_cobro`'s FK-safe explicit
# delete order (SQLite doesn't enforce FKs, so an unconditional cascade must
# still respect it). Every one of these is fully cuenta-scoped, so a full
# delete-all across every user is a correct global wipe.
_DELETE_ALL: list[tuple[str, type]] = [
    ("evidencia_obligacion", EvidenciaObligacion),
    ("evidencia", Evidencia),
    ("clasificacion_job", ClasificacionEvidenciasJob),
    ("actividad", Actividad),
    ("borrador", BorradorCuentaCobro),
    ("documento_requisito_vinculo", DocumentoRequisitoVinculo),
    ("documento_cuenta_cobro", DocumentoCuentaCobro),
    ("documento_checklist_candidato", DocumentoChecklistCandidato),
    ("requisito_cuenta", RequisitoCuenta),
]
# Kept rows — only the nullable pointer is cleared.
_NULLIFY: list[tuple[str, type]] = [
    ("documento_fuente_actualizados", DocumentoFuente),
    ("conversacion_actualizadas", Conversacion),
]


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


async def wipe_cuentas_globalmente(
    db: AsyncSession, storage: StoragePort, *, origen: str = "purgar_todo_admin"
) -> dict[str, int]:
    """Hard-delete every cuenta de cobro (ALL users) and its uploaded files.

    Extracted from `scripts/wipe_cuentas.py` so the CLI and `purgar_todo_admin`
    share one delete order instead of maintaining two. Does not commit — the
    caller decides the transaction boundary (API request lifecycle commits via
    `get_db`; the CLI script commits explicitly). `origen` only tags storage
    failure logs so both callers keep their own log provenance.
    """
    evidencia_storage_keys = [k for (k,) in (await db.execute(select(Evidencia.storage_key))).all() if k]
    cuentas = (
        await db.execute(
            select(CuentaCobro.id, CuentaCobro.pdf_storage_key, Contrato.usuario_id).outerjoin(
                Contrato, Contrato.id == CuentaCobro.contrato_id
            )
        )
    ).all()
    pdf_storage_keys = [row[1] for row in cuentas if row[1]]

    counts: dict[str, int] = {}
    for name, model in _DELETE_ALL:
        counts[name] = (await db.execute(delete(model))).rowcount or 0
    for name, model in _NULLIFY:
        counts[name] = (
            await db.execute(update(model).where(model.cuenta_cobro_id.is_not(None)).values(cuenta_cobro_id=None))
        ).rowcount or 0
    counts["cuenta_cobro"] = (await db.execute(delete(CuentaCobro))).rowcount or 0

    # Best-effort storage cleanup — DB deletion is the source of truth and
    # must never be failed by a storage-side error.
    ok, failed = await cuenta_cobro_service._borrar_storage_keys_best_effort(
        storage, evidencia_storage_keys, origen=origen
    )
    ok2, failed2 = await cuenta_cobro_service._borrar_storage_keys_best_effort(storage, pdf_storage_keys, origen=origen)
    ok += ok2
    failed += failed2
    for cuenta_id, _pdf_key, usuario_id in cuentas:
        if usuario_id is None:
            continue  # contrato gone too — no reliable prefix to list
        obj_ok, obj_failed = await cuenta_cobro_service._listar_y_borrar_prefix_best_effort(
            storage, cuenta_cobro_service._paquete_prefix(usuario_id, cuenta_id), origen=origen
        )
        ok += obj_ok
        failed += obj_failed

    counts["storage_keys_ok"] = ok
    counts["storage_keys_failed"] = failed
    return counts


async def purgar_todo_admin(db: AsyncSession, storage: StoragePort) -> dict[str, int]:
    """GLOBAL wipe — ALL users' cuentas de cobro + storage (via
    `wipe_cuentas_globalmente`) plus a full obligaciones reset on every
    contrato. Admin-gated at the API layer (`AdminUser` dependency) — this
    function trusts its caller and never checks role itself.

    The requesting admin's own `usuarios` row (and every other user's row) is
    never touched: every table this function mutates is contrato/cuenta-scoped,
    `usuarios` itself is never a delete/update target here — so there is no
    auto-lockout to guard against.
    """
    counts = await wipe_cuentas_globalmente(db, storage)

    contratos = (await db.execute(select(Contrato.id, Contrato.usuario_id))).all()
    usuarios_afectados = len({row[1] for row in contratos})

    # Safe as a plain bulk delete (no ownership loop needed like
    # `limpiar_obligaciones`'s per-user guard): the cascade above already
    # deleted every `actividad`/`evidencia_obligacion` row globally, so no
    # active-cuenta reference can block it here.
    obligaciones_borradas = (await db.execute(delete(Obligacion))).rowcount or 0
    await db.execute(update(Contrato).values(obligaciones_extraidas=None))

    await logger.ainfo(
        "purgar_todo_admin",
        cuentas_borradas=counts["cuenta_cobro"],
        obligaciones_borradas=obligaciones_borradas,
        contratos_afectados=len(contratos),
        usuarios_afectados=usuarios_afectados,
    )

    return {
        "cuentas_borradas": counts["cuenta_cobro"],
        "obligaciones_borradas": obligaciones_borradas,
        "contratos_afectados": len(contratos),
        "usuarios_afectados": usuarios_afectados,
        "storage_keys_ok": counts["storage_keys_ok"],
        "storage_keys_failed": counts["storage_keys_failed"],
    }
