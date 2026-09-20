# Apply Progress: Radicación Sin Fricción

**Change**: `radicacion-sin-friccion` · **Mode**: Strict TDD (proposal decision #5 — strict TDD plus named edge-case categories on every slice; injected by the orchestrator for apply and verify) · **Artifact store**: openspec
**Status**: 55/55 tasks complete (`tasks.md`: 46 original slices + 4.8 closed out + Phase 5 = 5.1–5.8). Nothing pending.
**Checkpoint**: backend `cashing-backend-master` `81aab19` (PR #95) · frontend `cashing-frontend` `11bf4c4` (PR #71). Suites at the checkpoint: backend **3329 passed, 1 skipped** (Postgres-only test); frontend vitest **1389 passed**; live stack journey 13/13, edge-cases 18 passed / 1 skipped, `radicacion-paquete-completo` passes.

## How to read this file (provenance)

This artifact was **written retroactively on 2026-09-19** (WARNING-4 of the first `sdd-verify` run: the change had a dense per-slice TDD narrative in `tasks.md` but no `apply-progress` and no tabular TDD evidence). It is a **transcription, not new evidence**: every cell is copied from what `tasks.md` and the merged PR bodies/commit messages state. Nothing was re-executed to produce it. Where a source is silent the cell says **not recorded**; it is never inferred. In particular:

- `RED` is marked `✅ Written` only where a source says the test was written first / was RED before the fix; a slice whose source only reports a passing suite has `not recorded`.
- `GREEN` carries the suite count the source reports, not a fresh run.
- `Safety Net` is `not recorded` unless a source names the pre-existing tests that were run before touching code.
- `Test File` lists only files a source names (for Phase 5, files named in the PR body or shown in the PR's diff stat); `not named in tasks.md` means the slice's evidence is a suite count and review narrative, and the covering files must be read from the repo (they exist; see `verify-report.md` §4 for the spec-to-test matrix).
- Suite counts mix two repos and change base between rows; they are point-in-time counts, not a single running total.

## TDD Cycle Evidence

Layer key: U = unit, I = integration / full-mount, E = Playwright against the live fake-LLM stack, — = no test layer.

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| 0.1 | — (workspace-root `start-local*.ps1`, not under git) | — | N/A | N/A (script repoint) | ✅ target path resolves | ➖ N/A | not recorded |
| 0.2 | `tests/test_query_counter_fixture.py` (5), `tests/test_query_budgets.py` (5) | U + I | N/A (new) | not recorded | ✅ suite 2401 passed, 4 skipped | ✅ nested SAVEPOINT counting, reset-between-calls, budget-failure message, listener-leak cleanup, Postgres skip guard | not recorded |
| 0.3 | `tests/test_agent_chat_observability.py` (6), `tests/test_litellm_adapter.py` (+5), `tests/test_main_log_level.py` (3) | U + I | not recorded | not recorded | ✅ suite 2405 passed, 4 skipped (run twice) | ✅ tool-error branch still timed; Langfuse-unreachable fail-open; two concurrent turns via real `asyncio.gather` (contextvars do not leak) | not recorded |
| 0.4 | `tests/test_fake_llm_adapter.py` (51 tests — the original "55" claim was corrected by review), parametrized over **32/32** catalog tools | U + I | not recorded | not recorded (first review NO-GO: the E2E asserted tool names, never `status`) | ✅ suite 2450 passed, 4 skipped, run twice, default order | ✅ 32-tool parametrization; case-insensitive `LLM_PROVIDER` folding; unguarded `model_validate` | ✅ leaking `monkeypatch` pattern removed in favour of `capture_logs()` |
| 0.5 | — (`ci.yml` in both repos) | — | N/A | N/A (workflow YAML) | ✅ both YAML files parse; frontend `npm run build` succeeds without Firebase env (15/15 static pages) | ➖ N/A | not recorded |
| 0.6 | `tests/test_fake_llm_adapter.py` (98 in file) | U + I | not recorded | not recorded | ✅ suite 2542 (+39) | ✅ result missing `id`; two tools returning the same key; malformed tool content; recap truncated; concurrent sessions do not share IDs | ✅ `resumen_checklist` docstring corrected, test rewritten against the real compact shape |
| 1.1 | `components/__tests__/checklist-full-view` (3), `components/stepper/__tests__/stepper-radicar.test.tsx` (9, full-mount) | I | not recorded | not recorded | ✅ 570/570 first; 584/584 after 4 review rounds | ✅ incomplete checklist, coherence failure, network failure, double-click, unknown code, `ResumenCard` no longer radicates | ✅ loading/error guard reordered (`yaRadicada` first); **untested by admission**: no discriminating test could be built for that last race |
| 1.2 | not named in `tasks.md` (files: `cuenta_cobro_service.py`; concurrency via `asyncio.gather`) | I | not recorded | not recorded | ✅ SQLite 2493 passed; real Postgres `make test-pg` 113 passed (pgvector/pg16) | ✅ two concurrent `radicar`, radicar-after-rechazada, radicar-after-pagada; Postgres-only identity-map bug found and fixed | not recorded |
| 1.3 | not named in `tasks.md` | I | not recorded | not recorded | ✅ suite 2503 (+4); no migration, so no Postgres run | ✅ REST and agent-tool surfaces both gated | not recorded |
| 1.4 | `lib/backend-error-codes.ts` checked-in list + a vitest that fails on any gap | U + I | not recorded | not recorded (the first regression net was found vacuous by mutation — deleting a code still passed 10/10 — and rewritten to `isKnownCode && !== GENERIC_CODE_MESSAGE`) | ✅ suite 584 → 617; `tsc` clean; build 15 pages | ✅ `{detail:[{foo:1}]}`, `{detail:[]}`, `codeMessage("constructor")`, HTML 502 | not recorded |
| 1.5a | `tests/test_exceptions.py` (17 new) | U | not recorded | not recorded | ✅ suite 2559 (+17) | ✅ new subclass, unmapped root `DomainError` → 500, two-mapped-bases order pinned via `mro.index()` | not recorded |
| 1.5b | not named (ESLint `no-restricted-syntax` rule proven live by reverting one fixed site; several migrated catch blocks have **no** dedicated content test — tracked) | I | not recorded | not recorded | not recorded (count) | ✅ `detail` as string / object / array / missing; network error with no response | not recorded |
| 1.6 | not named in `tasks.md` | U + I | not recorded | not recorded | ✅ suite 2599 (+22 from baseline) | ✅ marking no-aplica on an enviada cuenta rejects; cross-user ownership is 403 (pre-existing convention) | one test renamed to match its behaviour |
| 1.7 | not named in `tasks.md` | U + I | not recorded | not recorded | ✅ suite 2622 (+23) | ✅ 10-obligación/40-link discovery; expired handle; double-redeem; cross-user handle hijack rejected | not recorded |
| 1.8 | not named in `tasks.md` | U + I | not recorded | not recorded | ✅ suite 2686 (+64 over the slice) | ✅ deadlock reproduced by round 2; 36 tools' schemas checked for false-positive unlock | ✅ sticky recap line fixed for budget crowd-out |
| 1.9 | `tests/test_agente_cadena_completa.py` (extended; real `chat_with_tools` loop, `LLM_PROVIDER=fake`) | I | not recorded | not recorded | ✅ suite 2699 unchanged (test-only restructuring) | ✅ 4-turn realistic conversation; cap hit mid-playbook; failure on a mid-batch file | ✅ restructured after review (9 attached files in one turn exceeded `MAX_CHAT_FILES`) |
| 1.10 | frontend gate parsers (27 unit tests); backend `scripts/pre-merge.ps1` | U | not recorded | not recorded (a forced `GATE FAIL e2e` proved the stray-`test.only` fix) | ✅ frontend `GATE OK 06623f3 unit=671 …`; backend 2503 passed, coverage 86.24% | ✅ failing step never prints `GATE OK`; dirty tree reported; coverage below threshold fails | not recorded |
| 2.1 | not named in `tasks.md` | U | not recorded | not recorded | ✅ suite 2756 (+23) | ✅ exception propagation unchanged; no shared mutable state across the thread boundary | not recorded |
| 2.2 + 2.3 | not named in `tasks.md` | I | not recorded | not recorded | ✅ suite 2774 (+18) | ✅ third instance of the same bug class found by review (money decimal scale) and fixed | not recorded |
| 2.4a | `tests/test_raise_on_sql_slice_2_4a.py` | I | not recorded | not recorded | ✅ suite 2792 (+0, comment-only final commit) | ✅ exhaustive `rg` sweep separated real relationship reads from same-named attributes | ✅ misleading comment corrected in 3 model files |
| 2.4b | — (closed as no-op: zero production call sites) | — | N/A | N/A | N/A (no code changed) | ➖ N/A | N/A |
| 2.5 | not named in `tasks.md` | I | not recorded | not recorded | ✅ suite 2782 (+8) | ✅ two calls in one session hit the catalog cache (real query counter); `/refresh-secop` rollback path | ✅ `expired` check added on every cache read |
| 2.6 | not named in `tasks.md` | I | not recorded | not recorded (a new flushed-stub assertion caught a real bug in the first fix attempt) | ✅ suite 2801 → 2814 (+13); Docker Postgres dedup/concurrency/BackgroundTasks tests green | ✅ seen-set, unbounded fan-out bounded, rollback invariant | not recorded |
| 2.7 | not named in `tasks.md` (backend); frontend 30/30 vitest for the paired copy fix | I | not recorded | not recorded (BLOCKER regression test added after the r1 reviewer's reproduction script; reverted by hand → genuine failure, restored → green) | ✅ suite 2814 → 2825 (+11: 10 slice, +1 regression) | ✅ stale PENDING / RUNNING / FAILED probed by round 2 | ✅ backend `detail` made byte-identical to the frontend message |
| 2.8 | not named in `tasks.md` (backend +7; frontend +16 incl. a regression test locking invalidation-after-reveal) | U + I | not recorded | not recorded | ✅ backend 2825 → 2832 (+7); frontend 969 → 985 (+16) | ✅ invalidation-after-reveal regression test | ✅ `rootMargin: "200px"` |
| 3.1 | none new (pure refactor) | I + E | ✅ 882/882 vitest as the regression net; 27 live Playwright tests | N/A (no behaviour change) | ✅ 882/882 unchanged | ➖ N/A | ✅ 1672 → 421 lines (`checklist-full-view.tsx`), 983 → 657 (`stepper-shell.tsx`) |
| 3.2a | — (read-only audit) | — | N/A | N/A | N/A | ➖ N/A | N/A |
| 3.2 | not named in `tasks.md` | I + E | not recorded | not recorded | ✅ suite 882 → 899 (+17); 12 + 17 live Playwright tests | ✅ reabrir failure; loading/error vs genuine-zero on step 7; aprobada/pagada path byte-identical | ✅ orphaned `RadicarModoModal` deleted |
| 3.3 | `step-2-cuota.test.tsx` (10), `definir-checklist-gate.test.tsx` (7) | I | not recorded | ✅ malformed-inference crash reproduced as a genuine RED before the fix | ✅ 985 → 1002 (+17) at implementation; 985 → 1007 (+22) after review rounds | ✅ whitespace-only/emoji-only labels, accents, 50-char cap, already-inferred dedupe, zero-item inference | not recorded |
| 3.4 | files not named; `tasks.md` names the `ProgressBar` 8-case test, `clampGroup`/`GROUPS` tests, a dedicated `use-group-auto-advance` hook test and step-4/5/6 tests | I | ✅ existing tests broke against the new `FilaEnlace`/`PlantillaCard` before their migration (real regressions caught) | **weaker RED disclosed**: `ProgressBar` (component and 8-case test written together); step-6 ordering tests RED **not** literally reproduced (lower confidence); hook test for `activeGroup < 3` RED after mutation | ✅ 1007 → 1044 (+37) at implementation; → 1062 (+55) after reviews; `GATE OK 5c6f140` | ✅ `clampGroup`/`GROUPS`; per-member scroll+focus; `hidden={false}` gap closed with `not.toBeVisible()` | not recorded |
| 3.5 | unit files not named; e2e `radicacion-paquete-completo` Escenario 5 (new) | I + E | not recorded | not recorded (r2: 3 mutations turned tests RED; F2 tests mutation-proved) | ✅ 1062 → 1103 (+41); live Playwright 28/28; `GATE OK 03c3f18` | ✅ never-triggered cuenta polls nothing; trigger-fail confirmation refetch; stall bounds both pinned | ✅ redundant invalidation guard removed |
| 3.6 | not named in `tasks.md` | I + E | not recorded | not recorded | ✅ suite 899 → 920 (+21); 16 live Playwright tests | ✅ obligaciones-draft and dirty form at once; error inside `role="dialog"`; document-delete error does not leak across dialogs | not recorded |
| 3.7 | not named in `tasks.md` | I + E | not recorded | not recorded | ✅ suite 945 → 949 (+4); 10 live Playwright tests, zero new contrast violations | ✅ `cn()` executed for every folded `Button` call site | not recorded |
| 3.8 | not named (new permanent Playwright regression for the mobile `main` width; `movil-390px.spec.ts` un-skipped) | I + E | not recorded | not recorded (mutation proof: the new regression test fails at ~272px with the fix reverted) | ✅ suite 920 → 945 (+25); 10 live Playwright tests | ✅ nested "remove file" keydown must not re-open the picker; 4 dropzones keyboard-operable | not recorded |
| 3.9 | not named in `tasks.md` | I + E | not recorded | not recorded | ✅ suite 949 → 968 (+19); 15 live Playwright tests | ✅ 13 tú-form fixes each grammar-checked; e2e sweep of 22 old-copy fragments in 20 specs | not recorded |
| 3.10 | backend: SSE + approval-gate tests; frontend `e2e/journey/agent-dock-footer-overlap.spec.ts` (real bounding boxes at 4 viewports) | I + E | not recorded | not recorded | ✅ backend 2733 (+34); frontend unit 882 (+37), Playwright 74/20 (+4) | ✅ 7 adversarial SSE parsing cases; every R1 backend fix mutation-verified by R2 | ✅ first CTA-overlap fix disproved by a real-browser measurement and replaced |
| 4.1 | not named in `tasks.md` (12 new tests) | U | ✅ 3 existing tests refactored onto the builders | not recorded | ✅ suite 2571 (+12) | ✅ cascade confirmed by mutation test; uniqueness safe for 2571+ tests | not recorded |
| 4.2 | not named in `tasks.md` (scenarios named: double `crear_cuenta_cobro` same month, double classification enqueue, phantom insert) | I | ✅ slice 1.2's double-`radicar` suite re-run, still green | not recorded (the corrupted-`numero_cuota` test was mutation-tested by the reviewer; the phantom-insert retry test was found decorative in R2 and rebuilt) | ✅ suite 2792 → 2798 (4.2a) / 2798 (4.2b) → 2801 combined | ✅ race loser gets `CUENTA_MES_DUPLICADA` with credit rolled back; first-ever-trigger phantom insert | ✅ bare `except RuntimeError` narrowed to `UpsertJobRetryExhaustedError` |
| 4.3 | 5 new files: `tests/test_{gmail,drive,calendar}_adapter_failures.py`, `test_litellm_adapter_failures.py`, `test_evidence_discovery_failure_tolerance.py` (+ route-level tests) | U + I | not recorded | not recorded (tasks.md: the tests exposed real bugs, all fixed) | ✅ suite 2832 → 2974; `GATE OK dba0adf tests=2974 coverage=88%` | ✅ timeout / 4xx / 5xx / DNS / non-`OSError` transport / malformed base64 / `"data": null` / bad message in a batch; real `litellm.exceptions.*` through the 3-model chain | ✅ shared `_run()` wrapper, `google_errors.py`; `RefreshError` invariant pinned by a dedicated test |
| 4.4 | not named ("the validity suite") | U | not recorded | not recorded (a truncated-PDF mutation passed the 8-byte check → real trailer/`%%EOF` check added) | ✅ unit 794 (+16); `--list` 52/9 unchanged | ✅ per-fixture trailer check reusing the manifest's `corrupt` flag | not recorded |
| 4.5 | not named (unit coverage for `seedChecklist`, `seedObligaciones`, `fetchConReintento429`, register body) | U | not recorded | not recorded | ✅ unit 693 (+22); `--list` 52/9 | ✅ seeder bodies mutation-tested; register body keys; 429 retry | ✅ 5 specs de-duplicated onto the helpers |
| 4.6 | not named (static-grep regression net pinning 7 journey-critical testids) | U | not recorded | not recorded (regression net mutation-verified) | ✅ suite 842 (+7); `--list` 52/9 unchanged | ✅ additive: all 20 existing spec-referenced testids untouched | not recorded |
| 4.7a | `e2e/journey/**` (6 specs) + backend `RATE_LIMIT_ENABLED` tests (6, TDD, mutation-verified) | E + U | not recorded | ✅ backend toggle: "TDD, 6 tests, mutation-verified" (frontend specs: not recorded) | ✅ journey 28/28 (3 full runs); backend 2575 passed (+4); frontend unit 842 | ✅ two real spec-layer races found and fixed | not recorded |
| 4.7b | `e2e/edge-cases/**` (10 specs) | E | not recorded | not recorded | ✅ edge-cases 16/16 real assertions, 2 deliberately skipped; journey 28/28 re-run; unit 845 | ✅ 4 real axe violations found and fixed; case 8 gated behind `E2E_FAKE_LLM_SCRIPT_READY=1` | not recorded |
| 4.8 | — (gate scripts + hook + PR template; `pre-merge.ps1` bug found by dogfooding) | — | N/A | N/A | ✅ backend `GATE OK 754387d tests=2833 coverage=87% … dirty=no`; frontend `GATE OK 5645519 unit=1062 e2e=7 … dirty=no` | ➖ N/A | ✅ `dirty=yes` bug (`$null -ne ""`) fixed |
| 4.9 | not named (27 guard unit tests); `e2e/smoke/prod.spec.ts` | U + E | not recorded | not recorded | ✅ unit 644; mocked specs 7; remote smoke vs real prod API 2 passed / 5 skipped without credentials | ✅ method-override spread order, host-checked login exception, `/auth/refresh`, credential-gated skips | ✅ per-project `testMatch` after R2 NO-GO |
| 5.1 | not named in the PR body | not recorded | not recorded | **not recorded** | ✅ `GATE OK 4235707 tests=3235 coverage=88% ruff=517 mypy=355` | ✅ named in PR: query-budget starvation, dedup collapse, stopword scoring, LLM-relevance bypass, first-bracket JSON parser, failure-fallback score | not recorded |
| 5.2 | not named in the PR body; commit body records fixture repairs (`posicion` defaulted every test cuenta to RECURRENTE) | I | ✅ commit body: 4 pre-existing `informe_service` tests and the `radicacion_prep` fixtures re-pointed | **not recorded** | ✅ `GATE OK 8cd6f68 tests=3039 coverage=88% ruff=512 mypy=338`; `280fd25` test-only isolation commit | ✅ no-first-cuota fail-safe, tombstoned first cuota, `heredado` row, custom mapping to CONTRATO, settled-cuenta delete refusal (end-to-end reproduction) | not recorded |
| 5.3 | from the PR's diff stat: `section-model.test.ts` (509 lines), `staged-section-list.test.tsx` (1340), `contexto-section.test.tsx` (649), `contexto-omitido.test.ts`, `reduced-motion.test.ts`, `stepper-shell.test.tsx`, `steps.test.ts`, `use-group-auto-advance.test.tsx`, step-4/5/6 tests, `requisito-row.test.tsx`, `checklist-shared.test.ts` | I + E | not recorded | ✅ PR body: every fix TDD'd, failing regression test first (per-test detail **not recorded**) | ✅ `GATE OK 9b29498 unit=1330 e2e=7 …`; journey 9/9 consecutive live runs | ✅ rounds 4–7 each added a regression test for the previous round's own fix; round 7 closed the class (8 mutable refs → one predicate) | ✅ round 3 replaced five completeness helpers with `section-model.ts` |
| 5.4 | `app/cuentas-cobro/[id]/__tests__/page.test.tsx` (`it.each`: borrador/rechazada shown with `href=/radicar/{id}`; enviada/aprobada/pagada hidden) | I | not recorded | **not recorded** | ✅ 5 new `it.each` cases (gate 1330 at PR #68 head; the `sdd-verify` run at `7af14b5` counted 1335) | ✅ 2 + 3 estado cases | ➖ N/A |
| 5.5 | `tests/test_evidence_classification_concurrency.py`, `tests/test_paquete_job_service.py` | I | not recorded | ✅ PR body: strict TDD in every round, RED first | ✅ `GATE OK 8f9bf9f tests=3330 coverage=88% ruff=526 mypy=357`; suite 3329 passed, 1 skipped at `81aab19` | ✅ stale PENDING equal-total, stale RUNNING, genuine two-session interleaving, winner-finished-job, payload nulling | ✅ two SUGGESTIONs applied in round 3 |
| 5.6 | `lib/__tests__/checklist-api.test.ts`, `components/ui/__tests__/progreso-etapas.test.tsx`, `step-6-justificacion.test.tsx` | U + I | not recorded | ✅ 15 tests RED first, confirmed by mutation (both fixes reverted → 15 fail) | ✅ `GATE OK d9708d9 unit=1355 e2e=7 … lint=backlog(3e/8w)` | ✅ 422 array / object / junk `detail`; 429 unchanged; `CHECKLIST_LINK_FAILED` byte-identical | ✅ one duplicate test removed |
| 5.7 | from the PR's diff stat: `e2e/helpers/seed.test.ts` (+272), `step-2-cuota.test.tsx`, `vitest.setup.ts` | U + I + E | not recorded | ✅ 15 tests RED before the floor; double-click guard removal fails deterministically ("called 2 times") | ✅ `GATE OK 4b21643 unit=1389 e2e=7 …`; journey 13/13, paquete-completo + edge-cases 26 passed / 1 skipped | ✅ checklist fetch 4xx/5xx, non-JSON, `requisitos_definidos=false`, empty, malformed item, none of the tipos present, four-code floor, custom requisitos | ✅ `asyncUtilTimeout: 5000`; microtask flush before the call-count assertion |
| 5.8 | `components/stepper/__tests__/contraste.test.ts`, `lib/__tests__/api.test.ts` | U | not recorded | N/A (timing fix: the before/after stress protocol is the evidence, no RED test) | ✅ `GATE OK b19179f unit=1389 e2e=7 …` | ✅ 64 burners: contraste 18/40 → 0/40 (A), 4/8 → 0/8 (B); `api.test.ts` 0/48 before and after (justified by mechanism only) | ✅ dead `vi.resetModules()` removed |

### Test Summary

- **Final suites**: backend 3329 passed, 1 skipped at `81aab19`; frontend 1389 passed at `11bf4c4`; mocked Playwright 7 (inside the gate); live journey 13/13; live edge-cases 18 passed / 1 skipped; `radicacion-paquete-completo` passes.
- **Layers used**: unit and integration/full-mount (pytest, vitest with `@testing-library/react`), E2E (Playwright: 2 mocked specs in the gate, 6 journey + 10 edge-case specs against a live `LLM_PROVIDER=fake` backend, one read-only production smoke spec).
- **Approval tests (refactoring)**: 3.1 used the existing vitest suite (882/882) as its net; 2.2/2.3 and 2.4a relied on the existing suites plus new targeted tests.
- **Rows where RED is `not recorded`**: 0.2, 0.3, 0.4, 0.6, 1.1, 1.2, 1.3, 1.4, 1.5a, 1.5b, 1.6, 1.7, 1.8, 1.9, 1.10, 2.1, 2.2 + 2.3, 2.4a, 2.5, 2.6, 2.7, 2.8, 3.2, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7b, 4.9, 5.1, 5.2, 5.4 — for these the source states a passing suite, a mutation proof and/or a review verdict but not a test-first cycle. This is a recording gap, not evidence that TDD was skipped.

## Mutation-testing and review record (from `tasks.md` and PR bodies)

`tasks.md` promoted "every regression net must be mutation-tested" into `design.md`'s Testing Strategy after three slices shipped green-but-vacuous tests (0.4, 1.1, 1.4).

| Task | Mutation / stress proof recorded | Fresh-context review rounds and verdicts |
|------|----------------------------------|------------------------------------------|
| 0.2 | not recorded | 1 round: GO (2 findings folded in) |
| 0.3 | not recorded | 1 round: CONDITIONAL GO (3 findings fixed) |
| 0.4 | round 3 deliberately broke F-1/F-4/F-5 and confirmed each test fails; disproved own hypothesis empirically | R1 NO-GO (first of the plan), R2 fix pass, R3 GO on substance |
| 0.6 | not recorded | CONDITIONAL GO (dead alias, silent fallback fixed) |
| 1.1 | blockers 2 and 3 mutation-confirmed (fix reverted → new tests fail) | R1 NO-GO, R2 NO-GO (narrow), R3, R4 GO |
| 1.2 | not recorded | R1 CONDITIONAL GO, R2 GO, R3 GO |
| 1.3 | agent-tool fix mutation-verified | CONDITIONAL GO |
| 1.4 | net found a no-op by mutation (delete a code → 10/10 passed) and rewritten | R1 CONDITIONAL GO, R2 GO |
| 1.5a | C3 linearization traced independently | GO, no findings |
| 1.5b | 1.5b-i and 1.5b-ii bugs mutation-confirmed; rule proven live | 1.5b-i GO |
| 1.6 | tool guard confirmed by mutation | CONDITIONAL GO |
| 1.7 | handle-hijack attack tested and rejected | GO |
| 1.8 | round 2 reproduced the deadlock, confirmed the fix by execution and mutation | R1 found severe bug, R2 re-review |
| 1.9 | not recorded | R1 CONDITIONAL GO |
| 1.10 | forced `GATE FAIL e2e` for a stray `test.only` | frontend CONDITIONAL GO, backend GO |
| 2.1 | not recorded | GO |
| 2.2 / 2.3 | not recorded | review found a third instance of the same bug class |
| 2.4a | not recorded (exhaustive `rg` sweep instead) | GO |
| 2.5 | not recorded | GO |
| 2.6 | seen-set disabled by hand (real duplicate row created), rollback invariant twice | 2 passes, CONDITIONAL GO both |
| 2.7 | BLOCKER regression test reverted and re-run by hand; reviewer mutation with its own scratchpad plugin | R1 CONDITIONAL GO, R2 GO |
| 2.8 | query-key mutation made the invalidation test fail | GO (backend), GO (frontend) |
| 3.1 | mechanical multiset-diff of extracted files (not a mutation) | GO |
| 3.2 | not recorded | GO |
| 3.3 | four hand mutations across two rounds | R1 GO, R2 narrow (found the `VARCHAR(50)` gap) |
| 3.4 | 6 reviewer mutations; two hand mutations | r1 CONDITIONAL GO, r2 narrow |
| 3.5 | r2: 3 mutations RED; F2 tests mutation-proved | r1 CONDITIONAL GO, r2 |
| 3.6 | reviewer mutation-tested the covering tests as non-vacuous | CONDITIONAL GO |
| 3.7 | not recorded | CONDITIONAL GO |
| 3.8 | regression test failed at ~272px with the fix reverted | NO-GO (first pass) |
| 3.9 | not recorded | CONDITIONAL GO |
| 3.10 | R2 mutation-verified every R1 backend fix | backend R1 + R2 GO; frontend review found 2 CRITICAL |
| 4.1 | cascade confirmed by a mutation test | GO |
| 4.2 | corrupted-data test and phantom-insert retry mutation-tested | R1 CONDITIONAL GO, R2, R3 fix pass |
| 4.3 | r1: 5 mutations; r3: 6 mutations | R1 CONDITIONAL GO, R2 CONDITIONAL GO, R3 GO |
| 4.4 | truncated-fixture mutation | CONDITIONAL GO |
| 4.5 | seeder bodies mutation-tested | GO |
| 4.6 | regression net mutation-verified | GO |
| 4.7a | backend toggle mutation-verified | (review folded into the PR) |
| 4.7b | not recorded | CONDITIONAL GO |
| 4.9 | not recorded | R1 CONDITIONAL GO, R2 NO-GO |
| 5.1 | not recorded | 3 rounds (two judges + skeptic pass, opus); verdict per round not stated |
| 5.2 | not recorded | 4 rounds: R1 NO GO, R2 CONDITIONAL GO, R3 found BLOCKER; round 4 not recorded |
| 5.3 | not recorded in the PR body | 7 rounds (two judges + skeptic pass, opus); per-round verdict not stated |
| 5.4 | not recorded | not recorded (direct commit) |
| 5.5 | PR body: "mutation-proved" in every round | r1 CONDITIONAL GO, r2 GO |
| 5.6 | 15 tests RED, both fixes reverted → 15 fail | GO (7 SUGGESTIONs, 4 applied) |
| 5.7 | double-click guard removal fails deterministically; 15 tests RED before the floor | CONDITIONAL GO on commit 1; commits `1bc3e8a`, `4b21643` **not re-reviewed** |
| 5.8 | injected-defect checks and a 64-burner stress protocol | none (test-only, stated in PR body) |

## Deviations from Design

None recorded by the apply phase — `design.md` D1–D12 were verified as implemented as designed by the first `sdd-verify` run. Design amendments made on 2026-09-19 (D11 updated to `revealMode: "staged"`, new D13 and D14) reflect merged work, not deviations.

## Issues Found

Open, non-blocking, all tracked in `tasks.md` and the specs' Out-of-Scope sections:

- **Group-2 height** is an open USER decision (`design.md` Open Questions); not decided by any artifact.
- 5.5 leaves the D13 fix proven on SQLite only (no real-Postgres run), no heartbeat during the classification LLM batch, and the same identity-map trap at `secop_service.py:1443-1448`.
- Backend `ruff`/`mypy` backlog above the 4.3 baseline (`ruff=526 mypy=357` versus 501/336); frontend lint backlog `3e/8w`.
- Frontend load-flaky tests near vitest's 5000 ms budget under CPU starvation (5.8 follow-ups).
- `npm run gate -- --live-e2e` not built; live Playwright remains a manual runbook.
- Unverifiable in this change: the 3-user unmoderated test (Phase 3 exit criterion), the Neon-dev checklist-under-1s measurement (Phase 2), and the Engram-side timing baselines (Phase 0).

## Remaining Tasks

None. 55/55 `[x]` in `tasks.md`.

## Workload / PR Boundary

- Mode: stacked-to-main PRs, one per numbered slice (proposal decision #3); Phase 5 follows the same convention (backend #93/#94/#95, frontend #68/#69/#70/#71) plus one direct frontend commit (`7af14b5`) and one direct backend test-only commit (`280fd25`).
- Delivery: each slice gated locally before push (`GATE OK <sha> …`), GitHub Actions being billing-locked.
- `size:exception`: not recorded as used. Several Phase 5 PRs are far above 400 lines (#68 +9,241/−779; #93 +7,598/−318; #94 +3,401/−128) — the PR bodies do not mention a size exception or a split decision.

## Status

55/55 tasks complete. Ready for `sdd-verify`.
