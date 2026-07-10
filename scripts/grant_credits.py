"""Pilot credit grant CLI — manually credit a user's account.

Run from the cashing-backend/ directory with the venv activated:

    python scripts/grant_credits.py --email pilot@example.com --cantidad 50
    python scripts/grant_credits.py --usuario-id <uuid> --cantidad 50
    python scripts/grant_credits.py --email pilot@example.com --cantidad 50 --yes

Resolves the user by email OR usuario-id (exactly one must be given), prints
the user's email/id, current balance, and the requested cantidad, then asks
for interactive confirmation before mutating anything (skip with `--yes` for
non-interactive/scripted use). Calls the EXISTING
`credito_service.agregar_creditos(...)` — the same ledger write path used
everywhere else in the app (no ad-hoc DB writes here) — with `tipo=BONUS` and
`referencia="pilot_manual_grant"`, and prints the resulting balance.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

sys.path.insert(0, ".")  # run from cashing-backend/

from app.core.config import settings
from app.models.credito import TipoCredito
from app.models.usuario import Usuario
from app.services import credito_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_REFERENCIA = "pilot_manual_grant"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grant pilot credits to a user's account.")
    identifier = parser.add_mutually_exclusive_group(required=True)
    identifier.add_argument("--email", help="User's email address.")
    identifier.add_argument("--usuario-id", help="User's UUID.")
    parser.add_argument(
        "--cantidad",
        type=int,
        required=True,
        help="Number of credits to grant (must be a positive integer).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    return parser.parse_args()


async def _resolver_usuario(db: AsyncSession, *, email: str | None, usuario_id: str | None) -> Usuario:
    if email:
        result = await db.execute(select(Usuario).where(Usuario.email == email))
    else:
        try:
            uid = uuid.UUID(usuario_id) if usuario_id else None
        except ValueError:
            print(f"ERROR: '{usuario_id}' is not a valid UUID.")
            sys.exit(1)
        result = await db.execute(select(Usuario).where(Usuario.id == uid))

    usuario = result.scalar_one_or_none()
    if usuario is None:
        identifier = email or usuario_id
        print(f"ERROR: no user found for '{identifier}'.")
        sys.exit(1)
    return usuario


async def grant_credits(*, email: str | None, usuario_id: str | None, cantidad: int, yes: bool = False) -> None:
    if cantidad <= 0:
        print(f"ERROR: --cantidad must be a positive integer, got {cantidad}.")
        sys.exit(1)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        usuario = await _resolver_usuario(db, email=email, usuario_id=usuario_id)
        saldo_actual = await credito_service.obtener_saldo(db, usuario.id)

        print(f"Usuario: {usuario.email} ({usuario.id})")
        print(f"Saldo actual: {saldo_actual} créditos")
        print(f"Créditos a otorgar: {cantidad}")

        if not yes:
            confirmacion = input("¿Confirmar operación? [s/N]: ").strip().lower()
            if confirmacion not in ("s", "y", "si", "sí", "yes"):
                print("Operación cancelada.")
                await engine.dispose()
                return

        print(f"Otorgando {cantidad} créditos a {usuario.email} ({usuario.id})...")
        await credito_service.agregar_creditos(
            db,
            usuario.id,
            cantidad,
            tipo=TipoCredito.BONUS,
            referencia=_REFERENCIA,
        )

        saldo = await credito_service.obtener_saldo(db, usuario.id)
        print(f"OK. Saldo actual de {usuario.email}: {saldo} créditos.")

    await engine.dispose()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(grant_credits(email=args.email, usuario_id=args.usuario_id, cantidad=args.cantidad, yes=args.yes))
