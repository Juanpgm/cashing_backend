# Design: Radicación Sin Fricción

> **Retroactive (as-built).** Written 2026-09-18 after the 46 planned slices merged (backend `master` @ `a730acf` / PR #92, frontend `master` @ `db45c9f` / PR #67), and **amended 2026-09-19** to the state after the first `sdd-verify` run: backend `master` @ `81aab19` (PR #95), frontend `master` @ `11bf4c4` (PR #71). Work merged after the original checkpoint is Phase 5 in `tasks.md` (5.1–5.8: PRs backend #93/#94/#95, frontend #68/#69/#70/#71 and commit `7af14b5`). It documents the architecture that actually landed, so `sdd-verify` can check the implementation against it. Slice-by-slice evidence lives in `tasks.md`.

## Technical Approach

Incremental hardening in place, no rewrite (proposal decision #7 and the non-goals). Each phase attacks one axis and leaves the next phase a measurable seam:

| Phase | Approach as built |
|---|---|
| 0 — Foundations | Make the system *measurable and deterministic* before changing it: a `QueryCounter` fixture with per-endpoint budgets (`tests/conftest.py`), agent-turn instrumentation (`ToolEvent.duration_ms`, `fallback_depth`, structlog `contextvars`), and a real-app fake LLM (`LLM_PROVIDER=fake`) — not a test-only stub. |
| 1 — Close the loop | Wire the existing pieces end to end: wizard step 7 owns `radicar`; `radicar` becomes idempotent/lock-safe; one error-code contract UI↔backend; agent gets escape-hatch tools, a handle-based `persistir_evidencias`, a phase-gated tool set and per-turn timeouts. |
| 2 — Performance | Remove cost without changing contracts: `raise_on_sql` instead of the `selectin` cascade, no `_reload_*` double-loads, `asyncio.to_thread` for doc generators, a poll-based `PaqueteJob`, batched evidence upload, a process-shared `httpx.AsyncClient`. |
| 3 — Three-screen UX | Reshape the existing 7-step wizard shell into 3 screens by merging `GROUPS`, not by rebuilding it: split the two largest components first (pure refactor, vitest as the net), then one entry point, one confirmation idiom, tokens, mobile/a11y floor, copy, docked agent. |
| 4 — Edge cases | Transversal: factory-boy builders, concurrency and network-failure-injection suites, synthetic fixtures replacing real client data, shared e2e helpers, `data-testid` coverage, journey + edge-case Playwright against a live fake-LLM backend. |

## Architecture Decisions

| # | Decision | Rejected alternative | Rationale |
|---|---|---|---|
| D1 | **`radicar` = unlocked ENVIADA short-circuit → gates → single-column `SELECT … FOR UPDATE` (`_leer_estado_bajo_lock`) → CAS `UPDATE … WHERE estado IN (borrador, rechazada) AND deleted_at IS NULL`**; `rowcount == 0` returns the existing result. | Hold the lock across the whole gate run (~76 queries); keep returning 422 on the second call. | Narrow lock window; second submit is idempotent, not an error. `_reload_cuenta_response` needs `populate_existing=True` — the identity map otherwise returns a stale cross-session row (Postgres-only bug). |
| D2 | **`requisitos_modo` NULL stays a real tri-state**; `modo_efectivo()` resolves it, and the SECOP-refresh / auto-vincular paths are gated on the checklist gate. | Materialize `estandar` at creation + backfill migration (the original plan). | Audit overturned the plan: `DefinirChecklistGate` and `stepper_state_service`'s step-3 completion both read `requisitos_definidos`. Materializing would silently skip the gate. **No migration written.** |
| D3 | **Package generation is async-first in the wizard**: `POST /paquete/regenerar-async` (202) + `GET /paquete/job` poll, backed by a `PaqueteJob` row per cuenta mirroring `ClasificacionEvidenciasJob` (`FOR UPDATE` re-select, staleness window, phantom-insert retry). Sync `POST /paquete/regenerar` keeps its exact 200 shape, now behind the same lock, 409 on lock loss. | Replace the sync endpoint; keep sync-only generation. | Additive: agent/REST callers are untouched, the wizard stops freezing. Client polls only while `running` (or `pending` after a local trigger) — the backend returns a synthetic `pending` for a never-triggered cuenta. |
| D4 | **Per-row paquete status stays a per-row query, visibility-gated by `useInView`** (`lib/use-in-view.ts`, IntersectionObserver, reveal-once). | Fold paquete status into a batch/JOIN list endpoint. | The live data is a storage HEAD call plus obligación recomputation — no cheap DB-only equivalent without accepting a stale snapshot. |
| D5 | **Shared `httpx.AsyncClient` per event loop** (`app/core/http_clients.py`, `WeakKeyDictionary` keyed by the loop object) for SECOP/Graph/Wompi — **except the `graph-token` OAuth client, which stays per-call**. | Module-level singleton (the `langfuse_client.py` pattern); share every client. | A module singleton breaks under pytest-asyncio's per-test loops (same lesson as the asyncpg `NullPool` fix). A shared OAuth-token client would **leak cookies across users** — no perf case justifies it. |
| D6 | **The pre-merge gate is local**: `.githooks/pre-push` → `scripts/pre-merge.ps1` (backend) / `npm run gate` (frontend), with a literal `GATE OK <sha> … dirty=no` line required by `.github/pull_request_template.md`. Workflows stay as dormant spec. | GitHub Actions (billing-locked, `startup_failure` in 0-5 s on both repos); Railway as a merge gate; branch protection. | Railway has no PR/merge integration and the frontend does not deploy through it; protection on the private repo needs a paid plan *and* a runner. Escape hatches: `SKIP_GATE=1`, `--no-verify`. |
| D7 | **Determinism seam = `LLM_PROVIDER=fake` in the running app**; `FakeLLMPort` is stateless and derives position from `messages` (`_tool_results` / `_known_ids` thread real IDs; cross-turn continuity via `AGENT_RECAP_MARKER`; `FAKE_LLM_SCRIPT=happy\|malformed\|stall`). | A test-only `ScriptedLLM` opt-out; an instance-stateful fake. | Playwright and the E2E playbook must drive the **real** `chat_with_tools` loop. Statelessness is what makes concurrent sessions safe. |
| D8 | **One error-code contract**: `domain_to_http` walks the MRO; `lib/backend-error-codes.ts` is the checked-in list with a vitest that fails on any gap; `extractApiError` is the single reader, enforced by an ESLint `no-restricted-syntax` ban on `.detail` outside `lib/api.ts` — which covers `lib/**` too since 5.6 migrated the last hand-rolled reader (`lib/checklist-api.ts`). | Exact-`type(exc)` mapping; per-site `err.detail` reads (18 sites). | Unknown codes fall back to a generic Spanish message — never the raw code, never axios English. |
| D9 | **Kill the `selectin` cascade with `raise_on_sql`** on `Contrato.cuentas_cobro`, `Obligacion.actividades`, `CuentaCobro.borradores`; memoize the checklist catalog **per session** (`db.info`). | Blanket explicit `selectinload` at "~12 call sites"; a process-wide catalog cache. | An exhaustive sweep proved zero production call sites read those relationships by attribute (2.4b closed as a genuine no-op). A process-wide cache is unsafe given how tests recreate schema and how `/refresh-secop` rolls back. |
| D10 | **Agent cuenta-scoped tool unlock is evidence-based** (a real `CuentaCobro`-shaped record in any tool output), plus a sticky recap line; `persistir_evidencias` takes a cache **handle**, not a re-emitted payload. | Tool-name-based unlock; re-emitting `list[ObligacionJustificada]`. | Name-based unlock deadlocked any user who found their cuenta via `listar_cuentas_cobro`. Re-emission is structurally impossible under the 1,024-token output cap. 10,659 → 5,604 tokens/iteration. |
| D11 | **Three screens by merging `GROUPS` (4 → 3)**, adding `GroupMeta.revealMode`. Group 2 landed as `"all"` (3.4, PR #65) and **is `"staged"` on master** (5.3, PR #68 `a93b16e`): `members: [4, 6, 3, 5]`, `stagedOrder: [CONTEXTO_MEMBER, 4, 6, 3, 5]`, `revealMode: "staged"` (`components/stepper/steps.ts:244-246`). `members` stays raw-steps-only (`Math.max`/`Math.min` derivations), so gating is order-independent and the virtual Contexto section can never block radicación. One pure model, `section-model.ts` (`computeSectionModel`), separates `done` (backend says finished — the only thing that may claim completion) from `passable` (the walk may move on), and is the single source for the rail, rows, ack, receipt, footer and gate. | Tabs; restructuring the backend's 7-step contract; five ad-hoc completeness helpers (round 3 of PR #68 found them contradicting each other on screen). | The backend step contract is untouched by the UI — the change is presentational. (Backend `_step5_formato` did change in 5.2 to gate on the two informes; the frontend consumes it and never re-derives it, per D12.) |
| D12 | **The frontend never re-derives gate outcomes**; the backend's `radicar` gate is authoritative and its findings are rendered. | Client-side readiness gating before `POST /radicar`. | Confirmed by 4.7b: a client-derived gate cannot see a checklist document removed concurrently. |
| D13 | **Job-row stale reset is race-safe by construction** (5.5, PR #95): in both `_upsert_job` functions the `FOR UPDATE` re-select carries `populate_existing=True`, the reset assigns `updated_at = datetime.now(UTC)` explicitly, and the paquete reset nulls the previous run's result payload (`storage_key`, `filename`, `size_bytes`, `listo_para_radicar`, `pendientes`, `advertencias_coherencia`, `es_borrador`). | Relying on `onupdate=func.now()` alone; a plain locking `SELECT` that trusts the identity map. | SQLAlchemy returns an already-mapped, non-expired instance as-is and emits no UPDATE for an assignment equal to the current value, so a second caller kept the stale `status`/`updated_at` and double-enqueued (found by review r1, proven with a two-session interleaving test). Nulling the payload stops a `pending` job from exposing the previous package to a poller. `populate_existing` is safe only under autoflush (the app's session config); under `no_autoflush` it would discard an uncommitted write — the same trap `_reload_cuenta_response` documents (D1). Not yet proven against real Postgres lock serialization (SQLite tests only). |
| D14 | **First-cuota applicability is one shared seam** (5.2, PR #94): `requisito_aplica_a_cuenta` / `listar_filas_visibles` in `checklist_service.py` decide which standard requisitos apply to a cuenta and are read by the checklist view, resumen, radicar gate, constancia PDF and package. CEDULA/RUT/RPC/CDP hide after cuota 1; CONTRATO hides only while a shared document exists and the contrato has obligaciones; ACTA_INICIO/FICHA_TECNICA stay (nivel-contrato); a row with real content is never hidden; a settled cuenta's requisitos are never created or deleted by a redefinition. Legacy rows are filtered at read time; migration `043` updates the catalog flags for existing deployments. | Five ad-hoc checks per consumer; a data backfill that deletes legacy rows. | Deleting rows on a settled cuenta was a data-loss BLOCKER found in review round 3 (an approved cuenta's mandatory documents vanished and `radicacion_lista` flipped to true). Read-time filtering needs no data migration for the legacy rows and hides them instead of deleting them. |

## Data Flow

```
Screen 1 Preparar        Screen 2 Completá         Screen 3 Revisá y radicá
(contrato, cuota,        (staged rail: Contexto →  (paquete preview + radicar)
 checklist MODE)          Evidencias → Justific. →
                          Soportes → Formatos)
      │                        │                          │
      ▼                        ▼                          ▼
 stepper-shell.tsx ── shared ["checklist", cuentaId] / stepper-state queries
      │                        │                          │
      │                        │              POST /paquete/regenerar-async (202)
      │                        │                          │  ┌── GET /paquete/job (poll while running)
      ▼                        ▼                          ▼  ▼
  cuenta_cobro_service   evidence/classification    paquete_job_service ── PaqueteJob row
      │                   (batched IN dedup,          (FOR UPDATE + staleness window)
      │                    gather+Semaphore(8))               │
      └──────────── POST /radicar ────────────────────────────┘
                     ENVIADA short-circuit → gates → FOR UPDATE → CAS update

AgentDock (SSE /agent/chat/stream) ──► chat_with_tools ──► phase-gated tool catalog
        approval gate on write-tagged tools          ──► LLMPort (litellm | FakeLLMPort)
Google adapters ──► google_errors.py (GOOGLE_TRANSPORT_ERRORS / GOOGLE_REAUTH_REQUIRED) ──► 502 ExternalServiceError
```

## File Changes (module granularity — see `tasks.md` for per-slice detail)

| Path | Action | Role |
|---|---|---|
| `cashing-backend-master/app/services/cuenta_cobro_service.py` | Modify | Idempotent/lock-safe `radicar`, `populate_existing` reload fix, `IntegrityError` → `CUENTA_MES_DUPLICADA`. |
| `cashing-backend-master/app/models/paquete_job.py`, `app/services/paquete_job_service.py`, `alembic/versions/042_paquete_job.py` | Create | Poll-based package job + per-cuenta lock. |
| `cashing-backend-master/app/core/http_clients.py` | Create | Event-loop-scoped shared `httpx.AsyncClient` cache (D5). |
| `cashing-backend-master/app/adapters/google_errors.py` | Create | Shared transport-error tuple + reauth mapping (D8/4.3). |
| `cashing-backend-master/app/services/checklist_service.py`, `app/services/requisito_cuenta_service.py`, `app/services/stepper_state_service.py`, `alembic/versions/043_checklist_primera_cuota_flags.py` | Modify/Create | First-cuota applicability seam, settled-cuenta guard, step-5 informes gate, catalog-flag data migration (D14, 5.2). |
| `cashing-backend-master/app/services/{paquete_job_service,evidence_classification_service}.py` | Modify | Race-safe stale reset: `populate_existing`, explicit `updated_at`, payload nulling (D13, 5.5). |
| `cashing-backend-master/app/adapters/llm/fake_adapter.py`, `app/core/config.py` | Create/Modify | `LLM_PROVIDER=fake`, `FAKE_LLM_SCRIPT`, outcome-aware routing (D7). |
| `cashing-backend-master/app/services/agent_chat_service.py`, `app/tools/catalog/*` | Modify/Create | Escape-hatch tools, handle-based evidencias, phase-gated catalog, per-turn timeout, SSE + approval gate. |
| `cashing-backend-master/app/core/exceptions.py` | Modify | MRO-walking `domain_to_http`, new codes. |
| `cashing-backend-master/{scripts/pre-merge.ps1,.githooks/pre-push,.github/pull_request_template.md}` | Create | Local gate (D6). Frontend mirror: `scripts/gate.mjs`, `.githooks/pre-push`. |
| `cashing-frontend/components/stepper/{steps.ts,stepper-shell.tsx,hooks/*,steps/step-*.tsx}` | Modify/Create | 3-group merge, `revealMode`, screens 1–3, async paquete wiring, docked agent. |
| `cashing-frontend/components/checklist/*` | Create | 3.1 split of `checklist-full-view.tsx` (1672 → 421 lines at 3.1; 466 on master after 5.3), later `use-requisito-patch.ts` and `checklist-shared.tsx` helpers (`categoriaRequisito`, `resumenDeItems`). |
| `cashing-frontend/components/stepper/{section-model.ts,staged-section-list.tsx,contexto-section.tsx,contexto-omitido.ts,hooks/use-done-ack.ts}` | Create | Staged Pantalla 2: single completeness model, Contexto section, done-ack (D11, 5.3). |
| `cashing-frontend/lib/{backend-error-codes.ts,use-in-view.ts,paquete-api.ts}` | Create | Error-code contract, visibility gate, async job client. |
| `cashing-frontend/e2e/{journey,edge-cases,smoke,helpers,fixtures}/**` | Create/Move | Playwright layout, shared seed/auth, synthetic fixtures, read-only prod write-guard. |
| `cashing-frontend/components/RadicarModoModal.tsx` | Delete | Duplicate creation path retired (3.2). |

## Interfaces / Contracts

```
POST /api/v1/cuentas-cobro/{id}/paquete/regenerar-async  → 202 {job}     # new, wizard path
GET  /api/v1/cuentas-cobro/{id}/paquete/job              → 200 {estado}  # pending|running|done|failed
POST /api/v1/cuentas-cobro/{id}/paquete/regenerar        → 200 (unchanged shape) | 409 PAQUETE_GENERACION_EN_CURSO
POST /api/v1/cuentas-cobro/{id}/radicar                  → 200 (idempotent on ENVIADA)
POST /api/v1/agent/chat/stream                           → SSE tool_events + approval-control endpoints
```
Error codes are a closed set shared by `app/core/exceptions.py` and `lib/backend-error-codes.ts`; adding one backend-side without the frontend entry fails a vitest.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit (backend) | Services, adapters, tools | pytest, strict TDD per slice; `factory-boy` builders; network-failure injection (timeout/4xx/5xx/malformed) for Gmail/Drive/Calendar/LLM. **Every "regression net" must be mutation-tested** (three slices shipped green-but-vacuous tests before this rule). |
| Perf regression | Query budgets | `QueryCounter` fixture, per-endpoint budgets: contratos 11, contrato detail 19→11, checklist 47→36, radicar 80→70(+1), stepper-state 24, evidencias 41. |
| Concurrency | Double radicar / double create-same-month / double job enqueue | Real `asyncio.gather` + Postgres (`make test-pg`, `scripts/test-postgres.sh`) — several races only reproduce off SQLite. |
| Unit (frontend) | Components, hooks, error mapping | vitest full-mount; 1103 tests at the original checkpoint, **1389 at `11bf4c4`**; backend `pytest` 3329 passed / 1 skipped at `81aab19`. |
| E2E | 6 journey + 10 edge-case + smoke specs (plus 2 mocked specs in the gate and 2 env-gated screenshot specs at the `e2e/` root) | Playwright against a **live** backend with `LLM_PROVIDER=fake`, synthetic fixtures only; production smoke is read-only behind a mechanical write-guard fixture. |
| Review | Every slice | Implementer self-verification **plus** a fresh-context adversarial opus review before any PR (proposal decision #6). |

## Migration / Rollout

Two migrations: `042_paquete_job.py` (additive table) and `043_checklist_primera_cuota_flags.py` (data-only `UPDATE` of the `solo_primera_cuenta` flag for RPC/CDP/CONTRATO in `requisitos_documento`, because the in-app seed only inserts missing codes; chained on 042, added by 5.2). D2 deliberately produced **no** migration. `raise_on_sql` (D9) is a code-level flag, not a schema change. No feature flags; `LLM_PROVIDER` / `FAKE_LLM_SCRIPT` / `RATE_LIMIT_ENABLED` default to production-safe values and are opted out of only via a git-ignored `secrets/.env.local`. Rollout is per-slice `stacked-to-main` PRs, each gated locally before push (D6). No production deploy is in scope (proposal non-goals).

## Open Questions

- [ ] **Group 2 height — OPEN USER DECISION (recorded, not decided).** Under `revealMode: "all"` group 2 spanned ~5800–7100 px in a ~373 px scroll band and 3.4 left the call (collapse members by default / accordion / keep) to the user. Group 2 is now `"staged"` (D11, 5.3): PR #68's body says only one section renders at a time, which mitigates the length, but no artifact records a decision — the mitigation is context for it, not the answer. Nothing in `tasks.md`, this file or any spec decides it.
- [ ] `GET /paquete/job` returns a synthetic `pending` for a never-triggered cuenta — no real "no job" signal (404/null), which also hides a resumed job whose worker died before starting.
- [ ] Langfuse tracing is wired but permanently disabled (`langfuse` is not a declared dependency) — adding it is a real cost decision left to the user.
- [ ] Prod smoke still blocked on user inputs: Vercel production URL, live API hostname confirmation, the dedicated read-only account, prod `WAITLIST_ENABLED`.
- [ ] `cruzar`/semáforo manual re-analysis trigger: explicitly deferred at 3.2a, needs its own design.

## Known Risks / Tracked Follow-ups (deliberately out of scope)

- In-process caches (`evidence_handle_cache`, shared http clients, per-session catalog) are correct **only** under the current single-worker deployment (`--workers 1`).
- `PaqueteJob.updated_at` is stamped once at the `running` transition, never heartbeat-refreshed; the crash-recovery window widened 120 s → 300 s as an accepted consequence. The **no-op reassignment / `onupdate` never fires** bug in the sibling `evidence_classification_service._upsert_job` reset branch was **fixed in 5.5** (D13); a third unlocked writer to the same storage key still lives in `app/tools/catalog/paquete.py`.
- Job-reset residuals from 5.5 (non-blocking): no heartbeat during the LLM batch in `_ejecutar_clasificacion` (a live job can exceed the 120 s stale window); `secop_service.py:1443-1448` has the same identity-map trap on a second locking select (still no `populate_existing`); the reset writes `updated_at` from the app clock while other writes use `func.now()` (cosmetic at 120 s); D13 is proven on SQLite only — the two touched test files have not been run against real Postgres (`scripts/pre-merge.ps1 -IncludePg`).
- `checklist_service`'s requisito-mutators, `PATCH /checklist/{codigo}` and the auto-vincular-on-mount path have **no estado gate** (guarded at the agent-tool layer only) — P1, needs a paired frontend read-only mode for `enviada`. 5.2 added a settled-cuenta guard only on checklist redefinition (`definir_set`) and row materialization (D14), not on these per-requisito mutators (`cuenta_esta_cerrada` is not referenced from `app/api/v1/checklist.py`).
- `numero_cuota` / `posicion=primera` have no backing partial unique index; a test pins the corrupted state loudly rather than passing silently.
- A suppressed classification enqueue during a job's `running` window can leave evidence unclassified forever with no surfaced error.
- `QueryCounter.assert_budget` is upper-bound only — Phase 2's reductions do not auto-tighten the ratchet.
- Live-backend Playwright is an on-demand local runbook, not an automated gate (`npm run gate -- --live-e2e` tracked, not built; confirmed absent from `scripts/gate.mjs` at `11bf4c4`).
- Backend `ruff`/`mypy` backlogs drifted above the 4.3 baseline (`ruff=501 mypy=336` → `ruff=526 mypy=357` at `81aab19`); non-blocking by design, largely in files touched by 5.1/5.2.
- Load-flaky frontend unit tests near vitest's 5000 ms wall-clock budget under CPU starvation (`checklist-full-view.test.tsx` "chunks a drop of more than 20 files…" 3/8 in stressed runs; others listed in 5.8).
- Design-token layer has only 2 real consumers (188/116/94 raw violet/rose/emerald references remain); `text-base`/`text-sm` collapse to the same 14 px on ~13 headings, a direct consequence of the plan's own "base 14px" ask.
