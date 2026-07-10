"""Pilot invite-code CLI — mint invite codes for the waitlist gate.

Run from the cashing-backend/ directory with the venv activated:

    python scripts/create_invite_codes.py --cantidad 5
    python scripts/create_invite_codes.py --cantidad 1 --max-usos 10 --nota "campaña piloto julio"
    python scripts/create_invite_codes.py --codigo AMIGOS-CASHIN --max-usos 20 --yes

With ``WAITLIST_ENABLED=true`` every signup (email or first Google sign-in)
requires one of these codes; each signup consumes one use. Codes are written
directly as ``InviteCode`` rows (there is no creation service — consumption
lives in ``auth_service._consume_invite_code``).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

sys.path.insert(0, ".")  # run from cashing-backend/

from app.core.config import settings
from app.models.invite_code import InviteCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L ambiguity


def _generar_codigo() -> str:
    cuerpo = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"CASHIN-{cuerpo[:4]}-{cuerpo[4:]}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create invite codes for the waitlist gate.")
    parser.add_argument(
        "--cantidad",
        type=int,
        default=1,
        help="Number of random codes to create (ignored when --codigo is given).",
    )
    parser.add_argument("--codigo", help="Explicit code instead of random generation.")
    parser.add_argument(
        "--max-usos",
        type=int,
        default=1,
        help="Uses allowed per code (default 1 = single-use).",
    )
    parser.add_argument("--nota", help="Optional label, e.g. campaign or invitee name.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    return parser.parse_args()


async def create_invite_codes(
    *,
    cantidad: int,
    codigo: str | None,
    max_usos: int,
    nota: str | None,
    yes: bool = False,
) -> None:
    if max_usos <= 0:
        print(f"ERROR: --max-usos must be a positive integer, got {max_usos}.")
        sys.exit(1)
    if codigo is None and cantidad <= 0:
        print(f"ERROR: --cantidad must be a positive integer, got {cantidad}.")
        sys.exit(1)

    codigos = [codigo] if codigo else [_generar_codigo() for _ in range(cantidad)]

    print(f"Códigos a crear ({len(codigos)}, max_usos={max_usos}, nota={nota or '-'}):")
    for c in codigos:
        print(f"  {c}")

    if not yes:
        confirmacion = input("¿Confirmar operación? [s/N]: ").strip().lower()
        if confirmacion not in ("s", "y", "si", "sí", "yes"):
            print("Operación cancelada.")
            return

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        for c in codigos:
            existente = await db.execute(select(InviteCode).where(InviteCode.codigo == c))
            if existente.scalar_one_or_none() is not None:
                print(f"OMITIDO (ya existe): {c}")
                continue
            db.add(InviteCode(codigo=c, max_usos=max_usos, nota=nota))
            print(f"CREADO: {c}")
        await db.commit()

    await engine.dispose()
    print("OK.")


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        create_invite_codes(
            cantidad=args.cantidad,
            codigo=args.codigo,
            max_usos=args.max_usos,
            nota=args.nota,
            yes=args.yes,
        )
    )
