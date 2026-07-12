# Tasks: SECOP II Full Document & Dataset Acquisition

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~250 / ~350 / ~380 / ~200 (4 slices, ~1180 total) |
| 400-line budget risk | Low per slice / High as single PR |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 (resilience) → PR 2 (datasets) → PR 3 (scraper wiring) → PR 4 (scraper persistence) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Resilience: 2024 probe, retry/backoff, token warning | PR 1 → main | No dependency; foundation for 2-4 |
| 2 | Datasets: Adiciones/Modif.Procesos/Ubicaciones + accessor | PR 2 → main | Depends on PR 1 (retry wraps `_query_socrata`) |
| 3 | Scraper wiring end-to-end, no persistence | PR 3 → main | Independent of PR 2; flag default False |
| 4 | Scraper persistence via `_process_uploaded_document` | PR 4 → main | **Blocked** on `backend-local-first-sync` merge |

## Reconciliation: Spec vs Design (tool names & error codes)

- `sincronizar_documentos_secop` (write) — unchanged, extended internally (Slice 2).
- `obtener_estado_datasets_secop` (read) — spec-only, not contradicted by design; added in Slice 2.
- Scraper trigger tool: spec proposed `explorar_documentos_plataforma_secop`; **design's final name is `explorar_documentos_secop_agentico`** (matches existing "agentic" convention: `secop_agentic_quota.py`, `SECOP_AGENTIC_HOURLY_LIMIT`, `POST /secop/explorar-agentico`). Design wins — use `explorar_documentos_secop_agentico` (Slice 3).
- `verificar_configuracion_secop` (read) — spec-only, no conflicting design name; added in Slice 1, mirroring the `/health/llm` diagnostic pattern but exposed as a tool, not a new endpoint.
- `SECOP_SCRAPER_QUOTA_EXCEEDED` — reconciled to design D7: raised via `enforce_scraper_quota()` (mirrors `enforce_agentic_quota`), reuses `RateLimitExceededError` → HTTP 429. Not a distinct exception class.
- `SECOP_SCRAPER_CAPTCHA_REQUIRED` — reconciled to design D6: **not raised** as an error. Surfaced as `ScraperFallbackResult.estado="captcha_required"` in a 200 response with `manual_action_url`.

## Cross-Change Coordination

- **billing-resilience-templates slice #4** consumes `obtener_adiciones_contrato(db, id_contrato) -> list[dict]` (Slice 2, Task 2.6). Contract: reads `SecopContrato.datos_raw["_adiciones"]`, returns contract link + `valor_adicion` + effective date. Do not change this signature without notifying that change.
- **Slice 4 is blocked** on `backend-local-first-sync` merging `_process_uploaded_document` + `DocumentoFuente.sha256`. See blocker task 4.1.

---

## Slice 1: Resilience Foundation (2024 probe, retry/backoff, token warning)

PR title: `fix(secop): resolve 2024 archive gap and harden Socrata acquisition`
Est. lines: ~250 | Spec: `secop-acquisition-resilience` + one requirement from `secop-dataset-ingestion`

- [x] 1.1 [RED] Add `@pytest.mark.live` test (`tests/test_secop_datasets.py`) probing `_DS_DOCS_2025` (`dmgg-8hin`) for `fecha_carga` in 2024; skipped in CI.
- [x] 1.2 Run the live probe manually; record result (real gap vs. mapping bug) in PR #1 description.

  **Live probe evidence (recorded 2026-07-11, direct `curl` against `www.datos.gov.co/resource/*.json`):**

  | Query | Result |
  |---|---|
  | `dmgg-8hin` (`_DS_DOCS_2025`), `fecha_carga` between 2024-01-01 and 2024-12-31, `$select=count(*)` | `24565` rows |
  | `dmgg-8hin`, same range, `$select=fecha_carga, count(*) $group=fecha_carga $order=fecha_carga` | **single value**: `2024-12-31` → `24565` (ALL 2024-tagged rows share one bulk-load date) |
  | `dmgg-8hin`, `fecha_carga` in 2025-01 only, `$select=count(*)` | `4,333,282` rows (confirms the dataset's real bulk coverage is 2025+) |
  | `3skv-9na7` (`_DS_DOCS_2023`), `fecha_carga` between 2024-01-01 and 2024-12-31, `$select=count(*)` | `0` rows (confirmed: does not cover 2024 at all) |
  | `3skv-9na7`, 2023-06 sanity check | request timed out (network) — not required for the conclusion below |

  **Conclusion**: neither binary scenario in spec/D4 applies cleanly — this is a **partial** result. `dmgg-8hin` is NOT gap-free for 2024 (it holds exactly one bulk-upload day, 2024-12-31, not full-year coverage), so the `secop_docs_gap_2024` warning is **NOT disproven** and is **KEPT** (counter-scenario: "Probe confirms a real gap ... surfaced as known limitation"). However the existing comment `"desde 01/01/2025 (2024 gap: no public dataset exists)"` is factually inaccurate — some 2024-dated docs ARE retrievable (and were already being fetched, since `dmgg-8hin` is already part of `_ALL_DOCS_DATASETS`). Per D4 "act only on the probe's result": the comment is corrected to describe the real nuance; the gap-preserved log branch is left in place with clarified wording. See Slice 1 "Deviation from D4" note below.
- [x] 1.3 [RED] Test asserting year-range fix or gap-preserved branch, per probe result.
- [x] 1.4 [GREEN] `app/services/secop_service.py`: fix `_DS_DOCS_2025` comment (line ~49) and conditionally drop/keep `secop_docs_gap_2024` log (line ~573) per D4.
- [x] 1.5 [RED] `tests/test_secop_service.py`: mock httpx returning 429 then 200 — `_query_socrata` retries, returns data, no error.
- [x] 1.6 [RED] Mock httpx returning 429/5xx every attempt — `_query_socrata` raises `ExternalServiceError` after bounded attempts (no hang).
- [x] 1.7 [GREEN] `_query_socrata` (line ~100): add bounded exponential backoff + full jitter (max 3 attempts, base 0.5s) per D5, preserving the `patch.object` test seam.
- [x] 1.8 [RED] Extend `tests/test_secop_datasets.py` partial-result test — fan-out still returns `datasets_con_error` after one dataset exhausts retries.
- [x] 1.9 [RED] Test: empty `SECOP_APP_TOKEN` at startup does not raise; warning is queryable.
- [x] 1.10 [GREEN] `app/core/config.py`: non-blocking validation/log warning for empty `SECOP_APP_TOKEN`.
- [x] 1.11 [RED] Test: `secop_service.verificar_configuracion_secop()` reports token presence/health.
- [x] 1.12 [GREEN] Add `verificar_configuracion_secop()` to `secop_service.py`.
- [x] 1.13 `app/tools/catalog/secop.py`: register `verificar_configuracion_secop` tool (read).
- [x] 1.14 Mirror-update: re-run `tests/test_secop_datasets.py`, `tests/test_secop_sincronizar.py` full files — confirm no regression from retry wiring.
- [x] 1.15 Verification gate: `.venv\Scripts\ruff.exe check .` + `uv run python -m pytest tests/test_secop_service.py tests/test_secop_datasets.py -v`, then full suite `uv run python -m pytest`.
- [ ] 1.16 **NO PUSH without explicit user OK.** Prepare PR #1 (chain diagram 📍PR1, base=main, rollback=revert commit range).

## Slice 2: New Datasets Wiring (Adiciones, Modif. Procesos, Ubicaciones)

PR title: `feat(secop): wire Adiciones, Modificaciones a Procesos and Ubicaciones datasets`
Est. lines: ~350 | Spec: `secop-dataset-ingestion` (minus 2024 requirement)
Depends on: Slice 1 merged (retry wraps all `_query_socrata` calls below).

- [ ] 2.1 [Manual, not CI] `@pytest.mark.live` schema test: `cb9c-h8sn` exposes `id_contrato`; `e2u2-swiw` exposes `proceso_de_compra`/`id_del_portafolio`; `gra4-pcp2` exposes `referencia_del_contrato`. Run before wiring; record result in PR #2.
- [ ] 2.2 [RED] `tests/test_secop_datasets.py`: dataset with schema missing its join key → marked unavailable, appended to `datasets_con_error`, logged — never silently dropped.
- [ ] 2.3 [GREEN] `secop_service.py`: add `_DS_ADICIONES = "cb9c-h8sn"`, `_DS_MODIF_PROCESOS = "e2u2-swiw"`, `_DS_UBICACIONES = "gra4-pcp2"` constants + schema-presence guard per dataset.
- [ ] 2.4 [RED] Test: `e2u2-swiw` row joins via `proceso_de_compra`, falls back to `id_del_portafolio`; `gra4-pcp2` joins via `referencia_del_contrato`; no matching key → zero rows, no crash.
- [ ] 2.5 [GREEN] `secop_service.py`: fan-out queries for the 3 datasets using confirmed join keys, following `buscar_documentos_contrato` multi-key pattern (line ~418); non-destructive upsert into `SecopContrato.datos_raw["_adiciones"]` / `SecopContrato.datos_raw["_ubicaciones"]` / `SecopProceso.datos_raw["_modif_procesos"]` (D1).
- [ ] 2.6 [RED] Test: `obtener_adiciones_contrato(db, id_contrato)` returns contract link, `valor_adicion`, effective date from `datos_raw["_adiciones"]` without querying Socrata.
- [ ] 2.7 [GREEN] Implement `obtener_adiciones_contrato()` accessor (D2) — this is the contract consumed by `billing-resilience-templates` slice #4.
- [ ] 2.8 [RED] Test: cached rows survive a failed/empty refresh for the 3 new datasets (non-destructive upsert, D1).
- [ ] 2.9 `app/tools/catalog/secop.py`: register `obtener_estado_datasets_secop` tool (read) surfacing `datasets_con_error` + per-dataset schema-verification status.
- [ ] 2.10 Mirror-update: `tests/test_secop_datasets.py` fan-out assertions to include the 3 new dataset ids; `tests/test_secop_sincronizar.py` if `sincronizar_documentos_secop` signature/output changed.
- [ ] 2.11 Verification gate: ruff + `uv run python -m pytest tests/test_secop_datasets.py tests/test_secop_service*.py -v`, then full suite.
- [ ] 2.12 **NO PUSH without explicit user OK.** Prepare PR #2 (📍PR2, base=main, rollback=revert commit range; cache regenerable).

## Slice 3: Scraper End-to-End Wiring (no persistence)

PR title: `feat(secop): wire scraper fallback end-to-end behind SECOP_SCRAPER_ENABLED`
Est. lines: ~380 | Spec: `secop-document-scraper` (D6, D7) — persistence deferred to Slice 4.

- [ ] 3.1 `app/core/config.py`: add `SECOP_SCRAPER_ENABLED: bool = False`, `SECOP_SCRAPER_HOURLY_LIMIT: int = 5`.
- [ ] 3.2 [RED] Test: flag off → `get_secop_scraper()` returns `NullSecopScraperAdapter`; flag on + URL/token set → returns `SecopScraperHttpAdapter`.
- [ ] 3.3 [GREEN] `app/api/deps.py`: add `get_secop_scraper()` per D-adapter-selection rule.
- [ ] 3.4 [RED] `tests/test_secop_agentic_quota.py`-style test: `enforce_scraper_quota(user_id)` allows ≤`SECOP_SCRAPER_HOURLY_LIMIT`/hour, rejects the next with `RateLimitExceededError` before invoking the adapter.
- [ ] 3.5 [GREEN] `app/core/secop_agentic_quota.py`: add `enforce_scraper_quota()` — separate deque namespaced from the existing 20/hr agentic bucket (D7).
- [ ] 3.6 [RED] New `tests/test_secop_scraper_service.py`: captcha → `ScraperFallbackResult(estado="captcha_required", manual_action_url=...)`, no automatic retry.
- [ ] 3.7 [RED] Same file: `ScraperUnavailableError`/timeout → fail-soft, `estado="unavailable"`, triggering flow completes degraded (no exception raised to caller).
- [ ] 3.8 [GREEN] Create `app/services/secop_scraper_service.py`: `explorar_documentos_agentico(db, scraper, user_id, numero_contrato)` — quota check, derive `notice_uid` from `datos_raw`, call `fetch_contract_docs` (60s), map `CaptchaRequiredError`/`ScraperUnavailableError` to `estado`.
- [ ] 3.9 `app/schemas/secop.py`: add `ScraperFallbackResult` (`estado: Literal["ok","captcha_required","unavailable","quota_exceeded"]`, `documentos`, `manual_action_url`, `datasets_con_error`).
- [ ] 3.10 [RED] Integration test: `POST /secop/explorar-agentico` — flag off returns empty/no-op; flag on + quota exceeded returns 429; captcha maps to 200 body.
- [ ] 3.11 [GREEN] `app/api/v1/secop.py`: add `POST /secop/explorar-agentico`, manual trigger only (never called from `sincronizar_documentos`/checklist flows).
- [ ] 3.12 `app/tools/catalog/secop.py`: register `explorar_documentos_secop_agentico` tool (write) — reconciled name (see Reconciliation).
- [ ] 3.13 Mirror-update: `tests/test_secop_scraper_adapter.py` (adapter unchanged, re-run); `tests/test_secop_api_agentic.py` if agentic quota tests share fixtures.
- [ ] 3.14 Explicitly stub/skip persistence in this slice: on `estado="ok"`, return fetched doc metadata only — do NOT call any upload/persistence path yet (Slice 4 wires that).
- [ ] 3.15 Verification gate: ruff + `uv run python -m pytest tests/test_secop_scraper_adapter.py tests/test_secop_agentic_quota.py tests/test_secop_service_agentic.py -v`, then full suite.
- [ ] 3.16 **NO PUSH without explicit user OK.** Prepare PR #3 (📍PR3, base=main, rollback=revert; flag defaults False so no behavior change until enabled).

## Slice 4: Scraper Persistence (BLOCKED on `backend-local-first-sync`)

PR title: `feat(secop): persist scraped documents via document ingestion pipeline`
Est. lines: ~200 (folded into scope; adjust vs. proposal's Slice 4 "tools" line — tools already registered per-slice above) | Spec: `secop-document-scraper` persistence requirement.

- [ ] 4.1 **BLOCKER CHECKPOINT** — verify `backend-local-first-sync` has merged to main and both `_process_uploaded_document(db, doc, content, …)` and `DocumentoFuente.sha256` exist in `app/services/document_service.py` / `app/models/`. If either is missing: **STOP this slice**, report status to the user, do not proceed.
- [ ] 4.2 [RED] `tests/test_secop_scraper_service.py`: new document (unseen sha256) → downloaded, deduped by hash, persisted via `document_service.upload_document(...)` → `_process_uploaded_document`, classified, auto-linked to matching checklist requisito.
- [ ] 4.3 [RED] Same file: document whose content hash matches an existing `DocumentoFuente` → recognized as duplicate, no second copy created, no re-download of bytes beyond the hash check.
- [ ] 4.4 [GREEN] `secop_scraper_service.py`: per-doc download bytes → sha256 → skip if `DocumentoFuente` with that hash exists for the contrato → else `upload_document(...)`.
- [ ] 4.5 Verify `moto[s3]` mocks cover the new upload path (reuse existing document_service test fixtures).
- [ ] 4.6 Mirror-update: `tests/test_secop_scraper_adapter.py` unaffected; run full `document_service` test file to confirm no regression from the new caller.
- [ ] 4.7 Verification gate: ruff + `uv run python -m pytest tests/test_secop_scraper_service.py -v`, then full suite.
- [ ] 4.8 **NO PUSH without explicit user OK.** Prepare PR #4 (📍PR4, base=main, rollback=revert; only reachable when `SECOP_SCRAPER_ENABLED=True`).

---

## Review Workload Forecast (Per-Slice Recap)

| Slice | PR | Est. lines | Risk | Blocked by |
|-------|-----|-----------|------|------------|
| 1 | Resilience | ~250 | Low | none |
| 2 | Datasets | ~350 | Low | Slice 1 merge |
| 3 | Scraper wiring | ~380 | Low-Med | none (independent of Slice 2) |
| 4 | Scraper persistence | ~200 | Low | `backend-local-first-sync` merge (see 4.1) |

Decision needed before apply: No (auto-chain, stacked-to-main already resolved). Apply proceeds slice-by-slice; each PR requires explicit user OK before push.
