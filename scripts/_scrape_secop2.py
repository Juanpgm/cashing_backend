"""
Try to fetch SECOP II opportunity page and find contract document links.
The opportunity page with isFromPublicArea=True should be publicly accessible.
"""
import httpx
import re

NTC_UID = "CO1.NTC.9506401"
BDOS_UID = "CO1.BDOS.9493653"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "es-CO,es;q=0.9,en-US;q=0.8",
}

def try_url(label: str, url: str) -> None:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        print(f"\n{label}")
        print(f"  URL: {url}")
        print(f"  Status: {r.status_code}  Len: {len(r.content)}")
        body = r.text
        # Look for document IDs
        doc_ids = re.findall(r"DocumentId[=\s'\"]+(\d+)", body)
        if doc_ids:
            print(f"  DocumentIds found: {doc_ids}")
        # Look for document names
        filenames = re.findall(r"[\w\s]+\.(pdf|doc|docx|zip|PDF|DOC|DOCX|ZIP)", body)
        if filenames[:10]:
            print(f"  Filenames: {filenames[:10]}")
        # Look for any JSON-like data containing document info
        json_blocks = re.findall(r'"nombre[^"]*"\s*:\s*"([^"]{5,80})"', body)
        if json_blocks[:5]:
            print(f"  nombre fields: {json_blocks[:5]}")
        if r.status_code == 200 and len(r.content) > 5000:
            # Save snippet
            with open(f"scripts/_secop2_page_{label[:10].replace(' ','_')}.html", "w", encoding="utf-8") as f:
                f.write(body[:20000])
            print(f"  Saved first 20KB to scripts/_secop2_page_{label[:10].replace(' ','_')}.html")
    except Exception as exc:
        print(f"\n{label}: ERROR {exc}")


# Test various public SECOP II URLs
urls = [
    ("Opportunity NTC public", f"https://community.secop.gov.co/Public/Tendering/OpportunityDetail/Index?noticeUID={NTC_UID}&isFromPublicArea=True"),
    ("ContractDetail PCCNTR", f"https://community.secop.gov.co/Public/Tendering/ContractDetail/Index?noticeUID=CO1.PCCNTR.8874900&isFromPublicArea=True"),
    ("ContractDetail NTC", f"https://community.secop.gov.co/Public/Tendering/ContractDetail/Index?noticeUID={NTC_UID}&isFromPublicArea=True"),
    ("GetContractDocs GET headers", f"https://community.secop.gov.co/Public/Tendering/OpportunityDetail/GetContractDocuments?noticeUID={NTC_UID}&isFromPublicArea=True"),
    ("BDOS OpportunityDetail", f"https://community.secop.gov.co/Public/Tendering/OpportunityDetail/Index?noticeUID={BDOS_UID}&isFromPublicArea=True"),
]

for label, url in urls:
    try_url(label, url)

print("\nDone")
