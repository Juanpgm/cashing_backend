"""Get full contract row and document URL details from SECOP."""
import asyncio
import httpx
import json

BASE = "https://www.datos.gov.co/resource"


async def main() -> None:
    proceso = "CO1.BDOS.9493653"
    # Full contract row
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BASE}/jbjy-vk9h.json",
            params={"$where": "proceso_de_compra = '" + proceso + "'", "$limit": "1"},
        )
    rows = r.json()
    if rows:
        print("Contract row fields:")
        for k, v in rows[0].items():
            print(f"  {k}: {v}")

    print()

    # Full document row for known document
    async with httpx.AsyncClient(timeout=30) as client:
        r2 = await client.get(
            f"{BASE}/dmgg-8hin.json",
            params={"$where": "proceso = '" + proceso + "'", "$limit": "2"},
        )
    docs = r2.json()
    if docs:
        print("Document row fields:")
        for k, v in docs[0].items():
            print(f"  {k}: {v}")


asyncio.run(main())
