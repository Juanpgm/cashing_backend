"""Test GetContractDocuments with PCCNTR UID and session cookie."""
import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/Index?noticeUID=CO1.NTC.9506401",
}
BASE = "https://community.secop.gov.co"

# Test with various UIDs
uids = ["CO1.PCCNTR.8874900", "CO1.NTC.9506401", "CO1.BDOS.9493653"]
for uid in uids:
    url = f"{BASE}/Public/Tendering/OpportunityDetail/GetContractDocuments"
    r = httpx.get(url, params={"noticeUID": uid, "isFromPublicArea": "True"}, headers=HEADERS, timeout=15)
    ct = r.headers.get("content-type", "?")
    print(f"GET GetContractDocuments?noticeUID={uid}: {r.status_code} len={len(r.content)} ct={ct}")
    if r.text:
        print(f"  Body: {r.text[:200]}")

# Try with session cookie from index page
print()
r0 = httpx.get(
    f"{BASE}/Public/Tendering/OpportunityDetail/Index?noticeUID=CO1.NTC.9506401&isFromPublicArea=True",
    headers=HEADERS,
    timeout=15,
)
cookies = dict(r0.cookies)
print(f"Got cookies from index: {cookies}")

r1 = httpx.get(
    f"{BASE}/Public/Tendering/OpportunityDetail/GetContractDocuments",
    params={"noticeUID": "CO1.PCCNTR.8874900", "isFromPublicArea": "True"},
    headers=HEADERS,
    cookies=cookies,
    timeout=15,
)
print(f"With session cookie PCCNTR: {r1.status_code} len={len(r1.content)}")
if r1.text:
    print(f"  Body: {r1.text[:500]}")

r2 = httpx.get(
    f"{BASE}/Public/Tendering/OpportunityDetail/GetContractDocuments",
    params={"noticeUID": "CO1.NTC.9506401", "isFromPublicArea": "True"},
    headers=HEADERS,
    cookies=cookies,
    timeout=15,
)
print(f"With session cookie NTC: {r2.status_code} len={len(r2.content)}")
if r2.text:
    print(f"  Body: {r2.text[:500]}")
