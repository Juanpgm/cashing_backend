"""Backfill valor_adicion and recalculate valor_mensual for existing contracts.

Run from the cashing-backend/ directory with the venv activated:

    python scripts/backfill_valor_adicion.py [--dry-run]

What it does:
  - Joins contratos c with secop_contratos sc ON sc.numero_contrato = c.numero_contrato
  - Reads valor_del_contrato and valor_adicion from sc.datos_raw
  - Recalculates valor_mensual using calendar months (same logic as secop_service.py)
  - Updates c.valor_total, c.valor_adicion, c.valor_mensual in-place
  - Reports a summary at the end

Flags:
  --dry-run   Print what would change without writing to the DB.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, ".")  # run from cashing-backend/

from datetime import date

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.contrato import Contrato
from app.models.secop import SecopContrato

log = structlog.get_logger("backfill")


# ---------------------------------------------------------------------------
# Helpers (mirror of secop_service.py)
# ---------------------------------------------------------------------------

def _meses_calendario(fecha_inicio: date, fecha_fin: date) -> int:
    meses = (fecha_fin.year - fecha_inicio.year) * 12 + (fecha_fin.month - fecha_inicio.month)
    if fecha_fin.day >= fecha_inicio.day:
        meses += 1
    return max(1, meses)


def _calcular_valor_mensual(valor_total: Decimal, fecha_inicio: date, fecha_fin: date) -> Decimal:
    meses = _meses_calendario(fecha_inicio, fecha_fin)
    try:
        return (valor_total / Decimal(meses)).quantize(Decimal("0.01"))
    except (InvalidOperation, ZeroDivisionError):
        return valor_total


# ---------------------------------------------------------------------------
# Main backfill logic
# ---------------------------------------------------------------------------

async def backfill(dry_run: bool = False) -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # Load all active contracts that have a documento_proveedor (came from SECOP)
        contratos_result = await db.execute(
            select(Contrato).where(
                Contrato.deleted_at.is_(None),
                Contrato.documento_proveedor.is_not(None),
            )
        )
        contratos = contratos_result.scalars().all()
        print(f"Contratos con documento_proveedor: {len(contratos)}")

        # Load all secop_contratos rows indexed by numero_contrato
        secop_result = await db.execute(
            select(SecopContrato).where(SecopContrato.numero_contrato.is_not(None))
        )
        secop_rows: dict[str, SecopContrato] = {
            sc.numero_contrato: sc
            for sc in secop_result.scalars().all()
            if sc.numero_contrato
        }
        print(f"Registros en secop_contratos: {len(secop_rows)}")

        updated = 0
        skipped_no_match = 0
        skipped_no_raw = 0
        errors = 0

        for contrato in contratos:
            sc = secop_rows.get(contrato.numero_contrato)
            if sc is None:
                skipped_no_match += 1
                continue

            raw: dict = sc.datos_raw or {}
            if not raw:
                skipped_no_raw += 1
                continue

            # Parse values from raw
            try:
                valor_base = Decimal(str(raw.get("valor_del_contrato") or 0)).quantize(Decimal("0.01"))
            except InvalidOperation:
                errors += 1
                continue

            try:
                valor_adicion = Decimal(str(raw.get("valor_adicion") or 0)).quantize(Decimal("0.01"))
                if valor_adicion < 0:
                    valor_adicion = Decimal("0.00")
            except InvalidOperation:
                valor_adicion = Decimal("0.00")

            if valor_base <= 0:
                skipped_no_raw += 1
                continue

            valor_total_nuevo = valor_base + valor_adicion
            valor_mensual_nuevo = _calcular_valor_mensual(
                valor_total_nuevo, contrato.fecha_inicio, contrato.fecha_fin
            )
            valor_adicion_nuevo = float(valor_adicion) if valor_adicion > 0 else None

            old_total = float(contrato.valor_total or 0)
            old_adicion = contrato.valor_adicion
            old_mensual = float(contrato.valor_mensual or 0)

            changed = (
                abs(old_total - float(valor_total_nuevo)) > 0.01
                or old_adicion != valor_adicion_nuevo
                or abs(old_mensual - float(valor_mensual_nuevo)) > 0.01
            )

            print(
                f"  {'[DRY]' if dry_run else '[UPD]' if changed else '[OK ]'} "
                f"{contrato.numero_contrato:<30} "
                f"total: {old_total:>15,.0f} → {float(valor_total_nuevo):>15,.0f}  |  "
                f"adicion: {old_adicion or 0:>12,.0f} → {float(valor_adicion or 0):>12,.0f}  |  "
                f"mensual: {old_mensual:>12,.0f} → {float(valor_mensual_nuevo):>12,.0f}"
            )

            if changed and not dry_run:
                contrato.valor_total = float(valor_total_nuevo)
                contrato.valor_adicion = valor_adicion_nuevo
                contrato.valor_mensual = float(valor_mensual_nuevo)
                updated += 1

        if not dry_run and updated > 0:
            await db.commit()

        print(
            f"\n{'=== DRY RUN — no changes written ===' if dry_run else '=== DONE ==='}\n"
            f"  Actualizados:       {updated}\n"
            f"  Sin match SECOP:    {skipped_no_match}\n"
            f"  Sin datos raw:      {skipped_no_raw}\n"
            f"  Errores de parseo:  {errors}\n"
        )

    await engine.dispose()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(backfill(dry_run=dry_run))
