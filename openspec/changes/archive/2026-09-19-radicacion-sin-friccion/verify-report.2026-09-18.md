# Verification Report: Radicación Sin Fricción

**Change**: `radicacion-sin-friccion`
**Phase**: sdd-verify · **Artifact store**: openspec · **Mode**: Strict TDD
**Verified**: 2026-09-18
**Repos verified at**: backend `cashing-backend-master` @ `280fd25` (clean) · frontend `cashing-frontend` @ `7af14b5` (clean)
**Artifact checkpoint claimed by spec/design**: backend `a730acf` (PR #92) · frontend `db45c9f` (PR #67)

**Verdict**: **FAIL — artifact accuracy only.**
Every executable gate is green and coverage is above floor. The failure is traceability:
`tasks.md` and `specs/stepper-ux/spec.md` no longer describe `master`. No code defect
blocks this change; an artifact amendment does.

---

## 1. Execution Evidence (real runs, this session)

### Backend — `cashing-backend-master` @ `280fd25`

| Command | Result |
|---|---|
| `uv run python -m pytest -q` | **3299 passed, 1 skipped, 15 deselected, 0 failed**, 46 warnings, 518.30 s, exit 0 |
| `powershell ./scripts/pre-merge.ps1` (official gate, D6) | **`GATE OK 280fd25 tests=3300 coverage=88% ruff=526 mypy=357 dirty=no`**, exit 0 |
| `uv run ruff check .` | 381 errors (non-blocking by CI design; gate's own counter reports 526 including format) |
| `uv run ruff format --check .` | 100 files would be reformatted (non-blocking) |
| `uv run mypy app` | 357 errors in 80 files / 290 checked (non-blocking) |

Coverage **88%** vs the 70% `fail_under` floor → above threshold.
The single skip is `test_radicar_lock_bloquea_segunda_transaccion_bajo_postgres`
(`@pytest.mark.skipif(not _IS_PG)`) — environmental, not a gap.
Expected ~2974; actual 3299 because `master` carries three commits added after the
artifact checkpoint (see CRITICAL-1).

### Frontend — `cashing-frontend` @ `7af14b5`

| Command | Result |
|---|---|
| `npm run gate` | **`GATE OK 7af14b5 unit=1335 e2e=7 tsc=clean lint=backlog(5e/8w) build=ok dirty=no node=25`**, exit 0 |
| `npx vitest run` (inside gate) | **1335 passed, 0 failed, 0 skipped** across 76 test files |
| `npx tsc --noEmit` (inside gate) | clean |
| `npx playwright test` (2 mocked specs, inside gate) | **7 passed** (9.1 s) |
| `next build` (inside gate) | ok, 15 static pages |
| `npx eslint .` | **13 problems (5 errors, 8 warnings)** — see WARNING-3 |

Expected ~1100+; actual 1335 (see CRITICAL-1).
The orchestrator's brief mentioned "19 preexisting lint warnings"; the measured backlog is
**5 errors + 8 warnings = 13**.

### Not executed in this pass (stated limitation)

Live-backend Playwright (`e2e/journey/**` 28 assertions, `e2e/edge-cases/**` 16) requires
booting backend `:8000` with `LLM_PROVIDER=fake` plus frontend `:3000`. Per task 4.8's
2026-09-16 redefinition this is an **on-demand local runbook, not an automated gate**, and
`npm run gate` does not include it. Their last recorded green run is 3.5's
`GATE OK 03c3f18` (28/28 journey). **This report does not re-prove those 44 assertions.**
The real-Postgres suite (`-IncludePg`) was likewise skipped (gate default).
No processes were killed; no `dev.db` contention was observed.

---

## 2. Completeness

| Metric | Value |
|---|---|
| Tasks declared in `tasks.md` | 47 (46 slices marked `[x]`, 1 marked `[~]`) |
| Tasks complete `[x]` | 46 |
| Tasks in progress `[~]` | 1 — **4.8** (see WARNING-1) |
| Tasks blocked `[!]` / not started `[ ]` | 0 |
| Merged code **not** represented by any task | **2 commits, 82 files, ~+9,279/-779 lines** (see CRITICAL-1) |

Every `[x]` slice names a merged PR and a suite count; each was cross-checked against a real
file or test that exists today. Task-to-evidence mapping is recorded in §4 and §5.

---

## 3. Issues Found

### CRITICAL

**CRITICAL-1 — Merged implementation is not represented in `tasks.md`.**
`master` in both repos is ahead of the checkpoint the spec and design declare:

| Repo | Checkpoint in artifacts | Actual `master` | Unaccounted commits |
|---|---|---|---|
| backend | `a730acf` (PR #92) | `280fd25` | `aa35a4a` (PR #93, semantic per-obligación discovery), `46b73d6` (PR #94, first-cuota-only identity documents / settled-cuenta data-loss blocker), `280fd25` (test isolation) |
| frontend | `db45c9f` (PR #67) | `7af14b5` | `a93b16e` (PR #68, staged Pantalla 2 — **80 files, +9,241/-779**), `7af14b5` (direct wizard entry point on account detail, 2 files, +38) |

PR #68 alone restructured group 2, added `computeSectionStatuses`, a `categoriaRequisito`
classifier, `ChecklistFullView variante="soportes"`, the `useRequisitoPatch` extraction, a
"Generar informes" block, a virtual Contexto section, and 7 new test files — none of it
has a task entry, an exit criterion, or a spec requirement. Archiving now would freeze a
record that materially under-describes what shipped.
*Evidence*: `git log db45c9f..HEAD`, `git show --stat a93b16e`.

**CRITICAL-2 — `specs/stepper-ux/spec.md` is contradicted by `master`.**
The spec states group 2 is *"(2, steps 3-6, **all members revealed at once**)"*. On `master`,
`components/stepper/steps.ts:243-246` declares:

```ts
members: [4, 6, 3, 5],
stagedOrder: [CONTEXTO_MEMBER, 4, 6, 3, 5],
revealMode: "staged",
```

`design.md` D11 correctly documents the `"all"` → `"staged"` move and the raw-steps-only
`members` invariant; `tasks.md` 3.4 still documents `revealMode: "all"`; the spec documents
neither. The requirement as written is false against the implementation, and the display
order now opens with a virtual Contexto section that the spec's "steps 3-6" phrasing does
not admit. The other clauses of that requirement (3 groups, clamp-to-3, collapsed row idiom)
**do** hold and are covered — see §4.
*Evidence*: `components/stepper/__tests__/steps.test.ts:63,71,99` assert the staged model
explicitly, so the code and its tests agree with each other and disagree with the spec.

### WARNING

**WARNING-1 — Task 4.8 checkbox is stale relative to its own redefined scope.**
4.8 is `[~]`. Its 2026-09-16 redefinition moved the deliverable from a GitHub Actions job to
the local gate + versioned `pre-push` hook + PR template, and all of those merged (backend
PR #91, frontend PR #66). Verified present: `scripts/pre-merge.ps1`, `.githooks/pre-push`,
`.github/pull_request_template.md` in both repos; `scripts/gate.mjs` in the frontend; dormant
`ci.yml` (+ `e2e.yml`) retained. The only residual is `npm run gate -- --live-e2e`, which the
task itself calls *"tracked, not blocking"* — confirmed absent from `scripts/gate.mjs`.
Classified WARNING (tooling/cleanup), not CRITICAL: no core deliverable is missing.

**WARNING-2 — `stepper-ux` "Progress" scenario is only half compliant.**
The scenario requires that justification generation show *"a staged (non-numeric) progress
indicator **with an accessible name**"*. `components/ui/progreso-etapas.tsx:102-107` renders
`role="progressbar"` with `aria-valuenow/min/max` and **no** `aria-label` or
`aria-labelledby` — no accessible name (WCAG 2.0 A). Step 6 wires this component
(`step-6-justificacion.tsx:36`). By contrast the step-4 real-fraction bar
(`components/ui/progress-bar.tsx:72-77`) *does* carry the 3.4 W1 fix.
The spec's Out-of-Scope carves out only *"`ProgresoEtapas` accessible name on **legacy**
uses"* — step 6 is a new use introduced by slice 3.4, so the carve-out does not cover it.
Status: ⚠️ PARTIAL, not ✅ COMPLIANT.

**WARNING-3 — Two live violations of the `.detail` lint rule the error-code contract depends on.**
`specs/radicacion-flow/spec.md` requires *"Error `detail` MUST be read only via
`extractApiError` (enforced by lint)"* (design D8). The ESLint `no-restricted-syntax` rule
exists and fires correctly, but `lib/checklist-api.ts:352-353` (`extractUploadErrorMessage`)
still hand-rolls `data.detail ?? data.error` and is reported as 2 of the 5 lint **errors**:

```
lib/checklist-api.ts
  352:16  error  Do not read `.detail` directly off an error — use extractApiError()/…
  353:30  error  Do not read `.detail` directly off an error — use extractApiError()/…
```

Introduced by `bfebe95` (2026-09-05), i.e. **pre-existing** — confirmed an ancestor of
`db45c9f`. Slice 1.5b enumerated 18 sites across `app/**` and `components/**` and never swept
`lib/**`, so this survived. It is **not** listed in the spec's Out-of-Scope section, and it
sits on a real production error path (upload failure messages). The gate tolerates it as
backlog because the ESLint step is changed-files-scoped.
The other 3 lint errors are unrelated backlog (`app/page.tsx` `<a>`-vs-`<Link>`,
`e2e/smoke/prod.spec.ts` rules-of-hooks false positive on a function named `guard`,
`next-env.d.ts` triple-slash on a generated file).

**WARNING-4 — No `apply-progress` artifact and no "TDD Cycle Evidence" table.**
Strict TDD verify expects a tabular RED/GREEN/TRIANGULATE/SAFETY-NET record in
`apply-progress`. No `apply-progress.md` exists in this change directory, and Engram tools
are **not available in this executor's toolset**, so an Engram-side copy could not be
checked. `tasks.md` instead carries a dense per-slice TDD narrative — including explicit
mutation-testing proof in 0.4, 1.2, 1.4, 2.6, 2.7, 3.3, 3.4, 3.6, 4.2 and 4.3, and an honestly
self-reported weaker RED for `ProgressBar` in 3.4.
Deliberately **not** escalated to CRITICAL: the substantive requirement (tests exist and pass
at runtime) was independently re-established by this pass — 3299 backend and 1335 frontend
tests green. The gap is format/traceability, not missing evidence.

**WARNING-5 — Backend lint and typecheck backlogs drifted above the change's own baseline.**
`tasks.md` 4.3 recorded `ruff=501 mypy=336`. Measured now: **`ruff=526 mypy=357`** (+25/+21).
Both are non-blocking by CI design (`continue-on-error: true`, mirrored by the gate), so the
gate still prints `GATE OK`. The mypy hotspots —
`evidence_discovery_service.py` (15), `evidence_matcher.py` (16), `evidence_justify.py` (14),
`evidence_filter.py` (10), `drive_adapter.py` (20), `checklist_service.py` (26) — correlate
with the files touched by PRs #93/#94, i.e. the drift is attributable to the post-checkpoint
commits of CRITICAL-1 rather than to any slice of this change.

**WARNING-6 — `evidence_classification_service._upsert_job` no-op-reassignment bug: CONFIRMED REAL.**
Already tracked (`design.md` Known Risks; `specs/performance-jobs` Out-of-Scope) — listed once
here with a confirmed classification, not re-raised as a new defect.
`app/services/evidence_classification_service.py:179-183` resets a stale job with:

```python
job.status = EstadoClasificacionJob.PENDING.value
job.total = total
job.procesadas = 0
job.error = None
```

and no explicit `updated_at` bump, whereas the fixed sibling
`paquete_job_service.py:138-152` does force `job.updated_at = datetime.now(UTC)`.
`updated_at` uses column-level `onupdate=func.now()` (`app/models/base.py:28-33`), which only
fires when an UPDATE is actually emitted.
**Exact blast radius (narrower than "identical"):** the bug bites only for a stale **PENDING**
row whose `total` equals the incoming `total`, with `procesadas == 0` and `error is None` —
then all four assignments are value-identical, SQLAlchemy emits no UPDATE, the staleness clock
never resets, and a second concurrent caller also reads the row as stale and also "wins".
A stale **RUNNING** row is safe: `status` genuinely changes, so the UPDATE fires.
Consistent with this, `tests/test_evidence_classification_concurrency.py` has
`test_stale_running_job_is_reset_and_reenqueued_under_lock` but **no** stale-PENDING analogue,
while `tests/test_paquete_job_service.py:392` does have
`test_stale_pending_paquete_job_reset_is_not_re_stale_for_a_second_caller`.
Severity: WARNING — a duplicated classification enqueue, not a money/state-machine corruption,
and reachable only inside the 120 s `EVIDENCE_JOB_STALE_SECONDS` boundary case.

### SUGGESTION

- **S1** — `design.md` Testing Strategy says "7 journey … specs"; `e2e/journey/` holds 6 files.
  The "7" matches the 7 *mocked* e2e tests in the `GATE OK` line. Cosmetic wording.
- **S2** — `design.md` Testing Strategy pins frontend at "1103 tests at merge"; `master` is
  **1335**. A direct consequence of CRITICAL-1.
- **S3** — `e2e/screens-r4.spec.ts` and `e2e/screens-r5.spec.ts` (env-gated screenshot passes,
  arrived with PR #68) sit at the `e2e/` root, outside the `journey|edge-cases|smoke` layout
  `specs/quality-gate` mandates. The three pre-existing root specs are explicitly sanctioned
  by task 4.7a; these two are not mentioned anywhere.
- **S4** — `QueryCounter.assert_budget` remains upper-bound only (already tracked, 0.2).

### OPEN — user decision, recorded not decided

- **Group 2 height.** `design.md` Open Questions still records this as the user's call
  (collapse members by default / accordion / keep), spanning ~5800–7100 px in a ~373 px scroll
  band. PR #68's `revealMode: "staged"` + `stagedOrder` materially changes the context in which
  that decision would be taken, but **no decision is recorded** in `tasks.md`, `design.md` or
  any spec. It remains open. This report does not decide it.
- The four other `design.md` Open Questions (synthetic `pending` on `GET /paquete/job`;
  Langfuse enablement; prod-smoke user inputs; deferred `cruzar`/semáforo trigger) are
  unchanged and remain open.

---

## 4. Spec Compliance Matrix

Statuses: COMPLIANT (covering test exists and passed at runtime) / PARTIAL / FAILING-CONTRADICTED.
All backend tests below ran inside the 3299-passing suite; all frontend tests inside the 1335-passing suite.

### 4.1 `specs/radicacion-flow` — 6 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Step 7 owns radicar (1.1, 3.5) | Successful radicar | `stepper-radicar.test.tsx` > "happy path: clicking Radicar cuenta calls the mutation, shows a loading state, then the real success state" | COMPLIANT |
| | Triple rapid click | same file > "double-click: rapid synchronous clicks (no tick between them) fire the mutation exactly once"; `e2e/edge-cases/double-submit.spec.ts` | COMPLIANT |
| | Backend rejects (checklist / coherence) | same file > "checklist incompleto (400 CHECKLIST_INCOMPLETE)..." and "coherencia fallida (400 COHERENCE_CHECK_FAILED)..." | COMPLIANT |
| | Network failure | same file > "network/server failure: the button becomes retryable again, with no silent failure or fake success"; "fix 3(c): a true transport failure (no response body at all)..." | COMPLIANT |
| | Estado query unresolved | same file > "NEAR-BLOCKER 4: while the cuenta's own estado is still loading, radicar is disabled"; "fix 2: obtenerCuentaCobro REJECTS (not just pending)..." | COMPLIANT |
| | Legacy page retains radicar | `app/cuentas-cobro/[id]/radicacion/page.tsx:103,142-214` (`RadicarCard` uses `useRadicarCuenta`) | COMPLIANT |
| Radicar idempotent + lock-safe (1.2) | Concurrent double radicar | `test_radicar_idempotente.py::test_radicar_concurrente_una_sola_transicion`; `::test_radicar_cas_loser_verdadero_rowcount_cero`; `::test_radicar_lock_bloquea_segunda_transaccion_bajo_postgres` (PG-only, skipped on SQLite) | COMPLIANT |
| | enviada / rechazada / pagada | `::test_radicar_segunda_vez_devuelve_mismo_payload_200`; `::test_radicar_despues_de_rechazada_nueva_fecha_envio`; `::test_radicar_desde_aprobada_o_pagada_422`; `::test_radicar_lock_detecta_estado_terminal_post_lock` | COMPLIANT |
| | not-soft-deleted clause | `::test_radicar_cas_no_transiciona_fila_soft_deleted` | COMPLIANT |
| `requisitos_modo` NULL unresolved (1.3) | Refresh on unresolved cuenta (REST and agent tool) | REST: `test_checklist_api.py::test_refresh_secop_gate_when_undefined`, `::test_auto_vincular_documentos_gate_when_undefined`, `::test_get_checklist_gate_when_undefined`. Agent tools: `test_tool_catalog.py::test_detectar_desde_secop_gates_on_undefined_requisitos_modo`, `::test_auto_vincular_documentos_gates_on_undefined_requisitos_modo`. Resolver: `test_checklist_service.py::test_modo_efectivo_defaults_null_to_estandar`, `::test_modo_efectivo_never_overwrites_an_explicit_choice` | COMPLIANT |
| MRO exception mapping (1.5a) | New subclass | `tests/test_exceptions.py` (8 tests: MRO walk, 500 fallback, `::test_multiple_inheritance_picks_leftmost_mro_mapped_status`) | COMPLIANT |
| No raw backend text/codes (1.4, 1.5b) | Unknown or hostile code | `lib/__tests__/backend-error-codes.test.ts`; `components/stepper/__tests__/step-messages.test.ts` (includes the `constructor` prototype-leak case) | COMPLIANT |
| | Malformed detail | `lib/__tests__/extract-api-error.test.ts`; `lib/__tests__/api.test.ts` | COMPLIANT |
| | Lint enforcement of the single reader | Rule present and firing, but 2 live violations in `lib/checklist-api.ts:352-353` | PARTIAL (WARNING-3) |
| One cuenta per contrato-month (4.2a) | Double create | `test_cuenta_cobro_concurrency.py::test_crear_cuenta_cobro_race_loser_gets_clean_error_not_raw_500`; `::test_crear_cuenta_cobro_race_loser_does_not_get_debited`; `::test_concurrent_crear_cuenta_cobro_different_meses_both_succeed_but_numero_cuota_is_corrupted` (pins the known gap loudly) | COMPLIANT |

**Summary**: 13/14 COMPLIANT, 1 PARTIAL.

### 4.2 `specs/agent` — 7 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Turn observability (0.3) | Tool error still timed | `tests/test_agent_chat_observability.py` (6); `tests/test_litellm_adapter.py` | COMPLIANT |
| | Concurrent sessions | `test_agent_chat_observability.py` (real `asyncio.gather` two-turn contextvars test) | COMPLIANT |
| Deterministic fake LLM (0.4, 0.6) | Happy path with real IDs | `tests/test_fake_llm_adapter.py` (66 tests); `app/core/config.py:97-107` Literal plus case-folding `field_validator`; `litellm_adapter.py:417` dispatch | COMPLIANT |
| | Degenerate tool results | same file (missing `id`, duplicate key, malformed content, truncated recap, concurrent sessions) | COMPLIANT |
| Escape-hatch tools (1.6) | Mark on enviada | Tools present: `checklist.py:204 marcar_requisito`, `stepper.py:25 obtener_estado_radicacion`, `cuentas.py:162 generar_cuenta_cobro_pdf`, `actividades.py:185 editar_actividad`; `tests/test_tool_catalog_requisitos.py`, `tests/test_tool_catalog.py` | COMPLIANT |
| Evidence persistence by handle (1.7) | Large discovery | `evidencias.py:52,101` (`handle_id` additive on `descubrir_evidencias`); `tests/test_evidence_handle_cache.py` (12); `tests/test_tool_catalog_evidencias_handle.py` | COMPLIANT |
| | Expired / foreign / repeated handle | `tests/test_evidence_handle_cache.py` (TTL, cross-user rejection, double-redeem) | COMPLIANT |
| Bounded prompt and time budget (1.8) | Existing cuenta found via listing | `agent_chat_service.py:93` `MAX_TOOL_ITERATIONS = 20`; `:138` 180s interactive timeout; evidence-based unlock plus sticky recap; slice-1.8 tests in suite | COMPLIANT |
| Full playbook completes (1.9) | Cap hit mid-playbook | `tests/test_agente_cadena_completa*.py` driving the real `chat_with_tools` loop with `LLM_PROVIDER=fake`; `agent_chat.py:29` `MAX_CHAT_FILES = 6` | COMPLIANT |
| | Failure on a mid-batch file | `tests/test_evidencia_batch_upload_concurrency.py`; `tests/test_evidence_persist.py` | COMPLIANT |
| Agent docked plus approval gate (3.10) | Write tool awaits approval | `app/api/v1/agent_chat_stream.py:78` (`POST /chat/stream`) and `:138,159,173,192` (approve / reject / cancel / retry); frontend `components/stepper/__tests__/agent-dock.test.tsx`, `lib/__tests__/agent-stream-api.test.ts`; `e2e/journey/agent-dock-footer-overlap.spec.ts` (4 viewports, not re-run this pass) | COMPLIANT |

**Summary**: 11/11 COMPLIANT.

### 4.3 `specs/stepper-ux` — 6 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Three groups (3.4, 3.5) | 3 groups, clamp to 3 | `steps.test.ts` > "has exactly 3 groups, numbered 1..3"; "clamps a stale pre-3.4 group value of 4 down to 3"; "clamps any value above 3 down to 3" | COMPLIANT |
| | "all members revealed at once" | `steps.ts:246` is `revealMode: "staged"`; `steps.test.ts:99` asserts `"staged"` | **CONTRADICTED** (CRITICAL-2) |
| | Navigation within group 2 | `stepper-shell.test.tsx` (per-member `#step-section-{step}` scroll and focus, guard-aware, group-path fallback); `use-group-auto-advance` hook test | COMPLIANT |
| | Progress: real `procesadas/total` bar | `components/ui/progress-bar.tsx` wired into `ClasificacionYSemaforo`; `step-4-evidencias.test.tsx`; `e2e/journey/evidencias-clasificacion.spec.ts` | COMPLIANT |
| | Progress: staged indicator with an accessible name | `progreso-etapas.tsx:102-107` has `role="progressbar"` and no accessible name | PARTIAL (WARNING-2) |
| | Collapsed row idiom, errors not hidden | `requisito-row.tsx:407` `requisito-detalles-toggle`; `step-4-evidencias.tsx:471` `detalles-{evId}-{obId}`; `step-5-formato.tsx:443`; `components/checklist/__tests__/requisito-row.test.tsx` | COMPLIANT |
| Screen 1 "Tu cuota" (3.3) | Named manual row gets a derived codigo, max 50 chars | `definir-checklist-gate.test.tsx` > "includes a manually-added, named row in the actual definirRequisitos payload with a real codigo"; "derives distinct codigos for two manually-added rows..."; "derives a codigo distinct from an ALREADY-inferred row's codigo, case-insensitively"; "caps a derived codigo at 50 chars" | COMPLIANT |
| | Malformed inference response | same file > "does not crash and shows the empty state when the API returns a malformed (non-array) shape"; "shows the empty-list state, not a crash, when inference returns zero items" | COMPLIANT |
| | Editar disclosure, standard default, per-row undo | `step-2-cuota.test.tsx` (10 cases); `definir-checklist-gate.test.tsx` > "shows the 'Usar estándar' panel by default"; "orders the tabs..."; "marks an item mapped to the standard catalog distinctly..."; "removes a row ... restores it on Deshacer in the same position"; "restores an undone row to its original position" | COMPLIANT |
| Screen 3 "Revisá y radicá" (3.5, 3.2) | Never-triggered cuenta starts no polling | `lib/__tests__/paquete-api.test.tsx` > "does NOT poll a pending status when it was not triggered locally"; "fetches exactly once for a never-triggered pending job ... even after several poll-interval ticks" | COMPLIANT |
| | Trigger fails in transport, one confirmation refetch | same file plus `step-7-paquete.test.tsx` (F2 fix, 2 mutation-proved tests) | COMPLIANT |
| | Resume a running job on mount; poll only while running | `paquete-api.test.tsx` > "polls every 2s while status is running, even when NOT triggered locally (resume-on-mount ...)"; "stops polling once status is done/failed"; "treats a malformed payload (missing status) as no-job" | COMPLIANT |
| | Data-driven preview plus 3 standalone downloads | `step-7-paquete.test.tsx` (43 assertions, including loading/error vs genuine-zero distinction) | COMPLIANT |
| One entry point (3.2, 3.2a) | New cuenta from a contract lands on `/radicar/{id}` | `RadicarModoModal` deleted (absent from the tree); `app/contratos/[id]` estado-branching tests; e2e navigation assertions | COMPLIANT |
| Single confirmation idiom (3.6) | Failed delete renders inside the dialog | `ConfirmDialog` tests; zero `window.confirm` under `components/stepper/**`; `use-unsaved-changes-guard` sequential-dialog tests | COMPLIANT |
| Visual and a11y floor (3.7-3.9, 4.6) | Mobile 390px | `components/__tests__/app-shell.test.tsx`; `sidebar.test.tsx`; `stepper/__tests__/contraste.test.ts` (400 lines); `e2e/edge-cases/movil-390px.spec.ts` and `a11y-axe.spec.ts` (not re-run this pass); `e2e/critical-testid-regression.test.ts` | COMPLIANT |

**Summary**: 14/16 COMPLIANT, 1 PARTIAL, 1 CONTRADICTED.

### 4.4 `specs/performance-jobs` — 6 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Query-count budgets (0.2, 2.2-2.5) | Budget breach message | `tests/test_query_counter_fixture.py` (5); `tests/test_query_budgets.py` (8) | COMPLIANT |
| | Budget values match the spec | see the budget table below | COMPLIANT |
| | Money scale preserved | slice-2.3 targeted-refresh tests in suite | COMPLIANT |
| No implicit relationship loads (2.4a/b) | Implicit access raises | `tests/test_raise_on_sql_slice_2_4a.py` | COMPLIANT |
| Doc generation off the event loop (2.1) | Generator raises, same exception propagates | slice-2.1 `asyncio.to_thread` tests in suite | COMPLIANT |
| Batch evidence upload (2.6, 1.9) | Duplicate within one batch | `tests/test_evidencia_batch_upload_concurrency.py` | COMPLIANT |
| | Failure at item N commits nothing | same file (service-level rollback + flushed-stub assertion) | COMPLIANT |
| Package job (2.7, 3.5) | Concurrent triggers converge on one job | `test_paquete_job_service.py` — 4 concurrency tests, see below | COMPLIANT |
| | Stale pending job reset, `updated_at` bumped | `::test_stale_pending_paquete_job_reset_is_not_re_stale_for_a_second_caller` | COMPLIANT |
| | Stale running job reset | `::test_stale_running_paquete_job_is_reset_and_reenqueued_under_lock` | COMPLIANT |
| | Never-triggered cuenta returns synthetic pending | `::test_get_paquete_job_returns_pending_default_when_never_triggered` | COMPLIANT |
| | Stale window is 300s | `app/core/config.py:528` `PAQUETE_JOB_STALE_SECONDS: int = 300` | COMPLIANT |
| Shared HTTP clients, query hygiene (2.8) | Row scrolled into view | `lib/use-in-view.ts` + `lib/__tests__/use-in-view.test.tsx` | COMPLIANT |
| | Graph OAuth client stays per-call | `app/core/http_clients.py` (D5 exclusion) | COMPLIANT |
| | No refetch on window focus | `lib/providers.tsx:17` `refetchOnWindowFocus: false` | COMPLIANT |

Measured query budgets in `tests/test_query_budgets.py`, matching the spec exactly:

| Endpoint | Budget | Line |
|---|---|---|
| `GET /api/v1/contratos/` | 11 | :280 |
| `GET /api/v1/contratos/{id}` | 11 | :302 |
| `GET /cuentas-cobro/{id}/checklist` | 36 | :335 |
| `GET /cuentas-cobro/{id}/checklist` (recurrente) | 26 | :365 |
| `POST /cuentas-cobro/{id}/radicar` | 70 | :400 |
| `GET /cuentas-cobro/{id}/stepper-state` | 24 | :415 |
| `POST /cuentas-cobro/{id}/evidencias/subir` | 41 | :498 |
| `POST /cuentas-cobro/{id}/paquete/regenerar-async` | 10 | :541 |

The 4 package-job concurrency tests in `test_paquete_job_service.py`:
`::test_concurrent_regenerar_async_same_cuenta_schedules_background_run_exactly_once`,
`::test_concurrent_regenerar_sync_same_cuenta_loser_gets_409_not_duplicate_run`,
`::test_concurrent_regenerar_sync_vs_async_same_cuenta_async_loser_is_a_no_op`,
`::test_concurrent_first_trigger_same_cuenta_phantom_insert_retries_and_recovers`.

**Summary**: 15/15 COMPLIANT.

### 4.5 `specs/resilience` — 5 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Uniform Google error mapping (4.3) | 5xx / timeout / DNS failure | `test_gmail_adapter_failures.py` (43), `test_drive_adapter_failures.py` (45), `test_calendar_adapter_failures.py` (23) | COMPLIANT |
| | Non-`OSError` transport errors covered | `google_errors.py:45-50` tuple includes `httplib2.ServerNotFoundError` and `google.auth TransportError` | COMPLIANT |
| | Drive covers all methods, not only search | `drive_adapter.py:72` shared `_run()` wrapper, 9 call sites | COMPLIANT |
| | 4xx returns status plus per-class hint | `google_errors.py:132-137` hints (401/403/404/429); `:150-183` `raise_google_http_error` | COMPLIANT |
| Revoked grants distinguished (4.3) | Revoked token yields `GOOGLE_REAUTH_REQUIRED` | `google_errors.py:99-119` `raise_google_reauth_required` | COMPLIANT |
| | `RefreshError` caught before the transport tuple at every site | Gmail `:271,354,406,492`; Drive `_run` `:109`; Calendar `:168,219` — ordering verified by reading each | COMPLIANT |
| | A test pins `RefreshError` out of the tuple | `test_drive_adapter_failures.py:386-389` (membership plus no-subclass assertions) | COMPLIANT |
| Malformed payloads never leak or crash (4.3) | One bad message in a batch | `test_gmail_adapter_failures.py` (per-item skip-and-log plus aggregate warning) | COMPLIANT |
| | Malformed attachment (bad base64 or null) | same file (`binascii.Error` / `TypeError` mapped) | COMPLIANT |
| | No raw SDK text or request URIs in detail | `google_errors.py:70-96`; `include_exc_type=False` for `HttpError` | COMPLIANT |
| LLM failure chain (4.3, 1.8) | Primary model 5xx, fallback depth recorded | `test_litellm_adapter_failures.py` (12, real `litellm.exceptions.*`) | COMPLIANT |
| | All models fail, one sanitized error | same file plus `generar_actividades_agente` sanitization | COMPLIANT |
| Evidence discovery fails open (4.3) | Gmail down, Drive up | `test_evidence_discovery_failure_tolerance.py` (8) | COMPLIANT |

**Summary**: 13/13 COMPLIANT.

### 4.6 `specs/quality-gate` — 6 requirements

| Requirement | Scenario | Evidence | Result |
|---|---|---|---|
| Local gate is the official gate (0.5, 1.10, 4.8) | A failing step never prints GATE OK | Both gates executed this session, exit 0 — see section 1 | COMPLIANT |
| | Dirty tree, stray `test.only`, stale :3000 | `scripts/gate.mjs:35-37,61,284`; both runs reported `dirty=no` | COMPLIANT |
| | Hook, PR template, dormant workflows | `.githooks/pre-push` + `pull_request_template.md` in both repos; `ci.yml` (+ `e2e.yml`) retained | COMPLIANT |
| | Zero-`.env` Postgres checkout | `scripts/test-postgres.sh` + `docker-compose.infra.test.yml`; static evidence only (`-IncludePg` skipped) | COMPLIANT (static) |
| Test builders (4.1) | Cascade | `tests/factories.py` + `tests/test_factories.py` (12) | COMPLIANT |
| Synthetic fixtures only (4.4) | Truncated PDF fixture fails validity | `e2e/fixtures/fixtures.test.ts` (12, real trailer check) plus the leak guard | COMPLIANT |
| | No `context/SYJ` or `context/DAGMA` dependency | no reference remains in `e2e/**` | COMPLIANT |
| Shared helpers and layout (4.5, 4.7a) | Default rate limit enabled | `app/core/config.py:147` `RATE_LIMIT_ENABLED: bool = True` | COMPLIANT |
| | Shared seed/auth helpers with 429 retry | `e2e/helpers/seed.ts` + `seed.test.ts` (17); `auth.ts` + `auth.test.ts` (16) | COMPLIANT |
| | `journey` / `edge-cases` / `smoke` layout | present; 2 unlisted root specs noted in S3 | COMPLIANT (see S3) |
| Edge-case suite (4.7b, 4.2) | All 10 named specs exist | 10/10 present under `e2e/edge-cases/`; not re-run live this pass | COMPLIANT |
| | Concurrent classification enqueue | `test_evidence_classification_concurrency.py` (5) | COMPLIANT |
| Production smoke is read-only (4.9) | A write attempt fails naming method and URL | `e2e/helpers/remote-guard.ts` + `remote-guard.test.ts` (22) | COMPLIANT |
| | No credentials, authenticated specs skip clearly | `e2e/smoke/prod.spec.ts:185-232` credential-gated skips | COMPLIANT |
| | Localhost base URL boots `webServer` | `playwright.config.ts` per-project `testMatch` + `PLAYWRIGHT_BASE_URL` | COMPLIANT |

**Summary**: 15/15 COMPLIANT.

### Overall compliance

| Spec | COMPLIANT | PARTIAL | CONTRADICTED |
|---|---|---|---|
| radicacion-flow | 13 | 1 | 0 |
| agent | 11 | 0 | 0 |
| stepper-ux | 14 | 1 | 1 |
| performance-jobs | 15 | 0 | 0 |
| resilience | 13 | 0 | 0 |
| quality-gate | 15 | 0 | 0 |
| **Total** | **81** | **2** | **1** |

**81/84 fully compliant (96.4%).**

---

## 5. Exit Criteria (from `tasks.md`)

### Phase 0
> CI green in both repos; query-count and timing baselines recorded in Engram; a chat turn
> completable with `LLM_PROVIDER=fake` and no network.

| Criterion | Status | Basis |
|---|---|---|
| CI green in both repos | **PASS (superseded)** | GitHub Actions is billing-locked; D6 replaced it with the local gate. Both gates green this session. Formally superseded by 4.8's 2026-09-16 redefinition. |
| Query-count baselines recorded | **PASS** | 8 budgets pinned in `tests/test_query_budgets.py`, all green. Recorded in code, not only Engram. |
| Timing baselines recorded in Engram | **UNVERIFIABLE** | Engram tools are unavailable in this executor's toolset. Instrumentation exists; the recorded baselines could not be read. |
| Chat turn with `LLM_PROVIDER=fake`, no network | **PASS** | `test_fake_llm_adapter.py` (66) and `test_agente_cadena_completa*.py` drive the real loop; green. |

### Phase 1
> A fresh local user radicates via the guided wizard without touching step 3's button; the
> full-playbook agent test is green.

| Criterion | Status | Basis |
|---|---|---|
| Wizard radicates without step 3's button | **PASS** | Step 7 owns the mutation; `ResumenCard` radicar removed; 15 covering tests green. Live e2e last recorded at `GATE OK 03c3f18`, not re-run here. |
| Full-playbook agent test green | **PASS** | Part of the 3299-passing suite. |

### Phase 2
> Query-count budgets from 0.2 green in CI; zero document generation on the event loop;
> local smoke against Neon dev shows checklist load under 1s.

| Criterion | Status | Basis |
|---|---|---|
| Budgets green | **PASS (superseded on "in CI")** | All 8 green in the local gate. |
| Zero doc generation on the event loop | **PASS** | Slice 2.1 moved every generator to `asyncio.to_thread`; covering tests green. |
| Checklist load under 1s against Neon dev | **UNVERIFIABLE** | No recorded measurement in `tasks.md`; the spec itself defers this to manual verification. Proxy only: checklist 47 to 36 queries (26 recurrent). |

### Phase 3
> 3-user unmoderated test radicates a cuota in under 10 minutes with zero internal-concept
> questions asked.

| Criterion | Status | Basis |
|---|---|---|
| 3-user unmoderated test | **UNVERIFIABLE** | No recorded evidence the study was run. Not a FAIL: no contrary evidence either. |

### Phase 4
> `npx playwright test` green in CI with no external network and no client data; every
> Phase 1-3 slice's named edge cases are green, not just its happy path.

| Criterion | Status | Basis |
|---|---|---|
| Playwright green in CI | **PARTIAL (superseded)** | The 2 mocked specs (7 tests) ran inside the gate and passed. The 6 journey and 10 edge-case specs are an on-demand runbook per 4.8, not re-run here. |
| No external network / no client data | **PASS** | Synthetic fixtures plus leak guard; fake-LLM backend; prod smoke behind a mechanical write-guard. |
| Every slice's named edge cases green | **PASS, one exception** | See section 6. The exception is WARNING-2. |

**Tally**: 8 PASS, 2 PASS-superseded, 1 PARTIAL-superseded, 3 UNVERIFIABLE, 0 FAIL.

Tracked follow-ups under `tasks.md` "Follow-ups tracked" and each spec's "Out of Scope"
section are **not** re-flagged as defects. They are acknowledged once, as designed. The one
exception is WARNING-6, which the brief explicitly asked to be confirmed and classified.

---

## 6. Edge-Case Audit (standing user requirement)

Verifying edge cases are written **and green**, not just happy paths.

**Boundary values** — COVERED, GREEN.
Derived codigo capped at 50 chars (backend `VARCHAR(50)`); stale window exactly
`PAQUETE_JOB_STALE_SECONDS`; stall threshold with both bounds pinned client-side;
prompt budget 10,659 to 5,604 tokens/iteration; 8 query budgets as upper bounds.

**Empty / null / malformed input** — COVERED, GREEN.
Malformed inference 200 without `requisitos`/`avisos` arrays; `detail` as `[]`, object
array, absent, non-string; malformed paquete-job payload (missing `status`, non-object,
unrecognized status string); Gmail `"data": null`, invalid base64, missing keys; fake-LLM
results missing `id` or returning duplicate keys; zero-item inference recovery.

**Concurrency / double-submit** — COVERED, GREEN.
Backend: concurrent `radicar` via `asyncio.gather` with a real `FOR UPDATE` under Postgres;
CAS `rowcount == 0` loser path; a soft-delete landing inside the lock window; double
create-same-month with credit rollback; double classification enqueue; phantom-insert
retry-once; 4 package-job trigger races (async/async, sync/sync, sync/async, first-ever).
Frontend: synchronous triple-click fires exactly one mutation; `e2e/edge-cases/double-submit.spec.ts`.

**Network / server failures** — COVERED, GREEN.
43 Gmail + 45 Drive + 23 Calendar failure tests (timeout, 4xx, 5xx, DNS, non-`OSError`
transport); 12 LLM failure tests across the real 3-model fallback chain; `GOOGLE_REAUTH_REQUIRED`
distinguished from a transient outage and pinned by a dedicated invariant test; radicar
network-failure and codeless-transport frontend cases; `secop-caido` and `google-desconectado`
e2e specs.

**Stale / pending job states** — COVERED, GREEN.
Stale-pending reset with a forced `updated_at`; stale-running reset; never-triggered
synthetic `pending` that the client refuses to poll; resume-on-mount for a `running` job;
trigger-failed-but-server-enqueued confirmation refetch.

**State-machine invalid transitions** — COVERED, GREEN.
radicar from `aprobada`/`pagada` rejected with 422; terminal state detected after the lock;
`enviada` returns the existing result instead of 422; `rechazada` allowed with a fresh
`fecha_envio`; cross-user access rejected with 403 (the codebase's pre-existing convention).

### Assertion quality

Zero tautologies found — `assert True`, `expect(true).toBe(true)` and `expect(1).toBe(1)`
return 0 matches across both repos' test trees.
Zero unconditionally skipped frontend unit tests. Every Playwright `test.skip` is conditional
on credentials or an explicit env opt-in. Backend carries 6 skip markers, all environmental
(Postgres-only, unreachable datos.gov.co, absent vault PDFs); exactly 1 fired in the run.
`tasks.md` records hand mutation-testing of covering tests in at least 10 slices. Three
separate occurrences of "green test that verifies the wrong thing" were caught and fixed
during apply, which is why the mutation-testing rule was promoted into `design.md`'s Testing
Strategy. Slice 3.4 honestly self-reports a weaker RED for `ProgressBar` and one step-6 test
pair; both carry real assertions today.

**Section verdict**: no happy-path-only area found. The only gap is the accessibility
attribute in WARNING-2, which is a missing attribute, not a missing edge case.

---

## 7. TDD Compliance (Strict TDD Mode)

| Check | Result | Detail |
|---|---|---|
| TDD evidence reported | PARTIAL | No `apply-progress` artifact, no "TDD Cycle Evidence" table (WARNING-4) |
| All tasks have tests | YES | Every `[x]` slice names test files or counts; all referenced files exist |
| RED confirmed (test files exist) | YES | Every path sampled in section 4 resolves on disk |
| GREEN confirmed (tests pass now) | YES | Independently re-run: 3299 backend + 1335 frontend, 0 failures |
| Triangulation adequate | YES | 66 fake-LLM, 45 Drive-failure, 15 radicar-idempotency, 14 checklist-gate cases |
| Safety net for modified files | YES | 3.1 and 2.2/2.3 used existing suites as regression nets |
| Mutation testing of regression nets | YES | Documented in 0.4, 1.2, 1.4, 2.6, 2.7, 3.3, 3.4, 3.6, 4.2, 4.3 |

**TDD compliance: 6/7 checks pass.** The exception is artifact format, not missing evidence.

### Test layer distribution

| Layer | Tests | Files | Tooling |
|---|---|---|---|
| Unit + integration (backend) | 3299 passed, 1 skipped | ~190 under `tests/` | pytest, pytest-asyncio, moto, factory-boy, aiosqlite |
| Unit + integration (frontend) | 1335 passed | 76 | vitest, @testing-library/react full-mount |
| E2E mocked (inside gate) | 7 passed | 2 | Playwright + `page.route()` |
| E2E live backend (not re-run) | 28 journey + 16 edge-case recorded | 16 | Playwright vs a live fake-LLM stack |
| E2E prod smoke (read-only) | credential-gated | 1 | Playwright + mechanical write-guard |

### Coverage

Backend changed-file coverage was not isolated; the gate reports **88% project-wide** against
a 70% floor. The frontend has no coverage tool wired into `npm run gate`, so per-file frontend
coverage is **not available** — not a failure, simply not measured.

### Quality metrics

- Backend linter: gate counter **526**; `ruff check .` alone 381 errors plus 100 unformatted files. Non-blocking by CI design; drifted from the 501 baseline (WARNING-5).
- Backend type checker: **357** mypy errors in 80 of 290 files. Non-blocking; drifted from 336 (WARNING-5).
- Frontend linter: **5 errors, 8 warnings**; 2 errors are real contract violations (WARNING-3).
- Frontend type checker: `tsc --noEmit` **clean**.

---

## 8. Design Coherence

| Decision | Followed? | Evidence |
|---|---|---|
| D1 radicar short-circuit, unlocked gates, narrow lock, CAS | YES | `cuenta_cobro_service.py:1304-1377`, CAS `:1147-1156`, lock `:1254-1258`, `populate_existing` `:259` |
| D2 `requisitos_modo` NULL tri-state, no migration | YES | `modo_efectivo()` present; both REST and agent-tool paths gated; no backfill migration in the tree |
| D3 Package job async-first, sync keeps 200, 409 on lock loss | YES | `paquete_job_service.py`; all 3 endpoints resolve; 9 job tests green |
| D4 Per-row paquete status gated by `useInView` | YES | `lib/use-in-view.ts`; sole consumer `app/cuentas-cobro/page.tsx` |
| D5 Shared client per event loop, `graph-token` excluded | YES | `app/core/http_clients.py` |
| D6 Local pre-merge gate is official | YES | Both gates executed green; hooks, PR templates, dormant workflows present |
| D7 `LLM_PROVIDER=fake` in the running app, stateless fake | YES | `fake_adapter.py`; validator `config.py:99-107`; dispatch `litellm_adapter.py:417` |
| D8 One error-code contract, MRO walk, lint-enforced reader | MOSTLY | MRO walk and `backend-error-codes.ts` verified; lint rule fires but 2 live violations (WARNING-3) |
| D9 `raise_on_sql`, per-session catalog memo | YES | `test_raise_on_sql_slice_2_4a.py`; checklist budget 36 confirms the memo |
| D10 Evidence-based unlock, handle-based `persistir_evidencias` | YES | `handle_id` additive; `evidence_handle_cache` with 12 tests |
| D11 Three groups, group 2 now `"staged"` with `stagedOrder` | YES in code | `steps.ts:243-246` matches D11 exactly. `tasks.md` and `specs/stepper-ux` do not (CRITICAL-2) |
| D12 Frontend never re-derives gate outcomes | YES | `use-radicar-cuenta.ts` adds no client-side hard gate; `checklist-incompleto.spec.ts` |

**12/12 design decisions implemented as designed.** D11 is the only one whose sibling
artifacts have fallen out of sync with the code.

---

## 9. High-Risk Spot-Checks (read from source, not summaries)

### 1. Idempotent / lock-safe `radicar` — CONFIRMED as designed

`app/services/cuenta_cobro_service.py:1304-1377`. Order verified by reading:

1. plain ownership read (`_get_cuenta_con_ownership`);
2. `ENVIADA` short-circuit **before** the gates, returning the existing response;
3. non-`BORRADOR`/`RECHAZADA` rejected with the shared 422 helper;
4. coherence and checklist gates run **unlocked**;
5. `_leer_estado_bajo_lock` (`:1254-1258`) issues
   `SELECT estado ... WHERE id = ? AND deleted_at IS NULL FOR UPDATE`, estado-only, with a
   test-only `nowait` knob, and re-checks for both `ENVIADA` and terminal states;
6. `cambiar_estado` (`:1147-1156`) performs the CAS
   `UPDATE ... WHERE id = ? AND deleted_at IS NULL AND estado IN (borrador, rechazada)`
   with `synchronize_session=False` plus an explicit `db.expire(cuenta)`;
7. `rowcount == 0` re-reads to distinguish idempotent success from a genuine invalid transition.

`_reload_cuenta_response` carries `populate_existing=True` (`:259`) with a docstring naming the
cross-session identity-map staleness it fixes. Matches D1 and the spec clause for clause.

### 2. Package-job stale reset — fix CONFIRMED; sibling bug CONFIRMED still present

`paquete_job_service.py:138-152` forces `job.updated_at = datetime.now(UTC)`, with a comment
naming `History.from_scalar_attribute` as the reason. The sibling
`evidence_classification_service.py:179-183` does **not**:

```python
job.status = EstadoClasificacionJob.PENDING.value
job.total = total
job.procesadas = 0
job.error = None
```

`updated_at` uses column-level `onupdate=func.now()` (`app/models/base.py:28-33`), which fires
only when an UPDATE is actually emitted. The bug is therefore real, but **narrower than
"identical"**: it bites only a stale **PENDING** row whose `total` equals the incoming `total`,
with `procesadas == 0` and `error is None` — then all four assignments are value-identical,
no UPDATE is emitted, the staleness clock never resets, and a second concurrent caller also
reads the row as stale and also wins the lock. A stale **RUNNING** row is safe because
`status` genuinely changes.

The test files mirror this precisely: `test_paquete_job_service.py:392` has
`test_stale_pending_paquete_job_reset_is_not_re_stale_for_a_second_caller`, while
`test_evidence_classification_concurrency.py` has only
`test_stale_running_job_is_reset_and_reenqueued_under_lock` — no stale-pending analogue.

Classification: **WARNING-6**. Impact is a duplicated classification enqueue, not money or
state-machine corruption, and it is reachable only in that boundary case inside the 120 s
`EVIDENCE_JOB_STALE_SECONDS` window. Already tracked in `design.md` Known Risks and
`specs/performance-jobs` Out-of-Scope; recorded once here as confirmed, not re-raised.

### 3. `google_errors.py` mapping — CONFIRMED correct, including ordering at every call site

- `GOOGLE_TRANSPORT_ERRORS` is `(OSError, TimeoutError, httplib2.ServerNotFoundError, TransportError)` and deliberately excludes `RefreshError`.
- That exclusion is pinned by `test_drive_adapter_failures.py:386-389` with **both** a membership assertion and a no-subclass assertion.
- `raise_google_reauth_required` emits `GOOGLE_REAUTH_REQUIRED` with a canned reconnect message and never interpolates `str(exc)`.
- `raise_google_http_error` appends the numeric status plus a per-class hint, and never includes the class name — "HttpError" would reintroduce the forbidden "http" substring the leak tests check for.
- Ordering verified by reading **all 7** call sites: `except RefreshError` precedes `except GOOGLE_TRANSPORT_ERRORS` at Gmail `:271,354,406,492`, Drive's shared `_run` `:109`, and Calendar `:168,219`.
- Drive's `_run` wrapper has 9 call sites, satisfying the spec's "all methods, not only search".

### 4. Group-2 height UX decision — CONFIRMED STILL OPEN, not decided here

`design.md` Open Questions records it as the user's call (collapse members by default /
accordion / keep). `tasks.md` 3.4 lists it as a follow-up. No decision appears in any artifact.
PR #68's `revealMode: "staged"` plus a `stagedOrder` that opens with a virtual Contexto section
materially changes the context in which that decision would be taken, but it is not the
decision. **Recorded as open. This report does not decide it.**

---

## 10. Verdict

### FAIL — artifact accuracy only

The implementation is healthy. Every executable gate is green, coverage is above floor,
81 of 84 spec scenarios have a covering test that passed at runtime in this session, and
all 12 design decisions are implemented as designed. No code change is required by this
report.

The verdict is FAIL because sdd-verify's job is to prove that implementation matches
**specs, design and tasks** — and two of those three no longer describe `master`:

- `tasks.md` accounts for 47 slices but not for 2 merged commits totalling 82 files and roughly 9,279 added lines, including PR #68's restructuring of the exact surface Phase 3 specified.
- `specs/stepper-ux` asserts that group 2 reveals "all members at once"; `master` has shipped `revealMode: "staged"` with a reordered `stagedOrder` and a virtual Contexto section.

Archiving now would freeze a record that materially under-describes what shipped, and would
close out exit criteria against a state the artifacts do not represent.

### What unblocks archive

1. Amend `tasks.md` with slices covering `a93b16e` (PR #68) and `7af14b5`, with their own edge-case lists and exit-criteria impact.
2. Amend `specs/stepper-ux`'s "Three groups" requirement to describe `revealMode: "staged"`, `stagedOrder`, and the virtual Contexto section — `design.md` D11 already has the correct text to align to.
3. Re-baseline the checkpoint SHAs in `design.md` and every spec header from `a730acf`/`db45c9f` to `280fd25`/`7af14b5`.
4. Decide WARNING-2 (add an accessible name to `ProgresoEtapas`) or move it explicitly into the spec's Out-of-Scope with the "legacy uses" wording corrected.
5. Decide WARNING-3 (migrate `lib/checklist-api.ts:352-353` onto `extractApiError`) or add it explicitly to the spec's Out-of-Scope.
6. Flip task 4.8 to `[x]` with a note that only the `--live-e2e` convenience remains, or restate what still blocks it.

Items 1-3 are documentation. Items 4-6 are small and could equally be recorded as accepted
follow-ups. None require reopening the implementation.

### Issue counts

**CRITICAL: 2 · WARNING: 6 · SUGGESTION: 4 · OPEN (user decision): 5**

### Verification limitations, stated plainly

- Live-backend Playwright (28 journey + 16 edge-case assertions) was **not** re-run; it is an on-demand runbook per 4.8's redefinition, not part of `npm run gate`.
- The real-Postgres suite (`-IncludePg`) was **not** run; several concurrency tests only reproduce their race off SQLite.
- Engram was **not** reachable from this executor, so Engram-side artifacts (timing baselines, `sdd/radicacion-sin-friccion/state`, any `apply-progress`) could not be read. All findings rest on files and on executed commands.
- Frontend per-file coverage is not measured by the project's own gate.
