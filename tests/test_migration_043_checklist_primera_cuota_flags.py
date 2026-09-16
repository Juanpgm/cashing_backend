"""Regression test for the `043_checklist_primera_cuota_flags` migration.

`checklist_service._seed_catalogo_si_vacio` only INSERTS missing catalog codes
(additive backfill) — it never touches an already-seeded row's columns (see its
docstring). Flipping `_CATALOGO_SEED`'s RPC/CDP/CONTRATO flags in code is
therefore not enough for any deployment whose `requisitos_documento` table was
already populated before this change: those rows keep their old
`solo_primera_cuenta` value forever unless a migration updates them directly.

Exercises `upgrade()`/`downgrade()` via Alembic's `Operations`/`MigrationContext`
API against a throwaway SQLite DB, same pattern as
`test_migration_024_backfill.py` (the migration file's numeric-prefixed name
means it cannot be `import`ed normally, so it's loaded by path).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "alembic" / "versions" / "043_checklist_primera_cuota_flags.py"
)


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_043_checklist_primera_cuota_flags", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_requisitos_documento_table(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE requisitos_documento ("
            "codigo VARCHAR(50) PRIMARY KEY, "
            "solo_primera_cuenta BOOLEAN NOT NULL"
            ")"
        )
    )


def _seed_rows(conn: sa.Connection) -> None:
    conn.execute(
        sa.text("INSERT INTO requisitos_documento (codigo, solo_primera_cuenta) VALUES (:c, :v)"),
        [
            {"c": "RPC", "v": False},
            {"c": "CDP", "v": False},
            {"c": "CONTRATO", "v": False},
            {"c": "CEDULA", "v": True},  # already True — untouched either way
            {"c": "SEGURIDAD_SOCIAL", "v": False},  # out of scope — must stay False
        ],
    )


class TestMigration043ChecklistPrimeraCuotaFlags:
    def test_upgrade_flips_rpc_cdp_contrato_to_solo_primera_cuenta(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")

        with engine.begin() as conn:
            _create_requisitos_documento_table(conn)
            _seed_rows(conn)

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]

            rows = dict(
                conn.execute(sa.text("SELECT codigo, solo_primera_cuenta FROM requisitos_documento")).fetchall()
            )

        assert bool(rows["RPC"]) is True
        assert bool(rows["CDP"]) is True
        assert bool(rows["CONTRATO"]) is True
        assert bool(rows["CEDULA"]) is True
        assert bool(rows["SEGURIDAD_SOCIAL"]) is False

    def test_upgrade_with_no_matching_rows_is_a_noop(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")

        with engine.begin() as conn:
            _create_requisitos_documento_table(conn)
            conn.execute(
                sa.text("INSERT INTO requisitos_documento (codigo, solo_primera_cuenta) VALUES (:c, :v)"),
                {"c": "SEGURIDAD_SOCIAL", "v": False},
            )

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]

            count = conn.execute(
                sa.text("SELECT COUNT(*) FROM requisitos_documento WHERE solo_primera_cuenta = 1")
            ).scalar_one()

        assert count == 0

    def test_downgrade_restores_rpc_cdp_contrato_to_false(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")

        with engine.begin() as conn:
            _create_requisitos_documento_table(conn)
            _seed_rows(conn)

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]
                migration.downgrade()  # type: ignore[attr-defined]

            rows = dict(
                conn.execute(sa.text("SELECT codigo, solo_primera_cuenta FROM requisitos_documento")).fetchall()
            )

        assert bool(rows["RPC"]) is False
        assert bool(rows["CDP"]) is False
        assert bool(rows["CONTRATO"]) is False
        # CEDULA was never touched by this migration (already True beforehand)
        # and is NOT in its scope — downgrade must not flip it back to False.
        assert bool(rows["CEDULA"]) is True
