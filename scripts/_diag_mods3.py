"""Investigate ALL contract entries and document links for a referencia."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

REFERENCIA = "4161.010.26.1.155.2026"
ID_CONTRATO_SECOP = "CO1.PCCNTR.8874900"  # from previous diag

async def main():
    import httpx
    from app.core.config import settings

    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}

    async with httpx.AsyncClient(timeout=30.0) as client:

        # 1. All contracts by numero_contrato (shows original + mods)
        print("=" * 70)
        print(f"Contratos con numero_contrato = '{REFERENCIA}'")
        print("=" * 70)
        r = await client.get(
            "https://www.datos.gov.co/resource/jbjy-vk9h.json",
            params={"$where": f"numero_contrato = '{REFERENCIA}'", "$limit": "100"},
            headers=headers,
        )
        raw = r.text
        data = r.json()
        print(f"HTTP {r.status_code}, tipo={type(data).__name__}")
        if isinstance(data, list):
            print(f"Total contratos: {len(data)}")
            procesos_all = []
            for c in data:
                if not isinstance(c, dict):
                    print(f"  Elemento no-dict: {c!r}")
                    continue
                proc = c.get("proceso_de_compra", "")
                procesos_all.append(proc)
                print(f"\n  id_contrato={c.get('id_contrato')!r}")
                print(f"  referencia={c.get('referencia_del_contrato')!r}")
                print(f"  numero_contrato={c.get('numero_contrato')!r}")
                print(f"  proceso_de_compra={proc!r}")
                print(f"  estado_contrato={c.get('estado_contrato')!r}")
                print(f"  ultima_actualizacion={c.get('ultima_actualizacion')!r}")
                print(f"  documentos_tipo={c.get('documentos_tipo')!r}")
                print(f"  descripcion_documentos_tipo={c.get('descripcion_documentos_tipo')!r}")
        else:
            print(f"Respuesta inesperada: {raw[:500]}")
            procesos_all = []

        # 2. Search by referencia_del_contrato too
        print("\n" + "=" * 70)
        print(f"Contratos con referencia_del_contrato = '{REFERENCIA}'")
        print("=" * 70)
        r2 = await client.get(
            "https://www.datos.gov.co/resource/jbjy-vk9h.json",
            params={"$where": f"referencia_del_contrato = '{REFERENCIA}'", "$limit": "100"},
            headers=headers,
        )
        data2 = r2.json()
        if isinstance(data2, list):
            for c in data2:
                if not isinstance(c, dict):
                    continue
                proc = c.get("proceso_de_compra", "")
                if proc not in procesos_all:
                    procesos_all.append(proc)
                    print(f"  NUEVO proceso: {proc!r} estado={c.get('estado_contrato')!r}")
                else:
                    print(f"  Ya conocido: {proc!r}")

        # 3. Search docs by id_contrato (CO1.PCCNTR format)
        print("\n" + "=" * 70)
        print(f"Docs con n_mero_de_contrato = '{ID_CONTRATO_SECOP}'")
        print("=" * 70)
        r3 = await client.get(
            "https://www.datos.gov.co/resource/dmgg-8hin.json",
            params={"$where": f"n_mero_de_contrato = '{ID_CONTRATO_SECOP}'", "$limit": "1000"},
            headers=headers,
        )
        d3 = r3.json()
        print(f"Total: {len(d3)}")
        for d in d3:
            if isinstance(d, dict):
                print(f"  id={d.get('id_documento')!r} nombre={d.get('nombre_archivo','')!r} ext={d.get('extensi_n','')!r}")

        # 4. Full text search for any doc with this referencia in any field
        print("\n" + "=" * 70)
        print(f"Docs con entidad '890399011' (NIT)")
        print("=" * 70)
        r4 = await client.get(
            "https://www.datos.gov.co/resource/dmgg-8hin.json",
            params={
                "$where": f"nit_entidad = '890399011' AND fecha_carga >= '2026-01-01T00:00:00'",
                "$limit": "200",
                "$order": "fecha_carga DESC"
            },
            headers=headers,
        )
        d4 = r4.json()
        print(f"Total docs de esa entidad desde 2026: {len(d4)}")
        for d in d4:
            if isinstance(d, dict):
                nombre = d.get('nombre_archivo','')[:60]
                print(f"  id={d.get('id_documento')!r} proc={d.get('proceso','')!r} nombre={nombre!r} n_contrato={d.get('n_mero_de_contrato','')!r}")

        # 5. Check all unique procesos found
        unique_procesos = list(dict.fromkeys(p for p in procesos_all if p))
        print(f"\nPROCESOS UNICOS ENCONTRADOS: {unique_procesos}")
        print("\n" + "=" * 70)
        print("DOCS POR CADA PROCESO UNICO")
        print("=" * 70)
        for proc in unique_procesos:
            r5 = await client.get(
                "https://www.datos.gov.co/resource/dmgg-8hin.json",
                params={"$where": f"proceso = '{proc}'", "$limit": "1000"},
                headers=headers,
            )
            d5 = r5.json()
            print(f"\n  {proc} → {len(d5)} docs")
            for d in d5:
                if isinstance(d, dict):
                    nombre = d.get('nombre_archivo','')[:60]
                    print(f"    {d.get('id_documento')!r} {nombre!r} n_c={d.get('n_mero_de_contrato','')!r}")

asyncio.run(main())
