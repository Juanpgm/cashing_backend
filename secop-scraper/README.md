# SECOP II Scraper Microservice

A small FastAPI service that uses Playwright + Chromium to scrape contract
documents from the SECOP II public portal (`community.secop.gov.co`).

The portal is protected by reCAPTCHA, so this service runs a real browser
with stealth tweaks. A persistent `storage_state.json` (mounted volume in
production) keeps the cookies/session alive between requests so we don't
have to solve a captcha every time.

## Why a separate service?

- Playwright + Chromium adds ~300 MB to the image
- Scraping blocks the event loop for several seconds per request
- The main API can call this with a simple `httpx` client and mock it in tests

## Endpoints

```
GET  /health                          → liveness check
POST /scrape/contract-docs            → scrape documents for one contract
     body: {"notice_uid": "CO1.NTC.9506401", "ref_contrato": "..."}
     header: X-Internal-Token: <SECOP_SCRAPER_INTERNAL_TOKEN>
     200: {"docs": [...], "duration_ms": N}
     503: {"error": "captcha_required", "manual_action_url": "..."}
```

## Local dev

```bash
cd secop-scraper
pip install -r requirements.txt
playwright install chromium
SECOP_SCRAPER_INTERNAL_TOKEN=dev-token uvicorn app.main:app --port 8090 --reload
```

## Deploy on Railway

This is a separate Railway service pointing to `secop-scraper/`.
Required env vars:
- `SECOP_SCRAPER_INTERNAL_TOKEN` — shared secret with the main API
- `SECOP_STORAGE_STATE_PATH` — defaults to `/data/storage_state.json`
- `SECOP_HEADLESS` — `true` for production
