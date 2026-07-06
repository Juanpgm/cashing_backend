"""Quick live test: query Socrata by proceso_de_compra."""
import asyncio
import sys

import httpx

SECOP_BASE = "https://www.datos.gov.co/resource"
DS_CONTRATOS = "jbjy-vk9h"
ALL_DOCS = ["f8va-cf4m", "kgcd-kt7i", "3skv-9na7", "dmgg-8hin"]


async def test(proceso: str) -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        safe = proceso.replace("'", "''")

        # 1. Find contract by proceso_de_compra
        r = await client.get(
            f"{SECOP_BASE}/{DS_CONTRATOS}.json",
            params={"$where": f"proceso_de_compra = '{safe}'", "$limit": "5"},
        )
        contratos = r.json()
        print(f"\n=== Contratos por proceso_de_compra='{proceso}' ===")
        print(f"Encontrados: {len(contratos)}")
        for ct in contratos:
            print(f"  numero_contrato: {ct.get('numero_contrato')}")
            print(f"  referencia:      {ct.get('referencia_del_contrato')}")
            print(f"  proceso:         {ct.get('proceso_de_compra')}")
            print()

        # 2. Find documents in all datasets
        print("=== Documentos ===")
        total = 0
        for ds in ALL_DOCS:
            r2 = await client.get(
                f"{SECOP_BASE}/{ds}.json",
                params={"$where": f"proceso = '{safe}'", "$limit": "20"},
            )
            docs = r2.json()
            total += len(docs)
            if docs:
                print(f"  Dataset {ds}: {len(docs)} docs")
                for d in docs[:3]:
                    print(f"    {d.get('nombre_archivo', '?')} [{d.get('extensi_n', '?')}]")
        print(f"\nTotal documentos: {total}")


if __name__ == "__main__":
    proceso = sys.argv[1] if len(sys.argv) > 1 else "CO1.BDOS.2913018"
    asyncio.run(test(proceso))
