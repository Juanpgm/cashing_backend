# Performance and Background Jobs Specification

Retroactive spec, amended 2026-09-19 to backend `81aab19` (originally `a730acf`). Task ids refer to `tasks.md`.

## Purpose

Reads are cheap, the event loop is not blocked by document generation, and long operations run as lock-safe, pollable jobs.

## Requirements

### Requirement: Query-count budgets (0.2, 2.2-2.5)

A reusable query counter MUST enforce per-endpoint budgets in tests (budget assertions are upper bounds). Final measured values after Phase 2: contratos list 11, contrato detail 11 (was 19), checklist 36 (was 47), radicar 70 (was 80/81). Response builders MUST use the in-session object (no reload double-loads) except where a targeted refresh is needed for DB-normalized values (e.g. money scale, `updated_at`).

#### Scenario: Budget breach
- WHEN an endpoint exceeds its budget
- THEN the test fails with a message naming endpoint, actual and budget

#### Scenario: Money scale preserved
- GIVEN a monetary value written as `"1234567.8"`
- THEN the response shows the DB-normalized `"1234567.80"`

### Requirement: No implicit relationship loads (2.4a, 2.4b)

`Contrato.cuentas_cobro`, `Obligacion.actividades` and `CuentaCobro.borradores` MUST use `raise_on_sql`. Callers MUST query child models directly. No explicit `selectinload` sites were needed (2.4b closed as no-op).

#### Scenario: Implicit access
- WHEN code accesses one of these relationships unloaded
- THEN it raises instead of issuing a lazy query

### Requirement: Document generation off the event loop (2.1)

WeasyPrint, python-docx, openpyxl and zip generation MUST run in worker threads and MUST NOT share mutable state across the thread boundary; exceptions MUST propagate unchanged.

#### Scenario: Generator raises
- WHEN a generator raises
- THEN the same exception reaches the caller

### Requirement: Batch evidence upload (2.6, 1.9)

Evidence upload MUST dedupe with one `IN` query plus an in-memory seen-set, upload with bounded concurrency (8), and commit once per batch. On batch failure the service MUST roll back itself, leaving no flushed stub actividad. Matcher/justify fan-out MUST be bounded. Background tasks MUST be plumbed only through the synchronous chat path.

#### Scenario: Duplicate within one batch
- WHEN two files in one batch are identical
- THEN one row is created

#### Scenario: Failure at item N
- WHEN item N fails
- THEN nothing from the batch is committed

### Requirement: Package generation as a pollable job (2.7, 3.5, 5.5)

`POST /paquete/regenerar-async` MUST return 202 and `GET /paquete/job` MUST report status. A per-cuenta row lock MUST make concurrent triggers converge on one job; a job is stale only after `PAQUETE_JOB_STALE_SECONDS` (300). The existing sync `POST /paquete/regenerar` MUST keep its 200 shape and MUST return 409 `PAQUETE_GENERACION_EN_CURSO` if it loses the lock race.

The stale-job reset in **both** job upserts (`paquete_job_service._upsert_job` and `evidence_classification_service._upsert_job`, stale window `EVIDENCE_JOB_STALE_SECONDS` = 120 for classification) MUST: (a) re-select the row under `FOR UPDATE` with `populate_existing=True`, so a caller that read the row before a winner committed does not keep the stale identity-mapped `status`/`updated_at`; (b) assign `updated_at` explicitly (`datetime.now(UTC)`), because SQLAlchemy emits no UPDATE when every assigned attribute equals its current value and `onupdate=func.now()` would never fire. The paquete reset MUST also null the previous run's result payload (`storage_key`, `filename`, `size_bytes`, `listo_para_radicar`, `pendientes`, `advertencias_coherencia`, `es_borrador`) so a `pending` job never exposes the previous package; the response shape is unchanged. `populate_existing` is safe here only because the session runs with autoflush on; under `no_autoflush` it would discard an uncommitted write.

#### Scenario: Concurrent triggers
- WHEN two triggers arrive for one cuenta
- THEN one job runs and the second receives the existing job

#### Scenario: Stale pending job
- GIVEN a pending job older than the stale window
- WHEN triggered again
- THEN the row is reset and `updated_at` is bumped so a third caller sees it fresh

#### Scenario: Stale pending classification job with an equal total
- GIVEN a stale PENDING classification job whose `total` equals the incoming `total`, with `procesadas == 0` and no error
- WHEN triggered again
- THEN `updated_at` is still bumped and a second concurrent caller sees the row as fresh (no duplicate enqueue)

#### Scenario: Second caller with a stale identity map
- GIVEN caller B read the job row before caller A committed its reset
- WHEN B takes the `FOR UPDATE` lock
- THEN B sees A's committed `status` and `updated_at` and does not enqueue a second run

#### Scenario: Reset clears the previous result
- GIVEN a stale paquete job that previously finished with a package
- WHEN it is reset to `pending`
- THEN `storage_key`, `filename`, `size_bytes`, `listo_para_radicar`, `pendientes`, `advertencias_coherencia` and `es_borrador` are all null

#### Scenario: Never-triggered cuenta
- WHEN `GET /paquete/job` is polled for a never-triggered cuenta
- THEN a synthetic `pending` is returned and the client does not poll on it

### Requirement: Shared HTTP clients and query hygiene (2.8)

SECOP and Wompi calls MUST reuse a per-event-loop cached `httpx.AsyncClient`. The Graph OAuth token client MUST be fresh per call (no cookie sharing across users). The frontend MUST disable refetch on window focus and MUST fetch per-row paquete status only once the row is visible.

#### Scenario: Row scrolled into view
- GIVEN a list row not yet visible
- THEN its paquete query has not fired; after reveal it fires and later invalidations still apply

## Out of Scope (tracked follow-ups)

- Heartbeat/age exposure on `GET /paquete/job`; unlocked agent-tool writer to the package storage key (`app/tools/catalog/paquete.py`). (The classification job's reset no-op bug was fixed in 5.5 and is now a requirement above.)
- No heartbeat during the LLM batch in `_ejecutar_clasificacion`: a classification job can exceed the 120 s stale window while still alive (pre-existing; 5.5 follow-up).
- `secop_service.py:1443-1448` has the same identity-map trap on a second locking select (no `populate_existing`; 5.5 follow-up).
- The stale reset writes `updated_at` from the application clock while other writes use `func.now()` — clock skew is cosmetic at a 120 s threshold (5.5 follow-up).
- The two touched job test files have not been run against real Postgres (`scripts/pre-merge.ps1 -IncludePg`): the SQLite tests prove the identity-map defect and its fix, not real lock serialization (5.5 follow-up).
- Unwrapped parsing/inference call sites (2.1); exact-match ratchet mode for budgets (0.2).
- The Phase 2 exit criterion "checklist load under 1s against Neon dev" has no recorded measurement in `tasks.md`; verify manually, not asserted here.
