# Verification Report: Radicación Sin Fricción (re-verification)

**Change**: `radicacion-sin-friccion`
**Phase**: sdd-verify (2nd run, re-verification) · **Artifact store**: openspec · **Mode**: Strict TDD
**Verified**: 2026-09-19
**Repos verified at**: backend `cashing-backend-master` @ `81aab19` (clean) · frontend `cashing-frontend` @ `11bf4c4` (clean)
**Checkpoint claimed by the artifacts**: backend `81aab19` (PR #95) · frontend `11bf4c4` (PR #71) — **matches `master` in both repos**
**Supersedes**: `verify-report.2026-09-18.md` (kept in this directory; FAIL — artifact accuracy only, 2 CRITICAL / 6 WARNING / 4 SUGGESTION)

**Verdict**: **PASS WITH WARNINGS.**
Both CRITICAL findings of the first run are RESOLVED, and all six WARNINGs and all four
SUGGESTIONs are either resolved or converted into explicitly tracked, non-blocking
follow-ups recorded in the artifacts themselves. Every executable gate is green at the
exact SHAs the artifacts claim. The remaining warnings are process/traceability items and
one genuine verification limitation (real-Postgres), none of which requires reopening the
implementation. **Recommendation: proceed to `sdd-archive`.**

---

## 1. Execution evidence (real runs, this session)

Nothing was modified: no source, spec, design, `tasks.md` or `apply-progress.md` edit; no
commit, push, stash, reset, clean or checkout. Only the two report files named above were
written. Both trees were re-verified clean after every run.

### 1.1 Repo HEADs and tree cleanliness (step 1)

`git fetch --all` only; no pull, no checkout, no mutation.

| Repo | Expected | Actual HEAD | Branch state | Tree |
|---|---|---|---|---|
| `cashing-backend-master` | `81aab19` | `81aab19` `fix(jobs): make stale-job reset race-safe in classification and paquete job upserts (#95)` | `## master...origin/master` (no ahead/behind) | clean |
| `cashing-frontend` | `11bf4c4` | `11bf4c4` `test(unit): keep two starvation-prone tests inside the wall-clock timeout (#71)` | `## master...origin/master` (no ahead/behind) | clean |

Both confirmed. `git status --porcelain` empty in both repos before and after the runs.

### 1.2 Backend — `cashing-backend-master` @ `81aab19`

| Command | Result |
|---|---|
| `uv run python -m pytest -q` | **3329 passed, 1 skipped, 15 deselected**, 46 warnings, **525.19 s (0:08:45)**, exit 0 |
| `powershell ./scripts/pre-merge.ps1` (official gate, D6) | **`GATE OK 81aab19 tests=3330 coverage=88% ruff=526 mypy=357 dirty=no`**, exit 0 |

Expected ~3329 passed / 1 skipped → **exact match, zero drift**.
The 15 deselected are the opt-in `live_llm` / `live` markers excluded by
`pyproject.toml:184` `addopts = -m "not live_llm and not live"` (real-network tests).
The single skip is `test_radicar_idempotente.py:695`
`test_radicar_lock_bloquea_segunda_transaccion_bajo_postgres`
(`@pytest.mark.skipif(not _IS_PG)`) — environmental, not a coverage gap. Those are the only
two `skipif` markers left in the whole backend test tree; the other one
(`tests/test_query_budgets.py:50`) is a module-level guard that fires only under Postgres.
Coverage floor is `pyproject.toml:198` `fail_under = 70`.

### 1.3 Frontend — `cashing-frontend` @ `11bf4c4`

| Command | Result |
|---|---|
| `npm run gate` | **`GATE OK 11bf4c4 unit=1389 e2e=7 tsc=clean lint=backlog(3e/8w) build=ok dirty=no node=25`**, exit 0 |
| `npx vitest run` (inside gate) | **1389 passed, 0 failed, 0 skipped** across **77 test files**, 17.28 s |
| `npx tsc --noEmit` (inside gate) | clean |
| `npx playwright test` (2 mocked specs, inside gate) | **7 passed** (8.6 s) |
| `next build --turbopack` (inside gate) | ok, 15/15 static pages |
| `npx eslint .` | **11 problems (3 errors, 8 warnings)** — full list in §3, WARNING-3 |
| `npx eslint lib` | **0 problems** — the WARNING-3 violations are gone |

Expected unit=1389, e2e=7, tsc clean, build ok → **exact match, zero drift**.
Lint backlog went `5e/8w` (first run) → **`3e/8w`**, exactly the −2 errors PR #69 claims.

### 1.4 Not executed in this pass (stated limitations)

- **Live-backend Playwright** (`e2e/journey/**`, `e2e/edge-cases/**`) was **not re-executed**
  here, by instruction. It is an on-demand local runbook per 4.8's 2026-09-16 redefinition
  and is not part of `npm run gate` (`--live-e2e` confirmed absent from `scripts/gate.mjs`
  and `package.json` at `11bf4c4`). The results quoted in this report are **reported**, from
  `tasks.md` (Checkpoint 2026-09-19, 5.7) and `apply-progress.md`: journey **13/13**,
  edge-cases **18 passed / 1 skipped**, `radicacion-paquete-completo` passes, PR #70's
  combined run 26 passed / 1 skipped. **This report does not re-prove those assertions.**
- **Real-PostgreSQL suite: NOT RUN.** `docker info` fails
  (`npipe:////./pipe/dockerDesktopLinuxEngine`, daemon not running). Per instruction Docker
  was **not** started. **PG suite not run: Docker Desktop daemon is not running.**
  Stated plainly: **`SELECT … FOR UPDATE` is a no-op on SQLite/aiosqlite** (the services'
  own docstrings say so at `paquete_job_service.py:109` and
  `evidence_classification_service.py:147`), so **the row-lock half of PR #95 is
  unverified**. What the SQLite suite does prove is the other half: the identity-map refresh
  (`populate_existing=True`), the explicit `updated_at` bump and the payload nulling.
- **No live servers were started** and nothing touched production.
- **Engram is not reachable from this executor's toolset** (no `mem_*` tool is exposed).
  All findings rest on files and executed commands. The `mem_save` of this artifact could
  not be performed; the openspec file is the authoritative copy. Note that proposal decision
  #2 chose a *hybrid* store, so the Engram mirror of this report is missing.
- **Frontend per-file coverage** is not measured by the project's own gate (no coverage tool
  wired into `npm run gate`). Not a failure — simply not available.
- **No process was left running.** The pytest run exited on its own; the gate's node and
  Playwright processes exited with it; ports 8000/3000 were never bound by this session and
  `local_dev.db` was never touched.

---

## 2. Completeness

| Metric | Value |
|---|---|
| Tasks declared in `tasks.md` | **55** (46 original + 4.8 closed out + Phase 5's 5.1–5.8) |
| Tasks complete `[x]` | **55** |
| Tasks `[~]` / `[!]` / `[ ]` | **0** |
| Merged code **not** represented by a task | **0 commits** |

Verified mechanically: `rg -c "^- \[.\] \*\*" tasks.md` → 55; `rg -c "^- \[x\] \*\*"` → 55;
`rg "^- \[[^x]\] \*\*"` → no match. This matches `gentle-ai sdd-status`' 55/55.

Post-checkpoint commit reconciliation — **every commit is accounted for, with exact SHAs and
diff stats** (verified with `git log a730acf..master`, `git log db45c9f..master`, and
`git show --stat` per commit; not trusted from the summaries):

| Repo | Commit | Recorded as | `git show --stat` | `tasks.md` claim | Match |
|---|---|---|---|---|---|
| backend | `aa35a4a` | 5.1, PR #93 | 41 files, +7598/−318 | 41 files, +7,598/−318 | exact |
| backend | `46b73d6` | 5.2, PR #94 | 32 files, +3401/−128 | 32 files, +3,401/−128 | exact |
| backend | `280fd25` | 5.2 (test-only) | — | named in 5.2 | yes |
| backend | `81aab19` | 5.5, PR #95 | 4 files, +689/−2 | 4 files, +689/−2 | exact |
| frontend | `a93b16e` | 5.3, PR #68 | 80 files, +9241/−779 | 80 files, +9,241/−779 | exact |
| frontend | `7af14b5` | 5.4 (direct commit) | 2 files, +38 | 2 files, +38 | exact |
| frontend | `7fddacb` | 5.6, PR #69 | 5 files, +246/−11 | 5 files, +246/−11 | exact |
| frontend | `d369fbe` | 5.7, PR #70 | 5 files, +384/−40 | 5 files, +384/−40 | exact |
| frontend | `11bf4c4` | 5.8, PR #71 | 2 files, +2/−3 | 2 files, +2/−3 | exact |

No stale SHA survives anywhere: `a730acf` / `db45c9f` / `280fd25` / `7af14b5` appear in the
artifacts **only** as historical provenance ("the original as-built checkpoint was …"),
never as a current-state claim.

---

## 3. Re-check of every finding from the first run

Method: each finding was re-tested against the current code and git history by reading
source and running commands, not by trusting `tasks.md` or `apply-progress.md`.

### CRITICAL-1 — merged implementation not represented in `tasks.md` → **RESOLVED**

`tasks.md` now carries a `## Phase 5` section (lines 277–319) with 8 `[x]` slices covering
all 9 post-checkpoint commits, each with PR number, squash SHA, file/line diff stat, as-built
description, named edge cases, review-round record and explicit **"not recorded"** where a PR
body is silent. Reconciliation table in §2 above: every SHA, PR number and diff stat matches
`git` exactly. `apply-progress.md` (new) repeats the same mapping in its Workload / PR
Boundary section. The `## Checkpoint 2026-09-19` block (lines 7–14) re-baselines the masters,
the task count, the suite counts, the lint/type backlog and the open user decision.
*Evidence*: `git log a730acf..master`, `git log db45c9f..master`, `git show --stat <sha>` x8.

### CRITICAL-2 — `specs/stepper-ux` contradicted by `master` → **RESOLVED**

`specs/stepper-ux/spec.md:13` now reads: group 2 is *"raw steps 3-6, revealed as a **staged**
rail … MUST declare `revealMode: "staged"`, `members: [4, 6, 3, 5]` and `stagedOrder:
[CONTEXTO_MEMBER, 4, 6, 3, 5]`; `members` MUST stay raw-steps-only so gating derivations do
not depend on display order or on the virtual Contexto section."*
Read from source, `components/stepper/steps.ts`:

```ts
244:    members: [4, 6, 3, 5],
245:    stagedOrder: [CONTEXTO_MEMBER, 4, 6, 3, 5],
246:    revealMode: "staged",
```

Exact match, including the line numbers `design.md` D11 cites (`steps.ts:244-246`).
Beyond fixing the false clause, the spec gained a whole new requirement — *"Screen 2
'Completá tu cuota' is a staged rail, context first, with one completeness model (5.3)"*
(`spec.md:23-82`) — with 10 scenarios. Those are backed by real, executed tests (see §4.3).
`tasks.md` 3.4's historical `revealMode: "all"` text is now correctly superseded by 5.3,
which says so explicitly ("Supersedes 3.4's `revealMode: "all"`").

### WARNING-1 — task 4.8 stale `[~]` → **RESOLVED**

4.8 is `[x]` (`tasks.md:263`) with a dedicated *"Closed 2026-09-19 (WARNING-1 of the first
`sdd-verify` run)"* paragraph (line 265). Re-verified present in both repos this session:
`scripts/pre-merge.ps1`, `scripts/gate.mjs`, `.githooks/pre-push`,
`.github/pull_request_template.md`, and the dormant `ci.yml` (+ frontend `e2e.yml`). The
residual `npm run gate -- --live-e2e` is explicitly restated as tracked, non-blocking, and
confirmed absent (`rg "live-e2e" scripts/gate.mjs package.json` → no match). `e2e/README.md`
holds the boot notes the entry cites.

### WARNING-2 — `ProgresoEtapas` had no accessible name → **RESOLVED**

`components/ui/progreso-etapas.tsx:116` now renders
`aria-label={label || DEFAULT_PROGRESS_LABEL}` on the `role="progressbar"` element, with a
doc comment naming WCAG 2.0 A and explaining why it uses `aria-label` rather than
`aria-labelledby` (unlike `progress-bar.tsx`). Both call sites pass a real label:
`step-6-justificacion.tsx:243` `label="Generando justificaciones…"`,
`step-7-paquete.tsx:500` `label="Generando el paquete…"`.
Covering tests (all inside the 1389-passing run): `components/ui/__tests__/progreso-etapas.test.tsx`
has 17 tests, of which 9 target the name specifically — "names the progressbar after the
caller-provided label", "falls back to the default name 'Progreso' when no label is
provided", "falls back to the default name for an empty-string label", "lets a different
caller label override the name", "keeps the name across every stage: first, middle and
last", "is named on the first stage when the threshold is 0", "stays named at the upper
boundary", "is named even when no stages are provided (empty etapas)", "follows a label
change on re-render" — plus the negative case "exposes no progressbar (named or not) while
inactive or under the threshold". Integration proof at
`components/stepper/steps/__tests__/step-6-justificacion.test.tsx:423-463`:
`getByRole("progressbar", { name: "Generando justificaciones…" })`.
The spec clause was updated to match (`stepper-ux/spec.md:21`) and the misleading
"legacy uses" Out-of-Scope carve-out is gone.

### WARNING-3 — live `.detail` lint violations in `lib/checklist-api.ts` → **RESOLVED**

`npx eslint lib` → **0 problems** (run this session). `lib/checklist-api.ts:353` now carries
the comment *"`detail` is read only through `extractApiError` (lint-enforced)"* and
`extractUploadErrorMessage` delegates to `hasApiErrorDetail`/`extractApiError`.
Full-repo `npx eslint .` is **3 errors / 8 warnings** — the two `lib/checklist-api.ts:352-353`
errors are gone. The 3 remaining errors are unrelated pre-existing backlog, none of them a
`.detail` read:

```
app/page.tsx           155:11  error  Do not use an `<a>` element to navigate to `/contratos/` … @next/next/no-html-link-for-pages
e2e/smoke/prod.spec.ts  51:11  error  React Hook "use" is called in function "guard" …          react-hooks/rules-of-hooks   (false positive)
next-env.d.ts            3:1   error  Do not use a triple slash reference …                     @typescript-eslint/triple-slash-reference (generated file)
```

Covering tests: `lib/__tests__/checklist-api.test.ts:410+`
(`describe("extractUploadErrorMessage")`, ~20 cases: string / absent / both / 422 array /
object / junk / `{}` / null / undefined / structured `{code,message}` / unknown top-level
code / very long string). The spec was tightened accordingly
(`radicacion-flow/spec.md:96-110`, new scenario *"Upload error message (lib layer)"*), and
D8 in `design.md` now states the rule covers `lib/**`.

### WARNING-4 — no `apply-progress`, no TDD Cycle Evidence table → **RESOLVED (residual SUGGESTION)**

`openspec/changes/radicacion-sin-friccion/apply-progress.md` now exists with a **55-row
"TDD Cycle Evidence" table** (Task / Test File / Layer / Safety Net / RED / GREEN /
TRIANGULATE / REFACTOR), a mutation-testing-and-review record table, deviations, issues,
workload/PR boundary and status. It carries an explicit provenance header stating it was
written retroactively on 2026-09-19, that it is a **transcription and not new evidence**, and
that cells say **"not recorded"** rather than inferring. That is exactly the honest form the
strict-TDD module asks for. Residual (SUGGESTION, not a defect): 40 of 55 rows have
`RED: not recorded`, listed by name in the file's own "Rows where RED is `not recorded`"
paragraph. See §7 for the audit of those cells.

### WARNING-5 — backend ruff/mypy backlog drift → **STILL OPEN, now DOCUMENTED and tracked**

Re-measured this session by the official gate: see the `GATE OK` line in §1.2. The drift from
the 4.3 baseline (`ruff=501 mypy=336`) is now recorded in three places instead of nowhere —
`tasks.md:13` (Checkpoint block), `design.md:126` (Known Risks), `apply-progress.md` Issues
Found — with the attribution to PR #93/#94's files. Non-blocking by CI design
(`continue-on-error: true`, mirrored by the gate), so the gate still prints `GATE OK`.
Status: acknowledged tracked follow-up, not a defect of this change.

### WARNING-6 — `evidence_classification_service._upsert_job` no-op reassignment → **RESOLVED**

Read from source at `81aab19`, both files:

- `app/services/evidence_classification_service.py:164-180` — the `FOR UPDATE` re-select now
  carries `.execution_options(populate_existing=True)` with a comment naming the identity-map
  trap; `:201` `job.updated_at = datetime.now(UTC)` with a comment naming
  `History.from_scalar_attribute`.
- `app/services/paquete_job_service.py:132-137` — same `populate_existing=True`;
  `:156-162` nulls the previous run's result payload (`storage_key`, `filename`,
  `size_bytes`, `listo_para_radicar`, `pendientes`, `advertencias_coherencia`,
  `es_borrador`); `:174` explicit `job.updated_at = datetime.now(UTC)`.

Covering tests verified to **exist on master with exactly the names the artifacts claim**
(read with `rg "^async def test_"`, all green inside the 3329-passing run):
`tests/test_evidence_classification_concurrency.py` →
`test_stale_pending_job_noop_reset_is_not_re_stale_for_a_second_caller` (:447),
`test_stale_pending_job_reset_moves_updated_at_forward` (:478),
`test_stale_threshold_boundary_for_pending_and_running` (:517),
`test_stale_pending_job_with_different_total_is_reset_and_reenqueued` (:552),
`test_stale_job_with_error_set_is_reset_and_error_cleared` (:574),
`test_fresh_pending_or_running_job_is_not_reset` (:600),
`test_loser_with_stale_identity_map_sees_winners_fresh_reset_and_does_not_enqueue` (:725),
`test_loser_with_stale_identity_map_sees_winners_changed_total` (:744),
`…_after_winner_finished_job_resets_from_fresh_state` (:760),
`test_loser_is_still_reset_when_row_is_genuinely_stale_and_winner_did_nothing` (:784).
`tests/test_paquete_job_service.py` → `test_reset_clears_previous_run_result_payload` (:496),
`test_polling_a_done_job_preserves_its_result_payload` (:554), plus the same three `loser_*`
identity-map tests (:630, :645, :667). The stale-PENDING analogue the first run flagged as
missing now exists. Assertion density: 58 asserts / 16 tests and 75 asserts / 15 tests
respectively; zero bare `is not None`-only assertions.
The behaviour is also promoted into a spec requirement
(`performance-jobs/spec.md:51-79`, 4 new scenarios) and a design decision (D13).
**Residual, stated plainly**: proven on SQLite only — `FOR UPDATE` is a no-op there, so the
real lock-serialization half is untested (tracked as a follow-up in `performance-jobs`
Out-of-Scope and `design.md:120`).

### SUGGESTIONS from the first run

| # | First-run finding | Status now |
|---|---|---|
| S1 | `design.md` said "7 journey … specs"; `e2e/journey/` holds 6 | **RESOLVED** — `design.md:101` now says *"6 journey + 10 edge-case + smoke specs (plus 2 mocked specs in the gate and 2 env-gated screenshot specs at the `e2e/` root)"*. Counted this session: journey 6 files, edge-cases 10 specs + `_shared.ts`, smoke 1 spec + README, root 6 files of which 2 are `screens-r*`. |
| S2 | `design.md` pinned frontend at "1103 tests at merge" | **RESOLVED** — `design.md:100` now reads *"1103 tests at the original checkpoint, **1389 at `11bf4c4`**; backend `pytest` 3329 passed / 1 skipped at `81aab19`"*. Both numbers re-measured this session and exact. |
| S3 | `e2e/screens-r4.spec.ts` / `screens-r5.spec.ts` outside the mandated layout | **RESOLVED as acknowledged** — now listed in `stepper-ux` Out of Scope (`spec.md:148`) and in 5.3's follow-ups (`tasks.md:297`). Still at the `e2e/` root; env-gated (`R4_SCREENS=1` / `R5_SCREENS=1`), so they cannot run by accident. |
| S4 | `QueryCounter.assert_budget` is upper-bound only | **STILL TRACKED** (unchanged, by design) — `design.md:124`, `performance-jobs/spec.md:100`, `quality-gate/spec.md:75`. |

### NEW findings from this run

**NEW-1 (WARNING) — Phase 5's largest backend slice has no spec requirement.**
5.1 / PR #93 `aa35a4a` is 41 files and +7,598 lines and `tasks.md:283` says so itself:
*"the specs of this change contain no requirement about discovery ranking; this slice is
recorded for traceability only."* That is honest and it is tested — the commit adds ~20 test
files (`test_evidence_discovery.py` +757, `test_evidence_query_budget.py` +501,
`test_evidence_matcher_precision.py` +441, `test_evidence_expansion_and_cache.py` +334, …)
all of which run inside the 3329-passing suite. But it means the archived record will contain
the single biggest backend change of the whole change-set with **zero rows in the compliance
matrix**: nothing in any spec states its required behaviour, so nothing can regress-check it
at the spec level. Not blocking (behaviour is pinned by tests and described in detail in
`tasks.md` 5.1); flagged so the decision to archive it as "traceability only" is deliberate.

**NEW-2 (WARNING) — `size:exception` was never recorded for four oversized PRs.**
`apply-progress.md` Workload / PR Boundary says it plainly: *"`size:exception`: not recorded
as used. Several Phase 5 PRs are far above 400 lines (#68 +9,241/−779; #93 +7,598/−318;
#94 +3,401/−128) — the PR bodies do not mention a size exception or a split decision."*
Confirmed against `git show --stat` (§2). The Review Workload Guard's 400-line budget was
therefore exceeded without the `ask-on-risk` escalation proposal decision #3 requires. This
is a process/record gap in already-merged work, not a code defect; it is disclosed by the
artifact itself. No remediation is possible retroactively beyond recording it — which the
artifact does.

**NEW-3 (WARNING) — three merged changes skipped the mandatory fresh-context review.**
`design.md:102` and proposal decision #6 require *"a fresh-context adversarial opus review
before any PR … No PR opens on a single-agent's self-report alone."* Self-disclosed
deviations, all in Phase 5:

- 5.4 / `7af14b5` — direct commit, no PR: *"Review: **not recorded**"* (`tasks.md:299`). This
  is the only one that is a **product** change (a new primary link on the account detail page).
- 5.7 / PR #70 — *"The second-round commits (`1bc3e8a`, `4b21643`) are test-only and did NOT
  get a second fresh review"* (`tasks.md:311`).
- 5.8 / PR #71 — *"test-only change, **no fresh-context review**"* (`tasks.md:316`).

5.4 is covered by real tests (`app/cuentas-cobro/[id]/__tests__/page.test.tsx:143-164`,
`it.each` over borrador/rechazada shown with `href=/radicar/{id}`, enviada/aprobada/pagada
hidden) and by a spec scenario (`stepper-ux/spec.md:115-118`). 5.7/5.8 are test-only and
carry mutation/stress evidence. Non-blocking; recorded because the design's own rule was
relaxed three times and the archive should say so.

**NEW-4 (SUGGESTION) — 40 of 55 TDD rows have `RED: not recorded`.**
See §7. This is a recording gap inherited from `tasks.md`, disclosed by name in
`apply-progress.md` itself, not evidence that TDD was skipped — the substantive requirement
(tests exist and pass at runtime) was re-established independently by this pass.

**NEW-5 (SUGGESTION) — Engram side of the hybrid store is missing for this phase.**
Proposal decision #2 chose `hybrid` (openspec + Engram). This executor has no `mem_*` tool,
so this report exists only as an openspec file. Anyone relying on
`sdd/radicacion-sin-friccion/verify-report` in Engram will not find this run.

### Finding tally

**5 of 6 first-run WARNINGs are RESOLVED** (W1, W2, W3, W4, W6); W5 is open but now
documented in three artifacts. **3 of 4 SUGGESTIONs are RESOLVED**; S4 remains a tracked
follow-up. Both CRITICALs are RESOLVED. Three new WARNINGs (NEW-1..3) and two new
SUGGESTIONs (NEW-4..5) are process/traceability items, none of them a code defect.

| | First run | This run |
|---|---|---|
| CRITICAL | 2 | **0** |
| WARNING | 6 | **4** (W5 carried over + 3 new) |
| SUGGESTION | 4 | **4** (S4 carried over + 2 new + 1 residual of W4) |

---

## 4. Spec compliance matrix

38 requirements / 81 declared scenarios across 6 specs. Statuses: **COMPLIANT** = a covering
test exists and passed at runtime in this session's runs (backend inside the 3329-passing
suite, frontend inside the 1389-passing suite, mocked e2e inside the 7-passing gate run);
**REPORTED** = covered only by a live-Playwright assertion not re-executed here;
**STATIC** = verified by reading code/config, no runtime assertion available.

### 4.1 `specs/radicacion-flow` — 7 requirements / 19 scenarios

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Wizard step 7 owns radicar (1.1, 3.5) | Successful radicar | `components/stepper/__tests__/stepper-radicar.test.tsx` > happy path | COMPLIANT |
| | Triple rapid click | same file > "double-click: rapid synchronous clicks … fire the mutation exactly once" | COMPLIANT |
| | Backend rejects (checklist / coherence) | same file > `CHECKLIST_INCOMPLETE` / `COHERENCE_CHECK_FAILED` cases | COMPLIANT |
| | Network failure | same file > transport-failure cases (no response body) | COMPLIANT |
| | Estado query unresolved | same file > loading-disabled and `obtenerCuentaCobro` REJECTS cases | COMPLIANT |
| | Legacy page retains radicar | `app/cuentas-cobro/[id]/radicacion/page.tsx` `RadicarCard` | STATIC |
| Radicar idempotent + lock-safe (1.2) | Concurrent double radicar | `tests/test_radicar_idempotente.py` (15 tests) incl. `test_radicar_concurrente_una_sola_transicion`, `test_radicar_cas_loser_verdadero_rowcount_cero` | COMPLIANT |
| | after enviada / rechazada / pagada | same file: `…_segunda_vez_devuelve_mismo_payload_200`, `…_despues_de_rechazada_nueva_fecha_envio`, `…_desde_aprobada_o_pagada_422`, `…_lock_detecta_estado_terminal_post_lock` | COMPLIANT |
| | (PG-only lock timing) | `test_radicar_lock_bloquea_segunda_transaccion_bajo_postgres` | SKIPPED (no Docker) |
| `requisitos_modo` NULL unresolved (1.3) | Refresh on unresolved cuenta (REST + agent tool) | `tests/test_checklist_api.py` gate tests + `tests/test_tool_catalog.py` gate tests + `test_checklist_service.py::test_modo_efectivo_*` | COMPLIANT |
| **First-cuota identity documents (5.2)** | Second cuota | `tests/test_checklist_service.py::test_previsualizar_checklist_solo_primera_cuenta_hidden_on_later_cuenta` (:472), `::test_asegurar_checklist_creates_rows_first_cuenta` (:103) | COMPLIANT |
| | CONTRATO on a later cuota | `::test_asegurar_checklist_contrato_reaparece_sin_documento_compartido` (:178), `::test_asegurar_checklist_contrato_reaparece_sin_obligaciones` (:208), `::test_contrato_no_reaparece_en_una_cuenta_cerrada` (:2215) | COMPLIANT |
| | Content is never hidden (`heredado`) | `::test_construir_checklist_completo_muestra_custom_mapeado_a_codigo_de_primera_cuota` (:1335), `::test_construir_checklist_completo_first_cuenta_unaffected` (:1405) | COMPLIANT |
| | First cuota was deleted | `::test_asegurar_checklist_sin_primera_activa_trata_cuenta_como_primera` (:289), `::test_asegurar_checklist_sin_primera_activa_solo_la_cuenta_mas_antigua_falla_segura` (:316) | COMPLIANT |
| | Settled cuenta and checklist redefinition | `tests/test_requisitos_cuenta_api.py::test_definir_set_rechaza_una_cuenta_cerrada_sin_tocar_el_checklist` (:180), `::test_definir_endpoint_devuelve_422_en_una_cuenta_cerrada` (:251), `::test_definir_set_sigue_permitido_en_una_cuenta_abierta` (:233) | COMPLIANT |
| | Informes gate on step 5 | `tests/test_stepper_state.py::test_step5_formato_incomplete_when_no_informe_generated` (:543), `…_when_only_one_informe_generated` (:559), `…_complete_when_both_informes_generated` (:577), `…_complete_when_reemplazar_mode_dropped_informe_rows` (:617) | COMPLIANT |
| | (shared seam) | `requisito_aplica_a_cuenta` (`checklist_service.py:581`) / `listar_filas_visibles` (:3179) reached by checklist view (`api/v1/checklist.py` x5), radicar gate (`cuenta_cobro_service.py:1355`), resumen, constancia (`constancia_service.py:123`), package (`radicacion_prep_service.py:57`); migration `043` + `tests/test_migration_043_checklist_primera_cuota_flags.py` (3 tests) | COMPLIANT |
| MRO exception mapping (1.5a) | New subclass | `tests/test_exceptions.py` (8 tests) | COMPLIANT |
| No raw backend text/codes (1.4, 1.5b, **5.6**) | Unknown or hostile code | `lib/__tests__/backend-error-codes.test.ts`, `components/stepper/__tests__/step-messages.test.ts` (incl. `constructor`) | COMPLIANT |
| | Malformed detail | `lib/__tests__/extract-api-error.test.ts`, `lib/__tests__/api.test.ts` | COMPLIANT |
| | **Upload error message (lib layer)** | `lib/__tests__/checklist-api.test.ts:410+` (~20 cases) + `npx eslint lib` → 0 problems | COMPLIANT |
| One cuenta per contrato-month (4.2a) | Double create | `tests/test_cuenta_cobro_concurrency.py` (3 tests incl. credit-rollback and the loudly-pinned `numero_cuota` gap) | COMPLIANT |

**19/19 scenarios covered** (1 of them PG-only and therefore skipped this run).

### 4.2 `specs/agent` — 7 requirements / 11 scenarios

Unchanged by Phase 5; re-confirmed that every cited test file still exists and passed inside
the 3329-passing suite: `tests/test_agent_chat_observability.py` (6),
`tests/test_fake_llm_adapter.py` (**66**), `tests/test_tool_catalog*.py`,
`tests/test_evidence_handle_cache.py` (**12**), `tests/test_agente_cadena_completa*.py`,
`app/api/v1/agent_chat_stream.py` SSE + approval endpoints, frontend
`components/stepper/__tests__/agent-dock.test.tsx`, `lib/__tests__/agent-stream-api.test.ts`.
`e2e/journey/agent-dock-footer-overlap.spec.ts` (4 viewports) is **REPORTED**, not re-run.

**11/11 COMPLIANT** (1 scenario's e2e half REPORTED).

### 4.3 `specs/stepper-ux` — 7 requirements / 21 scenarios

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Three groups (3.4, 3.5, **5.3**) | 3 groups, clamp to 3, staged group-2 shape | `components/stepper/__tests__/steps.test.ts` (asserts "staged", the clamp, `stagedOrder`); source `steps.ts:244-246` | COMPLIANT |
| | Navigation within group 2 | `stepper-shell.test.tsx` per-member scroll/focus; `hooks/__tests__/use-group-auto-advance.test.tsx` (4 tests) | COMPLIANT |
| | Progress: real `procesadas/total` bar | `components/ui/progress-bar.tsx` + `step-4-evidencias.test.tsx` | COMPLIANT |
| | Progress: staged indicator **with an accessible name** | `progreso-etapas.tsx:116`; `progreso-etapas.test.tsx` (17 tests, 9 on the name); `step-6-justificacion.test.tsx:423-463` | COMPLIANT (was PARTIAL) |
| | Collapsed row idiom, errors not hidden | `components/checklist/__tests__/requisito-row.test.tsx`; `staged-section-list.test.tsx` | COMPLIANT |
| **Screen 2 staged rail (5.3)** | Context first | `staged-section-list.test.tsx` > "nothing done: Contexto active, everything after it locked with a plain-language next action"; `stepper-shell.test.tsx:1320` > "does NOT auto-fire evidence discovery while Contexto is still open" | COMPLIANT |
| | Incomplete context blocks the rail, not radicación | `section-model.test.ts` > "never lets Contexto be the reason the cuota cannot be radicada" | COMPLIANT |
| | Contexto done by text, skip, or nothing left to ask | `section-model.test.ts` > it.each "counts Contexto as done for %s, so the rail opens Evidencias", "keeps Contexto gating while its cuenta query is still in flight"; `contexto-omitido.test.ts` (9 tests: per-cuenta scoping, clear, missing id, junk value, storage unavailable, write failure) | COMPLIANT |
| | Section order and unlocks | `section-model.test.ts` > "walks past Evidencias on the secured bar but never calls it done" | COMPLIANT |
| | Partial Evidencias is never "Completo" | `staged-section-list.test.tsx` > describe "the collapsed pill never contradicts the backend" | COMPLIANT |
| | Zero-obligaciones contrato | `section-model.test.ts` > "treats a zero-obligaciones contrato as finished without asking for evidence"; `staged-section-list.test.tsx` > "zero-obligaciones contrato: the backend own vacuous completeness still advances the walk" | COMPLIANT |
| | Acknowledgement of a section finished in place | `staged-section-list.test.tsx` (68 tests) + `hooks/use-done-ack.ts` tests | COMPLIANT |
| | Resuming mid-stage | `staged-section-list.test.tsx` resume cases; `section-model.test.ts` | COMPLIANT |
| | Closing receipt and footer | `section-model.test.ts` > "refuses the ready receipt while a passable-but-not-done section remains", "gives the ready receipt only when every raw member is strictly done"; `staged-section-list.test.tsx` > "says Casi listo while the backend still reports informes pendientes"; `use-group-auto-advance.test.tsx` > "never auto-advances out of a STAGED group" | COMPLIANT |
| | Editar and locked marks | `staged-section-list.test.tsx` > "Editar expands one at a time", "re-opens a complete section from its header mark, the same as Editar" | COMPLIANT |
| | Mobile floor (short labels + hidden full names) | `staged-section-list.test.tsx` > "keeps the full section name available to assistive tech even where the visible label shortens" | COMPLIANT |
| | (reduced motion) | `reduced-motion.test.ts` (3 tests incl. `matchMedia` unavailable) | COMPLIANT |
| Screen 1 "Tu cuota" (3.3) | Named manual row / malformed inference | `definir-checklist-gate.test.tsx`, `step-2-cuota.test.tsx` | COMPLIANT |
| Screen 3 "Revisá y radicá" (3.5, 3.2) | never-triggered / trigger fails in transport | `lib/__tests__/paquete-api.test.tsx`, `step-7-paquete.test.tsx` | COMPLIANT |
| **One wizard route + account-detail entry (3.2, 3.2a, 5.4)** | New cuenta from a contract | `app/contratos/[id]` estado-branching tests; `RadicarModoModal` absent from the tree | COMPLIANT |
| | **Direct entry from the account detail page** | `app/cuentas-cobro/[id]/__tests__/page.test.tsx:143` it.each "shows Completar en el wizard linking to /radicar/{id} when estado is %s" (borrador, rechazada) and :164 `queryByTestId(...).not.toBeInTheDocument()` (enviada, aprobada, pagada) | COMPLIANT |
| Single confirmation idiom (3.6) | Failed delete | `ConfirmDialog` tests; zero `window.confirm` under `components/stepper/**` | COMPLIANT |
| Visual and a11y floor (3.7-3.9, 4.6) | Mobile 390px | `app-shell.test.tsx`, `sidebar.test.tsx`, `contraste.test.ts` (14 tests); `e2e/edge-cases/movil-390px.spec.ts` + `a11y-axe.spec.ts` | COMPLIANT (e2e half REPORTED) |

**21/21 scenarios covered**, with the two previously non-compliant ones (staged reveal,
accessible name) now COMPLIANT.

### 4.4 `specs/performance-jobs` — 6 requirements / 13 scenarios

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Query-count budgets (0.2, 2.2-2.5) | Budget breach message / values | `tests/test_query_counter_fixture.py` (5), `tests/test_query_budgets.py` — budgets read from source: contratos `:280` **11**, contrato detail `:302` **11**, checklist `:335` **36**, checklist recurrente `:365` **26**, radicar `:400` **70**, stepper-state `:415` **24**, evidencias `:498` **41**, regenerar-async `:541` **10**. Matches the spec text exactly. | COMPLIANT |
| | Money scale preserved | slice-2.3 targeted-refresh tests | COMPLIANT |
| No implicit relationship loads (2.4a/b) | Implicit access raises | `tests/test_raise_on_sql_slice_2_4a.py` | COMPLIANT |
| Doc generation off the event loop (2.1) | Generator raises | slice-2.1 `asyncio.to_thread` tests | COMPLIANT |
| Batch evidence upload (2.6, 1.9) | Duplicate in one batch / failure at item N | `tests/test_evidencia_batch_upload_concurrency.py` | COMPLIANT |
| **Package job + race-safe reset (2.7, 3.5, 5.5)** | Concurrent triggers | `test_paquete_job_service.py` — 4 trigger-race tests | COMPLIANT |
| | Stale pending job, `updated_at` bumped | `::test_stale_pending_paquete_job_reset_is_not_re_stale_for_a_second_caller` (:434) | COMPLIANT |
| | **Stale pending classification job with an equal total** | `test_evidence_classification_concurrency.py::test_stale_pending_job_noop_reset_is_not_re_stale_for_a_second_caller` (:447), `::test_stale_pending_job_reset_moves_updated_at_forward` (:478), `::test_stale_threshold_boundary_for_pending_and_running` (:517) | COMPLIANT |
| | **Second caller with a stale identity map** | three `test_loser_with_stale_identity_map_*` tests in each of the two files (:725/:744/:760 and :630/:645/:667) | COMPLIANT (SQLite; lock half unverified) |
| | **Reset clears the previous result** | `test_paquete_job_service.py::test_reset_clears_previous_run_result_payload` (:496), `::test_polling_a_done_job_preserves_its_result_payload` (:554) | COMPLIANT |
| | Never-triggered cuenta returns synthetic pending | `::test_get_paquete_job_returns_pending_default_when_never_triggered`; client half `lib/__tests__/paquete-api.test.tsx` | COMPLIANT |
| | Stale windows | `app/core/config.py:514` `EVIDENCE_JOB_STALE_SECONDS = 120`, `:528` `PAQUETE_JOB_STALE_SECONDS = 300` | STATIC |
| Shared HTTP clients, query hygiene (2.8) | Row scrolled into view | `lib/use-in-view.ts` + `lib/__tests__/use-in-view.test.tsx`; `app/core/http_clients.py`; `lib/providers.tsx` `refetchOnWindowFocus: false` | COMPLIANT |

**13/13 scenarios covered.**

### 4.5 `specs/resilience` — 5 requirements / 8 scenarios

Re-confirmed file-by-file, all green inside the 3329-passing run:
`tests/test_gmail_adapter_failures.py` (**43**), `tests/test_drive_adapter_failures.py`
(**45**), `tests/test_calendar_adapter_failures.py` (**23**),
`tests/test_litellm_adapter_failures.py` (**12**),
`tests/test_evidence_discovery_failure_tolerance.py` (**8**). `app/adapters/google_errors.py`
still excludes `RefreshError` from `GOOGLE_TRANSPORT_ERRORS` and that exclusion is still
pinned by a dedicated test.

**8/8 COMPLIANT.**

### 4.6 `specs/quality-gate` — 6 requirements / 9 scenarios

| Requirement | Evidence | Result |
|---|---|---|
| Local gate is the official gate (0.5, 1.10, 4.8) | Both gates executed this session, exit 0 (see section 1); `dirty=no` in both | COMPLIANT |
| Hook, PR template, dormant workflows | `.githooks/pre-push` + `.github/pull_request_template.md` in **both** repos; `ci.yml` in both, `e2e.yml` in the frontend — all present, listed this session | STATIC |
| Zero-`.env` Postgres checkout | `scripts/test-postgres.sh` + compose overlay present; **not executed** (Docker daemon down); the gate printed `>> skipping real-Postgres suite (default; pass -IncludePg to run it)` | STATIC only |
| Test builders (4.1) | `tests/factories.py` + `tests/test_factories.py` (**12**) | COMPLIANT |
| Synthetic fixtures only (4.4) | `e2e/fixtures/fixtures.test.ts` + leak guard; no `context/SYJ`/`DAGMA` reference in `e2e/**` | COMPLIANT |
| Shared helpers and layout (4.5, 4.7a) | `e2e/helpers/seed.ts` + `seed.test.ts` (594 lines), `auth.ts` + `auth.test.ts`; layout `journey` (6) / `edge-cases` (10 + `_shared.ts`) / `smoke` (1 + README); `RATE_LIMIT_ENABLED: bool = True` default | COMPLIANT (2 root `screens-r*` noted, S3) |
| Edge-case suite (4.7b, 4.2) | 10/10 named specs present; backend concurrency suites green | COMPLIANT (live half REPORTED) |
| Production smoke is read-only (4.9) | `e2e/helpers/remote-guard.ts` + `remote-guard.test.ts`; `prod.spec.ts` credential-gated skips; per-project `testMatch` | COMPLIANT |

**9/9 scenarios covered**, one of them (zero-`.env` Postgres) by static evidence only.

### Overall

| Spec | Requirements | Scenarios | Covered | Not covered |
|---|---|---|---|---|
| radicacion-flow | 7 | 19 | 19 | 0 |
| agent | 7 | 11 | 11 | 0 |
| stepper-ux | 7 | 21 | 21 | 0 |
| performance-jobs | 6 | 13 | 13 | 0 |
| resilience | 5 | 8 | 8 | 0 |
| quality-gate | 6 | 9 | 9 | 0 |
| **Total** | **38** | **81** | **81** | **0** |

**81/81 declared scenarios have a covering test or code location (100%).** Of those, 76 were
proven by a test that passed at runtime in this session; 4 rest on live-Playwright runs that
are REPORTED, not re-executed; 1 (zero-`.env` Postgres checkout) is static-only because
Docker is down. The two scenarios that were PARTIAL/CONTRADICTED in the first run are now
fully compliant.

---

## 5. Exit criteria

### Phase 0
> CI green in both repos; query-count and timing baselines recorded in Engram; a chat turn completable with `LLM_PROVIDER=fake` and no network.

| Criterion | Status | Basis |
|---|---|---|
| CI green in both repos | **PASS (superseded)** | GitHub Actions is billing-locked; D6 replaced it with the local gate, formally via 4.8 2026-09-16 redefinition. Both gates green this session. |
| Query-count baselines recorded | **PASS** | 8 budgets pinned in `tests/test_query_budgets.py`, all green in the 3329-passing run. Recorded in code, not only Engram. |
| Timing baselines recorded in Engram | **UNVERIFIABLE** | No `mem_*` tool in this executor. Instrumentation exists (`ToolEvent.duration_ms`, `fallback_depth`, `turn_duration_ms`); the recorded baselines could not be read. Unchanged from the first run — **no new evidence**. |
| Chat turn with `LLM_PROVIDER=fake`, no network | **PASS** | `tests/test_fake_llm_adapter.py` (66) and `tests/test_agente_cadena_completa*.py` drive the real `chat_with_tools` loop; green. |

### Phase 1
> A fresh local user radicates via the guided wizard without touching step 3 button; the full-playbook agent test is green.

| Criterion | Status | Basis |
|---|---|---|
| Wizard radicates without step 3 button | **PASS** | Step 7 owns the mutation; `ResumenCard` radicar removed; covering tests green. Live journey **reported** 13/13 at the remediation. |
| Full-playbook agent test green | **PASS** | Inside the 3329-passing suite. |

### Phase 2
> Query-count budgets from 0.2 green in CI; zero document generation on the event loop; local smoke against Neon dev shows checklist load under 1s.

| Criterion | Status | Basis |
|---|---|---|
| Budgets green | **PASS (superseded on "in CI")** | All 8 green in the local gate. |
| Zero doc generation on the event loop | **PASS** | Slice 2.1 `asyncio.to_thread`; covering tests green. |
| **Checklist load under 1s against Neon dev** | **UNVERIFIABLE** | **No new evidence.** `performance-jobs/spec.md:101` states it plainly: it has no recorded measurement in `tasks.md`, verify manually, not asserted here. `tasks.md:319` repeats it. Proxy only: checklist 47 to 36 queries (26 recurrent). |

### Phase 3
> 3-user unmoderated test radicates a cuota in under 10 minutes with zero internal-concept questions asked.

| Criterion | Status | Basis |
|---|---|---|
| **3-user unmoderated test** | **UNVERIFIABLE** | **No new evidence.** `tasks.md:319` (Phase 5 exit-criteria impact) says it still has no recorded evidence and remains unverified, not failed. Not a FAIL: there is no contrary evidence either. |

### Phase 4
> `npx playwright test` green in CI with no external network and no client data; every Phase 1-3 slice named edge cases are green, not just its happy path.

| Criterion | Status | Basis |
|---|---|---|
| Playwright green in CI | **PARTIAL (superseded)** | The 2 mocked specs (7 tests) ran inside the gate and passed. The 6 journey + 10 edge-case specs are an on-demand runbook per 4.8, **reported** 13/13 and 18 passed / 1 skipped, not re-executed here. |
| No external network / no client data | **PASS** | Synthetic fixtures + leak guard; fake-LLM backend; prod smoke behind a mechanical write-guard. |
| Every slice named edge cases green | **PASS** | The one exception of the first run (accessible name) was closed by 5.6. See section 6. |

### Phase 5
> No Phase 0-4 exit criterion changes state (`tasks.md:319`).

**Confirmed.** 5.6 closed the accessible-name gap in the Phase 4 clause; nothing else moved.

**Tally**: 8 PASS, 2 PASS-superseded, 1 PARTIAL-superseded, **3 UNVERIFIABLE**, **0 FAIL**.
The 3 UNVERIFIABLE criteria (Neon under 1s, 3-user unmoderated test, Engram timing baselines)
are exactly the three the first run flagged, and **no new evidence exists for any of them**.
They are recorded as unverified in the artifacts themselves; none is a FAIL.

---

## 6. Edge-case audit (standing user requirement)

Verifying edge cases are written **and green**, not just happy paths.

**Boundary values** — COVERED, GREEN. Derived codigo capped at 50 chars (backend
`VARCHAR(50)`); `test_stale_threshold_boundary_for_pending_and_running` pins both stale
windows exactly (120 s / 300 s); `progreso-etapas.test.tsx` pins the progressbar name at
threshold 0 and at the upper `aria-valuemax` boundary; the e2e seed helper four-code floor;
8 query budgets as upper bounds; file-size limit tested at, above and below 10 MB.

**Empty / null / malformed input** — COVERED, GREEN. `extractUploadErrorMessage` over
string / absent / both / 422 array / object / junk / empty object / null / undefined /
structured `{code,message}` / unknown code / very long string; `selectSeedableRequisitos`
over malformed body, malformed item (names the index), `requisitos_definidos=false`, empty
checklist, none of the tipos present, missing always-applicable code; malformed paquete-job
payload; `contexto-omitido` over missing cuenta id, junk stored value, storage unavailable,
write failure; `reduced-motion` over `matchMedia` unavailable; Gmail `"data": null` and
invalid base64; fake-LLM results missing `id` or duplicating a key.

**Concurrency / double-submit** — COVERED, GREEN. Backend: concurrent `radicar` via
`asyncio.gather`; CAS `rowcount == 0` loser path; soft-delete inside the lock window; double
create-same-month with credit rollback; **double classification enqueue including the
stale-PENDING equal-total case and three stale-identity-map loser cases**; phantom-insert
retry-once; 4 package-job trigger races; the winner-finished-job case. Frontend: synchronous
triple-click fires exactly one mutation (`step-2-cuota.test.tsx` hardened in 5.7 — removing
the `submitLockRef` guard fails deterministically with "called 2 times");
`e2e/edge-cases/double-submit.spec.ts` (reported).

**Network / server failures** — COVERED, GREEN. 43 Gmail + 45 Drive + 23 Calendar failure
tests; 12 LLM failure tests through the real 3-model fallback chain;
`GOOGLE_REAUTH_REQUIRED` distinguished and pinned by an invariant test; radicar
network-failure and codeless-transport frontend cases; checklist fetch 4xx/5xx and non-JSON
in the e2e seed helper.

**Stale / pending job states** — COVERED, GREEN. Stale-pending reset with a forced
`updated_at`; stale-pending **no-op** reset (the WARNING-6 case); stale-running reset;
stale-threshold boundary; different-total reset; error-set reset; fresh job not reset;
terminal job reset regardless of age; reset clears the previous result payload; a `done`
job payload preserved on polling; never-triggered synthetic `pending` the client refuses to
poll; resume-on-mount for a `running` job.

**State-machine invalid transitions** — COVERED, GREEN. radicar from `aprobada`/`pagada`
rejected 422; terminal state detected after the lock; `enviada` returns the existing result;
`rechazada` allowed with a fresh `fecha_envio`; **a settled cuenta (`enviada`/`aprobada`/
`pagada`) refuses checklist redefinition (422) without touching its checklist**; cross-user
access 403.

### Assertion quality

Swept both repos this session:

- `assert True` / `assert 1 == 1` in `cashing-backend-master/tests/` — **0 matches**.
- `expect(true).toBe(true)` / `expect(1).toBe(1)` under frontend `app|components|lib|e2e` — **0 matches**.
- Unconditional `it.skip` / `describe.skip` / `test.skip("...")` under frontend `app|components|lib` — **0 matches**.
- Backend skip markers: **2 total**, both `skipif` and both environmental (Postgres); exactly 1 fired.
- Assertion density on the Phase 5 test files: `section-model.test.ts` 89 expects / 0 mocks;
  `staged-section-list.test.tsx` 205 / 0 (1 class assertion); `contexto-section.test.tsx`
  50 / 1; `progreso-etapas.test.tsx` 39 / 0; `checklist-api.test.ts` 70 / 1;
  `seed.test.ts` 61 / 0; `test_evidence_classification_concurrency.py` 58 asserts / 16 tests;
  `test_paquete_job_service.py` 75 / 15. **No mock-heavy file** (mocks never approach 2x
  assertions), no ghost loops found, no smoke-test-only file.

**Assertion quality**: no tautologies, no orphan-empty assertions, no unconditional skips.
**Section verdict**: no happy-path-only area found. The gap the first run identified
(a missing accessibility attribute) is closed.

---

## 7. TDD compliance (Strict TDD Mode)

| Check | Result | Detail |
|---|---|---|
| TDD evidence reported | PASS | `apply-progress.md` now has a 55-row **TDD Cycle Evidence** table (WARNING-4 closed) |
| All tasks have tests | PASS | Every `[x]` slice either names test files or is a no-code slice explicitly marked as such (0.1, 0.5, 2.4b, 3.2a, 4.8) |
| RED confirmed (test files exist) | PASS | Every file path sampled in section 4 resolves on disk; the 5.3 row claimed sizes match exactly (`section-model.test.ts` **509**, `staged-section-list.test.tsx` **1340**, `contexto-section.test.tsx` **649**), as does 5.7 `seed.test.ts` **+272** (`git show --stat d369fbe`) |
| GREEN confirmed (tests pass now) | PASS | Independently re-run this session: **3329** backend + **1389** frontend + **7** mocked e2e, 0 failures |
| Triangulation adequate | PASS | 66 fake-LLM, 45 Drive-failure, 43 Gmail-failure, 16 classification-concurrency, 17 progressbar, 68 staged-section-list, 29 section-model cases |
| Safety net for modified files | PARTIAL | Recorded for 3.1 (882/882 vitest), 4.1, 4.2, 3.4 and **5.2** (verified: commit `46b73d6` body really does record 4 pre-existing `informe_service` tests and the `radicacion_prep` fixture repoint); "not recorded" elsewhere |
| Mutation testing of regression nets | PASS | Documented for 0.4, 1.1, 1.3, 1.4, 1.5b, 1.6, 1.7, 1.8, 2.6, 2.7, 2.8, 3.3, 3.4, 3.5, 3.6, 3.8, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7a, **5.5**, **5.6**, **5.7**, **5.8** |
| RED cell completeness | WARN | **40 of 55** rows say "RED: not recorded" — a recording gap inherited from `tasks.md`, disclosed by name in `apply-progress.md` itself (NEW-4) |

**TDD compliance: 7/8 checks pass.** The exception is RED-cell completeness, which is a
provenance gap, not missing evidence.

### Faithfulness spot-check of the TDD Cycle Evidence table

Checked against `tasks.md` and against `git`, per the instruction to flag fabricated or
unverifiable rows (8 rows sampled, 5 required):

| Row | Claim in `apply-progress.md` | Checked against | Verdict |
|---|---|---|---|
| 0.4 | `test_fake_llm_adapter.py` "(51 tests — the original 55 claim was corrected by review)" | `tasks.md:46` says "(new, 55 tests)"; `tasks.md:47` records the reviewer report-accuracy finding "test count (51, not 55)" | **faithful and honest** — it transcribes the corrected number, not the original overclaim |
| 3.4 | "weaker RED disclosed: `ProgressBar` (component and 8-case test written together); step-6 ordering tests RED not literally reproduced" | `tasks.md:201` verbatim: "RED not literally reproducible … honestly reported as a weaker RED than usual" and "mentally traced, not literally reverted-and-re-run … flagged as a lower-confidence RED" | faithful |
| 5.2 | Safety Net "commit body: 4 pre-existing `informe_service` tests and the `radicacion_prep` fixtures re-pointed" | `git log -1 --format=%B 46b73d6` lines 46-48 and 60-75 — the commit body really does say it | faithful, verified at source |
| 5.3 | Test files "from the PR diff stat: `section-model.test.ts` (509 lines), `staged-section-list.test.tsx` (1340), `contexto-section.test.tsx` (649)" | `wc -l` on master: **509 / 1340 / 649** | exact |
| 5.5 | RED "PR body: strict TDD in every round, RED first"; covering test names | `tasks.md:303` verbatim; all named tests found by `rg "^async def test_"` at the stated files | faithful |
| 5.7 | "`e2e/helpers/seed.test.ts` (+272)" | `git show --stat d369fbe` shows `e2e/helpers/seed.test.ts | 272 +` | exact |
| 5.8 | RED "N/A (timing fix; the before/after stress protocol is the evidence)" | `tasks.md:315-316` verbatim, incl. the honest admission that the `api.test.ts` half has "no measured defect to improve on" | faithful |
| 5.1 | Layer / Safety Net / RED all "not recorded" | `tasks.md:284`: "TDD / RED-GREEN / mutation evidence: **not recorded** in the PR body" | faithful (correctly abstains) |

**No fabricated row found. No unverifiable claim asserted as fact.** Every "not recorded"
cell checked corresponds to a source that really is silent.

### Test layer distribution

| Layer | Tests | Files | Tooling |
|---|---|---|---|
| Unit + integration (backend) | **3329 passed, 1 skipped** (15 deselected) | ~200 under `tests/` | pytest, pytest-asyncio, moto, factory-boy, aiosqlite |
| Unit + integration (frontend) | **1389 passed** | **77** | vitest 3.2.7, testing-library/react full-mount |
| E2E mocked (inside gate) | **7 passed** | 2 | Playwright 1.61 + `page.route()` |
| E2E live backend (**not re-run**) | journey 13/13, edge-cases 18 passed / 1 skipped (**reported**) | 16 | Playwright vs a live `LLM_PROVIDER=fake` stack |
| E2E prod smoke (read-only) | credential-gated | 1 | Playwright + mechanical write-guard |

### Coverage and quality metrics

- **Backend coverage**: **88%** against the `fail_under = 70` floor (`pyproject.toml:198`) —
  above threshold. Changed-file coverage was not isolated.
- **Frontend coverage**: **not available** — no coverage tool is wired into `npm run gate`.
  Not a failure.
- **Backend linter**: gate counter **ruff=526**; non-blocking by CI design; drifted from the
  4.3 baseline of 501 (WARNING-5).
- **Backend type checker**: **mypy=357** errors in 80 of 290 checked files; non-blocking;
  drifted from 336 (WARNING-5). Hotspots remain in `evidence_discovery_service.py` and the
  other files touched by 5.1/5.2.
- **Frontend linter**: **3 errors, 8 warnings** — full list in section 3 (WARNING-3). None is
  a `.detail` violation any more.
- **Frontend type checker**: `tsc --noEmit` **clean**.

---

## 8. Design coherence

| Decision | Followed? | Evidence (read from source at `81aab19` / `11bf4c4`) |
|---|---|---|
| D1 radicar short-circuit, unlocked gates, narrow lock, CAS, `populate_existing` reload | YES | `cuenta_cobro_service.py` — order re-read: ownership, ENVIADA short-circuit, 422 guard, unlocked gates, `_leer_estado_bajo_lock` (`FOR UPDATE`, estado-only), CAS `WHERE estado IN (borrador, rechazada) AND deleted_at IS NULL`, `rowcount == 0` re-read |
| D2 `requisitos_modo` NULL tri-state, no migration | YES | `modo_efectivo()` present; REST and agent-tool paths gated; no backfill migration in `alembic/versions` |
| D3 Package job async-first, sync keeps 200, 409 on lock loss | YES | `paquete_job_service.py`; 3 endpoints resolve; job tests green |
| D4 Per-row paquete status gated by `useInView` | YES | `lib/use-in-view.ts`; sole consumer `app/cuentas-cobro/page.tsx` |
| D5 Shared client per event loop, graph-token excluded | YES | `app/core/http_clients.py` |
| D6 Local pre-merge gate is official | YES | Both gates executed green this session; hooks, PR templates, dormant workflows present in both repos |
| D7 `LLM_PROVIDER=fake` in the running app, stateless fake | YES | `fake_adapter.py`; `config.py` validator; `litellm_adapter.get_llm()` dispatch |
| D8 One error-code contract, MRO walk, lint-enforced reader including the lib layer | YES (was MOSTLY) | MRO walk in `core/exceptions.py`; `lib/backend-error-codes.ts`; `npx eslint lib` gives 0 problems |
| D9 `raise_on_sql`, per-session catalog memo | YES | `test_raise_on_sql_slice_2_4a.py`; checklist budget 36 / 26 confirms the memo |
| D10 Evidence-based unlock, handle-based `persistir_evidencias` | YES | `handle_id` additive; `evidence_handle_cache` with 12 tests |
| D11 Three groups, group 2 staged with `stagedOrder`, one `computeSectionModel` | YES | `steps.ts:244-246` matches the D11 citation exactly; `section-model.ts` exports `computeSectionModel`, consumed by `stepper-shell.tsx`, `staged-section-list.tsx`, `use-auto-fire-evidencias.ts` and both test files. **`computeSectionStatuses` does not exist anywhere in the repo** — and, checked this session, **no current artifact claims it does** (the name appears only in the superseded 2026-09-18 report) |
| D12 Frontend never re-derives gate outcomes | YES | `use-radicar-cuenta.ts` adds no client-side hard gate |
| **D13 Race-safe job-row stale reset** | YES | `evidence_classification_service.py:164-180,201` and `paquete_job_service.py:132-137,156-162,174` — `populate_existing`, explicit `updated_at`, payload nulling all present, exactly as D13 describes, including the honest D13 caveat "Not yet proven against real Postgres lock serialization" |
| **D14 First-cuota applicability is one shared seam** | YES | `requisito_aplica_a_cuenta` (`checklist_service.py:581`) / `listar_filas_visibles` (:3179); consumers traced to the checklist view, resumen, radicar gate, constancia and package; migration `043_checklist_primera_cuota_flags.py` present with its own 3 tests |

**14/14 design decisions implemented as designed.** D8 and D11, the two the first run flagged
as out of sync, are now consistent across code, spec, design and tasks.

**One design-rule deviation** (not a decision, a process rule): `design.md:102` requires a
fresh-context adversarial review before any PR; 5.4, the 5.7 second commits and 5.8 did not
get one. Self-disclosed — see NEW-3.

---

## 9. High-risk spot-checks (read from source, not summaries)

### 1. `paquete_job_service._upsert_job` — CONFIRMED as specified

`paquete_job_service.py:120-191`. Read line by line: plain `SELECT`, then
`SELECT ... FOR UPDATE` **with `.execution_options(populate_existing=True)`** (`:132-137`,
comment naming the identity-map trap and the autoflush precondition), then the freshness
check against `PAQUETE_JOB_STALE_SECONDS` with tz-naive coercion (`:140-146`), then a reset
that sets `status`, `error`, `error_code`, **nulls all seven result-payload fields**
(`:156-162`), and forces `job.updated_at = datetime.now(UTC)` (`:174`) with a comment naming
`History.from_scalar_attribute`, then commit and refresh. The phantom-insert path retries
once on `IntegrityError` and raises `UpsertPaqueteJobRetryExhaustedError` after two attempts.
Matches D13 and `performance-jobs/spec.md:55` clause for clause.

### 2. `evidence_classification_service._upsert_job` — CONFIRMED FIXED

`evidence_classification_service.py:157-221`. Same shape: `populate_existing=True` on the
locking select (`:178`) and the explicit `job.updated_at = datetime.now(UTC)` (`:201`) that
the first run found missing. The comment at `:194-200` names the exact failure mode (a stale
`pending` row with the same `total`, `procesadas == 0` and `error is None` would otherwise
keep its old `updated_at`) — i.e. exactly the blast radius the first run characterised.
**WARNING-6 is closed.**
Caveat restated: the `FOR UPDATE` in both functions is a no-op on SQLite, and the PG suite
could not run, so only the identity-map and `updated_at` halves are verified by execution.

### 3. `lib/checklist-api.ts` — CONFIRMED no hand-rolled `.detail`

`npx eslint lib` gives 0 problems. Source read: the only remaining `detail` references are
(a) `matchLinkFailureResults(files, detail: string)`, which receives an already-extracted
string and does substring matching, and (b) the comment at `:353` stating the rule. No
`data.detail ?? data.error` survives.

### 4. `components/ui/progreso-etapas.tsx` — CONFIRMED named

`:112` `role="progressbar"`, `:116` `aria-label={label || DEFAULT_PROGRESS_LABEL}`, with a
doc comment at `:32-34` explaining the WCAG rationale and why it differs from the
`aria-labelledby` idiom in `progress-bar.tsx`. Both production call sites pass a real label.

### 5. `e2e/helpers/seed.ts` floor — CONFIRMED

`:249` defines `SIEMPRE_APLICAN` as seguridad_social, informe_actividades,
informe_supervision, acta_inicio, enforced at `:284-291` with an error message naming the
missing codes and the codes actually present. `selectSeedableRequisitos` throws (never
silently under-seeds) on malformed body, malformed item (naming the index),
`requisitos_definidos=false`, empty checklist, and none-of-the-tipos-present; custom rows
(`requisito_cuenta_id != null`) are deliberately excluded from the code set (`:276`).

### 6. Staged-rail spec scenarios mapped to real tests — CONFIRMED

Every one of the 10 new Screen 2 scenarios maps to a named test (section 4.3). The five files
the 5.3 row names all exist with the claimed line counts, and `computeSectionModel` really is
the single model: its only consumers are `stepper-shell.tsx`, `staged-section-list.tsx`,
`use-auto-fire-evidencias.ts` and the two test files.

### 7. Cross-artifact claim check — no contradiction found

- `computeSectionStatuses` vs `computeSectionModel`: only `computeSectionModel` exists, and
  only `computeSectionModel` is claimed by the current artifacts. OK
- Migration numbers: `042_paquete_job.py` and `043_checklist_primera_cuota_flags.py` both
  present in `alembic/versions/`, matching `design.md:106` and `radicacion-flow` 5.2. OK
- Test counts: backend 3329 passed / 1 skipped and frontend 1389 claimed by `tasks.md:11`,
  `apply-progress.md:5` and `design.md:100` — **all three re-measured exact** this session. OK
- SHAs: all 9 Phase 5 commits and their diff stats verified against `git`. OK
- Budget values: `performance-jobs/spec.md:13` says contratos 11, detail 11, checklist 36,
  radicar 70 — source `test_query_budgets.py` agrees exactly. OK
- Gate line: `tasks.md` 5.5 records `GATE OK 8f9bf9f tests=3330 coverage=88% ruff=526
  mypy=357` for PR #95; re-running the gate on master produced
  `GATE OK 81aab19 tests=3330 coverage=88% ruff=526 mypy=357 dirty=no` — identical counters,
  and `tests=3330` reconciles with pytest 3329 passed + 1 skipped. OK
- `secop_service.py:1443-1448` identity-map trap: re-read this session — the second locking
  `select(Contrato)` with `with_for_update()` at `:1442-1447` still carries **no**
  `populate_existing`, exactly as the follow-up says. OK (claim is true, not stale)
- `lib/api.ts:189` empty-string `detail`: re-read — line 189 is the
  `if (typeof detail === "string") return detail;` branch, so an empty string is returned
  verbatim, producing a message-less error. OK (claim is true)
- `checklist-full-view.test.tsx` "chunks a drop of more than 20 files into sequential batches
  of 20" exists at `:1252` and **passed** in this session run. OK

### 8. Group-2 height UX decision — CONFIRMED STILL OPEN, not decided here

Recorded as an open user decision in three places (`design.md:110`,
`stepper-ux/spec.md:136-138`, `tasks.md:14` plus the 5.3 OPEN USER DECISION paragraph), each
correctly stating that the staged rail *mitigates* the length but is **not** the decision.
**This report does not decide it.**

---

## 10. Known tracked follow-ups (listed once, not re-flagged as defects)

All of these are recorded in `tasks.md`, the specs Out-of-Scope sections and/or `design.md`
Known Risks. They are acknowledged here **once**, as designed, and are **not** counted as
findings of this run:

1. No heartbeat during the LLM batch in `_ejecutar_clasificacion` — a live classification job
   can exceed the 120 s stale window (pre-existing).
2. `secop_service.py:1443-1448` has the same identity-map trap on a second locking select
   (verified still true this session; 5.5 follow-up).
3. The stale reset writes `updated_at` from the app clock while other writes use `func.now()`
   — cosmetic at a 120 s threshold.
4. An empty-string `detail` still yields a message-less error (`lib/api.ts:189`, pre-existing).
5. The ESLint `.detail` selector is repo-wide and untyped; a narrower `data.detail` selector
   would avoid false positives.
6. `scripts/kill-local.ps1` assigns to the read-only `$pid` on PowerShell 7.
7. `components/__tests__/checklist-full-view.test.tsx` "chunks a drop of more than 20 files
   into sequential batches of 20" is load-flaky near the vitest wall-clock budget (passed
   this run; 3/8 timeouts under an artificial 64-burner load per 5.8).
8. The two touched job test files have not been run against real Postgres.
9. `QueryCounter.assert_budget` is upper-bound only (S4).
10. Backend ruff/mypy backlog above the 4.3 baseline (W5).
11. `npm run gate -- --live-e2e` not built; live Playwright stays a manual runbook.
12. `e2e/screens-r4.spec.ts` / `screens-r5.spec.ts` outside the mandated layout (S3).
13. Checklist mutators and `PATCH /checklist/{codigo}` have no estado gate (P1, from 1.6).
14. `numero_cuota` / `posicion=primera` have no backing partial unique index.
15. A suppressed classification enqueue during a running window can leave evidence
    unclassified with no surfaced error.
16. **Group-2 height = OPEN USER DECISION** — recorded, not decided, by anyone.

---

## 11. Verdict

### PASS WITH WARNINGS

Both CRITICAL findings of 2026-09-18 are resolved and were re-checked against code and git,
not against the summaries in `tasks.md` or `apply-progress.md`. Five of six WARNINGs are
resolved; the sixth (ruff/mypy drift) is non-blocking by CI design and is now documented in
three artifacts. Three of four SUGGESTIONs are resolved. Every executable gate is green at
the exact SHAs the artifacts claim, with **zero drift** in any reported number:

| Expected | Measured | Drift |
|---|---|---|
| backend ~3329 passed / 1 skipped | **3329 passed, 1 skipped, 15 deselected** (525.19 s) | none |
| backend gate | **`GATE OK 81aab19 tests=3330 coverage=88% ruff=526 mypy=357 dirty=no`** | none |
| frontend unit=1389 | **1389 passed, 77 files** | none |
| frontend e2e=7 | **7 passed** | none |
| frontend tsc clean, build ok | **clean, 15/15 pages** | none |
| tasks 55/55 | **55/55 `[x]`** | none |
| lint backlog 5e/8w then 3e/8w | **3e/8w** | none |

81/81 declared spec scenarios have a covering test or code location; 76 of them passed at
runtime in this session.

**This verdict is PASS WITH WARNINGS — not PASS — solely because of documented, tracked,
non-blocking items**: three process/traceability warnings about work that is already merged
and already disclosed by the artifacts themselves (NEW-1 spec-less 5.1; NEW-2 unrecorded
`size:exception`; NEW-3 three skipped fresh-context reviews), one open lint/type backlog
(W5), and three exit criteria that remain UNVERIFIABLE for lack of evidence that nobody
claims to have (Neon under 1s, the 3-user study, Engram timing baselines). **No code defect
blocks this change, and no implementation work is required by this report.**

**Recommendation: `sdd-archive`.**

### What unblocks archive

Nothing is blocking. The following are **optional record-keeping choices** for the archiver,
not prerequisites:

1. Decide whether to archive 5.1 (PR #93, +7,598 lines) as "traceability only" or to add a
   minimal spec requirement for it (NEW-1).
2. Record, in the `tasks.md` Review Workload Forecast or in the archive report, that four
   Phase 5 PRs ran over the 400-line budget without a recorded `size:exception` (NEW-2).
3. Record that three merged changes skipped the mandatory fresh-context review (NEW-3) — 5.4
   being the only product change among them.
4. Accept the three UNVERIFIABLE exit criteria as unverified (they already are, in the
   artifacts) or schedule them: a Neon-dev checklist timing, a 3-user study, and an
   Engram-side read of the Phase 0 timing baselines.
5. Run `scripts/pre-merge.ps1 -IncludePg` once Docker is available, to close the
   FOR-UPDATE-on-SQLite gap for the PR #95 row-lock half.
6. Mirror this report into Engram if the hybrid store of proposal decision #2 matters
   (NEW-5) — this executor had no `mem_*` tool.
7. The **group-2 height** user decision stays open. It is a product decision, not an archive
   blocker; archiving with it recorded as open is consistent with every other artifact.

### Issue counts

**CRITICAL: 0 · WARNING: 4 · SUGGESTION: 4 · OPEN (user decision): 5**

- CRITICAL: none.
- WARNING: W5 (ruff/mypy drift, tracked), NEW-1 (5.1 has no spec requirement), NEW-2 (no
  recorded `size:exception` for four oversized PRs), NEW-3 (three merged changes skipped the
  mandatory fresh-context review).
- SUGGESTION: S4 (`assert_budget` upper-bound only), NEW-4 (40 of 55 RED cells "not
  recorded"), NEW-5 (no Engram mirror of this artifact), and the residual of W4 (the TDD
  table is a faithful transcription, not first-hand evidence).
- OPEN user decisions: group-2 height; synthetic `pending` on `GET /paquete/job`; Langfuse
  enablement; prod-smoke user inputs; deferred cruzar/semaforo trigger.

**Resolved / still open / new**: **10 items resolved** (CRITICAL-1, CRITICAL-2, W1, W2, W3,
W4, W6, S1, S2, S3), **2 still open but tracked** (W5, S4), **5 new** (3 WARNING, 2
SUGGESTION).

### Verification limitations, stated plainly

- Live-backend Playwright (journey 13/13, edge-cases 18 passed / 1 skipped) was **not
  re-executed**; it is quoted as **reported** from `tasks.md` and `apply-progress.md`, per
  instruction.
- **PG suite not run: Docker Desktop daemon is not running** (not started, per instruction).
  `SELECT ... FOR UPDATE` is a **no-op on SQLite**, so the row-lock half of PR #95 is
  **unverified**; only the `populate_existing` and explicit-`updated_at` halves are proven.
- Engram is unreachable from this executor, so Engram-side artifacts (Phase 0 timing
  baselines, the DAG state topic) could not be read, and this report could not be mirrored
  there.
- Frontend per-file coverage is not measured by the project own gate.
- Nothing was modified, committed, pushed or deployed; no server was started; no process was
  left running; both trees were re-verified clean at `81aab19` / `11bf4c4`.
