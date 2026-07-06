"""Deeper diagnostic: check field contents of actual stored documents."""
import asyncio
import sys
import httpx

sys.path.insert(0, r"c:/Users/User/Documents/workspace/cashing/cashing-backend")

from app.core.config import settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

SECOP_BASE = "https://www.datos.gov.co/resource"
DS_DOCS_2025 = "dmgg-8hin"
DS_MODS = "u8cx-r425"


async def query_secop(dataset: str, where: str, limit: int = 3) -> list[dict]:
    url = f"{SECOP_BASE}/{dataset}.json"
    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}
    params = {"$where": where, "$limit": str(limit)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []


async def main():
    engine = create_async_engine(settings.DATABASE_URL)

    # Show ALL contracts in BD
    print("=== TODOS LOS CONTRATOS EN BD ===")
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT numero_contrato, referencia_del_contrato, proceso_de_compra, id_contrato_secop "
            "FROM secop_contratos ORDER BY updated_at DESC"
        ))
        contratos = rows.fetchall()
        for c in contratos:
            print(f"  numero={c[0]!r}  ref={c[1]!r}  proceso={c[2]!r}  id_secop={c[3]!r}")

    # Show actual datos_raw of a stored doc to see all fields
    print("\n=== MUESTRA DE datos_raw DE UN DOCUMENTO ===")
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT id_documento_secop, numero_contrato, proceso, datos_raw "
            "FROM secop_documentos LIMIT 3"
        ))
        for r in rows:
            import json
            raw = r[3]
            if isinstance(raw, str):
                raw = json.loads(raw)
            print(f"\n  id_doc={r[0]!r}  numero_contrato={r[1]!r}  proceso={r[2]!r}")
            # Show all fields that might contain a contract reference
            for k, v in (raw or {}).items():
                if any(word in k.lower() for word in ["contrato", "numero", "numer", "mero", "proceso", "secop", "dataset"]):
                    print(f"    raw[{k!r}] = {str(v)[:80]!r}")

    # Test SECOP fields in the 2025 dataset for the stored processes
    processes_to_check = ["CO1.BDOS.9493653", "CO1.BDOS.9833468"]
    for proc in processes_to_check:
        print(f"\n=== SECOP 2025 para proceso={proc} ===")
        rows = await query_secop(DS_DOCS_2025, f"proceso = '{proc}'", limit=2)
        print(f"  Results: {len(rows)}")
        if rows:
            print(f"  All fields: {list(rows[0].keys())}")
            for row in rows:
                print(f"  n_mero_de_contrato={row.get('n_mero_de_contrato')!r}")
                print(f"  numero_de_contrato={row.get('numero_de_contrato')!r}")
                print(f"  proceso={row.get('proceso')!r}")
                print(f"  url_descarga_documento type={type(row.get('url_descarga_documento'))}")
                print(f"  ---")

    # Check modificaciones for the actual contratos
    print("\n=== MODIFICACIONES para contratos 2024 ===")
    for c in contratos:
        id_secop = c[3]
        if not id_secop:
            continue
        rows = await query_secop(DS_MODS, f"id_contrato = '{id_secop}'", limit=3)
        if rows:
            print(f"  contrato={c[0]!r}  id_secop={id_secop!r}  modificaciones={len(rows)}")
            for row in rows:
                av = row.get("archivo_version_anterior", "")
                print(f"    archivo_version_anterior={str(av)[:100]!r}")
                # Check if it's a dict with url
                if isinstance(av, dict):
                    print(f"    (dict) keys={list(av.keys())}  url={av.get('url','')!r}")


asyncio.run(main())
