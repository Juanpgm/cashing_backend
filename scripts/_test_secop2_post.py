"""Try POST to SECOP II contract documents endpoints and test Azure Blob URLs."""
import asyncio
import httpx
import json

BASE = "https://community.secop.gov.co"
NTC = "CO1.NTC.9506401"
BDOS = "CO1.BDOS.9493653"
PCCNTR = "CO1.PCCNTR.8874900"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


async def try_post(path: str, body: dict, label: str) -> None:
    url = BASE + path
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.post(url, json=body, headers=HEADERS)
        ct = r.headers.get("content-type", "?")
        print(f"\nPOST {path} [{label}]")
        print(f"  Status: {r.status_code}  CT: {ct}  Len: {len(r.content)}")
        if len(r.text) > 0:
            print(f"  Body[:600]: {r.text[:600]}")
    except Exception as exc:
        print(f"\nPOST {path}: ERROR {exc}")


async def try_azure_blob(doc_id: int) -> None:
    """Try constructing Azure Blob URL from document ID."""
    blob_base = "https://accesopublicosecopiiprod.blob.core.windows.net"
    # Try common patterns
    urls = [
        f"{blob_base}/documents/{doc_id}",
        f"{blob_base}/secop/{doc_id}",
        f"{blob_base}/public/{doc_id}.pdf",
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        for url in urls:
            try:
                r = await client.head(url)
                print(f"  HEAD {url}: {r.status_code}")
            except Exception as exc:
                print(f"  HEAD {url}: ERROR {exc}")


async def main() -> None:
    # Try POST with various payloads
    await try_post(
        f"/Public/Tendering/OpportunityDetail/GetContractDocuments",
        {"noticeUID": NTC, "isFromPublicArea": True},
        "NTC POST json body",
    )
    await try_post(
        f"/Public/Tendering/OpportunityDetail/GetContractDocuments",
        {"noticeUID": BDOS, "isFromPublicArea": True},
        "BDOS POST json body",
    )

    # Try URL-encoded POST  
    url = BASE + "/Public/Tendering/OpportunityDetail/GetContractDocuments"
    hdrs = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.post(url, data={"noticeUID": NTC, "isFromPublicArea": "True"}, headers=hdrs)
    print(f"\nPOST GetContractDocuments urlencoded [{NTC}]")
    print(f"  Status: {r.status_code}  Len: {len(r.content)}")
    if r.text:
        print(f"  Body[:600]: {r.text[:600]}")

    # Azure blob test for known doc ID
    print("\n=== Azure Blob URL patterns (known doc 732982152) ===")
    await try_azure_blob(732982152)

    print("\nDone")


asyncio.run(main())
