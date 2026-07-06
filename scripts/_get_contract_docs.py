"""Get contract documents from the two working SECOP II endpoints."""
import asyncio
import httpx
import json

BASE = "https://community.secop.gov.co"
NTC = "CO1.NTC.9506401"
BDOS = "CO1.BDOS.9493653"


async def get_docs(notice_uid: str) -> None:
    path = f"/Public/Tendering/OpportunityDetail/GetContractDocuments?noticeUID={notice_uid}&isFromPublicArea=True"
    url = BASE + path
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
    print(f"\n=== GetContractDocuments for {notice_uid} ===")
    print(f"Status: {r.status_code}")
    print(f"Headers: {dict(r.headers)}")
    print(f"Body ({len(r.text)} chars):")
    print(r.text[:2000])
    # Try parse as JSON
    try:
        data = r.json()
        print(f"\nParsed JSON ({len(data)} items):")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
    except Exception as e:
        print(f"Not JSON: {e}")


async def main() -> None:
    await get_docs(NTC)
    await get_docs(BDOS)
    print("\nDone")


asyncio.run(main())
