"""Shared HTTP constants for SECOP document downloads.

community.secop.gov.co (the RetrieveFile document host, both the
DocumentId-cache path and the Playwright scraper fallback) returns 403
text/html for UA-less requests and 200 with the actual file for requests
carrying a browser User-Agent (verified manually, fixed once already for the
sniff path in commit 807da11). Every `httpx.AsyncClient` that downloads a
SECOP document — regardless of which service issues the request — must send
this header or the download fails.

NOT used for the Socrata REST API (`_query_socrata` in secop_service.py,
datos.gov.co): that is a different, token-authenticated host with no known
UA requirement.
"""

from __future__ import annotations

SECOP_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
