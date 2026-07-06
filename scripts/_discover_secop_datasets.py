"""
Descubre todos los datasets SECOP en datos.gov.co que contengan documentos.
Busca en el catálogo por palabras clave y prueba los campos de integración.

Uso:
    python scripts/_discover_secop_datasets.py "CO1.BDOS.562792" "CO1.PCCNTR.599010"
"""
from __future__ import annotations
import asyncio
import sys
import httpx

CATALOG = "https://www.datos.gov.co/api/catalog/v1"
SOCRATA_BASE = "https://www.datos.gov.co/resource"

# Datasets ya conocidos
KNOWN = {"jbjy-vk9h", "f8va-cf4m", "kgcd-kt7i", "3skv-9na7", "dmgg-8hin", "u8cx-r425", "p6dx-8zbt"}

# Palabras clave para buscar en el catálogo
SEARCH_TERMS = [
    "SECOP documentos",
    "SECOP II documentos contrato",
    "documentos contrato 2024",
    "SECOP archivo contrato",
]

# Campos de integración a probar en cualquier dataset nuevo
CANDIDATE_FIELDS = [
    "proceso", "n_mero_de_contrato", "id_contrato", "proceso_de_compra",
    "referencia_del_contrato", "numero_contrato", "id_proceso", "codigo_contrato",
]


async def discover() -> None:
    proceso = sys.argv[1] if len(sys.argv) > 1 else "CO1.BDOS.562792"
    id_contrato = sys.argv[2] if len(sys.argv) > 2 else "CO1.PCCNTR.599010"

    found_datasets: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Buscar en el catálogo de datos.gov.co
        print("=" * 60)
        print("BUSCANDO EN EL CATÁLOGO datos.gov.co")
        print("=" * 60)
        for term in SEARCH_TERMS:
            try:
                r = await client.get(
                    f"{CATALOG}/datasets",
                    params={"q": term, "limit": "20", "only": "datasets"},
                )
                data = r.json()
                results = data.get("results", [])
                for ds in results:
                    uid = ds.get("resource", {}).get("id", "")
                    name = ds.get("resource", {}).get("name", "")
                    if uid and uid not in KNOWN and uid not in found_datasets:
                        found_datasets[uid] = {"name": name, "term": term}
                        print(f"  [NUEVO] {uid} — {name[:80]}")
            except Exception as e:
                print(f"  Error buscando '{term}': {e}")

        # 2. Probar datasets conocidos que podrían tener 2024 gap
        EXTRA_CANDIDATE_DATASETS = [
            ("3nvr-bxqa", "Posible SECOP docs 2024 (candidato A)"),
            ("e5kh-k9na", "Posible SECOP docs 2024 (candidato B)"),
            ("rpmr-utcd", "Posible SECOP docs 2024 (candidato C)"),
            ("xvdy-vvsk", "Posible SECOP docs 2024 (candidato D)"),
            ("4zyd-nz5h", "Posible SECOP docs 2024 (candidato E)"),
        ]
        for ds_id, label in EXTRA_CANDIDATE_DATASETS:
            if ds_id not in KNOWN and ds_id not in found_datasets:
                found_datasets[ds_id] = {"name": label, "term": "manual_candidate"}

        if not found_datasets:
            print("  No se encontraron datasets nuevos en el catálogo.")

        # 3. Inspeccionar columnas de cada dataset nuevo y probar integración
        print()
        print("=" * 60)
        print("PROBANDO DATASETS NUEVOS")
        print("=" * 60)
        for ds_id, info in found_datasets.items():
            print(f"\n[{ds_id}] {info['name']}")
            try:
                # Obtener metadata
                meta_r = await client.get(f"https://www.datos.gov.co/api/views/{ds_id}.json")
                if meta_r.status_code != 200:
                    print(f"  → HTTP {meta_r.status_code} — no disponible")
                    continue
                meta = meta_r.json()
                cols = [c["fieldName"] for c in meta.get("columns", [])]
                print(f"  Columnas: {cols}")

                # Probar integración con proceso y id_contrato
                for field in CANDIDATE_FIELDS:
                    if field not in cols:
                        continue
                    for val in [proceso, id_contrato]:
                        safe = val.replace("'", "''")
                        r2 = await client.get(
                            f"{SOCRATA_BASE}/{ds_id}.json",
                            params={"$where": f"{field} = '{safe}'", "$limit": "5"},
                        )
                        data2 = r2.json()
                        if isinstance(data2, list) and data2:
                            print(f"  ✓ {field} = '{val}' → {len(data2)} registros")
                        # pequeña pausa para no saturar la API
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"  Error: {e}")

        # 4. Verificar si hay un dataset 2024 via búsqueda directa en socrata
        print()
        print("=" * 60)
        print("BÚSQUEDA DIRECTA EN SOCRATA (discovery endpoint)")
        print("=" * 60)
        try:
            r = await client.get(
                "https://www.datos.gov.co/api/catalog/v1/domains/www.datos.gov.co/categories",
            )
            print(f"Categorías HTTP {r.status_code}")
        except Exception as e:
            print(f"Error: {e}")

        # 5. Probar variantes del dataset 2024 conociendo el patrón de IDs
        print()
        print("=" * 60)
        print("PRUEBA RÁPIDA: ¿existe un dataset SECOP docs 2024?")
        print("=" * 60)
        # El portal tiene patrones como: buscar por año en el search endpoint
        try:
            r = await client.get(
                f"{CATALOG}/datasets",
                params={"q": "SECOP documentos 2024", "limit": "10"},
            )
            results = r.json().get("results", [])
            for ds in results:
                uid = ds.get("resource", {}).get("id", "")
                name = ds.get("resource", {}).get("name", "")
                print(f"  {uid} — {name[:80]}")
        except Exception as e:
            print(f"Error: {e}")

        # 6. Resumen final: probar todos los valores del contrato en datasets conocidos
        print()
        print("=" * 60)
        print("RESUMEN: LLAVES QUE FUNCIONAN EN DATASETS CONOCIDOS")
        print("=" * 60)
        KNOWN_DOCS = [
            ("f8va-cf4m", "docs_hist"),
            ("kgcd-kt7i", "docs_2022"),
            ("3skv-9na7", "docs_2023"),
            ("dmgg-8hin", "docs_2025"),
            ("u8cx-r425", "modificaciones"),
        ]
        referencia = sys.argv[3] if len(sys.argv) > 3 else "400-00129-354-0-2018"
        values_to_try = [
            ("proceso", proceso),
            ("n_mero_de_contrato", proceso),
            ("n_mero_de_contrato", id_contrato),
            ("n_mero_de_contrato", referencia),
        ]
        for ds_id, label in KNOWN_DOCS:
            print(f"\n  [{label}] {ds_id}")
            for field, val in values_to_try:
                safe = val.replace("'", "''")
                r = await client.get(
                    f"{SOCRATA_BASE}/{ds_id}.json",
                    params={"$where": f"{field} = '{safe}'", "$limit": "5"},
                )
                data = r.json()
                if isinstance(data, list) and data:
                    print(f"    ✓ {field} = '{val}' → {len(data)} docs")


if __name__ == "__main__":
    asyncio.run(discover())
