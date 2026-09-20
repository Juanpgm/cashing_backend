# Full radicación flow against the Neon DEV branch (real PostgreSQL) — 2026-09-19

Closes the gap "the real-PostgreSQL suite / FOR UPDATE behavior of backend PR #95 (`81aab19`) was
never executed". Docker Desktop is down, so `scripts/test-postgres.sh` could not run; instead the
whole local stack was pointed at the project's own Neon **dev** branch.

---

## 1. Target confirmation

| What | Value |
|---|---|
| Neon endpoint (name only) | `ep-autumn-union-acgvv03i` (region `sa-east-1`) |
| Branch | **dev** — matches `neon_dev_setup.md` (`br-frosty-moon-acaqoxba` → endpoint `ep-autumn-union-acgvv03i`) |
| Database | `neondb` |
| Server | PostgreSQL **16.15** |
| Railway production | **never touched** — no request of any kind was made to `cashin-api-production.up.railway.app` |

The DSN comes from `cashing-backend/secrets/.env.local` (`DATABASE_URL_NEON_DEV_DIRECT`), read at
runtime *inside* each process, exactly as `start-local-neon.ps1` does. It was never printed, echoed,
logged, or put on a command line. Every script carries a hard guard that refuses to run if the host
is not `ep-autumn-union-acgvv03i.*.aws.neon.tech` or looks like Railway.

`start-local-neon.ps1` lives at the **workspace root** (`C:\Users\User\Documents\workspace\cashing\`),
not inside the backend repo; it already points `$backend` at `cashing-backend-master`, but reads the
secrets file from that worktree — where `secrets/.env.local` only contains `LLM_PROVIDER=fake` and
`RATE_LIMIT_ENABLED=false`. The Neon DSN actually lives in the sibling `cashing-backend/secrets/.env.local`.
**The script as committed would fail its own guard.** (Not fixed — out of scope, reported.)

---

## 2. Schema drift (read-only check first)

`app/core/database.py:Base.metadata` vs `information_schema` of `neondb`:

| Finding | Detail |
|---|---|
| Missing tables | **1** — `paquete_job` (PR #95 / migration `042`) |
| Extra tables in DB | 0 (besides `alembic_version`) |
| Missing columns | **0** |
| Extra columns in DB | 0 |
| Enum drift | `tipo_documento_fuente` has 2 extra **lowercase** labels in the DB (`cdp`, `otros`) that the model does not declare. Harmless superset, leftover of the known `gotcha_enum_labels_postgres` issue. `integracion_provider` is absent from `pg_type` — **not drift**: the model declares it `native_enum=False` (VARCHAR + CHECK). |
| `alembic_version` | `041_cdp_enum_uppercase` |
| `requisitos_documento` catalog | **empty** in Neon (local SQLite has 14 rows). The app's `_seed_catalogo_si_vacio` seeds it from `_CATALOGO_SEED`, which already carries the post-`043` `solo_primera_cuenta` flags — so migration `043` is a no-op here and no flag drift exists. |

Drift was **additive only**, so the backend was allowed to boot normally.

### DDL actually applied (all by the app's own `lifespan`, all additive)

```sql
CREATE TABLE paquete_job (
  cuenta_cobro_id UUID NOT NULL, status VARCHAR(20) NOT NULL, storage_key VARCHAR(500),
  filename VARCHAR(255), size_bytes INTEGER, listo_para_radicar BOOLEAN, pendientes INTEGER,
  es_borrador BOOLEAN, advertencias_coherencia JSON, error TEXT, error_code VARCHAR(64),
  id UUID NOT NULL, created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
  PRIMARY KEY (id), CONSTRAINT uq_paquete_job_cuenta UNIQUE (cuenta_cobro_id),
  FOREIGN KEY(cuenta_cobro_id) REFERENCES cuentas_cobro (id));
CREATE INDEX ix_paquete_job_cuenta_cobro_id ON paquete_job (cuenta_cobro_id);
CREATE TABLE IF NOT EXISTS alembic_version (...);                        -- no-op, table existed
ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(255);  -- widening, app's own guard
```
Plus, later, a `CREATE DATABASE "e2e_pg_1789837784_test"` for the pytest run (dropped at cleanup).
**No DROP / TRUNCATE / DELETE of any pre-existing row, no `drop_all`, no downgrade.**

### Environment finding: alembic is still decorative here

`create_all` runs first and creates `paquete_job`; the subsequent `alembic upgrade head` then dies on
`042_paquete_job` with `DuplicateTableError: relation "paquete_job" already exists`, so
`alembic_version` stays pinned at `041` and `043` never runs. Logged as `alembic_failed`, non-fatal —
this is the pre-existing "alembic no construye el schema base" problem from `neon_dev_setup.md`, now
also confirmed to block *forward* migrations on a create_all-built DB.

---

## 3. Stack as run

| Piece | Setting |
|---|---|
| Backend | `cashing-backend-master` @ `81aab19`, `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| DB | Neon dev branch (direct endpoint, TLS via `app/core/db_ssl.prepare_pg_url`, asyncpg) |
| Env | `LLM_PROVIDER=fake`, `STORAGE_PROVIDER=local`, `RATE_LIMIT_ENABLED=false`, `WAITLIST_ENABLED=false`, `ENVIRONMENT=development`, `MCP_ENABLED=false`. No Google / production credentials. Docker never started. |
| `GET /health` | `{"status":"ok","environment":"development","version":"0.2.1"}` |
| Frontend | `cashing-frontend` @ `11bf4c4`, `npm run dev` on `:3000` with `NEXT_PUBLIC_API_URL=http://localhost:8000` set **on the process only** — `.env.local` (stale `:8003`) untouched |

**Measured Neon latency** (this is the key to the failure classification below):

| Call | Local SQLite baseline | Neon dev |
|---|---|---|
| `GET /cuentas-cobro/{id}/stepper-state` | milliseconds | **3.4 – 3.6 s** |
| `GET /cuentas-cobro/{id}` | milliseconds | **1.5 s** |
| `POST /cuentas-cobro/{id}/radicar` (concurrent pair) | milliseconds | **12.9 s / 13.8 s** |

---

## 4. Playwright results vs the local SQLite baseline

| Suite | Neon dev | SQLite baseline | Delta |
|---|---|---|---|
| `e2e/journey/diez-ejercicios-radicacion.spec.ts` | **6 passed / 6 failed** (18.0 m) | 13/13 | −6 |
| `e2e/edge-cases` | **13 passed / 5 failed / 1 skipped** (12.1 m) | 18 passed / 1 skipped | −5 |
| `e2e/journey/radicacion-paquete-completo.spec.ts` | 2 passed / 3 failed / 3 did not run | — | 3 failures |
| `e2e/journey/evidencias-clasificacion.spec.ts` | 2 passed / 1 failed / 1 skipped | — | 1 failure |
| `e2e/journey/dashboard-creditos.spec.ts` | 3 passed | — | 0 |
| `e2e/journey/secop-flujo.spec.ts` | 3 passed / 1 failed | — | 1 failure |
| `e2e/journey/agent-dock-footer-overlap.spec.ts` | 4 passed | — | 0 |

**Totals: 33 passed, 15 failed, 2 skipped, 4 did not run.**

### Every failure, with classification

All 15 failures fall into **three** root causes. **None is a backend product bug on PostgreSQL.**

#### (b) Root cause A — frontend resume-vs-click race, exposed by a 3.4 s `stepper-state` (11 failures)

Failing locator in every case: `step-2-cuota-resumen` (or `step-nav-3`) never becomes visible; the
captured page snapshot shows the wizard sitting on **"Paso 3 de 3 — Revisá y radicá"**, i.e. the
click on `step-nav-1` was silently undone.

Mechanism, confirmed in code — `components/stepper/hooks/use-group-auto-advance.ts:70-75`:

```ts
useEffect(() => {
  if (data && syncedCuentaRef.current !== cuentaId) {
    syncedCuentaRef.current = cuentaId;
    setActiveGroup(groupOf(clampStep(data.current_step)));   // <- overrides the user's click
  }
}, [cuentaId, data, setActiveGroup]);
```

The step trail renders before `stepper-state` resolves (`const steps = data?.steps ?? []`), so the
helper's immediate `click()` sets `activeGroup = 2`; ~3.4 s later the resume effect fires for the
first time and yanks it to the server's `current_step` (group 3). On SQLite the query resolves in
milliseconds — before the click — so the effect has already run and the click sticks.

This is a **latent product race** (a genuinely slow backend, e.g. a cold Railway/Neon instance, would
reproduce it for a real user), plus test helpers that click without waiting for the wizard to settle.
Reproduced deterministically in 3 independent runs.

Affected: `diez-ejercicios` Ejercicios 1, 4, 7, 8, 9, 10 · `edge-cases/movil-390px` "smoke funcional" ·
`edge-cases/double-submit` "dos clics rápidos" (passed on re-run — this one is on the edge) ·
`radicacion-paquete-completo` Escenarios 1, 2, 5.

#### (b) Root cause B — 60 s / 90 s per-test budget too small for Neon (3 failures)

`edge-cases/double-submit.spec.ts:20` (60 s), `edge-cases/radicar-tras-enviada.spec.ts:20` and `:42`
(60 s) and `journey/secop-flujo.spec.ts:74` (90 s) all die with a bare
`Test timeout of 60000ms exceeded` — the seed alone (contrato + cuenta + 8 checklist uploads +
actividad + evidencias) plus a ~13 s `POST /radicar` does not fit.

**Proven not to be a product bug:** the exact same scenarios were replayed through the live API with
no test-timeout budget and all passed — see §5. The SECOP one additionally depends on the external
`datos.gov.co` Socrata upstream (`secop_api_http_error` / `secop_api_retry` in the backend log), which
the spec itself declares "tolerante a upstream".

#### (b) Root cause C — fixed 400 ms wait in a test helper (1 failure)

`journey/evidencias-clasificacion.spec.ts:524` "semáforo": `asegurarSinEvidencia` clicks "Quitar"
then does `await page.waitForTimeout(400)` before re-reading the chip, against a backend whose
mutation + cobertura refetch round-trip is ~1.5–3 s on Neon. Chip reads `Débil` where the test wants
`Sin evidencia`. Reproduced twice.

### Corroborating evidence that no backend PostgreSQL bug fired

During the entire run (5.2 MB of backend log with SQLAlchemy `echo=True`):

* **Zero** HTTP 500s.
* **Zero** asyncpg/SQLAlchemy runtime errors. The only 4 PG-driver errors in the whole log are the
  boot-time `alembic` `DuplicateTableError` described in §2.
* The only tracebacks (44) are `ValueError: Missing Gemini API key` from the embeddings call, which
  degrades to a zero-vector by design — expected with no LLM credentials.
* Warnings are all expected-in-this-environment: `embedding_api_failed`, `obligaciones_llm_empty`,
  `justificaciones_no_provider_connected`, `secop_app_token_missing`.

---

## 5. PR #95 evidence on real PostgreSQL

### (i) Live-API paquete-job re-trigger — **7/7 PASS**

Seeded a complete cuenta through the public REST API (`e2e-neon-paquete-…@example.com`, 14 catalog
rows, real fixture PDFs, actividad + evidencias) against the Neon-backed backend:

| Check | Result |
|---|---|
| 1st `POST /paquete/regenerar-async` → 202 | PASS |
| while `running`: `storage_key` **and** `listo_para_radicar` are `null` | PASS |
| 2nd trigger while in flight is a no-op on the **same** job row (not restarted) | PASS |
| run reaches terminal `done` (real ZIP produced, `storage_key` set) | PASS |
| re-trigger **after** `done` re-enqueues the same row back to `pending` | PASS |
| re-trigger clears the previous run's result payload (`storage_key`/`listo_para_radicar`/`error_code` all null) — PR #95 commit 3 | PASS |
| `GET /paquete/job` right after the re-trigger also shows the cleared payload | PASS |

### (ii) Genuine two-session concurrency on real PostgreSQL — **13/13 PASS**

Two independent `AsyncSession`s (separate asyncpg connections) fired with `asyncio.gather` against a
stale job row I own, each committing at the end exactly like `api.deps.get_db` does:

**`paquete_job_service._upsert_job`**

| Scenario | Enqueues | Expected |
|---|---|---|
| stale `pending` (with previous result payload) | **1** | 1 |
| stale `running` | **1** | 1 |
| fresh `pending` (control) | **0** | 0 |
| terminal `done`, 2 concurrent | **1** | 1 |
| single caller on terminal `done` (not over-suppressed) | **1** | 1 |
| first-ever concurrent trigger (phantom insert / `uq_paquete_job_cuenta` backstop) | 1 row, 0 exceptions | 1 row |
| reset clears previous result payload | all 7 fields `None`, status `pending` | — |

**`evidence_classification_service._upsert_job`**

| Scenario | Enqueues | Expected |
|---|---|---|
| stale `pending`, same `total`/`procesadas`/`error` (**the exact PR #95 bug**) | **1** | 1 |
| stale `running` | **1** | 1 |
| fresh `pending` (control) | **0** | 0 |
| terminal `failed`, 2 concurrent | **1** | 1 |
| stale `pending` with a different `total` | **1** | 1 |
| single caller on terminal `failed` (not over-suppressed) | **1** | 1 |

### (ii-bis) Negative control — the harness is genuinely sensitive

A scratchpad-local re-implementation of the **pre-#95** `_upsert_job` (no `populate_existing=True` on
the locking re-SELECT, no explicit `job.updated_at = now()`) was run through the identical harness on
the same real PostgreSQL. **No repository file was modified.**

```
PRE-#95 code, stale PENDING, 2 concurrent sessions: enqueues=2  results=[True, True]
[PASS] harness sensitivity: the pre-fix bug IS reproduced (2 enqueues) on real PostgreSQL
```

So: **2 enqueues before the fix, exactly 1 after it, on PostgreSQL 16.15 with a real `SELECT … FOR
UPDATE` row lock.** This is the evidence PR #95 was missing — SQLite/aiosqlite makes `FOR UPDATE` a
no-op, so the guard had never been exercised against a real lock.

### (ii-ter) Bonus — `POST /radicar` CAS on real PostgreSQL — **5/5 PASS**

Same scenarios as the two failing `edge-cases` specs, replayed via the API with no test-timeout cap:

| Check | Result |
|---|---|
| two concurrent `POST /radicar` both return 200 | PASS (12.9 s / 13.8 s) |
| both report `estado: "enviada"` | PASS |
| identical `fecha_envio` → exactly ONE real transition | PASS (`2026-09-19T17:21:57.002920Z`) |
| repeat `POST /radicar` after success is 200 idempotent | PASS (3.9 s) |
| repeat keeps the same `fecha_envio` | PASS |

### (iii) The repo's own PG tests — **9 passed, 0 failed, 9 errored on network**

Selection: the 18 concurrency tests of `tests/test_paquete_job_service.py` +
`tests/test_evidence_classification_concurrency.py` matching
`-k "loser_with_stale_identity_map or loser_is_still_reset or concurrent_encolar_same_cuenta or
concurrent_regenerar_async_same_cuenta or concurrent_first_trigger or reset_clears_previous_run_result_payload"`,
run against the throwaway database `e2e_pg_1789837784_test` on the Neon dev branch.

```
9 passed, 26 deselected, 1 warning, 9 errors in 3072.11s (0:51:12)
```

* **0 test failures.** Every test that got to execute an assertion on real PostgreSQL passed —
  including `paquete_job_service`'s `loser_with_stale_identity_map_*` two-session interleaving
  regressions and `concurrent_regenerar_async_same_cuenta_schedules_background_run_exactly_once`.
* **All 9 errors are fixture-SETUP errors caused by the host losing network to Neon**, not
  assertions. 7 of 9 are `socket.gaierror: [Errno 11001] getaddrinfo failed` (DNS for the Neon
  endpoint stopped resolving), the other 2 are
  `ConnectionResetError: [WinError 10054]` / `asyncpg ConnectionDoesNotExistError`. They arrive as one
  unbroken run of 8 at the tail (`.E........EEEEEEEE`), the signature of the link dropping and never
  recovering — not of a code path. DNS was verified working again after the run.
* **Cost measured: ≈ 3 min/test average** (51 min for 18). The per-test
  `DROP SCHEMA public CASCADE` + `create_all` of 34 tables is ~70 round-trips to a remote managed
  Postgres. The full 1400+-test suite this way would be many hours — this route is a spot-check
  mechanism, not a replacement for `scripts/test-postgres.sh`.

Two blockers found on the way, both reported rather than worked around destructively:

1. **`tests/conftest.py` cannot take a TLS DSN as-is.** It builds `engine_test =
   create_async_engine(TEST_DATABASE_URL, …)` directly, bypassing `app.core.db_ssl.prepare_pg_url`,
   so a Neon DSN fails with `TypeError: connect() got an unexpected keyword argument 'sslmode'`. I did
   not edit the file; I stripped the query string from the DSN I passed and relied on asyncpg's own
   SSL negotiation, which worked.
2. **`tests/conftest.py` runs `DROP SCHEMA public CASCADE` before EVERY test.** Its own safety guard
   correctly refuses a database not named like a test DB, so `neondb` was never a candidate. I created
   a dedicated throwaway **database** on the dev branch, `e2e_pg_1789837784_test` (stronger isolation
   than a schema, and the fixture hard-codes schema `public`), and dropped it at cleanup.

There is **no `pg` pytest marker** in this repo — PostgreSQL is selected purely by
`TEST_DATABASE_URL`, so "PG-marked tests" means "the whole suite, re-run against PG".

---

## 6. Cleanup

Verified by a final read-only query against the dev branch:

| Item | State |
|---|---|
| Backend / frontend processes | stopped by PID (`kill-local.ps1` avoided — it is broken on PowerShell 7). `:8000` **free**, `:3000` **free**. No node/chrome/uvicorn process from this run survives. |
| Throwaway database `e2e_pg_1789837784_test` | **dropped** — `SELECT count(*) FROM pg_database WHERE datname LIKE 'e2e_pg_%'` → **0** |
| Rows I created directly in `neondb` (`e2e-neon-*@example.test` users + their contratos / cuentas / job rows) | **deleted**, verified → **0** |
| Pre-existing data | **untouched** — non-e2e usuarios still **2**, their contratos still **9** (baselines taken before any write) |
| Rows created through the public API (`e2e-*@example.com`, 49 users) | **left in place** — see §7 item 2 |
| `paquete_job` rows | 3, all belonging to those API-seeded e2e cuentas |
| `alembic_version` | still `041_cdp_enum_uppercase` (unchanged by me) |
| `cashing-frontend/.env.local` | **unchanged** — still `NEXT_PUBLIC_API_URL=http://localhost:8003`; the override was set per-process only |
| `git status` | **clean** in both repos; backend at `81aab19`, frontend at `11bf4c4`. No tracked file created, edited or deleted. |
| DSN on disk | only `scratchpad/throwaway_dsn.txt`, which I wrote and then **deleted**. Full sweep of the scratchpad for `neon.tech` / `npg_` / `postgresql://` leaves only: the host-name *constant* in my guard scripts, this report's mention of the endpoint name, and a pre-existing log's local Docker DSN. **No credential anywhere.** SQLAlchemy masks the URL in its own `echo=True` output, so the 5.2 MB backend log is clean. |
| Playwright `test-results/` | left as-is in `cashing-frontend` (gitignored, overwritten on each run). It contains throwaway-user JWTs in `error-context.md` files — no DSN. Say the word and I will wipe it. |

---

## 7. What I could NOT verify

1. **The repo's own PG suite end-to-end.** Only 18 of ~1400 tests ran against PostgreSQL, and 9 of
   those were aborted in fixture setup by the host's network/DNS dropping mid-run. At ≈3 min/test the
   full suite this way is many hours. The 9 that completed all passed and none of the 9 errors is an
   assertion, but **"the whole suite is green on PostgreSQL" remains unproven** — that still needs
   Docker back up (`scripts/test-postgres.sh`) or a local Postgres container.
2. **Seeded API rows were not deleted.** Playwright and the API probes created **49**
   `e2e-*@example.com` users with their contratos / cuentas / documentos / evidencias / paquete_job
   rows in `neondb` via the public API. Deleting them means cascading deletes across 10+ tables plus
   local storage objects — a destructive operation the safety rules put out of bounds, and the same
   residue every previous live E2E run against this branch left behind. They are all namespaced
   `e2e-…@example.com` and trivially identifiable. Say the word and I will write a scoped cleanup.
3. **Whether the 11 `step-2-cuota-resumen` failures also reproduce on a *local* Postgres.** The
   distinguishing variable I proved is latency, not the engine; a fast local PG would very likely pass
   them. Not tested (Docker down).
4. **`radicacion-paquete-completo` Escenario 1 test 2, Escenario 5 tests 2–3, and
   `evidencias-clasificacion` test 4** never ran (serial-mode dependants of a failed test), so they
   are unmeasured on PostgreSQL — not passing, not failing.
5. **Whether `043_checklist_primera_cuota_flags` behaves correctly on this DB.** It is unreachable
   while `alembic upgrade` dies on `042`; it is moot today only because `requisitos_documento` is
   empty and the code-side seed carries the right flags.
6. **The host's network dropped for a stretch during the pytest run** (`getaddrinfo failed` for the
   Neon endpoint). It had recovered by cleanup time, but any Neon-latency number in this report was
   measured on a link that is demonstrably not always stable.

---

## 8. Bottom line

* **PR #95's fix is verified on real PostgreSQL 16.15.** The `SELECT … FOR UPDATE` +
  `populate_existing=True` + explicit `updated_at` guard produces **exactly one enqueue** in all 13
  concurrency scenarios across both services, and the pre-fix code **double-enqueues** on the same
  harness. The repo's own 9 PR #95 concurrency tests that completed against PostgreSQL also passed,
  with zero assertion failures. The gap is closed.
* The paquete-job lifecycle over the live API (`regenerar-async` ×2, `GET /paquete/job` null-payload
  invariant, post-terminal re-trigger clearing the previous result) is **7/7 green on PostgreSQL**.
* The 15 Playwright failures are **environment/latency**, not PostgreSQL product bugs — but one of
  them exposes a **real latent frontend race** worth fixing on its own merits:
  `use-group-auto-advance.ts`'s resume effect silently overrides a user's step navigation whenever
  `stepper-state` resolves after the first click.
* Pre-existing data was never modified; the only writes to shared state were the additive
  `paquete_job` table, the `alembic_version` widening, and namespaced test rows.
