# Diagnose Railway Deploy

Run the full Railway deployment diagnostic and fix any blocking issues found.

## Steps

1. Run the diagnostic script:

```bash
uv run python scripts/diagnose_railway.py
```

2. For every **blocking issue** (✗) found, apply the fix described below and re-run until the script passes with no blocking issues.

3. After all checks pass, commit and push:

```bash
git add -A && git commit -m "fix(deploy): resolve Railway deployment blockers" && git push origin master
```

---

## Fix Reference

### SQLAlchemy mapper crash (`configure_mappers()` fails)
A `back_populates` in a model points to a column or non-existent attribute instead of a relationship.

**Locate:** find the model file mentioned in the error. Look for:
```python
relationship(back_populates="some_name")
```
Verify that `some_name` is a `relationship(...)` attribute on the target class, **not** a `mapped_column(...)`.

**Fix:** change `back_populates` to the correct relationship attribute name.

---

### DATABASE_URL scheme wrong (`postgres://` or `postgresql://`)
Railway injects `postgres://...` but SQLAlchemy async requires `postgresql+asyncpg://`.

**Fix:** add this validator to `app/core/config.py` inside the `Settings` class:
```python
@field_validator("DATABASE_URL", mode="before")
@classmethod
def normalize_database_url(cls, v: str) -> str:
    if v.startswith("postgres://"):
        return v.replace("postgres://", "postgresql+asyncpg://", 1)
    if v.startswith("postgresql://"):
        return v.replace("postgresql://", "postgresql+asyncpg://", 1)
    return v
```

---

### Missing migration (column in model but not in DB)
A column was added to a SQLAlchemy model after the initial deploy but no Alembic migration was created. `create_all` never adds columns to existing tables.

**Fix:** generate the migration:
```bash
make migration msg="add <column_name> to <table_name>"
```
Then verify the generated file in `alembic/versions/` is correct.

---

### CMD blocks uvicorn (`alembic upgrade head && uvicorn`)
If alembic fails (SSL error, wrong driver, DB unreachable), the `&&` prevents uvicorn from starting. Railway sees a crashed container and reverts to the previous deploy.

**Fix:** run alembic inside the FastAPI lifespan instead:
```python
# app/main.py — inside lifespan()
try:
    proc = await asyncio.create_subprocess_exec(
        "alembic", "upgrade", "head",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        structlog.get_logger("startup").warning(
            "alembic_upgrade_failed", stderr=stderr.decode().strip()
        )
except Exception as exc:
    structlog.get_logger("startup").warning("alembic_upgrade_error", error=str(exc))
```

---

### alembic/env.py uses wrong driver or missing SSL
Railway PostgreSQL requires SSL. If `env.py` uses `psycopg2` (not installed) or no SSL config, alembic fails silently.

**Fix:** make sure `alembic/env.py` `run_async_migrations` passes SSL and uses `settings.DATABASE_URL` directly:
```python
async def run_async_migrations() -> None:
    import ssl
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.DATABASE_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"ssl": ssl_ctx},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()
```

---

### PORT env var missing from Dockerfile CMD
Railway injects the `PORT` variable at runtime. The CMD must use it:
```dockerfile
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
```

---

## When to run this command

- Before every push to master that touches models, migrations, Dockerfile, or requirements.txt
- When Railway shows a deployment that "applied" but the old behavior persists
- After adding a new SQLAlchemy model or relationship
- After adding a new column to an existing model
