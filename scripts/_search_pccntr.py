"""Query SECOP datos.gov.co datasets using PCCNTR ID."""
import httpx

BASE = "https://www.datos.gov.co/resource"
datasets = ["f8va-cf4m", "kgcd-kt7i", "3skv-9na7", "dmgg-8hin"]
pccntr = "CO1.PCCNTR.8874900"

for ds in datasets:
    url = f"{BASE}/{ds}.json"
    where = f"n_mero_de_contrato='{pccntr}'"
    r = httpx.get(url, params={"$where": where, "$limit": 20}, timeout=15)
    data = r.json()
    if data:
        print(f"{ds}: {len(data)} docs")
        for d in data[:3]:
            print(f"  nombre_archivo={d.get('nombre_archivo','?')}")
    else:
        print(f"{ds}: 0 docs")

# Also try the filename pattern with PCCNTR 
print("\n=== Search in 2025 dataset by filename ===")
for search_text in ["VERIFICACION", "IDONEIDAD", "CLAUSULADO", "Documentos del Contrato"]:
    url = f"{BASE}/dmgg-8hin.json"
    where = f"nombre_archivo like '%{search_text}%'"
    r = httpx.get(url, params={"$where": where, "$limit": 10}, timeout=15)
    data = r.json()
    print(f"  '{search_text}': {len(data)} docs")
    for d in data[:2]:
        print(f"    {d.get('nombre_archivo','?')} | proceso={d.get('proceso','?')}")
