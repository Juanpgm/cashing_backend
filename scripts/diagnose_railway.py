#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Railway Deploy Diagnostic Script
=================================
Detects every class of error that prevents code changes from applying
to a Railway deployment. Run with: uv run python scripts/diagnose_railway.py

Exit codes:
  0 - all checks passed
  1 - one or more blocking issues found
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

PASS = "[OK]"
FAIL = "[!!]"
WARN = "[??]"

issues: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"  {PASS}  {msg}")


def fail(msg: str) -> None:
    print(f"  {FAIL}  {msg}")
    issues.append(msg)


def warn(msg: str) -> None:
    print(f"  {WARN}  {msg}")
    warnings.append(msg)


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ------------------------------------------------------------------
# 1. PYTHON IMPORT CHAIN
# ------------------------------------------------------------------
section("1 - Python import chain")

try:
    import app.models  # noqa: F401
    ok("app.models imports without error")
except Exception as e:
    fail(f"app.models import failed: {type(e).__name__}: {e}")

try:
    from app.api.router import api_v1_router  # noqa: F401
    ok("app.api.router imports without error")
except Exception as e:
    fail(f"app.api.router import failed: {type(e).__name__}: {e}")

try:
    from app.main import app  # noqa: F401
    ok("app.main imports without error")
except Exception as e:
    fail(f"app.main import failed: {type(e).__name__}: {e}")

# ------------------------------------------------------------------
# 2. SQLALCHEMY MAPPER VALIDATION
# ------------------------------------------------------------------
section("2 - SQLAlchemy mapper configuration")

try:
    from sqlalchemy.orm import configure_mappers
    configure_mappers()
    ok("configure_mappers() succeeded -- all back_populates are valid")
except Exception as e:
    fail(f"configure_mappers() failed: {type(e).__name__}: {e}")
    print(textwrap.indent(
        "  This crash prevents create_all from working -- the app crashes\n"
        "  on every startup and Railway reverts to the previous deploy.",
        "       ",
    ))

# Static scan: back_populates values vs mapped attribute names in same file
section("2b - back_populates static analysis")

models_dir = ROOT / "app" / "models"
bp_pattern = re.compile(r'relationship\([^)]*back_populates\s*=\s*["\'](\w+)["\']')
rel_pattern = re.compile(r'^\s+(\w+)\s*:.*=\s*relationship\(', re.MULTILINE)
col_pattern = re.compile(r'^\s+(\w+)\s*:.*=\s*mapped_column\(', re.MULTILINE)

file_relationships: dict[str, set[str]] = {}
file_columns: dict[str, set[str]] = {}

for model_file in sorted(models_dir.glob("*.py")):
    if model_file.name.startswith("_"):
        continue
    source = model_file.read_text(encoding="utf-8")
    file_relationships[model_file.stem] = set(rel_pattern.findall(source))
    file_columns[model_file.stem] = set(col_pattern.findall(source))

# Cross-file back_populates check
for model_file in sorted(models_dir.glob("*.py")):
    if model_file.name.startswith("_"):
        continue
    source = model_file.read_text(encoding="utf-8")
    bp_names = bp_pattern.findall(source)
    if not bp_names:
        ok(f"{model_file.name}: no relationships")
        continue
    ok(f"{model_file.name}: back_populates names found = {bp_names}")

# ------------------------------------------------------------------
# 3. DATABASE_URL FORMAT
# ------------------------------------------------------------------
section("3 - DATABASE_URL format")

db_url = os.environ.get("DATABASE_URL", "")
if not db_url:
    try:
        from app.core.config import settings
        db_url = settings.DATABASE_URL
    except Exception as e:
        fail(f"Cannot load settings: {e}")
        db_url = ""

if db_url:
    if db_url.startswith("postgresql+asyncpg://"):
        ok("DATABASE_URL scheme is correct (postgresql+asyncpg://)")
    elif db_url.startswith("postgres://") or db_url.startswith("postgresql://"):
        fail(
            f"DATABASE_URL scheme '{db_url.split('://')[0]}://' is wrong. "
            "SQLAlchemy async requires 'postgresql+asyncpg://'. "
            "Railway provides postgres:// -- add normalize_database_url validator in config.py."
        )
    elif db_url.startswith("sqlite"):
        warn("DATABASE_URL is SQLite (test/dev only, not Railway)")
    else:
        warn(f"Unrecognized DATABASE_URL scheme: {db_url[:40]}...")
else:
    warn("DATABASE_URL not set -- using default (OK for local dev)")

# ------------------------------------------------------------------
# 4. ALEMBIC CHAIN + MISSING MIGRATIONS
# ------------------------------------------------------------------
section("4 - Alembic migration chain")

try:
    result = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode == 0:
        heads = result.stdout.strip()
        if "(head)" in heads:
            ok(f"Migration chain valid. Head: {heads.split()[0]}")
        else:
            warn(f"alembic heads output unexpected: {heads!r}")
    else:
        fail(f"alembic heads failed: {result.stderr.strip()}")
except FileNotFoundError:
    warn("uv not found -- skipping alembic chain check")

section("4b - Missing migrations (column drift detection)")

alembic_dir = ROOT / "alembic" / "versions"
migration_sources = "".join(
    f.read_text(encoding="utf-8")
    for f in alembic_dir.glob("*.py")
)

COLUMNS_TO_VERIFY = [
    ("usuario.py", "failed_login_attempts"),
    ("usuario.py", "cedula"),
    ("usuario.py", "creditos_disponibles"),
    ("contrato.py", "documento_proveedor"),
    ("documento_fuente.py", "contrato_id"),
]

for model_fname, col_name in COLUMNS_TO_VERIFY:
    model_path = models_dir / model_fname
    if not model_path.exists():
        continue
    model_src = model_path.read_text(encoding="utf-8")
    if col_name not in model_src:
        continue
    if col_name in migration_sources:
        ok(f"{model_fname}:{col_name} -- found in migrations")
    else:
        warn(
            f"{model_fname}:{col_name} -- NOT in any migration file. "
            "If added after first Railway deploy, a migration is required."
        )

# ------------------------------------------------------------------
# 5. DOCKERFILE VALIDATION
# ------------------------------------------------------------------
section("5 - Dockerfile")

dockerfile = ROOT / "Dockerfile"
if not dockerfile.exists():
    fail("Dockerfile not found")
else:
    content = dockerfile.read_text()

    if "alembic upgrade head &&" in content:
        fail(
            "CMD uses 'alembic upgrade head && uvicorn'. "
            "If alembic fails, uvicorn never starts and Railway reverts to old deploy. "
            "Run migrations inside the app lifespan instead."
        )
    else:
        ok("CMD does not block uvicorn on alembic failure")

    if "${PORT" in content or "$PORT" in content:
        ok("Dockerfile handles PORT env var (Railway requirement)")
    else:
        fail("Dockerfile does not use PORT env var -- Railway injects PORT at runtime")

    for dep in ["libpango", "libcairo", "libgdk-pixbuf"]:
        if dep in content:
            ok(f"System dep present: {dep}")
        else:
            warn(f"System dep possibly missing: {dep} (required by WeasyPrint)")

    if "libmagic" in content:
        ok("libmagic1 present (required by python-magic)")
    else:
        warn("libmagic1 not found in Dockerfile (required by python-magic)")

# ------------------------------------------------------------------
# 6. RAILWAY.TOML
# ------------------------------------------------------------------
section("6 - railway.toml")

railway_toml = ROOT / "railway.toml"
if not railway_toml.exists():
    warn("railway.toml not found -- Railway will use defaults")
else:
    content = railway_toml.read_text()
    if "healthcheckPath" in content:
        ok("healthcheckPath configured")
    else:
        warn("healthcheckPath not set -- Railway may not detect startup failures")
    if "DOCKERFILE" in content:
        ok("builder = DOCKERFILE")
    else:
        warn("builder not explicitly set to DOCKERFILE in railway.toml")

# ------------------------------------------------------------------
# 7. HEALTH ENDPOINT
# ------------------------------------------------------------------
section("7 - Health endpoint")

try:
    from app.main import app as fastapi_app
    routes = [getattr(r, "path", "") for r in fastapi_app.routes]
    if "/health" in routes:
        ok("/health endpoint registered")
    else:
        fail("/health endpoint missing -- Railway health check will always fail")
except Exception:
    warn("Could not inspect routes (import error above -- fix it first)")

# ------------------------------------------------------------------
# 8. REQUIREMENTS.TXT SANITY
# ------------------------------------------------------------------
section("8 - requirements.txt")

req_file = ROOT / "requirements.txt"
if not req_file.exists():
    fail("requirements.txt not found")
else:
    req_text = req_file.read_text()

    if "asyncpg" in req_text:
        ok("asyncpg present (async DB driver)")
    else:
        fail("asyncpg missing -- SQLAlchemy async engine will fail")

    if "psycopg2" in req_text and "asyncpg" not in req_text:
        fail("psycopg2 without asyncpg -- async engine will fail")

    if "alembic" in req_text:
        ok("alembic present")
    else:
        fail("alembic missing -- migrations cannot run")

    if "bcrypt" in req_text and "passlib" in req_text:
        ok("bcrypt + passlib both present")

# ------------------------------------------------------------------
# 9. STARTUP MIGRATION STRATEGY
# ------------------------------------------------------------------
section("9 - Startup migration strategy (main.py)")

main_py = (ROOT / "app" / "main.py").read_text()

if "alembic" in main_py and "lifespan" in main_py:
    ok("main.py runs alembic inside lifespan (correct for Railway)")
elif "create_all" in main_py and "alembic" not in main_py:
    fail(
        "main.py uses create_all but NOT alembic. "
        "create_all never adds columns to existing tables -- "
        "new model columns after first deploy will be missing in production."
    )
else:
    warn("Could not determine migration strategy -- verify main.py manually")

if "create_all" in main_py:
    ok("create_all present -- new tables auto-created")

# ------------------------------------------------------------------
# SUMMARY
# ------------------------------------------------------------------
print(f"\n{'=' * 60}")
print("  SUMMARY")
print(f"{'=' * 60}")

if not issues and not warnings:
    print(f"\n  {PASS}  All checks passed -- deploy should apply correctly.\n")
    sys.exit(0)

if warnings:
    print(f"\n  {WARN}  {len(warnings)} warning(s):")
    for w in warnings:
        print(f"       - {w}")

if issues:
    print(f"\n  {FAIL}  {len(issues)} BLOCKING issue(s):")
    for i in issues:
        print(f"       - {i}")
    print("\n  Fix all blocking issues, then push. Run /diagnose-railway to verify.\n")
    sys.exit(1)

print(f"\n  {WARN}  No blocking issues. Review warnings above.\n")
sys.exit(0)
