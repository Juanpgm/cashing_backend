"""Check documentos_tipo field and all raw data of the specific contract."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

REFERENCIA = "4161.010.26.1.155.2026"

async def main():
    import httpx
    from app.core.config import settings
    from app.core.database import async_session_factory
    from app.models.secop import SecopContrato
    from sqlalchemy import select, or_

    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}

    # 1. Show full raw data from DB including documentos_tipo
    async with async_session_factory() as db:
        r = await db.execute(
            select(SecopContrato).where(
                or_(
                    SecopContrato.numero_contrato == REFERENCIA,
                    SecopContrato.referencia_del_contrato == REFERENCIA,
                )
            )
        )
        contratos = r.scalars().all()
        for c in contratos:
            raw = c.datos_raw or {}
            print("=" * 70)
            print(f"DATOS RAW CONTRATO: {c.referencia_del_contrato}")
            print("=" * 70)
            print(f"documentos_tipo: {raw.get('documentos_tipo')!r}")
            print(f"descripcion_documentos_tipo: {raw.get('descripcion_documentos_tipo')!r}")
            print(f"id_contrato: {raw.get('id_contrato')!r}")
            print(f"urlproceso: {raw.get('urlproceso')!r}")
            print(f"estado_contrato: {raw.get('estado_contrato')!r}")

    # 2. Get full contract from SECOP with ALL fields
    print("\n" + "=" * 70)
    print("FULL SECOP CONTRATO DATA")
    print("=" * 70)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            "https://www.datos.gov.co/resource/jbjy-vk9h.json",
            params={"$where": f"referencia_del_contrato = '{REFERENCIA}'", "$limit": "10"},
            headers=headers,
        )
        data = r.json()
        for c in data:
            if not isinstance(c, dict):
                continue
            print(json.dumps(c, indent=2, ensure_ascii=False, default=str))

    # 3. Check SECOP for additional document datasets (different from dmgg-8hin)
    # Socrata datasets list for datos.gov.co SECOP
    print("\n" + "=" * 70)
    print("PROBE EXTRA SECOP DATASETS")
    print("=" * 70)
    # Known additional datasets
    extra_datasets = {
        "cbjm-bszd": "SECOP I documentos",
        "g5gu-ztfe": "SECOP II documentos ejecucion",
        "rpmg-qbbz": "SECOP II seguimiento",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        for ds_id, ds_name in extra_datasets.items():
            try:
                r = await client.get(
                    f"https://www.datos.gov.co/resource/{ds_id}.json",
                    params={"$limit": "1"},
                    headers=headers,
                )
                data = r.json()
                if r.status_code == 200 and isinstance(data, list) and data:
                    cols = list(data[0].keys())
                    print(f"\n  {ds_id} ({ds_name}) HTTP {r.status_code}")
                    print(f"  Columns: {cols[:15]}")
                    # Try to find docs for our process
                    for field in cols:
                        if "proceso" in field.lower() or "contrato" in field.lower():
                            r2 = await client.get(
                                f"https://www.datos.gov.co/resource/{ds_id}.json",
                                params={"$where": f"{field} = 'CO1.BDOS.9493653'", "$limit": "10"},
                                headers=headers,
                            )
                            d2 = r2.json()
                            if isinstance(d2, list) and len(d2) > 0 and isinstance(d2[0], dict):
                                print(f"  *** MATCH en campo '{field}': {len(d2)} resultados")
                                for item in d2[:3]:
                                    print(f"    {json.dumps(item, ensure_ascii=False, default=str)[:200]}")
                                break
                else:
                    print(f"  {ds_id} ({ds_name}): HTTP {r.status_code}")
            except Exception as e:
                print(f"  {ds_id}: ERROR {e}")

    # 4. Try SECOP II contract documents endpoint directly
    print("\n" + "=" * 70)
    print("SECOP II COMMUNITY API (direct)")
    print("=" * 70)
    async with httpx.AsyncClient(timeout=15.0) as client:
        id_contrato = "CO1.PCCNTR.8874900"
        urls_to_try = [
            f"https://community.secop.gov.co/Public/Tendering/OpportunityDetail/Index?Id={REFERENCIA}",
            f"https://www.contratos.gov.co/consultas/detalleProceso.do?numConstancia=CO1.BDOS.9493653",
        ]
        for url in urls_to_try:
            try:
                r = await client.get(url, follow_redirects=True, timeout=10.0)
                print(f"  {url[:70]} → HTTP {r.status_code}")
            except Exception as e:
                print(f"  ERROR: {e}")

asyncio.run(main())
