"""Check NTC notice documents and community.secop.gov.co API."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

NTC_ID = "CO1.NTC.9506401"
BDOS_ID = "CO1.BDOS.9493653"
PCCNTR_ID = "CO1.PCCNTR.8874900"

async def main():
    import httpx
    from app.core.config import settings

    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}

    async with httpx.AsyncClient(timeout=30.0) as client:

        # 1. Try docs linked to the NTC notice
        print("=" * 70)
        print(f"Docs donde proceso = '{NTC_ID}'")
        r = await client.get(
            "https://www.datos.gov.co/resource/dmgg-8hin.json",
            params={"$where": f"proceso = '{NTC_ID}'", "$limit": "100"},
            headers=headers,
        )
        d = r.json()
        print(f"HTTP {r.status_code}, resultados: {len(d) if isinstance(d, list) else d}")
        if isinstance(d, list):
            for doc in d:
                print(f"  {doc.get('id_documento')} | {doc.get('nombre_archivo')} | {doc.get('proceso')}")

        # 2. Probe community.secop.gov.co API endpoints (direct API calls)
        print("\n" + "=" * 70)
        print("PROBE community.secop.gov.co API")
        print("=" * 70)
        community_base = "https://community.secop.gov.co"
        endpoints = [
            f"/Public/Tendering/OpportunityDocuments/GetDocuments?noticeUID={NTC_ID}",
            f"/Public/Tendering/OpportunityDocuments/GetDocumentList?Id={NTC_ID}",
            f"/api/1/Tendering/Notice/{NTC_ID}/Documents",
            f"/Public/Process/ContractDocuments/GetDocuments?contractReference={PCCNTR_ID}",
            f"/Public/Tendering/ContractDocuments/GetDocuments?Id={PCCNTR_ID}",
            f"/api/Tendering/ContractDocuments?noticeUID={NTC_ID}",
        ]
        for ep in endpoints:
            try:
                r = await client.get(
                    community_base + ep,
                    headers={"Accept": "application/json"},
                    follow_redirects=False,
                    timeout=8.0,
                )
                body = r.text[:300]
                print(f"\n  {ep[:60]}")
                print(f"  → HTTP {r.status_code} | Content-Type: {r.headers.get('content-type','')[:50]}")
                if r.status_code in (200, 201) and "json" in r.headers.get("content-type",""):
                    print(f"  BODY: {body}")
            except Exception as e:
                print(f"  {ep[:60]} → ERROR: {type(e).__name__}")

        # 3. Try the SOP Colombia open data catalog for more SECOP doc datasets
        print("\n" + "=" * 70)
        print("SEARCH datos.gov.co catalog for more SECOP doc datasets")
        print("=" * 70)
        catalog_url = "https://www.datos.gov.co/api/catalog/v1"
        try:
            r = await client.get(
                catalog_url,
                params={"q": "SECOP documentos contrato", "$limit": "5"},
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
            print(f"HTTP {r.status_code}")
            if r.status_code == 200:
                print(r.text[:1000])
        except Exception as e:
            print(f"ERROR: {e}")

        # 4. Check if SECOP II has a separate dataset for contract execution docs
        # Dataset IDs from SECOP Colombia open data portal
        candidate_datasets = [
            ("g5gu-ztfe", "SECOP II ejecucion"),
            ("eqmm-x8eq", "SECOP II documentos contratos"),
            ("4zew-q89b", "SECOP II seguimiento ejecucion"),
            ("jbjy-vk9h", "SECOP II contratos - check field documentos"),
        ]
        print("\n" + "=" * 70)
        print("CANDIDATE DATASETS FOR CONTRACT DOCS")
        print("=" * 70)
        for ds, name in candidate_datasets:
            try:
                r = await client.get(
                    f"https://www.datos.gov.co/resource/{ds}.json",
                    params={"$limit": "1"},
                    headers=headers,
                    timeout=8.0,
                )
                if r.status_code == 200 and r.text.strip().startswith("["):
                    data = r.json()
                    if data and isinstance(data, list) and isinstance(data[0], dict):
                        cols = list(data[0].keys())[:12]
                        print(f"\n  {ds} ({name}): OK, cols={cols}")
                    else:
                        print(f"  {ds}: empty")
                else:
                    print(f"  {ds}: HTTP {r.status_code}")
            except Exception as e:
                print(f"  {ds}: ERROR {type(e).__name__}")

asyncio.run(main())
