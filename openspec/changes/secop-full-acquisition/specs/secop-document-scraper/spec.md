# SECOP Document Scraper Specification

## Purpose

Wires the already-built Playwright scraper adapter (`SecopScraperPort` /
`SecopScraperHttpAdapter` / `NullSecopScraperAdapter`) end-to-end as a
manually-triggered, fail-soft fallback for platform-only SECOP II documents
(pliegos, anexos, contrato firmado) that Socrata archives don't carry, behind
`SECOP_SCRAPER_ENABLED`.

## Requirements

### Requirement: Adapter selection is flag- and config-gated

The system MUST inject `SecopScraperHttpAdapter` only when
`SECOP_SCRAPER_ENABLED` is `True` AND the scraper microservice URL/token are
configured; otherwise it MUST inject `NullSecopScraperAdapter`.

#### Scenario: Flag off

- GIVEN `SECOP_SCRAPER_ENABLED=False` (default)
- WHEN any code path requests the scraper port
- THEN the null adapter is injected and returns an empty, non-erroring result

#### Scenario: Flag on and configured

- GIVEN `SECOP_SCRAPER_ENABLED=True` and the microservice URL/token are set
- WHEN the scraper port is requested
- THEN the HTTP adapter is injected

### Requirement: Manual trigger only

Scraping MUST only run when explicitly invoked by a user action (endpoint or
tool call). Radicación, checklist evaluation, and routine SECOP sync flows
MUST NOT call the scraper automatically.

#### Scenario: User manually triggers exploration

- GIVEN a user identifies a platform-only document gap for their contract
- WHEN they invoke the manual scraper trigger
- THEN the scrape executes for that contract only

#### Scenario: Routine sync never scrapes

- GIVEN a routine `sincronizar_documentos_secop` run
- WHEN it completes
- THEN no scraper call was made as a side effect

### Requirement: Conservative per-user quota

The system MUST enforce a maximum of 3–5 scrapes per hour per user and a
~60-second execution budget per contract scrape.

#### Scenario: Within quota

- GIVEN a user has triggered fewer than the hourly limit
- WHEN they trigger another scrape
- THEN it proceeds

#### Scenario: Quota exceeded

- GIVEN a user has reached the hourly limit
- WHEN they trigger another scrape
- THEN the system MUST reject with a 429-style domain error before invoking
  the scraper adapter

### Requirement: Captcha-required is a distinct, non-retried state

When the adapter raises `CaptchaRequiredError`, the system MUST surface a
`requiere_intervencion` state carrying the manual action URL, and MUST NOT
automatically retry the scrape.

#### Scenario: Captcha detected

- GIVEN the scraper microservice returns 503 captcha-required
- WHEN the trigger handles the response
- THEN the caller receives a `requiere_intervencion` result with a manual
  link, and no automatic retry is scheduled

### Requirement: Fail-soft guarantee

Scraper failure (timeout, `ScraperUnavailableError`, captcha) MUST NEVER block
radicación, checklist evaluation, or any other flow that merely wanted the
extra document.

#### Scenario: Scraper unavailable during manual trigger

- GIVEN the scraper microservice is unreachable
- WHEN a user triggers a scrape
- THEN the triggering flow completes in a degraded state (no new docs) rather
  than failing the request

### Requirement: Persistence via the document pipeline contract

Scraped documents that are successfully retrieved MUST be persisted through
the existing document ingestion pipeline contract (content-hash dedup,
classification, checklist auto-link) — the same contract `document-upload`
exposes — not through a bespoke insert path.

#### Scenario: New scraped document persisted

- GIVEN the scraper returns a document not previously known
- WHEN it is persisted
- THEN it is deduped by content hash, classified, and auto-linked to any
  matching checklist requisito

#### Scenario: Scraped document duplicates an existing one

- GIVEN the scraper returns a document whose content hash matches an already
  stored document
- WHEN it is persisted
- THEN the pipeline recognizes the duplicate and does not create a second copy

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `explorar_documentos_plataforma_secop` | write | Manual trigger; quota-enforced; persists via document pipeline |

## Error Codes

| Code | Meaning |
|------|---------|
| `SECOP_SCRAPER_QUOTA_EXCEEDED` | Hourly per-user scrape quota reached |
| `SECOP_SCRAPER_CAPTCHA_REQUIRED` | Platform is captcha-gated; manual action needed |

## Deferred to sdd-design

- Exact quota constant/config name and storage (reuse vs. extend
  `secop_agentic_quota`).
- Endpoint route and MCP/tool registration wiring specifics.
- Selector hardening beyond conservative first-pass (explicitly out of scope
  per proposal).
- Sequencing: this capability's persistence wiring lands after
  `backend-local-first-sync` merges (it refactors
  `document_service.upload_document`).
