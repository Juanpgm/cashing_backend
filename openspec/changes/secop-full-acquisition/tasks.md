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

  #### Deviation from D4

  D4 was written expecting a binary probe outcome ("gap-free" vs. "real gap").
  The live result is a **third, partial** outcome that D4 did not enumerate:
  the dataset returns 2024-dated rows, but they are all a single bulk-load
  batch (`2024-12-31`), not genuine full-year coverage. Per D4's own guidance
  to "act only on the probe's result" rather than the letter of its two
  scenarios, this was resolved as: keep the `secop_docs_gap_2024` warning
  (closer in spirit to "real gap"), but correct the inaccurate "no public
  dataset exists" comment (since some 2024 rows genuinely are retrievable).
  This partial-result nuance is now captured as a third scenario in
  `specs/secop-dataset-ingestion/spec.md` ("Probe finds partial/anomalous
  coverage") and a matching note in `design.md` D4, so future probes with a
  similarly ambiguous result have a documented precedent instead of forcing a
  binary choice (Slice 2, task 2.10c).
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

- [x] 2.1 [Manual, not CI] `@pytest.mark.live` schema test: `cb9c-h8sn` exposes `id_contrato`; `e2u2-swiw` exposes `proceso_de_compra`/`id_del_portafolio`; `gra4-pcp2` exposes `referencia_del_contrato`. Run before wiring; record result in PR #2.

  **Live schema probe evidence (recorded 2026-07-11, `curl` + Socrata views metadata API against `www.datos.gov.co`):**

  | Dataset | Assumed key(s) | Live result | Verdict |
  |---|---|---|---|
  | `cb9c-h8sn` (Adiciones) | `id_contrato` | `id_contrato` IS present. Real title: "SECOP II - Modificaciones a Contratos". Fields: `identificador, id_contrato, tipo, descripcion, fecharegistro` — **no structured value field**; `tipo` distinguishes real value-additions via literal `"ADICION EN EL VALOR"` (not just `"ADICION"`). | Join key confirmed. **Deviation**: `valor_adicion` best-effort-parsed from free-text `descripcion` (`_extraer_valor_adicion_texto`), may be `None`. |
  | `e2u2-swiw` (Modif. Procesos) | `proceso_de_compra` / `id_del_portafolio` | Real fields are `portafolio` (format `CO1.BDOS.xxx`, matches our `proceso_de_compra`/`id_proceso_secop`) and `proceso` (format `CO1.REQ.xxx`, a different id space we don't track). Socrata description: *"Última fecha de Modificación para procesos que han sido objeto de cambio en su definición en los últimos 8 días"* — **rolling 8-day window**, not a historical archive. | **Deviation**: field names corrected to `portafolio`/`proceso`; query value still falls back between our own `proceso_de_compra` and `id_proceso_secop` (the fallback CONCEPT holds, only the Socrata field name was wrong). Zero rows for most contracts is expected (outside the 8-day window), not an error. |
  | `gra4-pcp2` (Ubicaciones) | `referencia_del_contrato` | Present exactly as assumed, plus bonus `id_contrato`/`proceso_de_compra`/`urlproceso` fields. | Confirmed, no deviation. |

  Encoded as a real pytest live test: `tests/test_secop_datasets.py::test_live_probe_nuevos_datasets_schema` (run manually with `-m live`, passed).
- [x] 2.2 [RED] `tests/test_secop_datasets.py`: dataset with schema missing its join key → marked unavailable, appended to `datasets_con_error`, logged — never silently dropped. (`TestNuevosDatasetsSchemaGuard`)
- [x] 2.3 [GREEN] `secop_service.py`: add `_DS_ADICIONES = "cb9c-h8sn"`, `_DS_MODIF_PROCESOS = "e2u2-swiw"`, `_DS_UBICACIONES = "gra4-pcp2"` constants + schema-presence guard (`_dataset_has_join_key`, `_query_dataset_guarded`) per dataset.
- [x] 2.4 [RED] Test: `e2u2-swiw` row joins via `portafolio` (real field; preferring contrato's `proceso_de_compra`, falling back to proceso's `id_proceso_secop` — see 2.1 deviation), `gra4-pcp2` joins via `referencia_del_contrato`; no matching key → zero rows, no crash. (`test_no_matching_key_yields_zero_rows_no_crash`)
- [x] 2.5 [GREEN] `secop_service.py`: `_fetch_nuevos_datasets_contrato` fans out to the 3 datasets using the confirmed (and corrected, per 2.1) join keys; non-destructive upsert (`_upsert_nuevos_datasets_raw`) into `SecopContrato.datos_raw["_adiciones"]` / `SecopContrato.datos_raw["_ubicaciones"]` / `SecopProceso.datos_raw["_modif_procesos"]` (D1), wired into `sincronizar_documentos_secop` (confirmar=True only).
- [x] 2.6 [RED] Test: `obtener_adiciones_contrato(db, id_contrato)` returns contract link, `valor_adicion`, effective date from `datos_raw["_adiciones"]` without querying Socrata. (`TestObtenerAdicionesContrato`)
- [x] 2.7 [GREEN] Implement `obtener_adiciones_contrato()` accessor (D2) — this is the contract consumed by `billing-resilience-templates` slice #4. Returns `{id_contrato_secop, numero_contrato, valor_adicion, fecha_efectiva, raw}`; `valor_adicion` may be `None` (see 2.1 deviation).
- [x] 2.8 [RED] Test: cached rows survive a failed/empty refresh for the 3 new datasets (non-destructive upsert, D1). (`TestNuevosDatasetsNonDestructiveUpsert`)
- [x] 2.9 `app/tools/catalog/secop.py`: register `obtener_estado_datasets_secop` tool (read) surfacing `datasets_con_error` + per-dataset schema-verification status. Implemented as `secop_service.obtener_estado_datasets_secop(db, cedula)`, offline (D3) — reads `datos_raw["_dataset_errors"]`/presence of `_adiciones`/`_ubicaciones`/`_modif_procesos` recorded by the most recent sync, does not itself query Socrata.
- [x] 2.10 Mirror-update: `tests/test_secop_datasets.py` fan-out assertions extended (schema-guard + accessor + non-destructive-upsert test classes added); full `tests/test_secop_sincronizar.py` + `tests/test_secop_service_documentos.py` re-run — 0 regressions (confirmar=False preview mode untouched by design since the new fan-out is gated on confirmar=True).
- [x] 2.10b [RED+GREEN] Surface the coverage gap to callers: `secop_service.obtener_documentos_con_cobertura()` wraps `buscar_documentos_contrato` (via a new optional `datasets_con_error_out` out-param, default `None`/fully backward-compatible) and returns `SecopDocumentosConCoberturaResponse{documentos, cobertura{gap, razon (Spanish), url_proceso}}`. Gap flag = 2024-window OR non-empty `datasets_con_error`. Wired into `GET /secop/documentos/{numero_contrato}` (response_model changed from `list[SecopDocumentoResponse]` to the new wrapper — the internal `buscar_documentos_contrato()` service function itself is UNCHANGED for its other callers: `consulta_completa`, the `secop_client` agent tool). (Live evidence 2026-07-11: contract 4161.010.26.1.2189.2024 legitimately yields exactly 2 docs from open data — ACTA DE CIERRE pdf + 1 modification stub whose file is a Marketplace-internal ID with no URL; early-lifecycle docs are platform-only.)
- [x] 2.10c Pre-slice cleanup from Slice 1 verify WARNINGs: added the missing "Deviation from D4" note near the Slice 1 probe task (see above); ran `ruff format` on the two retry tests in `tests/test_secop_service.py` (clean); added a third scenario ("Probe finds partial/anomalous coverage") to `specs/secop-dataset-ingestion/spec.md` and a matching note to `design.md` D4.
- [x] 2.11 Verification gate: `ruff check` + `ruff format --check` (scoped to touched files — repo-wide `ruff check .` has 987 pre-existing unrelated errors, confirmed via `git stash` baseline of 990) + `uv run python -m pytest tests/test_secop_datasets.py tests/test_secop_service*.py tests/test_secop_sincronizar.py tests/test_tool_catalog.py -v` (113 passed), then full suite: **1221 passed / 14 deselected** (was 1205/13 before this slice; +16 new tests, +1 new live-marked test → 14 deselected). `mypy` on touched files: 7 pre-existing errors (identical to `git stash` baseline), 0 new (one candidate new error from `asyncio.gather(..., return_exceptions=True)` + tuple-unpack was fixed by narrowing on `BaseException` instead of `Exception`).
- [ ] 2.12 **NO PUSH without explicit user OK.** Prepare PR #2 (📍PR2, base=main, rollback=revert commit range; cache regenerable).

## Slice 3: Scraper End-to-End Wiring (no persistence)

> **EXECUTION ORDER CHANGE (user decision 2026-07-11)**: this slice is PROMOTED ahead of Slice 2 — the user's priority is maximum document acquisition ("todo lo posible por obtener todos los documentos"), and live diagnosis of contract 4161.010.26.1.2189.2024 proved the missing documents are platform-only (scraper territory). Slice 3 has no technical dependency on Slice 2. Execution order: 1 → 3 → 2 → 4.

PR title: `feat(secop): wire scraper fallback end-to-end behind SECOP_SCRAPER_ENABLED`
Est. lines: ~380 | Spec: `secop-document-scraper` (D6, D7) — persistence deferred to Slice 4.

- [x] 3.1 `app/core/config.py`: add `SECOP_SCRAPER_ENABLED: bool = False`, `SECOP_SCRAPER_HOURLY_LIMIT: int = 5`.
- [x] 3.2 [RED] Test: flag off → `get_secop_scraper()` returns `NullSecopScraperAdapter`; flag on + URL/token set → returns `SecopScraperHttpAdapter`.
- [x] 3.3 [GREEN] `app/api/deps.py`: add `get_secop_scraper()` per D-adapter-selection rule.

  **Deviation note**: kept `app.adapters.secop_scraper.get_secop_scraper()` (URL/token-only factory, already tested by `test_secop_scraper_adapter.py::TestFactory`) UNCHANGED to avoid regressing its existing tests. Added a new `get_secop_scraper_gated()` in the same adapters module (flag check → delegates to the unflagged factory) and made `app/api/deps.py::get_secop_scraper()` a thin wrapper around it. This keeps adapter-selection logic out of the FastAPI-coupled `deps.py` (consistent with `app/tools/catalog/paquete.py` importing adapter factories directly from `app.adapters.*`, not from `deps.py`) while still satisfying "app/api/deps.py: add get_secop_scraper()" literally.
- [x] 3.4 [RED] `tests/test_secop_agentic_quota.py`-style test: `enforce_scraper_quota(user_id)` allows ≤`SECOP_SCRAPER_HOURLY_LIMIT`/hour, rejects the next with `RateLimitExceededError` before invoking the adapter.
- [x] 3.5 [GREEN] `app/core/secop_agentic_quota.py`: add `enforce_scraper_quota()` — separate deque namespaced from the existing 20/hr agentic bucket (D7).
- [x] 3.6 [RED] New `tests/test_secop_scraper_service.py`: captcha → `ScraperFallbackResult(estado="captcha_required", manual_action_url=...)`, no automatic retry.
- [x] 3.7 [RED] Same file: `ScraperUnavailableError`/timeout → fail-soft, `estado="unavailable"`, triggering flow completes degraded (no exception raised to caller).
- [x] 3.8 [GREEN] Create `app/services/secop_scraper_service.py`: `explorar_documentos_agentico(db, scraper, user_id, numero_contrato)` — quota check, derive `notice_uid` from `datos_raw` (parses `?noticeUID=` out of the cached `urlproceso`), call `fetch_contract_docs` (60s via `asyncio.wait_for`), map `CaptchaRequiredError`/`ScraperUnavailableError`/timeout to `estado`. Missing/undiscoverable `notice_uid` also degrades to `estado="unavailable"` (not in the original task list but required for fail-soft completeness — covered by a dedicated test).
- [x] 3.9 `app/schemas/secop.py`: add `ScraperFallbackResult` (`estado: Literal["ok","captcha_required","unavailable","quota_exceeded"]`, `documentos`, `manual_action_url`, `datasets_con_error`).

  **Deviation note**: added one additive field beyond the design table, `notas: str | None = None`, to carry a human-readable reason for non-captcha degraded states (e.g. "no notice_uid could be derived") — the design's literal field list had no place for this and an unexplained `estado="unavailable"` would be a worse caller experience. Does not change any existing field's meaning.
- [x] 3.10 [RED] Integration test: `POST /secop/explorar-agentico` — flag off returns empty/no-op; flag on + quota exceeded returns 429; captcha maps to 200 body.
- [x] 3.11 [GREEN] `app/api/v1/secop.py`: add `POST /secop/explorar-agentico`, manual trigger only (never called from `sincronizar_documentos`/checklist flows).
- [x] 3.12 `app/tools/catalog/secop.py`: register `explorar_documentos_secop_agentico` tool (write) — reconciled name (see Reconciliation).
- [x] 3.13 Mirror-update: `tests/test_secop_scraper_adapter.py` (adapter unchanged, re-run — 9/9 still green); `tests/test_secop_api_agentic.py` if agentic quota tests share fixtures.

  **Note**: `tests/test_secop_service_agentic.py` and `tests/test_secop_api_agentic.py` referenced by this task and 3.15 do not exist in the repo (only stale `__pycache__` entries from a prior branch/checkout) — there is no existing "agentic" HTTP endpoint or API test to mirror-update; `enforce_agentic_quota` itself is currently unused outside its own quota test. Skipped as not-applicable; new coverage lives in `tests/test_secop_api_scraper.py` instead.
- [x] 3.14 Explicitly stub/skip persistence in this slice: on `estado="ok"`, return fetched doc metadata only — do NOT call any upload/persistence path yet (Slice 4 wires that). Verified by `test_success_returns_doc_metadata_no_persistence` (asserts zero rows written to `secop_documentos`).
- [x] 3.15 Verification gate: ruff + `ruff format --check` + `uv run python -m pytest tests/test_secop_scraper_adapter.py tests/test_secop_agentic_quota.py tests/test_secop_scraper_deps.py tests/test_secop_scraper_service.py tests/test_secop_api_scraper.py tests/test_secop_scraper_tool.py -v`, then full suite. **Result: 1187 → 1204 passed (+17 new), 0 regressions, 13 deselected (unchanged); ruff check clean; ruff format clean; mypy: 0 new errors on touched files (2 pre-existing errors unrelated to this slice, confirmed via `git stash` diff).**
- [x] 3.16 **NO PUSH without explicit user OK.** Prepare PR #3 (📍PR3, base=main, rollback=revert; flag defaults False so no behavior change until enabled). Commits prepared locally, not pushed: `3a72092` (feat: wire scraper fallback), `e544dc2` (docs), `6a4f51a` (fix: fail-soft on non-enumerated adapter errors — slice 3 verify WARNING 1).

## Slice 4: Scraper Persistence (BLOCKED on `backend-local-first-sync`)

PR title: `feat(secop): persist scraped documents via document ingestion pipeline`
Est. lines: ~200 (folded into scope; adjust vs. proposal's Slice 4 "tools" line — tools already registered per-slice above) | Spec: `secop-document-scraper` persistence requirement.

- [ ] 4.1 **CHECKPOINT (relaxed per user decision 2026-07-11 — maximize acquisition)** — check whether `backend-local-first-sync` has merged (`_process_uploaded_document(db, doc, content, …)` + `DocumentoFuente.sha256` present). PREFERRED PATH if merged: persist via `_process_uploaded_document` with sha256 dedup (tasks 4.2-4.4 as written). FALLBACK PATH if NOT merged: proceed anyway using the current `upload_document(...)` entry point and its existing filename+tipo+contrato dedup (design's documented fallback); add a follow-up task line to re-point to `_process_uploaded_document`+sha256 once sync merges. Record which path was taken in this file. Do NOT modify `upload_document`'s body in either path (that function belongs to the sync change).
- [ ] 4.2 [RED] `tests/test_secop_scraper_service.py`: new document (unseen sha256) → downloaded, deduped by hash, persisted via `document_service.upload_document(...)` → `_process_uploaded_document`, classified, auto-linked to matching checklist requisito.
- [ ] 4.3 [RED] Same file: document whose content hash matches an existing `DocumentoFuente` → recognized as duplicate, no second copy created, no re-download of bytes beyond the hash check.
- [ ] 4.4 [GREEN] `secop_scraper_service.py`: per-doc download bytes → sha256 → skip if `DocumentoFuente` with that hash exists for the contrato → else `upload_document(...)`.
- [ ] 4.4b [RED+GREEN] DocumentId caching (captcha-free refetch, live-probe finding 2026-07-11): persist every SECOP `DocumentId` + its `/Public/Archive/RetrieveFile/Index?DocumentId={id}` URL seen from ANY source (scraper results, Socrata archive rows, modification stubs) into cached SECOP data (`datos_raw["_document_index"]` or equivalent — no migration). `RetrieveFile` is confirmed captcha-free: a document indexed once NEVER needs the scraper again. Refetch path must try the cached DocumentId URL BEFORE invoking the scraper (true fallback ordering).
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
