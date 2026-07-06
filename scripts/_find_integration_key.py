"""Find the integration key between SECOP contract API and documents dataset."""
import asyncio, sys, httpx, json
sys.path.insert(0, r"c:/Users/User/Documents/workspace/cashing/cashing-backend")
from app.core.config import settings
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

SECOP_BASE = "https://www.datos.gov.co/resource"

async def q(ds, where, limit=3):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{SECOP_BASE}/{ds}.json",
                        params={"$where": where, "$limit": str(limit)},
                        headers={"X-App-Token": settings.SECOP_APP_TOKEN})
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else []

async def main():
    engine = create_async_engine(settings.DATABASE_URL)

    # 1. Show FULL datos_raw of each contract (all fields from the SECOP contracts API)
    print("=== datos_raw COMPLETO DE CONTRATOS ===")
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT numero_contrato, proceso_de_compra, datos_raw FROM secop_contratos ORDER BY updated_at DESC LIMIT 3"
        ))
        contratos = []
        for r in rows:
            datos = r[2]
            if isinstance(datos, str):
                datos = json.loads(datos)
            contratos.append((r[0], r[1], datos))
            print(f"\n--- contrato={r[0]!r} proceso={r[1]!r} ---")
            print(f"  ALL FIELDS: {sorted(datos.keys())}")
            # Show any field that might be a document/process ref
            for k, v in sorted(datos.items()):
                sv = str(v)[:100]
                print(f"  {k}: {sv!r}")

    # 2. Inspect document dataset fields — get a sample doc for one of our processes
    print("\n\n=== CAMPOS DEL DATASET DE DOCUMENTOS 2025 (dmgg-8hin) ===")
    # Use the 2026 process that we know has docs
    docs = await q("dmgg-8hin", "proceso = 'CO1.BDOS.9493653'", limit=2)
    if docs:
        print(f"All fields: {sorted(docs[0].keys())}")
        for doc in docs:
            print(f"\n  Doc sample:")
            for k, v in sorted(doc.items()):
                print(f"    {k}: {str(v)[:100]!r}")

    # 3. Try to get docs for a specific contract using different field combinations
    print("\n\n=== INTENTANDO DIFERENTES CAMPOS EN DATASET 2022 (kgcd-kt7i) ===")
    # Get first few docs from the 2022 dataset to see what fields exist
    sample_2022 = await q("kgcd-kt7i", "id_documento IS NOT NULL", limit=2)
    if sample_2022:
        print(f"2022 fields: {sorted(sample_2022[0].keys())}")
        for doc in sample_2022:
            for k, v in sorted(doc.items()):
                print(f"  {k}: {str(v)[:100]!r}")
    else:
        print("No results from 2022 dataset with generic query")

    # 4. Check if the SECOP contratos dataset response has a "urlproceso" / link to docs
    if contratos:
        num, proc, datos = contratos[0]
        print(f"\n=== BUSCANDO LLAVE EN datos_raw DE {num!r} ===")
        # Fields that typically point to related resources
        for k in ["urlproceso", "url_proceso", "id_proceso", "proceso_de_compra",
                   "referencia_del_contrato", "id_contrato", "uid_contrato",
                   "numero_de_contrato", "numero_contrato"]:
            v = datos.get(k)
            if v:
                print(f"  {k} = {str(v)[:120]!r}")

    # 5. Try querying docs by NIT/entidad combination to see if that's a path
    if contratos:
        num, proc, datos = contratos[0]
        nit = datos.get("nit_entidad") or datos.get("nit")
        entidad = datos.get("entidad")
        print(f"\n=== QUERY BY nit_entidad ({nit}) en 2025 ===")
        if nit:
            docs_nit = await q("dmgg-8hin", f"nit_entidad = '{nit}'", limit=3)
            print(f"  Results: {len(docs_nit)}")
            for d in docs_nit:
                print(f"  proceso={d.get('proceso')!r}  id_doc={d.get('id_documento')!r}")

asyncio.run(main())
