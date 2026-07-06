"""Diagnose why contrato/modificaciones documents are not being fetched."""
import asyncio
import sys
import httpx

sys.path.insert(0, r"c:/Users/User/Documents/workspace/cashing/cashing-backend")

from app.core.config import settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

SECOP_BASE = "https://www.datos.gov.co/resource"
DS_DOCS_2022 = "kgcd-kt7i"
DS_DOCS_2023 = "3skv-9na7"
DS_DOCS_2025 = "dmgg-8hin"
DS_MODS = "u8cx-r425"
DS_CONTRATOS = "jbjy-vk9h"


async def query_secop(dataset: str, where: str, limit: int = 5) -> list[dict]:
    url = f"{SECOP_BASE}/{dataset}.json"
    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}
    params = {"$where": where, "$limit": str(limit)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []


async def main():
    engine = create_async_engine(settings.DATABASE_URL)

    # 1. Show local contratos
    print("=== CONTRATOS EN BD LOCAL ===")
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT numero_contrato, referencia_del_contrato, proceso_de_compra, id_contrato_secop "
            "FROM secop_contratos ORDER BY updated_at DESC LIMIT 5"
        ))
        contratos = rows.fetchall()
        for c in contratos:
            print(f"  numero_contrato={c[0]!r}  referencia={c[1]!r}  proceso={c[2]!r}  id_secop={c[3]!r}")

    # 2. Show local documentos count per tipo
    print("\n=== DOCUMENTOS EN BD LOCAL ===")
    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT numero_contrato, proceso, COUNT(*) FROM secop_documentos "
            "GROUP BY numero_contrato, proceso ORDER BY COUNT(*) DESC LIMIT 10"
        ))
        for row in r:
            print(f"  numero_contrato={row[0]!r}  proceso={row[1]!r}  count={row[2]}")

    if not contratos:
        print("No hay contratos en la BD. Para probar necesito un número de contrato.")
        return

    # Use the first contract for diagnosis
    numero = contratos[0][0]
    referencia = contratos[0][1]
    proceso = contratos[0][2]
    id_secop = contratos[0][3]

    print(f"\n=== PROBANDO CON contrato={numero!r} proceso={proceso!r} ===")

    # 3. Check what fields exist in each dataset (fetch 1 row by proceso)
    for ds_name, ds_id in [("2022", DS_DOCS_2022), ("2023", DS_DOCS_2023), ("2025", DS_DOCS_2025)]:
        print(f"\n--- Dataset {ds_name} ({ds_id}) ---")
        # Try query by proceso
        if proceso:
            rows_p = await query_secop(ds_id, f"proceso = '{proceso}'", limit=3)
            print(f"  Query proceso={proceso!r}: {len(rows_p)} results")
            if rows_p:
                print(f"  Fields: {list(rows_p[0].keys())[:12]}")
                n_field = rows_p[0].get("n_mero_de_contrato") or rows_p[0].get("numero_de_contrato") or "N/A"
                print(f"  n_mero_de_contrato value: {n_field!r}")

        # Try query by numero_contrato
        rows_n = await query_secop(ds_id, f"n_mero_de_contrato = '{numero}'", limit=3)
        print(f"  Query n_mero_de_contrato={numero!r}: {len(rows_n)} results")
        if referencia and referencia != numero:
            rows_r = await query_secop(ds_id, f"n_mero_de_contrato = '{referencia}'", limit=3)
            print(f"  Query n_mero_de_contrato={referencia!r}: {len(rows_r)} results")

    # 4. Check modificaciones dataset
    print(f"\n--- Modificaciones ({DS_MODS}) ---")
    if id_secop:
        rows_m = await query_secop(DS_MODS, f"id_contrato = '{id_secop}'", limit=5)
        print(f"  Query id_contrato={id_secop!r}: {len(rows_m)} results")
        if rows_m:
            print(f"  Fields: {list(rows_m[0].keys())}")
            for rm in rows_m:
                print(f"  archivo_version_anterior={str(rm.get('archivo_version_anterior',''))[:80]!r}")
    else:
        print("  No id_contrato_secop disponible para buscar en modificaciones")
        # Try by referencia
        if referencia:
            rows_m2 = await query_secop(DS_MODS, f"referencia_del_contrato = '{referencia}'", limit=5)
            print(f"  Query referencia={referencia!r}: {len(rows_m2)} results")
            if rows_m2:
                print(f"  Fields: {list(rows_m2[0].keys())}")


asyncio.run(main())
