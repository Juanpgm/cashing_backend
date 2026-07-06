"""Find ALL SECOP contrato entries for a referencia (original + modifications)."""
import asyncio, sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

REFERENCIA = "4161.010.26.1.155.2026"
CEDULA = "1110547406"  # also search by cedula

async def main():
    import httpx
    from app.core.config import settings

    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}

    async with httpx.AsyncClient(timeout=30.0) as client:

        # 1. All SECOP contrato rows with this referencia
        print("=" * 70)
        print(f"SECOP contratos con referencia_del_contrato = '{REFERENCIA}'")
        print("=" * 70)
        r = await client.get(
            "https://www.datos.gov.co/resource/jbjy-vk9h.json",
            params={"$where": f"referencia_del_contrato = '{REFERENCIA}'", "$limit": "100"},
            headers=headers,
        )
        contratos = r.json()
        print(f"Total: {len(contratos)}")
        procesos_encontrados = []
        for c in contratos:
            proc = c.get("proceso_de_compra", "")
            procesos_encontrados.append(proc)
            print(f"\n  id_contrato={c.get('id_contrato')!r}")
            print(f"  referencia={c.get('referencia_del_contrato')!r}")
            print(f"  numero_contrato={c.get('numero_contrato')!r}")
            print(f"  proceso_de_compra={proc!r}")
            print(f"  estado_contrato={c.get('estado_contrato')!r}")
            print(f"  descripcion={c.get('descripcion_del_proceso','')[:80]!r}")
            raw_keys = [k for k in c.keys()]
            print(f"  ALL KEYS: {raw_keys}")

        # 2. Also search by numero_contrato field
        print("\n" + "=" * 70)
        print(f"SECOP contratos con numero_contrato = '{REFERENCIA}'")
        print("=" * 70)
        r2 = await client.get(
            "https://www.datos.gov.co/resource/jbjy-vk9h.json",
            params={"$where": f"numero_contrato = '{REFERENCIA}'", "$limit": "100"},
            headers=headers,
        )
        c2 = r2.json()
        print(f"Total: {len(c2)}")
        for c in c2:
            proc = c.get("proceso_de_compra", "")
            if proc not in procesos_encontrados:
                procesos_encontrados.append(proc)
            print(f"  id={c.get('id_contrato')!r} proceso={proc!r} estado={c.get('estado_contrato')!r}")

        # 3. For EACH unique proceso found, get ALL its documents
        print("\n" + "=" * 70)
        print("DOCUMENTOS POR PROCESO")
        print("=" * 70)
        all_unique = list(dict.fromkeys(p for p in procesos_encontrados if p))
        print(f"Procesos a revisar: {all_unique}")

        grand_total = 0
        for proc in all_unique:
            r3 = await client.get(
                "https://www.datos.gov.co/resource/dmgg-8hin.json",
                params={"$where": f"proceso = '{proc}'", "$limit": "1000"},
                headers=headers,
            )
            docs = r3.json()
            grand_total += len(docs)
            print(f"\n  {proc} → {len(docs)} documentos")
            for d in docs:
                print(f"    id={d.get('id_documento')!r} nombre={d.get('nombre_archivo','')!r} ext={d.get('extensi_n','')!r} n_contrato={d.get('n_mero_de_contrato','')!r}")

        print(f"\nTOTAL DOCS TODOS PROCESOS: {grand_total}")

        # 4. Try to find any docs with n_mero_de_contrato matching variations
        print("\n" + "=" * 70)
        print("BUSQUEDA POR VARIACIONES DEL NUMERO")
        print("=" * 70)
        # Sometimes stored with different separators
        variations = [REFERENCIA, REFERENCIA.replace(".", "-"), REFERENCIA.replace(".", "/")]
        for v in variations:
            r4 = await client.get(
                "https://www.datos.gov.co/resource/dmgg-8hin.json",
                params={"$where": f"n_mero_de_contrato = '{v}'", "$limit": "100"},
                headers=headers,
            )
            d4 = r4.json()
            print(f"  n_mero='{v}' → {len(d4)} docs")

asyncio.run(main())
