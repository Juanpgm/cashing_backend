# ADR de codebase-memory-mcp — copia de respaldo (2026-09-19)

**Por qué existe este archivo:** un `index_repository` en modo `full` **borra el ADR** del proyecto en `codebase-memory-mcp` (verificado el 2026-09-19: el ADR del frontend desapareció al reindexarlo). Después de cada reindexado completo hay que reponerlo con `manage_adr(mode='update', content=...)` copiando la sección correspondiente de este archivo. Nombres de proyecto en el grafo: `C-Users-User-Documents-workspace-cashing-cashing-backend-master` y `C-Users-User-Documents-workspace-cashing-cashing-frontend`.

---

## Backend (`cashing-backend-master`, master 81aab19)

## PURPOSE
AI-assisted backend for Colombian contractor billing ("cuentas de cobro"): contracts, monthly cuentas, evidence discovery (Gmail/Drive/Calendar), justification generation, package (ZIP) generation and radicacion. Change `radicacion-sin-friccion` archived in openspec/changes/archive/2026-09-19-radicacion-sin-friccion/.

## STACK
Python 3.12, FastAPI, SQLAlchemy 2.0 async, PostgreSQL 16 (prod: Railway) / SQLite aiosqlite (local dev + tests), Alembic, custom async graph engine app/agent/engine.py (CompiledGraph; NOT LangGraph), LiteLLM (Gemini/Groq/Ollama fallback), MCP servers, uv. Deps: pyproject.toml is the single source; uv.lock/requirements*.txt are generated (make lock).

## ARCHITECTURE
Ports and adapters (LLMPort, StoragePort, EmailPort, DrivePort, CalendarPort); routers delegate to services; services raise domain exceptions (core/exceptions.py), never HTTPException. Long work runs as pollable background jobs (paquete job, evidence classification job) with a stale window. Checklist materialization is per cuota: nivel-contrato docs are shared, nivel-cuenta docs are isolated; on a 2nd+ cuota CEDULA/RUT/RPC/CDP are not materialized (PR #94). Debug routes are mounted only for ENVIRONMENT in development|dev|local|test (app/api/router.py).

## PATTERNS
- radicar is idempotent and lock-safe: ENVIADA short-circuit, estado-only SELECT ... FOR UPDATE, CAS UPDATE (cuenta_cobro_service).
- Job reset (`_upsert_job` in paquete_job_service and evidence_classification_service): locking select MUST use execution_options(populate_existing=True) (SQLAlchemy returns the identity-mapped row without refreshing columns), MUST assign updated_at explicitly (no-op reassignment emits no UPDATE so onupdate=func.now() never fires), and paquete reset nulls the previous run's result payload. Same trap still open at secop_service.py:1443-1448.
- Query-count budgets fixture (tests/test_query_budgets.py) + lazy="raise_on_sql" against implicit loads.
- FakeLLMPort (LLM_PROVIDER=fake) makes agent/e2e runs deterministic with zero spend.
- Google adapters map transport errors via app/adapters/google_errors.py; RefreshError is deliberately NOT transient -> GOOGLE_REAUTH_REQUIRED.
- Test command: `uv run python -m pytest` (bare `uv run pytest` segfaults on Windows: python-magic).

## TRADEOFFS
GitHub Actions is billing-locked: the official gate is scripts/pre-merge.ps1 + .githooks/pre-push (GATE OK line in every PR). SQLite tests prove identity-map behavior but FOR UPDATE is a no-op there. PR #95 was verified on real PostgreSQL 16.15 (Neon dev branch, 2026-09-19): two concurrent sessions on a stale job enqueue exactly once in 13/13 scenarios, and the pre-#95 code enqueues twice on the same harness. The repo's own suite on PG is still unproven (tests/conftest.py rejects TLS DSNs and runs DROP SCHEMA public CASCADE before every test, ~3 min/test on Neon; needs Docker: scripts/test-postgres.sh). alembic is decorative on a create_all-built DB (042 collides with create_all; 043 never runs) — check the prod migration path before deploying. Production build predates PR #94/#95 (OpenAPI diff, 2026-09-19). Known follow-ups: no heartbeat during the LLM batch in _ejecutar_clasificacion (stale != dead); app-clock vs func.now() skew; ruff 526 / mypy 357 non-blocking backlog; prod LLM chain has a retired 3rd model (gemini-2.0-flash).

## PHILOSOPHY
Work only in cashing-backend-master/ (cashing-backend/ is a stale checkout). Nothing that writes business data runs against production (docs/prod-test-plan.md): destructive e2e runs on a local stack with a disposable DB (or the Neon dev branch). TDD with RED first and mutation proof; adversarial fresh-context review before merge (a second round finds defects introduced by the first round's fix).

---

## Frontend (`cashing-frontend`, master f7e69c4)

## PURPOSE
Next.js frontend for CashIn: a 3-screen radicacion wizard (stepper) over contracts and cuentas de cobro, plus an agent dock. Backend counterpart: cashing-backend-master.

## STACK
Next.js 15 (App Router), React, TypeScript, Tailwind, React Query, axios (lib/api.ts), vitest + Testing Library (jsdom), Playwright (mocked e2e in the gate; live journeys/edge-cases against a local stack; read-only prod smoke).

## ARCHITECTURE
components/stepper: GROUPS in steps.ts = Screen 1 "Preparar" (contract + cuota + checklist mode), Screen 2 group 2 with revealMode "staged" (stagedOrder [CONTEXTO_MEMBER,4,6,3,5]; computeSectionModel is the single completeness model), Screen 3 "Revisa y radica" (async package job, data-driven preview, radicar). Direct wizard entry from the account detail page. Error handling: all API error detail is read only via extractApiError (ESLint no-restricted-syntax; covers lib/**).

## PATTERNS
- Double-submit guard in step-2-cuota: submitLockRef set synchronously BEFORE mutate (button `disabled` derives from state and lags a render).
- First-load resume (use-group-auto-advance.ts) is skipped if the user navigated before stepper-state resolved: the hook records the `userNavSignal` counter (single writer: stepper-shell switchToGroup -> nav.run; programmatic paths must never bump it) the first time it sees a cuentaId and compares it when data arrives (PR #72). While stepper-state loads only the click on the current group is reachable and it counts as intent (cancels the resume for the visit).
- ProgresoEtapas uses aria-label (stage text already lives in an aria-live region); ProgressBar uses aria-labelledby.
- e2e seedChecklist reads GET /cuentas-cobro/{id}/checklist and uploads only materialized requisitos, with a floor of the 4 always-applicable codes (SEGURIDAD_SOCIAL, INFORME_ACTIVIDADES, INFORME_SUPERVISION, ACTA_INICIO).
- e2e regression guards for timing races must gate the network on the test (hold a promise, release after the action), never on a wall-clock delay (a delay can false-pass under load).
- e2e/smoke is read-only by construction (remote-guard aborts every write except login/refresh).

## TRADEOFFS
GitHub Actions billing-locked: gate is `npm run gate` (unit + tsc + eslint changed files + build + mocked e2e) + pre-push hook. vitest's per-test timeout is WALL-CLOCK (Date.now), so CPU-starved runs fail 10 ms tests; contraste.test.ts fixed (18/40 -> 0/40 under load); components/__tests__/checklist-full-view.test.tsx "chunks a drop of more than 20 files..." still fails ~3/8 under load. Live specs have 60/90 s budgets that are too short for Neon latency (double-submit, radicar-tras-enviada, secop-flujo) and a fixed 400 ms wait in evidencias-clasificacion. Group-2 height UX (5800-7100 px in the "all" mode; staged rail mitigates) remains an OPEN USER decision.

## PHILOSOPHY
Local first: verify on a local stack (LLM_PROVIDER=fake) before any deploy; ask before deploying. Squash-merge PRs with the literal GATE OK line. .env.local may point at a stale port (:8003): set NEXT_PUBLIC_API_URL per process, never commit it.
