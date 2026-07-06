"""Test SECOP II internal API endpoints for contract documents."""
import asyncio
import httpx

BASE = "https://community.secop.gov.co"
NOTICE_UID = "CO1.NTC.9506401"


async def test_endpoint(path: str) -> None:
    url = BASE + path
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(url)
        ct = r.headers.get("content-type", "?")
        print(f"GET {path}")
        print(f"  Status: {r.status_code}  CT: {ct}")
        if r.status_code == 200 and ("json" in ct or r.text.strip().startswith("[")):
            print(f"  Body: {r.text[:400]}")
    except Exception as exc:
        print(f"GET {path}: ERROR {exc}")


async def main() -> None:
    paths = [
        f"/Public/Tendering/OpportunityDetail/GetDocuments?noticeUID={NOTICE_UID}",
        f"/Public/Tendering/OpportunityDetail/GetDocuments?noticeUID={NOTICE_UID}&isFromPublicArea=True",
        f"/Public/Tendering/ContractDetail/GetDocuments?noticeUID={NOTICE_UID}",
        f"/Public/Tendering/Notice/GetDocuments?noticeUID={NOTICE_UID}",
        f"/Public/Tendering/OpportunityDetail/GetAttachments?noticeUID={NOTICE_UID}",
        f"/api/opportunities/{NOTICE_UID}/documents",
        f"/api/notice/{NOTICE_UID}/documents",
    ]
    for p in paths:
        await test_endpoint(p)
    print("Done")


asyncio.run(main())
