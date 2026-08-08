"""Regression test for the `024_integraciones_table` migration's backfill.

Exercises `upgrade()` via Alembic's `Operations`/`MigrationContext` API against a
throwaway SQLite DB (the migration file's numeric-prefixed name means it cannot
be `import`ed normally, so it's loaded by path). Previously this backfill was
only verified with a manual throwaway script — see
openspec/changes/microsoft-365-integration/apply-progress.md.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "024_integraciones_table.py"


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_024_integraciones_table", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_google_tokens_table(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            "CREATE TABLE google_tokens ("
            "usuario_id CHAR(32) NOT NULL, "
            "access_token_encrypted TEXT NOT NULL, "
            "refresh_token_encrypted TEXT NOT NULL, "
            "scopes VARCHAR(500) NOT NULL, "
            "expires_at DATETIME, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")"
        )
    )


class TestMigration024Backfill:
    def test_upgrade_backfills_google_tokens_rows_as_google_provider(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")
        user_id = uuid.uuid4()

        with engine.begin() as conn:
            _create_google_tokens_table(conn)
            conn.execute(
                sa.text(
                    "INSERT INTO google_tokens "
                    "(usuario_id, access_token_encrypted, refresh_token_encrypted, scopes, "
                    "expires_at, created_at, updated_at) "
                    "VALUES (:uid, :access, :refresh, :scopes, NULL, :created, :updated)"
                ),
                {
                    "uid": user_id.hex,
                    "access": "enc-access-token",
                    "refresh": "enc-refresh-token",
                    "scopes": "https://www.googleapis.com/auth/gmail.readonly",
                    "created": "2026-01-01 00:00:00",
                    "updated": "2026-01-01 00:00:00",
                },
            )

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]

            rows = conn.execute(
                sa.text("SELECT usuario_id, provider, email, scopes, access_token_encrypted FROM integraciones")
            ).fetchall()

        assert len(rows) == 1
        row = rows[0]
        # SQLite (no native UUID type) round-trips sa.Uuid() as a plain hex string here
        # since these ad-hoc sa.table()/sa.column() helpers carry no type info for the
        # raw SELECT — on real Postgres this column is a native uuid.
        assert uuid.UUID(row.usuario_id) == user_id
        assert row.provider == "google"
        assert row.email == ""
        assert row.scopes == "https://www.googleapis.com/auth/gmail.readonly"
        assert row.access_token_encrypted == "enc-access-token"

    def test_upgrade_with_no_existing_rows_backfills_nothing(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")

        with engine.begin() as conn:
            _create_google_tokens_table(conn)

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]

            count = conn.execute(sa.text("SELECT COUNT(*) FROM integraciones")).scalar_one()

        assert count == 0
