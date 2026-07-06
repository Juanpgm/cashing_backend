"""Inspecciona los documentos encontrados y el dataset SECOP Integrado."""
import httpx
import asyncio
import json

PROCESO = "CO1.BDOS.562792"
ID_CONTRATO = "CO1.PCCNTR.599010"
BASE = "https://www.datos.gov.co/resource"


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Ver qué tienen los 5 docs de f8va-cf4m
        print("=== MUESTRA de los 5 docs en f8va-cf4m ===")
        r = await client.get(
            f"{BASE}/f8va-cf4m.json",
            params={"$where": f"proceso = '{PROCESO}'", "$limit": "5"},
        )
        docs = r.json()
        for d in docs:
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in d.items()
                        if k in [
                            "id_documento", "n_mero_de_contrato", "proceso",
                            "nombre_archivo", "extensi_n", "url_descarga_documento",
                        ]
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )

        # 2. Explorar rpmr-utcd (SECOP Integrado)
        print()
        print("=== rpmr-utcd SECOP Integrado - columnas ===")
        meta = (await client.get("https://www.datos.gov.co/api/views/rpmr-utcd.json")).json()
        cols = [c["fieldName"] for c in meta.get("columns", [])]
        print("Columnas:", cols)
        for field, val in [
            ("numero_de_proceso", PROCESO),
            ("numero_del_contrato", ID_CONTRATO),
            ("numero_del_contrato", "400-00129-354-0-2018"),
        ]:
            if field in cols:
                r2 = await client.get(
                    f"{BASE}/rpmr-utcd.json",
                    params={"$where": f"{field} = '{val}'", "$limit": "5"},
                )
                d2 = r2.json()
                if isinstance(d2, list) and d2:
                    print(f"  + {field} = '{val}' -> {len(d2)} registros")
                    print("  ", json.dumps(d2[0], ensure_ascii=False)[:300])

        # 3. Buscar en gra4-pcp2 (ubicaciones)
        print()
        print("=== gra4-pcp2 Ubicaciones contratos - columnas ===")
        meta2 = (await client.get("https://www.datos.gov.co/api/views/gra4-pcp2.json")).json()
        cols2 = [c["fieldName"] for c in meta2.get("columns", [])]
        print("Columnas:", cols2)


if __name__ == "__main__":
    asyncio.run(main())
