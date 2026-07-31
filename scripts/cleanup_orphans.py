"""Operator maintenance CLI — purge orphaned cuenta-de-cobro data (legacy
soft-delete leftovers + any row with a broken FK chain).

Run from the cashing-backend/ directory with the venv activated:

    python scripts/cleanup_orphans.py --dry-run   # preview candidate counts, no mutation
    python scripts/cleanup_orphans.py             # execute (asks for confirmation)
    python scripts/cleanup_orphans.py --yes       # execute, skip the confirmation prompt

Calls the EXISTING `cuenta_cobro_service.purgar_huerfanos_cuentas(...)` (the
same set-based cleanup used by the maintenance surface — no ad-hoc SQL here),
prints the resulting counts dict as JSON. No auth — operator-only tool, not
exposed via the API.

Guardrails (mirrors `scripts/grant_credits.py`'s confirmation gate):
  - Echoes the resolved DATABASE_URL target (credentials masked) before
    anything else runs.
  - `--dry-run` computes and prints the candidate counts WITHOUT deleting
    (delegates to `purgar_huerfanos_cuentas(..., dry_run=True)`).
  - A real run asks for interactive confirmation — must type PURGAR exactly —
    unless `--yes` is passed.
  - A real run against a local sqlite file automatically backs it up first
    (`{file}.bak-purga-{timestamp}`, a plain file copy). Non-sqlite targets
    (Postgres, etc.) skip this with a printed note — back those up with your
    own DB-level snapshot/backup tooling instead.
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
from app.services import cuenta_cobro_service
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_SQLITE_MARKER = ":///"


def _mask_database_url(url: str) -> str:
    """Mask a `user:password@` credential segment, if present. sqlite URLs
    (the local dev default) never have one and pass through unchanged."""
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def _sqlite_file_path(database_url: str) -> str | None:
    """Return the on-disk path for a `sqlite(+driver):///path` URL, or None
    for any other backend (Postgres, etc. — no local file to back up)."""
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
    dest = src.with_name(f"{src.name}.bak-purga-{timestamp}")
    shutil.copy2(src, dest)
    return str(dest)


async def cleanup_orphans(*, dry_run: bool, yes: bool) -> dict[str, int]:
    print(f"DATABASE_URL: {_mask_database_url(settings.DATABASE_URL)}")

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    storage = get_storage(settings.S3_BUCKET_PDFS)  # same bucket eliminar_cuenta_cobro uses for evidencia + pdf keys

    if dry_run:
        print("--dry-run: computing candidate counts, NOTHING will be deleted.")
        async with async_session() as db:
            counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, storage, dry_run=True)  # type: ignore[arg-type]
        await engine.dispose()
        return counts

    sqlite_path = _sqlite_file_path(settings.DATABASE_URL)
    if sqlite_path:
        backup_path = _backup_sqlite_file(sqlite_path)
        if backup_path:
            print(f"Backed up {sqlite_path} -> {backup_path}")
        else:
            print(f"WARNING: {sqlite_path} not found, skipping backup.")
    else:
        print("Non-sqlite DATABASE_URL — skipping automatic file backup; use your DB's own backup/snapshot first.")

    if not yes:
        prompt = 'Esta acción BORRA datos huérfanos de forma permanente. Escribí "PURGAR" para confirmar: '
        confirmacion = input(prompt).strip()
        if confirmacion != "PURGAR":
            print("Operación cancelada.")
            await engine.dispose()
            return {}

    print("Purgando...")
    async with async_session() as db:
        counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, storage)  # type: ignore[arg-type]

    await engine.dispose()
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Purge orphaned cuenta-de-cobro data (legacy soft-delete leftovers).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Only compute and print candidate counts, delete nothing."
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive PURGAR confirmation prompt.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = asyncio.run(cleanup_orphans(dry_run=args.dry_run, yes=args.yes))
    print(json.dumps(result, indent=2))
