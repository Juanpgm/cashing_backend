"""Check SECOP datasets for documents belonging to cedula 1110547406."""
import asyncio
import httpx

DATASETS = ["f8va-cf4m", "kgcd-kt7i", "3skv-9na7", "dmgg-8hin"]
PROCESOS = ["CO1.BDOS.9493653", "CO1.BDOS.7424672"]
BASE = "https://www.datos.gov.co/resource"


async def check_proceso(proceso: str) -> None:
    url = f"{BASE}/dmgg-8hin.json"
    where = "proceso = '" + proceso + "'"
    params = {"$where": where, "$limit": "200", "$order": "id_documento ASC"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
    data = r.json()
    print(f"\n=== proceso {proceso} ({len(data)} docs) ===")
    for row in data:
        print(f"  [{row.get('id_documento')}] {row.get('nombre_archivo')}")


async def check_dataset(ds: str) -> None:
    url = f"{BASE}/{ds}.json"
    cedula = "1110547406"
    where = "nombre_archivo like '" + cedula + "%'"
    params = {"$where": where, "$limit": "20"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
    print(f"\n=== Dataset {ds} === status={r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  Count: {len(data)}")
        for row in data:
            print(f"  - [{row.get('id_documento')}] {row.get('nombre_archivo')}")
            print(f"    n_mero_de_contrato={row.get('n_mero_de_contrato')} proceso={row.get('proceso')}")
    else:
        print(f"  Error: {r.text[:300]}")


async def check_contract_detail(proceso: str) -> None:
    """Query contracts dataset for a given proceso."""
    url = f"{BASE}/jbjy-vk9h.json"
    where = "proceso_de_compra = '" + proceso + "'"
    params = {"$where": where, "$limit": "10"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
    data = r.json()
    print(f"\n=== contratos for proceso {proceso} ({len(data)} rows) ===")
    for row in data:
        print(f"  referencia_del_contrato: {row.get('referencia_del_contrato')}")
        print(f"  numero_contrato:         {row.get('numero_contrato')}")
        print(f"  urlproceso:              {row.get('urlproceso')}")


async def check_secop2_api(referencia: str) -> None:
    """Try SECOP II direct API for contract documents."""
    # Try community.secop.gov.co
    url = "https://community.secop.gov.co/Public/Tendering/ContractDetail/GetDocuments"
    params = {"id": referencia}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(url, params=params)
            print(f"\n=== community.secop GetDocuments ({r.status_code}) ===")
            print(r.text[:500])
        except Exception as e:
            print(f"\n=== community.secop GetDocuments error: {e} ===")


async def check_by_referencia(referencia: str) -> None:
    """Check all doc datasets using n_mero_de_contrato = referencia."""
    print(f"\n=== Docs by n_mero_de_contrato = {referencia} ===")
    datasets = ["f8va-cf4m", "kgcd-kt7i", "3skv-9na7", "dmgg-8hin"]
    for ds in datasets:
        url = f"{BASE}/{ds}.json"
        where = "n_mero_de_contrato = '" + referencia + "'"
        params = {"$where": where, "$limit": "20"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params)
        data = r.json() if r.status_code == 200 else []
        count = len(data) if isinstance(data, list) else 0
        print(f"  {ds}: {count} docs")
        for row in (data[:5] if isinstance(data, list) else []):
            print(f"    [{row.get('id_documento')}] {row.get('nombre_archivo')}")


async def main() -> None:
    print("=== Contract detail (contratos dataset) ===")
    for p in PROCESOS:
        await check_contract_detail(p)

    await check_by_referencia("4161.010.26.1.155.2026")
    await check_by_referencia("4161.010.26.1.027.2025")

    print("\nDone.")


asyncio.run(main())
