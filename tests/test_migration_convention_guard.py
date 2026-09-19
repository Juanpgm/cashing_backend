"""Convention guard: new migrations must be idempotent against `create_all`.

`app.main.lifespan` runs `Base.metadata.create_all` BEFORE `alembic upgrade
head`, so a migration that calls `op.create_table(` / `op.create_index(` /
`op.add_column(` directly collides with what create_all already built
(`DuplicateTable`), aborts the upgrade, leaves `alembic_version` behind and every
later migration silently unapplied — production hit this with 042/043.

Every migration numbered AFTER 043 must use the guarded helpers in
`app.core.migration_helpers` (`create_table_if_missing`,
`create_index_if_missing`, `add_column_if_missing`) instead. Historical
migrations (<= 043) are grandfathered: they already ran and are not edited.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_LAST_GRANDFATHERED = 43
_FORBIDDEN = {
    "create_table": "create_table_if_missing",
    "create_index": "create_index_if_missing",
    "add_column": "add_column_if_missing",
}
_PREFIX = re.compile(r"^(\d+)_")
_HEADER = "from alembic import op\n\n\ndef upgrade() -> None:\n"


def find_violations(versions_dir: Path, last_grandfathered: int = _LAST_GRANDFATHERED) -> list[str]:
    """Return `file:line: op.X() -> use helper` for each offending call in migrations above the cutoff."""
    violations: list[str] = []
    for path in sorted(versions_dir.glob("*.py")):
        match = _PREFIX.match(path.name)
        if match is None or int(match.group(1)) <= last_grandfathered:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FORBIDDEN
                and isinstance(func.value, ast.Name)
                and func.value.id == "op"
            ):
                violations.append(f"{path.name}:{node.lineno}: op.{func.attr}() -> use {_FORBIDDEN[func.attr]}()")
    return violations


def test_real_migrations_use_the_idempotent_helpers() -> None:
    assert find_violations(_VERSIONS_DIR) == [], (
        "Migrations after 043 must not call op.create_table/create_index/add_column directly: "
        "create_all runs before alembic at boot, so they collide with existing objects. "
        "Use the helpers in app.core.migration_helpers."
    )


def test_scanner_catches_real_historical_offenders_when_the_cutoff_is_lowered() -> None:
    # Proves the scanner actually sees real migration files: historical
    # migrations DO call the raw ops (e.g. 007 create_table, 039 add_column) and
    # are only exempt because of the 043 cutoff. 042 itself no longer offends
    # since it was made idempotent.
    violations = find_violations(_VERSIONS_DIR, last_grandfathered=0)

    assert any(v.startswith("007_agent_runs_borradores.py:") and "op.create_table()" in v for v in violations)
    assert any(v.startswith("039_documento_fuente_sha256.py:") and "op.add_column()" in v for v in violations)
    assert not any(v.startswith("042_paquete_job.py:") for v in violations)


class TestScannerOnSyntheticMigrations:
    @staticmethod
    def _write(tmp_path: Path, name: str, body: str) -> None:
        (tmp_path / name).write_text(body, encoding="utf-8")

    @pytest.mark.parametrize(
        ("call", "helper"),
        [
            ('op.create_table("t", sa.Column("id", sa.Integer()))', "create_table_if_missing"),
            ('op.create_index("ix_t_id", "t", ["id"])', "create_index_if_missing"),
            ('op.add_column("t", sa.Column("c", sa.Integer()))', "add_column_if_missing"),
        ],
    )
    def test_flags_each_forbidden_call_in_a_migration_after_043(self, tmp_path: Path, call: str, helper: str) -> None:
        self._write(tmp_path, "044_bad.py", "import sqlalchemy as sa\n" + _HEADER + f"    {call}\n")

        violations = find_violations(tmp_path)

        assert len(violations) == 1
        assert violations[0].startswith("044_bad.py:6:")
        assert helper in violations[0]

    def test_flags_every_offender_in_the_same_file_and_across_files(self, tmp_path: Path) -> None:
        self._write(tmp_path, "044_a.py", _HEADER + '    op.create_table("a")\n    op.add_column("a", None)\n')
        self._write(tmp_path, "150_b.py", _HEADER + '    op.create_index("i", "b", ["x"])\n')

        assert len(find_violations(tmp_path)) == 3

    def test_call_nested_in_a_helper_function_or_branch_is_still_caught(self, tmp_path: Path) -> None:
        body = (
            "from alembic import op\n\n\n"
            "def _make() -> None:\n"
            "    if True:\n"
            "        op.create_table('x')\n\n\n"
            "def upgrade() -> None:\n"
            "    _make()\n"
        )
        self._write(tmp_path, "045_nested.py", body)

        assert len(find_violations(tmp_path)) == 1

    def test_migration_using_the_helpers_passes(self, tmp_path: Path) -> None:
        body = (
            "import sqlalchemy as sa\n"
            "from app.core.migration_helpers import (\n"
            "    add_column_if_missing,\n"
            "    create_index_if_missing,\n"
            "    create_table_if_missing,\n"
            ")\n\n\n"
            "def upgrade() -> None:\n"
            '    create_table_if_missing("t", sa.Column("id", sa.Integer()))\n'
            '    create_index_if_missing("ix_t_id", "t", ["id"])\n'
            '    add_column_if_missing("t", sa.Column("c", sa.Integer()))\n'
        )
        self._write(tmp_path, "044_good.py", body)

        assert find_violations(tmp_path) == []

    def test_mentions_in_docstrings_and_comments_are_not_calls(self, tmp_path: Path) -> None:
        body = (
            '"""Replaces a bare op.create_table(...) with the helper."""\n'
            "from alembic import op\n\n\n"
            "def upgrade() -> None:\n"
            "    # op.add_column('t', c) would collide with create_all\n"
            '    op.execute("SELECT 1")\n'
            '    op.drop_table("t")\n'
        )
        self._write(tmp_path, "044_doc.py", body)

        assert find_violations(tmp_path) == []

    def test_grandfathered_migrations_up_to_043_are_ignored(self, tmp_path: Path) -> None:
        for name in ("001_base.py", "042_paquete_job.py", "043_flags.py"):
            self._write(tmp_path, name, _HEADER + '    op.create_table("t")\n')

        assert find_violations(tmp_path) == []

    def test_boundary_044_is_the_first_guarded_number(self, tmp_path: Path) -> None:
        self._write(tmp_path, "043_x.py", _HEADER + '    op.create_table("t")\n')
        self._write(tmp_path, "044_x.py", _HEADER + '    op.create_table("t")\n')

        violations = find_violations(tmp_path)

        assert [v.split(":")[0] for v in violations] == ["044_x.py"]

    def test_non_numeric_and_non_python_files_are_ignored(self, tmp_path: Path) -> None:
        self._write(tmp_path, "__init__.py", 'from alembic import op\nop.create_table("t")\n')
        self._write(tmp_path, "abc_notes.py", 'from alembic import op\nop.create_table("t")\n')
        self._write(tmp_path, "044_notes.txt", 'op.create_table("t")\n')

        assert find_violations(tmp_path) == []

    def test_empty_directory_has_no_violations(self, tmp_path: Path) -> None:
        assert find_violations(tmp_path) == []
