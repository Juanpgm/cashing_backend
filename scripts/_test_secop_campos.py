"""
Inspect Socrata field names for all SECOP document datasets,
then attempt every known integration key against a sample contract.
"""
import asyncio
import sys

import httpx

SECOP_BASE = "https://www.datos.gov.co/resource"
SOCRATA_META = "https://www.datos.gov.co/api/views"

DS_CONTRATOS     = "jbjy-vk9h"
DS_DOCS_HIST     = "f8va-cf4m"   # 2018-2021
DS_DOCS_2022     = "kgcd-kt7i"
DS_DOCS_2023     = "3skv-9na7"
DS_DOCS_2025     = "dmgg-8hin"   # 2025+
DS_MODIFICACIONES = "u8cx-r425"

ALL_DATASETS = {
    "contratos":      DS_CONTRATOS,
    "docs_hist":      DS_DOCS_HIST,
    "docs_2022":      DS_DOCS_2022,
    "docs_2023":      DS_DOCS_2023,
    "docs_2025":      DS_DOCS_2025,
    "modificaciones": DS_MODIFICACIONES,
}


async def get_columns(client: httpx.AsyncClient, dataset_id: str) -> list[str]:
    """Return column field names for a Socrata dataset."""
    r = await client.get(f"{SOCRATA_META}/{dataset_id}.json", timeout=20.0)
    if r.status_code != 200:
        return [f"HTTP {r.status_code}"]
    meta = r.json()
    cols = meta.get("columns", [])
    return [c["fieldName"] for c in cols]


async def try_query(client: httpx.AsyncClient, dataset_id: str, field: str, value: str) -> int:
    """Return count of rows matching field='value' in dataset."""
    safe = value.replace("'", "''")
    try:
        r = await client.get(
            f"{SECOP_BASE}/{dataset_id}.json",
            params={"$where": f"{field} = '{safe}'", "$limit": "5"},
            timeout=20.0,
        )
        data = r.json()
        if isinstance(data, list):
            return len(data)
        return 0
    except Exception:
        return 0


async def main(proceso: str, numero_contrato: str, referencia: str) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        # ── 1. Print columns for each dataset ────────────────────────────────
        print("\n" + "="*60)
        print("COLUMNAS POR DATASET")
        print("="*60)
        all_cols: dict[str, list[str]] = {}
        tasks = {name: get_columns(client, dsid) for name, dsid in ALL_DATASETS.items()}
        results = await asyncio.gather(*tasks.values())
        for name, cols in zip(tasks.keys(), results):
            all_cols[name] = cols
            # Only show potentially linking fields
            link_cols = [c for c in cols if any(k in c for k in
                ["contrato", "proceso", "referencia", "portafolio", "numero", "mero"])]
            print(f"\n[{name}] ({ALL_DATASETS[name]})")
            for c in link_cols:
                print(f"  {c}")

        # ── 2. Possible values to try ─────────────────────────────────────────
        values = {
            "proceso":           proceso,
            "numero_contrato":   numero_contrato,
            "referencia":        referencia,
        }

        # ── 3. Candidate link fields in each doc dataset ─────────────────────
        doc_datasets = {
            "docs_hist":      DS_DOCS_HIST,
            "docs_2022":      DS_DOCS_2022,
            "docs_2023":      DS_DOCS_2023,
            "docs_2025":      DS_DOCS_2025,
            "modificaciones": DS_MODIFICACIONES,
        }

        print("\n" + "="*60)
        print("INTENTOS DE BÚSQUEDA (campo → valor → docs encontrados)")
        print("="*60)

        for ds_name, ds_id in doc_datasets.items():
            cols = all_cols.get(ds_name, [])
            print(f"\n[{ds_name}] ({ds_id})")
            for field in cols:
                # Only try fields that look like linking keys
                if not any(k in field for k in
                    ["contrato", "proceso", "referencia", "portafolio", "numero", "mero", "id_"]):
                    continue
                for val_name, val in values.items():
                    if not val:
                        continue
                    count = await try_query(client, ds_id, field, val)
                    if count > 0:
                        print(f"  ✓ {field} = '{val}' ({val_name}) → {count} docs")
                    # else silent to keep output clean


async def full_test(proceso: str) -> None:
    """Given a proceso_de_compra, find the contract and then try all possible keys."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        safe = proceso.replace("'", "''")
        # Resolve the contract to get referencia + id_contrato
        r = await client.get(
            f"{SECOP_BASE}/{DS_CONTRATOS}.json",
            params={"$where": f"proceso_de_compra = '{safe}'", "$limit": "5"},
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            print(f"No contract found for proceso_de_compra='{proceso}'")
            return
        ct = data[0]
        referencia = ct.get("referencia_del_contrato", "")
        id_contrato = ct.get("id_contrato", "")
        print(f"\nContrato encontrado:")
        print(f"  referencia_del_contrato: {referencia}")
        print(f"  id_contrato:             {id_contrato}")
        print(f"  proceso_de_compra:       {ct.get('proceso_de_compra')}")

    await main(proceso, id_contrato, referencia)


if __name__ == "__main__":
    # Pass: proceso_de_compra value
    proceso = sys.argv[1] if len(sys.argv) > 1 else "CO1.BDOS.562792"
    asyncio.run(full_test(proceso))
