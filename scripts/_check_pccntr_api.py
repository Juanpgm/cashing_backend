"""Test SECOP II contract detail API with PCCNTR ID."""
import asyncio
import httpx

BASE = "https://community.secop.gov.co"
PCCNTR = "CO1.PCCNTR.8874900"
NTC = "CO1.NTC.9506401"
BDOS = "CO1.BDOS.9493653"


async def test(path: str, label: str = "") -> None:
    url = BASE + path
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
        ct = r.headers.get("content-type", "?")
        lbl = f"[{label}]" if label else ""
        print(f"\nGET {path} {lbl}")
        print(f"  Status: {r.status_code}  CT: {ct}")
        if r.status_code == 200 and len(r.text) > 50:
            print(f"  Body[:400]: {r.text[:400]}")
    except Exception as exc:
        print(f"\nGET {path}: ERROR {exc}")


async def main() -> None:
    paths = [
        (f"/Public/Tendering/ContractPublicDetail/Index?noticeUID={PCCNTR}&isFromPublicArea=True", "ContractPublicDetail"),
        (f"/Public/Tendering/ContractPublicDetail/GetDocuments?noticeUID={PCCNTR}&isFromPublicArea=True", "ContractPublicDetail GetDocuments"),
        (f"/Public/Tendering/ContractDetail/GetContractDocumentList?noticeUID={PCCNTR}", "GetContractDocumentList"),
        (f"/Public/Tendering/Notice/ContractDocuments?noticeUID={PCCNTR}&isFromPublicArea=True", "Notice ContractDocs"),
        (f"/Public/Tendering/OpportunityDetail/GetContractDocuments?noticeUID={NTC}&isFromPublicArea=True", "NTC ContractDocs"),
        (f"/Public/Tendering/OpportunityDetail/GetContractDocuments?noticeUID={BDOS}&isFromPublicArea=True", "BDOS ContractDocs"),
        (f"/Public/Tendering/ContractPublicDetail/GetDocumentListByDossier?dossierUID={PCCNTR}", "dossier docs"),
    ]
    for path, label in paths:
        await test(path, label)
    print("\nDone")


asyncio.run(main())
