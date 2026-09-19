"""Scenario tests: migration 042 must survive `create_all` having run first.

Boot order in `app.main.lifespan` is `Base.metadata.create_all` FIRST, then
`alembic upgrade head`. `042_paquete_job` used to do a bare
`op.create_table("paquete_job", ...)`, which collides with the table create_all
already built (`DuplicateTable`): the upgrade aborted, `alembic_version` stayed
at 041 and migration 043 (a data UPDATE) never ran, while the deploy still
looked green because an alembic failure is only a logged warning.

These tests drive Alembic IN-PROCESS (real `env.py`, real revision files)
against a throwaway SQLite FILE. `env.py` reads `settings.DATABASE_URL`, so it
is monkeypatched to the temp DB — the dev DB is never touched. The chain is
stamped at 041 and only 042 -> 043 is executed: earlier migrations carry
Postgres-only constructs and are not meant to run on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from app.core.config import settings
from app.core.database import Base
from app.models.requisito_documento import RequisitoDocumento
from sqlalchemy.orm import Session

_ROOT = Path(__file__).resolve().parent.parent
_REV_041 = "041_cdp_enum_uppercase"
_REV_042 = "042_paquete_job"
_REV_043 = "043_checklist_primera_cuota_flags"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Config, sa.Engine]]:
    """A temp SQLite DB built by `create_all` (as the lifespan does) + an Alembic config aimed at it."""
    db_file = tmp_path / "mig.db"
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")

    import app.models  # noqa: F401  (register every table on Base.metadata)

    engine = sa.create_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(engine)

    # Deliberately no config file: env.py's `fileConfig` would disable the
    # loggers of the rest of the test session.
    cfg = Config()
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    yield cfg, engine
    engine.dispose()


def _seed_pre_043_flags(engine: sa.Engine) -> None:
    with Session(engine) as session:
        for codigo, solo_primera in (
            ("RPC", False),
            ("CDP", False),
            ("CONTRATO", False),
            ("CEDULA", True),
            ("RUT", True),
            ("SEGURIDAD_SOCIAL", False),
        ):
            session.add(RequisitoDocumento(codigo=codigo, etiqueta=codigo, solo_primera_cuenta=solo_primera))
        session.commit()


def _flags(engine: sa.Engine) -> dict[str, bool]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT codigo, solo_primera_cuenta FROM requisitos_documento")).fetchall()
    return {codigo: bool(flag) for codigo, flag in rows}


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one())


def _index_names(engine: sa.Engine, table: str) -> set[str]:
    return {str(ix["name"]) for ix in sa.inspect(engine).get_indexes(table)}


def _assert_flags_after_043(engine: sa.Engine) -> None:
    flags = _flags(engine)
    assert flags["RPC"] is True
    assert flags["CDP"] is True
    assert flags["CONTRATO"] is True
    assert flags["CEDULA"] is True  # untouched, already True
    assert flags["RUT"] is True  # untouched, already True
    assert flags["SEGURIDAD_SOCIAL"] is False  # out of scope


class TestScenarioACreateAllThenUpgrade:
    """create_all built paquete_job, alembic_version is at 041 (the production situation)."""

    def test_upgrade_head_succeeds_and_reaches_043_with_flags_flipped(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        command.stamp(cfg, _REV_041)
        assert "paquete_job" in sa.inspect(engine).get_table_names()  # precondition: create_all built it

        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        _assert_flags_after_043(engine)

    def test_existing_paquete_job_rows_survive(self, db: tuple[Config, sa.Engine]) -> None:
        # The migration must be a no-op on the table, never drop/recreate it.
        cfg, engine = db
        _seed_pre_043_flags(engine)
        cuenta_id = uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO paquete_job (id, cuenta_cobro_id, status, created_at, updated_at) "
                    "VALUES (:id, :cuenta, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": uuid.uuid4().hex, "cuenta": cuenta_id},
            )
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT cuenta_cobro_id, status FROM paquete_job")).fetchall()
        assert [tuple(r) for r in rows] == [(cuenta_id, "done")]

    def test_index_and_unique_constraint_are_still_there(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")

        assert "ix_paquete_job_cuenta_cobro_id" in _index_names(engine, "paquete_job")
        uniques = {u["name"] for u in sa.inspect(engine).get_unique_constraints("paquete_job")}
        assert "uq_paquete_job_cuenta" in uniques


class TestScenarioBFreshPath:
    """paquete_job does not exist yet (no create_all for it): 042 must create it."""

    def test_042_creates_the_table_index_and_unique_constraint(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE paquete_job"))
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        _assert_flags_after_043(engine)
        assert "ix_paquete_job_cuenta_cobro_id" in _index_names(engine, "paquete_job")
        uniques = {u["name"] for u in sa.inspect(engine).get_unique_constraints("paquete_job")}
        assert "uq_paquete_job_cuenta" in uniques

    def test_created_table_enforces_one_row_per_cuenta(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE paquete_job"))
        command.stamp(cfg, _REV_041)
        command.upgrade(cfg, "head")

        insert = sa.text(
            "INSERT INTO paquete_job (id, cuenta_cobro_id, status, created_at, updated_at) "
            "VALUES (:id, :cuenta, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        cuenta_id = uuid.uuid4().hex
        with engine.begin() as conn:
            conn.execute(insert, {"id": uuid.uuid4().hex, "cuenta": cuenta_id})
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"id": uuid.uuid4().hex, "cuenta": cuenta_id})

    def test_downgrade_still_drops_index_and_table(self, db: tuple[Config, sa.Engine]) -> None:
        # Downgrade behavior is unchanged by the idempotency fix.
        cfg, engine = db
        _seed_pre_043_flags(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE paquete_job"))
        command.stamp(cfg, _REV_041)
        command.upgrade(cfg, "head")

        command.downgrade(cfg, _REV_041)

        assert _version(engine) == _REV_041
        assert "paquete_job" not in sa.inspect(engine).get_table_names()
        flags = _flags(engine)
        assert flags["RPC"] is False and flags["CDP"] is False and flags["CONTRATO"] is False


class TestScenarioCIdempotentRuns:
    def test_upgrade_head_twice_is_a_noop_the_second_time(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        _assert_flags_after_043(engine)

    def test_replaying_042_and_043_after_they_already_ran_succeeds(self, db: tuple[Config, sa.Engine]) -> None:
        # e.g. an operator re-stamps back to 041 and re-upgrades: 042 finds the
        # table AND the index already present and must do nothing.
        cfg, engine = db
        _seed_pre_043_flags(engine)
        command.stamp(cfg, _REV_041)
        command.upgrade(cfg, "head")

        command.stamp(cfg, _REV_041, purge=True)
        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        _assert_flags_after_043(engine)

    def test_upgrade_from_042_to_head_only_runs_043(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        command.stamp(cfg, _REV_042)

        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        _assert_flags_after_043(engine)


class TestScenarioDPartialObjects:
    def test_table_exists_but_index_is_missing_recreates_only_the_index(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP INDEX ix_paquete_job_cuenta_cobro_id"))
        assert "ix_paquete_job_cuenta_cobro_id" not in _index_names(engine, "paquete_job")
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        assert "ix_paquete_job_cuenta_cobro_id" in _index_names(engine, "paquete_job")
        _assert_flags_after_043(engine)

    def test_table_and_index_both_exist_is_a_noop(self, db: tuple[Config, sa.Engine]) -> None:
        cfg, engine = db
        _seed_pre_043_flags(engine)
        assert "ix_paquete_job_cuenta_cobro_id" in _index_names(engine, "paquete_job")
        command.stamp(cfg, _REV_041)

        command.upgrade(cfg, "head")

        assert _version(engine) == _REV_043
        assert "ix_paquete_job_cuenta_cobro_id" in _index_names(engine, "paquete_job")
