"""Alembic async environment configuration."""

import asyncio
from logging.config import fileConfig

from alembic import context
from app.core.config import settings
from app.core.database import Base

# Import ALL models so Alembic can detect them for autogenerate
from app.models import (  # noqa: F401
    actividad,
    audit_log,
    contrato,
    conversacion,
    credito,
    cuenta_cobro,
    documento_fuente,
    evidencia,
    google_token,
    obligacion,
    pago,
    plantilla,
    secop,
    suscripcion,
    token_blacklist,
    usuario,
)
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url with the actual DATABASE_URL (keep asyncpg — psycopg2 is not installed)
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using async engine."""
    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.DATABASE_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry-point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
