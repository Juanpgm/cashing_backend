"""Inspects modificaciones dataset fields and sample records."""
import httpx
import asyncio
import json

async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        meta = (await client.get("https://www.datos.gov.co/api/views/u8cx-r425.json")).json()
        print("CAMPOS u8cx-r425:")
        for col in meta.get("columns", []):
            print(f"  {col['fieldName']:45} ({col.get('dataTypeName', '')})")

        for idc in ["CO1.PCCNTR.7058419", "CO1.PCCNTR.7321684", "CO1.PCCNTR.1010915"]:
            print(f"\nREGISTROS para {idc}:")
            r = await client.get(
                "https://www.datos.gov.co/resource/u8cx-r425.json",
                params={"$where": f"id_contrato = '{idc}'", "$limit": "3"},
            )
            rows = r.json()
            if isinstance(rows, list) and rows:
                for row in rows:
                    print(json.dumps(row, ensure_ascii=False, indent=2))
            else:
                print("  (sin resultados o error)")

if __name__ == "__main__":
    asyncio.run(main())
