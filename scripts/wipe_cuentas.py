"""Operator maintenance CLI — HARD-DELETE **every** cuenta de cobro and its
associated data + uploaded files. Unlike `scripts/cleanup_orphans.py` (which
only removes orphan/tombstone leftovers), this wipes ALL live cuentas for ALL
users. Contratos, obligaciones, conversaciones and documentos_fuente survive —
only their nullable `cuenta_cobro_id` pointer is cleared.

Run from the cashing-backend/ directory with the venv activated:

    python scripts/wipe_cuentas.py --dry-run   # preview row counts, no mutation
    python scripts/wipe_cuentas.py             # execute (asks for confirmation)
    python scripts/wipe_cuentas.py --yes       # execute, skip the confirmation prompt

Mirrors `eliminar_cuenta_cobro`'s FK-safe explicit delete order (SQLite does not
enforce FKs, so cascade alone would leave orphans there) applied unconditionally
across every row instead of one cuenta at a time. Reuses the same best-effort
storage-cleanup helpers so evidencia files, PDFs and paquete prefixes go too.

Guardrails (same as `scripts/cleanup_orphans.py`):
  - Echoes the resolved DATABASE_URL target (credentials masked) before anything.
  - `--dry-run` prints per-table row counts WITHOUT deleting.
  - A real run asks for interactive confirmation — type BORRAR TODO exactly —
    unless `--yes` is passed.
  - A real run against a local sqlite file backs it up first
    (`{file}.bak-wipe-{timestamp}`). Non-sqlite targets skip this — snapshot
    those with your own DB tooling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, ".")  # run from cashing-backend/

from app.adapters.storage import get_storage
from app.core.config import settings
from app.core.db_ssl import prepare_pg_url
from app.services import cuenta_cobro_service as ccs
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_SQLITE_MARKER = ":///"

# Child -> parent order, same as eliminar_cuenta_cobro. Every one of these is
# fully cuenta-scoped, so an unconditional delete-all is the correct full wipe.
_DELETE_ALL = [
    ("evidencia_obligacion", ccs.EvidenciaObligacion),
    ("evidencia", ccs.Evidencia),
    ("clasificacion_job", ccs.ClasificacionEvidenciasJob),
    ("actividad", ccs.Actividad),
    ("borrador", ccs.BorradorCuentaCobro),
    ("documento_requisito_vinculo", ccs.DocumentoRequisitoVinculo),
    ("documento_cuenta_cobro", ccs.DocumentoCuentaCobro),
    ("documento_checklist_candidato", ccs.DocumentoChecklistCandidato),
    ("requisito_cuenta", ccs.RequisitoCuenta),
]
# Kept rows — only the nullable pointer is cleared.
_NULLIFY = [
    ("documento_fuente_actualizados", ccs.DocumentoFuente),
    ("conversacion_actualizadas", ccs.Conversacion),
]


def _mask_database_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def _sqlite_file_path(database_url: str) -> str | None:
    if "sqlite" not in database_url:
        return None
    idx = database_url.find(_SQLITE_MARKER)
    if idx == -1:
        return None
    path = database_url[idx + len(_SQLITE_MARKER) :]
    return path or None


def _backup_sqlite_file(path: str) -> str | None:
    src = Path(path)
    if not src.exists():
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = src.with_name(f"{src.name}.bak-wipe-{timestamp}")
    shutil.copy2(src, dest)
    return str(dest)


async def _count_all(db: AsyncSession) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, model in [*_DELETE_ALL, ("cuenta_cobro", ccs.CuentaCobro)]:
        counts[name] = (await db.execute(select(func.count()).select_from(model))).scalar_one()
    for name, model in _NULLIFY:
        counts[name] = (
            await db.execute(select(func.count()).select_from(model).where(model.cuenta_cobro_id.is_not(None)))
        ).scalar_one()
    return counts


async def wipe_cuentas(*, dry_run: bool, yes: bool) -> dict[str, int]:
    print(f"DATABASE_URL: {_mask_database_url(settings.DATABASE_URL)}")

    # Postgres URLs carry libpq-only params (sslmode, channel_binding) asyncpg
    # can't parse directly — same translation app/core/database.py applies to
    # the real app engine; this script was never run through it before.
    _db_url, _connect_args = prepare_pg_url(settings.DATABASE_URL)
    engine = create_async_engine(_db_url, connect_args=_connect_args, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = get_storage(settings.S3_BUCKET_PDFS)  # same bucket eliminar_cuenta_cobro uses

    if dry_run:
        print("--dry-run: counting rows, NOTHING will be deleted.")
        async with async_session() as db:
            counts = await _count_all(db)
        await engine.dispose()
        return counts

    sqlite_path = _sqlite_file_path(settings.DATABASE_URL)
    if sqlite_path:
        backup_path = _backup_sqlite_file(sqlite_path)
        print(
            f"Backed up {sqlite_path} -> {backup_path}"
            if backup_path
            else f"WARNING: {sqlite_path} not found, skipping backup."
        )
    else:
        print("Non-sqlite DATABASE_URL — skipping automatic file backup; snapshot your DB first.")

    if not yes:
        prompt = 'Esta acción BORRA TODAS las cuentas de cobro y sus archivos de forma permanente. Escribí "BORRAR TODO" para confirmar: '
        if input(prompt).strip() != "BORRAR TODO":
            print("Operación cancelada.")
            await engine.dispose()
            return {}

    print("Borrando...")
    async with async_session() as db:
        # Collect storage keys BEFORE deleting the rows (session expires ORM
        # attrs on commit; keys must be captured as plain values first).
        evidencia_storage_keys = [k for (k,) in (await db.execute(select(ccs.Evidencia.storage_key))).all() if k]
        cuentas = (
            await db.execute(
                select(ccs.CuentaCobro.id, ccs.CuentaCobro.pdf_storage_key, ccs.Contrato.usuario_id).outerjoin(
                    ccs.Contrato, ccs.Contrato.id == ccs.CuentaCobro.contrato_id
                )
            )
        ).all()
        pdf_storage_keys = [row[1] for row in cuentas if row[1]]

        counts: dict[str, int] = {}
        for name, model in _DELETE_ALL:
            counts[name] = (await db.execute(sa_delete(model))).rowcount or 0
        for name, model in _NULLIFY:
            counts[name] = (
                await db.execute(
                    sa_update(model).where(model.cuenta_cobro_id.is_not(None)).values(cuenta_cobro_id=None)
                )
            ).rowcount or 0
        counts["cuenta_cobro"] = (await db.execute(sa_delete(ccs.CuentaCobro))).rowcount or 0
        await db.commit()

    # Best-effort storage cleanup AFTER the commit — DB deletion is the source
    # of truth and must never be failed by a storage-side error.
    ok, failed = await ccs._borrar_storage_keys_best_effort(storage, evidencia_storage_keys, origen="wipe_cuentas")
    ok2, failed2 = await ccs._borrar_storage_keys_best_effort(storage, pdf_storage_keys, origen="wipe_cuentas")
    ok += ok2
    failed += failed2
    for cuenta_id, _pdf_key, usuario_id in cuentas:
        if usuario_id is None:
            continue  # contrato gone too — no reliable prefix to list
        obj_ok, obj_failed = await ccs._listar_y_borrar_prefix_best_effort(
            storage, ccs._paquete_prefix(usuario_id, cuenta_id), origen="wipe_cuentas"
        )
        ok += obj_ok
        failed += obj_failed

    counts["storage_keys_ok"] = ok
    counts["storage_keys_failed"] = failed

    await engine.dispose()
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hard-delete ALL cuentas de cobro and their uploaded files.")
    parser.add_argument("--dry-run", action="store_true", help="Only count rows, delete nothing.")
    parser.add_argument("--yes", action="store_true", help='Skip the interactive "BORRAR TODO" confirmation prompt.')
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = asyncio.run(wipe_cuentas(dry_run=args.dry_run, yes=args.yes))
    print(json.dumps(result, indent=2))
