"""Storage orphan audit — Req 10c.

Detects objects in object storage with no matching DB row (orphans) and, ONLY
with `--delete`, removes them. Report-only by default. NOT a daemon, NOT an
endpoint, NOT a cron job — run it by hand when needed:

    uv run python -m scripts.audit_storage_orphans                       # report only, all scopes
    uv run python -m scripts.audit_storage_orphans --scope documentos    # report only, one scope
    uv run python -m scripts.audit_storage_orphans --delete              # delete orphans older than 24h (default)
    uv run python -m scripts.audit_storage_orphans --delete --min-age-hours 72

Scopes scanned:
  - evidencias: `evidencias/` prefix in `S3_BUCKET_PDFS`. Referenced set =
    every non-null `Evidencia.storage_key` (link-only evidencia rows have no
    storage_key and are irrelevant here).
  - documentos: `usuarios/` prefix in `S3_BUCKET_DOCUMENTOS`. Referenced set =
    every `DocumentoFuente.storage_key` (non-nullable on that model). Plantillas
    de organismo are `DocumentoFuente` rows too, so they're already covered —
    no separate handling needed.

Sub-prefix NOT scanned here — `app/agent/tools/file_organizer.py` also writes
`usuarios/{user_id}/cuentas/{cuenta_cobro_id}/{periodo}/*/.keep` marker keys,
but into `S3_BUCKET_EVIDENCIAS` (its default bucket when constructed with no
explicit bucket arg) — a DIFFERENT bucket from the `usuarios/` prefix scanned
by the documentos scope (`S3_BUCKET_DOCUMENTOS`). It never collides. Paquete
ZIPs live under `paquetes/` in `S3_BUCKET_EVIDENCIAS` and are out of scope for
both scans here (not audited by this script).

Safety:
  - `--delete` is off by default — without it, `storage.delete` is NEVER called.
  - With `--delete`, only orphans whose storage `last_modified` is older than
    `--min-age-hours` (default 24) are removed — protects uploads that are
    mid-flight (object written, DB row not committed yet). An orphan whose
    `last_modified` couldn't be determined is treated as too young (never
    deleted) — fail safe.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from app.adapters.storage import get_storage
from app.adapters.storage.port import StoragePort
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger("scripts.audit_storage_orphans")

SCOPES = ("evidencias", "documentos")

_BUCKET_BY_SCOPE = {
    "evidencias": settings.S3_BUCKET_PDFS,
    "documentos": settings.S3_BUCKET_DOCUMENTOS,
}
_PREFIX_BY_SCOPE = {
    "evidencias": "evidencias/",
    "documentos": "usuarios/",
}


@dataclass(frozen=True)
class Orphan:
    scope: str
    key: str
    size_bytes: int
    last_modified: datetime | None


async def _referenced_keys(db: AsyncSession, scope: str) -> set[str]:
    """Every storage_key currently referenced by a DB row for `scope`."""
    if scope == "evidencias":
        stmt = select(Evidencia.storage_key).where(Evidencia.storage_key.is_not(None))
    elif scope == "documentos":
        stmt = select(DocumentoFuente.storage_key)
    else:
        raise ValueError(f"Unknown scope: {scope!r}")
    result = await db.execute(stmt)
    return {key for key in result.scalars().all() if key}


async def find_orphans(db: AsyncSession, storage: StoragePort, scope: str) -> list[Orphan]:
    """List objects under `scope`'s prefix that have no matching DB row.

    Empty bucket / missing prefix just yields an empty list (`list_objects`
    returns `[]` for both adapters in that case — nothing special to handle).
    """
    referenced = await _referenced_keys(db, scope)
    objects = await storage.list_objects(prefix=_PREFIX_BY_SCOPE[scope])
    return [
        Orphan(scope=scope, key=obj.key, size_bytes=obj.size_bytes, last_modified=obj.last_modified)
        for obj in objects
        if obj.key not in referenced
    ]


async def delete_orphans(storage: StoragePort, orphans: list[Orphan], min_age_hours: int) -> tuple[int, int]:
    """Delete orphans older than `min_age_hours`. Returns (deleted, skipped_too_young).

    Never called unless the caller passed `--delete` — see `main()`.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=min_age_hours)
    deleted = 0
    skipped = 0
    for orphan in orphans:
        if orphan.last_modified is None or orphan.last_modified > cutoff:
            skipped += 1
            continue
        await storage.delete(orphan.key)
        await logger.ainfo("orphan_deleted", scope=orphan.scope, key=orphan.key, size_bytes=orphan.size_bytes)
        deleted += 1
    return deleted, skipped


async def _run_scope(db: AsyncSession, scope: str, *, do_delete: bool, min_age_hours: int, limit: int | None) -> None:
    bucket = _BUCKET_BY_SCOPE[scope]
    storage: StoragePort = get_storage(bucket)  # type: ignore[assignment]
    try:
        orphans = await find_orphans(db, storage, scope)
    except Exception as exc:  # storage/network unreachable — report cleanly, no traceback
        await logger.aerror("scope_scan_failed", scope=scope, bucket=bucket, error=str(exc))
        return

    total_bytes = sum(o.size_bytes for o in orphans)
    sample = orphans[:limit] if limit is not None else orphans
    await logger.ainfo(
        "scope_report",
        scope=scope,
        bucket=bucket,
        prefix=_PREFIX_BY_SCOPE[scope],
        orphan_count=len(orphans),
        total_bytes=total_bytes,
        sample_keys=[o.key for o in sample],
    )

    if not do_delete:
        return
    deleted, skipped = await delete_orphans(storage, orphans, min_age_hours)
    await logger.ainfo("scope_delete_summary", scope=scope, deleted=deleted, skipped_too_young=skipped)


async def main() -> None:
    args = _parse_args()
    scopes = SCOPES if args.scope == "all" else (args.scope,)

    async with async_session_factory() as db:
        for scope in scopes:
            await _run_scope(db, scope, do_delete=args.delete, min_age_hours=args.min_age_hours, limit=args.limit)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report (and optionally delete) storage objects with no matching DB row."
    )
    parser.add_argument("--scope", choices=(*SCOPES, "all"), default="all")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete orphans older than --min-age-hours. Default: report only, never deletes.",
    )
    parser.add_argument(
        "--min-age-hours",
        type=int,
        default=24,
        help="With --delete, only delete orphans whose storage last-modified is older than this (default: 24).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap sample keys printed per scope.")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main())
