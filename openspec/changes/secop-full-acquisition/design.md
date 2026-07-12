# Design: SECOP II Full Document & Dataset Acquisition

## Technical Approach

Extend the existing Socrata pipeline in `secop_service.py` (ports & adapters, non-destructive
upsert, JSON `datos_raw` cache) rather than adding new persistence. Three independent slices,
**datasets first, scraper last** (user decision #1). New Adiciones / Modificaciones-a-procesos /
Ubicaciones rows land as raw JSON on the EXISTING `secop_contratos`/`secop_procesos` `datos_raw`
columns under reserved keys, so **no migration** is owned by this change; sibling
`billing-resilience-templates` (migration 026) materializes the `adiciones_contrato` event table
by reading this change's accessor. The already-built scraper adapter is wired end-to-end as a
fail-soft fallback behind `SECOP_SCRAPER_ENABLED` (default False), sequenced AFTER
`backend-local-first-sync` merges so persistence targets its post-refactor
`_process_uploaded_document(db, doc, content, …)` contract.

## Architecture Decisions

| # | Decision | Alternatives rejected | Rationale |
|---|----------|-----------------------|-----------|
| D1 | Store new-dataset rows as raw JSON under reserved keys (`datos_raw["_adiciones"]`, `datos_raw["_modif_procesos"]`, `datos_raw["_ubicaciones"]`) on existing cache rows | New `secop_adiciones` table (migration) | Keeps this change migration-free; sibling owns the event table. Boundary: this change lands raw data + accessor, does NOT model events. |
| D2 | Boundary accessor `obtener_adiciones_contrato(db, id_contrato) -> list[dict]` reads `datos_raw["_adiciones"]` | Sibling queries Socrata itself | Single ingestion owner; sibling's migration 026 consumes the accessor, no duplicated fetch. |
| D3 | Schema verification via network-gated test (`@pytest.mark.live`, skipped in CI) run manually before wiring each dataset | Startup probe / first-call probe | Startup probe couples boot to datos.gov.co (breaks fail-soft); first-call probe adds per-request latency. Verify offline, wire constants. |
| D4 | 2024 gap resolved by a live probe (`fecha_carga between 2024-01-01..12-31`, `$limit=1`) against each `_ALL_DOCS_DATASETS`; if `dmgg-8hin` returns 2024 rows, fix the "desde 2025" comment and DELETE `secop_docs_gap_2024` logging | Blindly widen ranges / keep gap warning | Slice-1 probe decides on evidence before any code change; migration-free. **Deviation (2026-07-11)**: the live probe returned a third, partial outcome not enumerated above — `dmgg-8hin` DOES return 2024 rows, but all of them share one bulk-load date (`2024-12-31`), not full-year coverage. Resolution: keep the `secop_docs_gap_2024` warning (closer to "real gap" — most of 2024 is still uncovered) but correct the inaccurate "no public dataset exists" comment. See spec's third scenario ("Probe finds partial/anomalous coverage") and tasks.md Slice 1 "Deviation from D4". |
| D5 | Bounded exponential backoff + jitter INSIDE `_query_socrata` on 429/5xx (max 3 attempts, base 0.5s, full jitter) | Wrapper function; tenacity dep | Existing tests `patch.object(secop_service, "_query_socrata")` replace the whole fn, so folding retry inside preserves them; no new dependency. |
| D6 | Captcha surfaced as **domain state**, not HTTP 503 from the service | Raise 503 in service | Services never raise `HTTPException` (CLAUDE.md); fail-soft acquisition must still return Socrata results plus a `captcha_required` state + `manual_action_url`. |
| D7 | Separate scraper quota bucket: `SECOP_SCRAPER_HOURLY_LIMIT=5`, namespaced key in `secop_agentic_quota` | Reuse the 20/hr agentic bucket | Scraper is heavy/fragile; user decision #3 caps 3–5/hour/user independent of the lighter agentic trigger. |

## Data Flow

```
DATASETS (slices 1-2)
  numero/proceso ─→ _query_socrata(+retry) ─→ fan-out N datasets ─→ upsert (datos_raw JSON)
  cb9c-h8sn (id_contrato) ──────────→ SecopContrato.datos_raw["_adiciones"]  ─→ accessor ─→ [sibling 026]
  e2u2-swiw (id_del_portafolio) ────→ SecopProceso.datos_raw["_modif_procesos"]
  gra4-pcp2 (referencia_del_contrato)→ SecopContrato.datos_raw["_ubicaciones"]

SCRAPER (slice 3, flag ON, post local-first-sync)
  POST /secop/explorar-agentico ─→ enforce_scraper_quota(user) ─→ derive notice_uid from datos_raw
    ─→ SecopScraperPort.fetch_contract_docs (60s, wait_for) ─→ ScrapeResult | CaptchaRequiredError
    ─→ per doc: download bytes ─→ sha256 dedup ─→ upload_document → _process_uploaded_document (R2)
    ─→ ScraperFallbackResult{estado, docs, manual_action_url, datasets_con_error}
```

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/core/config.py` | Modify | Add `SECOP_SCRAPER_ENABLED=False`, `SECOP_SCRAPER_HOURLY_LIMIT=5`; empty-`SECOP_APP_TOKEN` startup warning surface |
| `app/services/secop_service.py` | Modify | 3 new dataset constants + descriptors; retry/backoff in `_query_socrata`; 2024 probe/fix; `obtener_adiciones_contrato` accessor; upsert new rows into `datos_raw` |
| `app/services/secop_scraper_service.py` | Create | Orchestration: quota, notice_uid derivation, `wait_for(60s)`, captcha→domain state, sha256 dedup, persist via `_process_uploaded_document` |
| `app/api/deps.py` | Modify | `get_secop_scraper()` → `SecopScraperHttpAdapter` if flag else `NullSecopScraperAdapter` |
| `app/api/v1/secop.py` | Modify | `POST /secop/explorar-agentico` (scraper fallback), maps `captcha_required` → 200 body |
| `app/core/secop_agentic_quota.py` | Modify | `enforce_scraper_quota(user_id)` — separate deque + `SECOP_SCRAPER_HOURLY_LIMIT` |
| `app/schemas/secop.py` | Modify | `ScraperFallbackResult` (estado enum, docs, manual_action_url) |
| `app/tools/catalog/secop.py` | Modify | `explorar_documentos_secop_agentico` (write), `sincronizar_documentos_secop` (write) tool wrappers |

## Interfaces / Contracts

```python
# secop_service.py
async def obtener_adiciones_contrato(db: AsyncSession, id_contrato: str) -> list[dict[str, Any]]: ...
# folded into existing _query_socrata (signature unchanged); retry on 429/5xx

# secop_scraper_service.py
async def explorar_documentos_agentico(
    db: AsyncSession, scraper: SecopScraperPort, user_id: uuid.UUID, numero_contrato: str,
) -> ScraperFallbackResult: ...

class ScraperFallbackResult(BaseModel):
    estado: Literal["ok", "captcha_required", "unavailable", "quota_exceeded"]
    documentos: list[SecopDocumentoResponse]
    manual_action_url: str | None = None
    datasets_con_error: list[str] = []
```

Persistence: reuse `document_service.upload_document(db, user_id, filename, content, content_type,
tipo=CONTRATO, contrato_id=...)`, which post-refactor delegates to `_process_uploaded_document`.
Dedup by `sha256` (column added by `backend-local-first-sync`); skip download+persist when a
`DocumentoFuente` with that hash already exists for the contrato.

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | Retry backoff on 429/5xx, bounded attempts, jitter | Mock `httpx` transport returning 429 then 200 |
| Unit | New datasets fan-out + `datos_raw` reserved keys; `obtener_adiciones_contrato` | `patch.object(_query_socrata)` per `test_secop_datasets.py` |
| Unit | 2024 probe branch: dataset covers 2024 → no gap log | Fake `_query_socrata` returning a 2024 row |
| Unit | Scraper service: captcha→`captcha_required` state; quota_exceeded; sha256 dedup skip; fail-soft on `ScraperUnavailableError` | `NullSecopScraperAdapter` + stub raising `CaptchaRequiredError`; `moto[s3]` for persist |
| Integration | `POST /secop/explorar-agentico` flag on/off; quota 429 | `httpx.AsyncClient`, `SECOP_SCRAPER_ENABLED` toggled |
| Live (manual, skipped) | Schema + join keys of `cb9c-h8sn`/`e2u2-swiw`/`gra4-pcp2`; 2024 coverage | `@pytest.mark.live` network test |

Strict TDD: write each test before the implementation slice.

## Migration / Rollout

- **Zero new migrations.** Dataset rows live in existing JSON `datos_raw`; scraper reuses
  `DocumentoFuente` + `sha256` from `backend-local-first-sync`. If a migration ever becomes
  unavoidable, number **028+** and rebase on the merge order of siblings 024–027.
- Slice sequencing: 1) resilience + 2024 fix, 2) datasets + Adición accessor, 3) scraper (flag
  default False; enable only after local-first-sync merges and scraper microservice reachable),
  4) tools.
- Rollback per slice: each is an independent stacked PR. Reverting restores prior 7-dataset
  behavior; cache is regenerable (upserts only, no deletes) so no data loss.

## Open Questions

None. Proposal open items resolved: #1 scraper gated behind `SECOP_SCRAPER_ENABLED` (D6/rollout);
#2 datasets first (sequencing); #3 60s budget + 5/hr scraper quota (D7); #4 SECOP I out of scope;
#5 scraped docs persist via `_process_uploaded_document` (user decision #2).
