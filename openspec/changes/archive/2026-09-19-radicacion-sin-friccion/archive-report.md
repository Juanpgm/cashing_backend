# Archive Report: Radicación Sin Fricción

**Change**: `radicacion-sin-friccion` · **Archived**: 2026-09-19 · **Phase**: sdd-archive (final closure)

**Artifact store**: openspec · **Project**: cashing · **Execution**: automatic with mandatory reporting

## Executive Summary

The "Radicación Sin Fricción" change has been successfully completed, tested, and archived. All 55 tasks are marked complete [x]. Verification returned PASS WITH WARNINGS (0 CRITICAL, 4 WARNING, 4 SUGGESTION). The implementation spans 5 phases across backend and frontend, with two major code merges and supporting test infrastructure. The change closes the gap between existing domain logic and a fully functional end-to-end wizard that allows users to radicate a cuenta de cobro safely and idempotently.

## Verification Summary

**Final Verdict**: PASS WITH WARNINGS (2026-09-19)
- **Status**: All 55 tasks complete [x]
- **CRITICAL issues**: 0
- **WARNING items**: 4 (ruff/mypy drift tracked; 3 NEW items disclosed by the artifacts)
- **SUGGESTION items**: 4 (mostly process/traceability notes)
- **UNVERIFIABLE exit criteria**: 3 (Neon checklist <1s, 3-user unmoderated study, Phase 0 baselines)
- **Recommendation**: Proceed to archive (no blockers)

See `verify-report.md` and `verify-report.2026-09-18.md` (first run) for full details.

## Specs Merged into Main Specs

Six delta specs have been merged into `openspec/specs/{domain}/spec.md`:

| Domain | Requirements | Scenarios | Status | Details |
|--------|--------------|-----------|--------|---------|
| `radicacion-flow` | 7 | 19 | CREATED | End-to-end wizard flow, idempotent radicar, error handling, concurrency |
| `agent` | 7 | 11 | CREATED | Agent observability, fake LLM, escape-hatch tools, evidence handles, budgets |
| `stepper-ux` | 7 | 21 | CREATED | 3-group wizard, staged rail, 3 screens, entry points, mobile/a11y |
| `performance-jobs` | 6 | 13 | CREATED | Query budgets, job polling, stale reset race-safety, HTTP client sharing |
| `resilience` | 5 | 8 | CREATED | Google adapter error mapping, revoked grants, malformed payloads, LLM chain |
| `quality-gate` | 6 | 9 | CREATED | Local pre-merge gate, test builders, synthetic fixtures, edge-case suite, prod smoke |

**Total**: 38 requirements, 81 scenarios declared. **81/81 scenarios have covering tests** (76 executed in this session, 4 reported from live Playwright, 1 static-only).

## Implementation Checkpoint

**Backend**: `cashing-backend-master` `81aab19` (PR #95) — original checkpoint was `a730acf` (PR #92); Phase 5 adds 9 commits post-checkpoint
**Frontend**: `cashing-frontend` `11bf4c4` (PR #71) — original checkpoint was `db45c9f` (PR #67); Phase 5 adds 4 commits + 1 direct commit post-checkpoint

### Test Suites at Checkpoint
- Backend: **3329 passed, 1 skipped** (Postgres-only test), 15 deselected (live-network tests)
- Frontend: **1389 passed** (77 test files)
- E2E mocked (gate): **7 passed** (2 files)
- E2E live backend (reported, not re-run): journey **13/13**, edge-cases **18 passed / 1 skipped**, radicacion-paquete-completo passes
- Backend coverage: **88%** (exceeds 70% floor)
- Backend lint/type backlog: ruff 526, mypy 357 (both non-blocking; drifted from 4.3 baseline)
- Frontend lint: **3 errors, 8 warnings** (down from 5e/8w post-PR #69); `tsc` clean

## Architecture Decisions Implemented

All 14 design decisions from `design.md` are implemented and verified:

| D# | Decision | Status | Notes |
|----|----------|--------|-------|
| D1 | Radicar short-circuit + lock-safe with CAS | ✅ IMPLEMENTED | `populate_existing` reload fix found and applied (identity-map staleness, reproduced on SQLite; the `FOR UPDATE` row-lock half is NOT executed anywhere: PG suite never ran) |
| D2 | `requisitos_modo` NULL tri-state, no migration | ✅ IMPLEMENTED | Audit overturned the plan; gates work correctly |
| D3 | Package job async-first | ✅ IMPLEMENTED | `POST /paquete/regenerar-async` returns 202 with polling |
| D4 | Per-row paquete status visibility-gated | ✅ IMPLEMENTED | `useInView` IntersectionObserver pattern |
| D5 | Shared `httpx.AsyncClient` per event loop | ✅ IMPLEMENTED | Graph token client stays per-call (no user cookie leaks) |
| D6 | Local pre-merge gate is official | ✅ IMPLEMENTED | GitHub Actions billing-locked; `.githooks/pre-push` enforces gate |
| D7 | `LLM_PROVIDER=fake` deterministic seam | ✅ IMPLEMENTED | Stateless, outcome-aware routing, real ID threading |
| D8 | One error-code contract, MRO walk, lint-enforced | ✅ IMPLEMENTED | `lib/backend-error-codes.ts` + ESLint ban; 0 problems on `lib/**` |
| D9 | `raise_on_sql` + per-session catalog memo | ✅ IMPLEMENTED | Budget reductions verified (checklist 47→36) |
| D10 | Evidence-based unlock + handle-based persistence | ✅ IMPLEMENTED | 5,604 tokens/iteration (down from 10,659) |
| D11 | 3 groups, staged group 2, one `computeSectionModel` | ✅ IMPLEMENTED | `revealMode: "staged"`, `members: [4, 6, 3, 5]` |
| D12 | Frontend never re-derives gate outcomes | ✅ IMPLEMENTED | Backend's `radicar` gate is authoritative |
| D13 | Job-row stale reset race-safe | ✅ IMPLEMENTED (SQLite verified) | `populate_existing`, explicit `updated_at`, payload nulling; PG half unverified |
| D14 | First-cuota applicability shared seam | ✅ IMPLEMENTED | Migration 043 + 5 consumers use single rule |

## Phase 5 Post-Checkpoint Work

Work merged after the original checkpoint (`a730acf` / `db45c9f`) is documented in Phase 5 (tasks 5.1–5.8) and includes:

- **5.1 (PR #93)**: Evidence discovery ranking, evidence expansion/caching, query budgeting (+7,598 backend lines)
- **5.2 (PR #94)**: First-cuota identity documents, settled-cuenta guard, step-5 informes gate, migration 043 (+3,401 backend lines)
- **5.3 (PR #68)**: Staged rail, screen 2 refactor, Contexto section, section model, CSS tokens, mobile refinement (+9,241 frontend lines)
- **5.4 (direct commit)**: Account detail page primary "Completar en el wizard" link for borrador/rechazada
- **5.5 (PR #95)**: Race-safe job stale reset fix (populate_existing + explicit updated_at) for both paquete and classification jobs (+689 backend lines)
- **5.6 (PR #69)**: ESLint `lib/**` coverage for `.detail` reader enforcement, 2 error fixes
- **5.7 (PR #70)**: E2E helpers and test resilience (seed.test.ts +272 lines, 2 additional tests)
- **5.8 (PR #71)**: Timing fix for starvation-prone tests; kept in wall-clock budget

All Phase 5 commits have SHAs, diff stats, and PR bodies reconciled in `tasks.md`.

## Known Tracked Follow-ups (Not Blockers)

### Process / Traceability Items (newly disclosed)

1. **NEW-1**: PR #93 (+7,598 lines) has no spec requirement — recorded as "traceability only" in tasks.md:283
2. **NEW-2**: Four Phase 5 PRs (#68, #93, #94, #70) exceeded 400-line budget; no recorded `size:exception`
3. **NEW-3**: Three merged changes (#5.4 direct commit, #5.7/#5.8 test-only) skipped mandatory fresh-context reviews

### Non-Blocking Known Items (already in artifacts)

- **W5**: Ruff/mypy backlog drift (501→526 / 336→357); non-blocking by CI design, documented in 3 places
- **S4**: `QueryCounter.assert_budget` is upper-bound only; floor mode not implemented
- **5.1 PG suite**: Not run (Docker daemon down); SQLite tests prove identity-map fix, not lock serialization
- **Secop service**: `secop_service.py:1443-1448` has same identity-map trap on second locking select (no `populate_existing`)
- **App clock skew**: Stale reset writes `updated_at` from app clock while others use `func.now()` (cosmetic at 120s threshold)
- **scripts/kill-local.ps1**: `$pid` assignment read-only on PowerShell 7
- **E2E load flakiness**: `checklist-full-view.test.tsx` "chunks > 20 files" test load-flaky near vitest budget (passed this run)
- **Group 2 height**: OPEN USER DECISION — not decided, recorded in design.md, stepper-ux spec, tasks.md

### Follow-ups Tracked in Specs' Out of Scope

- Gating checklist mutators and PATCH by estado (P1, from 1.6)
- DB constraint for numero_cuota / posicion=primera
- cruzar / semaforo manual re-analysis in wizard
- No heartbeat during LLM batch in _ejecutar_clasificacion
- ESLint `.detail` selector narrowing
- Nested `BaseModel` response_format in fake LLM; Langfuse enablement
- Authenticated prod smoke run (needs user credentials)
- Live-backend Playwright as `npm run gate -- --live-e2e` (runbook only)

## Exit Criteria Status

| Phase | Criterion | Status |
|-------|-----------|--------|
| 0 | CI green; baselines recorded; `LLM_PROVIDER=fake` turn completes | **PASS (superseded: no GitHub Actions)** |
| 1 | Wizard radicates without step 3 button; agent playbook green | **PASS** |
| 2 | Query budgets green; doc gen off event loop; checklist <1s Neon | **PASS / UNVERIFIABLE** (Neon timing: no recorded measurement) |
| 3 | 3-user unmoderated test | **UNVERIFIABLE** (no recorded evidence) |
| 4 | Playwright green CI; no external network; edge cases green | **PARTIAL/REPORTED** (mocked: 7 tests green; live: reported 13/13 + 18 passed / 1 skipped) |
| 5 | No Phase 0–4 state change | **PASS** (5.6 closed accessible-name gap in Phase 4; nothing else moved) |

**Tally**: 8 PASS, 2 PASS-superseded, 1 PARTIAL-superseded, **3 UNVERIFIABLE** (Neon timing, 3-user study, Engram Phase 0 baselines).

## Open User Decisions (Recorded, Not Decided Here)

1. **Group 2 height**: Under `revealMode: "all"` group 2 spanned 5800–7100 px; choice is collapse / accordion / keep — staged rail mitigates but decision stays open (design.md Open User Decision, stepper-ux spec)
2. **Synthetic `pending` on `GET /paquete/job`**: Client refuses to poll never-triggered accounts; spec allows but doesn't require this behavior
3. **Langfuse enablement**: Undeclared dependency; `LLM_PROVIDER` detection logs a warning but doesn't fail
4. **Prod-smoke user inputs**: Authenticated smoke run needs explicit URL + credentials
5. **Deferred cruzar/semaforo trigger**: Manual re-analysis not attempted (3.2a, deferred)

## Archive Contents

This directory contains the full immutable record:

- `proposal.md` — Original proposal with decisions and scope (retroactively reflects Phase 5)
- `design.md` — Architecture decisions, file changes, interfaces, testing strategy
- `specs/` — All 6 delta specs (now also in `openspec/specs/{domain}/spec.md`)
- `tasks.md` — 55 numbered slices with full history, PRs, SHAs, commit reconciliation
- `apply-progress.md` — TDD cycle evidence table (55 rows), retroactively written, honest "not recorded" entries
- `verify-report.md` — Final verification report (this session, 2026-09-19)
- `verify-report.2026-09-18.md` — First verification run (superseded by above)

The change folder has been moved from `openspec/changes/radicacion-sin-friccion/` to `openspec/changes/archive/2026-09-19-radicacion-sin-friccion/` on 2026-09-19.

## Mergeability Assessment

**Status**: Archived. The verify verdict is PASS WITH WARNINGS: no CRITICAL findings, 4 tracked non-blocking WARNINGs.

All implementation work is complete, tested (on SQLite + mocked e2e + a live local stack) and merged to master in both repos. This report does NOT assert deployment readiness: production was not tested by this change (project doctrine, `docs/prod-test-plan.md`: nothing that writes business data runs against production), the deployed version in production is unknown (health reports 0.2.1, same as local), and the real-PostgreSQL suite was never run (Docker was down), so the `FOR UPDATE` row-lock half of backend PR #95 is unverified by execution. Deploying is a separate, explicit user decision.

## Traceability

**Persistence**: the files in this folder are the source of truth (artifact store: openspec). Engram (project `cashing-backend`) holds only: the master checkpoint `sdd/radicacion-sin-friccion/state`, `sdd/radicacion-sin-friccion/verify-report` (2026-09-19 verdict summary) and `sdd/radicacion-sin-friccion/verify-warnings-fix`. Proposal, spec, design, tasks, apply-progress and this archive report were NOT mirrored to Engram (hybrid store only partially populated).

## Next Steps

1. Optionally schedule follow-up work:
   - Neon dev checklist timing measurement (<1s exit criterion)
   - 3-user unmoderated usability study
   - Docker PG suite run (`scripts/pre-merge.ps1 -IncludePg`) to verify row-lock half of PR #95
2. Decide group 2 height (user decision, not a blocker)
3. Address optional lint/type backlog items per team policy
4. Before any deployment: run the PG suite (item 1, third bullet) and decide explicitly whether to deploy; production currently runs an unknown build (health `version` is 0.2.1 in both prod and local, so it cannot identify the commit)

---

**Archived by**: sdd-archive agent · **Date**: 2026-09-19 · **Status**: CLOSED
