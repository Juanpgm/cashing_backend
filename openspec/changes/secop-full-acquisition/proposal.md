# Proposal: SECOP II Full Document & Dataset Acquisition

## Intent

Contracts do not acquire all their SECOP II documents. Users need every available
contract/process dataset wired and every public document downloadable. Today only 7
Socrata datasets feed acquisition; several public datasets are unwired, a complete
Playwright scraper is built but connected to nothing, there is no 429 retry/backoff,
and a suspected 2024 archive gap may be a mapping bug rather than a real gap.

## Scope

### In Scope
- Wire unwired Socrata datasets (schema-verified): `cb9c-h8sn` Adiciones, `e2u2-swiw`
  Modificaciones a Procesos, `gra4-pcp2` Ubicaciones ejecución.
- Live-probe and resolve the `secop_docs_gap_2024` false gap; fix archive year-range mapping.
- Wire the existing scraper end-to-end (deps → service → endpoint/tool → quota) as a
  fail-soft fallback document source for platform-only docs (pliegos, anexos, contrato firmado).
- Add 429 retry/backoff and `SECOP_APP_TOKEN` hygiene/validation.
- Expose new acquisition capability as tools in `app/tools/` registry.

### Out of Scope
- OCDS API (`apiocds.colombiacompra.gov.co`) — documented unstable; deferred pending a live probe.
- SECOP I legacy shards (`nj8y-g33j`) unless a trivial add for pre-2018 contracts.
- Questionable datasets `p8vk-huva` / `tb27-zmix` until field-diff proves distinct value.
- Scraper selector reverse-engineering hardening beyond conservative first-pass selectors.

## Capabilities

### New Capabilities
- `secop-dataset-ingestion`: additional contract/process datasets + archive year-range correctness.
- `secop-document-scraper`: end-to-end fail-soft scraper fallback for platform-only documents.
- `secop-acquisition-resilience`: 429 retry/backoff + app-token hygiene + partial-error surfacing.

### Modified Capabilities
- None (no existing `openspec/specs/`).

## Approach

Extend `_query_socrata` with the new dataset descriptors and confirmed join keys
(`id_contrato`, `proceso_de_compra`/`id_del_portafolio`, `referencia_del_contrato`).
Design Adición ingestion so the sibling `billing-resilience-templates` slice #4 can
consume `cb9c-h8sn`. Keep Socrata archives the bulk primary; invoke the scraper only
when a required doc is platform-only, honoring the 503 captcha-required contract and
`secop_agentic_quota` limits. Add bounded exponential backoff on Socrata 429.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `app/services/secop_service.py` | Modified | New datasets, year-range fix, retry/backoff |
| `app/adapters/secop_scraper/*` | Modified | Wire into deps + service |
| `app/api/deps.py` | Modified | Provide scraper adapter |
| `app/api/v1/secop*.py` | Modified | Scraper-fallback endpoint/tool |
| `app/core/secop_agentic_quota.py` | Modified | Enforce scraper quota |
| `app/tools/` | New | Expose acquisition tools |
| `alembic` migration `028+` | New | Only if new doc-source fields needed (rebase on sibling merge order) |

## Slice Plan (auto-chain, stacked-to-main, <400 lines each)

| # | Slice | Est. lines |
|---|-------|-----------|
| 1 | Resilience: 2024 live-probe + year-range fix + retry/backoff + token hygiene | ~250 |
| 2 | Wire Adiciones/Modif. Procesos/Ubicaciones (schema-verified) | ~350 |
| 3 | Wire scraper end-to-end, fail-soft + quota | ~380 |
| 4 | Expose acquisition tools in `app/tools/` | ~200 |

## Risks

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Scraper selector fragility / reCAPTCHA blocks | High | Fail-soft; scraper is fallback only; honor 503 contract + quota |
| New dataset schema drift (`cb9c-h8sn` key unverified) | Med | Per-slice live schema-verification task before wiring |
| 2024 gap is real, not a mapping bug | Low | Slice 1 live probe decides before code change |
| Migration number collision with siblings | Med | Start at 028+; rebase on merge order of 024/025-027 |
| ToS/volume for community.secop.gov.co | Low | Public records (Ley 1712/2014); verify ToS footer before scaling |

## Rollback Plan

Each slice is an independent stacked PR revertable in isolation. Dataset additions and
scraper wiring are behind non-destructive upsert + fail-soft paths, so reverting a slice
restores the prior 7-dataset behavior with no data loss (cache is regenerable).

## Dependencies

- `SECOP_APP_TOKEN` set in environment (verify).
- Standalone `secop-scraper` Chromium microservice reachable for Slice 3.
- Coordinate with `backend-local-first-sync` (migration 024, `document_service.upload_document`)
  and `billing-resilience-templates` (025-027, wants Adición data).

## Success Criteria

- [ ] Contracts acquire documents from all wired datasets + scraper fallback.
- [ ] 2024 archive coverage confirmed correct (gap resolved or proven false).
- [ ] Socrata 429s retried with backoff; partial errors surfaced via `datasets_con_error`.
- [ ] New acquisition tools registered in `app/tools/`.
- [ ] Adición data consumable by `billing-resilience-templates` slice #4.

## Review Workload Forecast

- Estimated total changed lines: ~1180 across 4 slices.
- 400-line budget risk: High (as single PR) → Low per stacked slice.
- Chained PRs recommended: Yes.
- Decision needed before apply: Yes — confirm stacked-to-main slice boundaries above.

## Proposal question round

Direct user Q&A was unavailable (sub-agent, no Engram). Assumptions to confirm in spec/design:
1. Is the scraper microservice deployed/reachable in the target env, or must Slice 3 gate behind a feature flag until it is?
2. Priority order — do users need the new datasets first, or the scraper fallback first (drives slice sequencing)?
3. Acceptable scraper latency/quota per contract for the manual "Exploración Agéntica" trigger?
4. Confirm SECOP I legacy (pre-2018) is truly out of scope for current users.
5. Should acquired platform-only docs be persisted via `document_service.upload_document` (coordination with 024) or kept as ephemeral links?
