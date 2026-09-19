"""Read-only post-deploy check: where is the database's Alembic state?

Reports `alembic_version` vs the repo's head revision, whether `paquete_job`
and its index exist, and the `solo_primera_cuenta` flag of every
`requisitos_documento` row (RPC/CDP/CONTRATO flip to True in migration 043).

SAFE AGAINST PROD, by construction:
  * every statement is a SELECT, and on PostgreSQL the session first asks the
    server for a READ ONLY transaction (`SET TRANSACTION READ ONLY`), so the
    server should refuse any write. That request is verified against a real
    PostgreSQL only by the PG test suite (`TestReadOnlyOnPostgres`, run with
    `scripts/test-postgres.sh`); the default SQLite suite checks the source
    (SELECT-only literals) and that the statement is issued first;
  * nothing that identifies the database is printed: no URL, host, user,
    password or database name — only the dialect. On failure only the
    exception CLASS NAME is shown, because driver messages can echo the DSN.

Usage (from the repo root; DATABASE_URL must resolve to a PUBLIC host, the
`*.railway.internal` one is unreachable from a laptop):
  railway run --service cashin-api -- uv run python scripts/check_alembic_state.py
  # or against any DATABASE_URL in the environment:
  uv run python scripts/check_alembic_state.py

Exit code: 0 = database is at the repo head, 2 = behind head / unversioned,
1 = could not read the database.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

# RELEASE-SPECIFIC: the index below, the `paquete_job` table checks in `_inspect` and the
# `solo_primera_cuenta` flags query are what release 042/043 needed to verify. Update them
# for every release whose migrations change what "healthy" looks like (docs/deploy-runbook.md,
# section 5.2). The generic part is the alembic_version vs repo head comparison.
_INDEX = "ix_paquete_job_cuenta_cobro_id"


def _repo_head() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _inspect(sync_conn: sa.Connection) -> dict[str, object]:
    inspector = sa.inspect(sync_conn)
    has_version = inspector.has_table("alembic_version")
    has_paquete = inspector.has_table("paquete_job")
    paquete_index = has_paquete and any(ix["name"] == _INDEX for ix in inspector.get_indexes("paquete_job"))
    flags: dict[str, bool] = {}
    if inspector.has_table("requisitos_documento"):
        rows = sync_conn.execute(sa.text("SELECT codigo, solo_primera_cuenta FROM requisitos_documento"))
        flags = {str(codigo): bool(flag) for codigo, flag in rows}
    version: str | None = None
    if has_version:
        row = sync_conn.execute(sa.text("SELECT version_num FROM alembic_version")).first()
        version = str(row[0]) if row is not None else None
    return {
        "alembic_version": version,
        "alembic_version_table": has_version,
        "paquete_job_table": has_paquete,
        "paquete_job_index": paquete_index,
        "flags": dict(sorted(flags.items())),
    }


async def _begin_read_only(conn: AsyncConnection) -> None:
    """Ask the server for a READ ONLY transaction. Must be the FIRST statement of the transaction."""
    await conn.execute(sa.text("SET TRANSACTION READ ONLY"))


async def collect(url: str) -> dict[str, object]:
    """Read the state of the database at `url` (read-only) and compare it with the repo head."""
    from app.core.db_ssl import prepare_pg_url

    clean_url, connect_args = prepare_pg_url(url)
    engine = create_async_engine(clean_url, connect_args=connect_args, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                await _begin_read_only(conn)
            report = await conn.run_sync(_inspect)
            await conn.rollback()
    finally:
        await engine.dispose()
    head = _repo_head()
    report["head"] = head
    report["at_head"] = report["alembic_version"] is not None and report["alembic_version"] == head
    report["dialect"] = engine.dialect.name
    return report


def _print(report: dict[str, object]) -> None:
    # Dialect only: no host, user, password or database name may reach the terminal.
    print(f"database        : {report['dialect']}")
    print(f"alembic_version : {report['alembic_version'] or '(none)'}")
    print(f"repo head       : {report['head']}")
    print(f"at head         : {'YES' if report['at_head'] else 'NO'}")
    print(f"paquete_job     : table={report['paquete_job_table']} index={report['paquete_job_index']}")
    flags = report["flags"]
    assert isinstance(flags, dict)
    print("requisitos_documento.solo_primera_cuenta: " + (", ".join(f"{k}={v}" for k, v in flags.items()) or "(none)"))


def main(url: str | None = None) -> int:
    try:
        if url is None:
            from app.core.config import settings

            url = settings.DATABASE_URL
        report = asyncio.run(collect(url))
    except Exception as exc:
        # Class name only: driver messages and pydantic's ValidationError embed the DSN /
        # input values, and nothing may escape as a traceback either.
        print(f"check failed: {type(exc).__name__}")
        return 1
    _print(report)
    return 0 if report["at_head"] else 2


if __name__ == "__main__":
    sys.exit(main())
