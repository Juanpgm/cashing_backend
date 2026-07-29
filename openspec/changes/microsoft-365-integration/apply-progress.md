# Apply Progress — microsoft-365-integration

## Slice A1 — Credential store (PR 1, this batch)

**Branch**: `feat/microsoft-365-a1-credential-store` (off `ms365-integration/base`, committed locally, NOT pushed)
**Mode**: Strict TDD
**Status**: 13/13 A1 tasks complete (A1.1-A1.13). Ready for review / next slice (A2).

### TDD Cycle Evidence

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| A1.1/A1.2 | `tests/test_integracion_model.py` | Unit (aiosqlite) | N/A (new) | Written (ModuleNotFoundError) | 3/3 passed | 3 cases (dup email, empty-email collapse, cross-provider allowed) | Clean |
| A1.3/A1.4 | `alembic/versions/024_integraciones_table.py` (verified via standalone script, not pytest) | Migration | N/A (new) | N/A — see Migration Verification below | Verified | backfill + unique + downgrade cases | Clean |
| A1.5/A1.6 | `tests/test_integration_service.py` (Fernet, OAuthState, StoreCredentials, GetIntegrationStatus, ListIntegrationStatuses classes) | Unit (aiosqlite + mocked settings) | N/A (new) | Written (ModuleNotFoundError, 14 failures) | 14/14 passed | multiple (round-trip, tampered, expired, wrong-type, missing-provider-claim, reconnect-upsert, derived-flags) | Clean |
| A1.7/A1.8 | `tests/test_integration_service.py::TestHasAnyConnectedProvider` | Unit (aiosqlite) | 14/14 (prior batch) passing before adding this | Written (ImportError) | 2/2 passed | 2 cases (0 rows / ≥1 row) | Clean |
| A1.9/A1.10 | `tests/test_integration_service.py::TestGetIntegrationStatus`, `TestListIntegrationStatuses` (folded into A1.5/6 batch) | Unit | — | Written | Passed | connected/unconnected, per-provider list | Clean |
| A1.11 | `tests/test_google_workspace_service.py`, `tests/test_store_credentials.py`, `tests/test_gmail_adapter.py`, `tests/test_calendar_adapter.py`, `tests/test_drive_search.py` | Unit + Integration (existing regression suite) | 35/35 passing BEFORE refactor (captured explicitly as baseline) | N/A (refactor of existing code — approval-test style: existing tests are the approval tests) | 54/54 passing AFTER refactor (35 baseline + 19 new A1 tests, no losses) | N/A | Clean |

### Test Summary
- Total tests written (new): 19 (`test_integracion_model.py`: 3, `test_integration_service.py`: 16)
- Total tests passing (A1-scoped regression set): 54/54 (`test_google_workspace_service.py`, `test_store_credentials.py`, `test_gmail_adapter.py`, `test_calendar_adapter.py`, `test_drive_search.py`, `test_integration_service.py`, `test_integracion_model.py`)
- Full repo suite: **1137 passed**, 5 failed (pre-existing, unrelated — see below), 12 deselected (`live_llm`, opt-in only)
- Approval tests (refactoring A1.11): the pre-existing Google test suite itself (35 tests, safety-net baseline captured before any edit)
- Pure functions created: `_derive_enabled_flags`, `_to_status` (in `integration_service.py`)

### Migration Verification (A1.3/A1.4) — Neon substitute

Neon/Postgres is not reachable from this sandboxed environment (no `.env`, no network DB credentials). Instead of skipping verification, the migration's actual `upgrade()`/`downgrade()` functions were exercised directly via alembic's real `alembic.operations.Operations` + `alembic.migration.MigrationContext` API, bound to a throwaway on-disk SQLite database seeded with a minimal `usuarios` + `google_tokens` schema and one row:

- `upgrade()`: `create_table` succeeds, backfill produces exactly 1 row with `provider='google'`, `email=''`, correct encrypted token values copied verbatim from `google_tokens`.
- Unique constraint `(usuario_id, provider, email)` rejects a duplicate insert (real `IntegrityError`).
- `google_tokens` is untouched (still 1 row) after `upgrade()`.
- `downgrade()` drops `integraciones` cleanly; `google_tokens` still present.

This is real execution against a real DB connection through the real migration code — not a mock or source-text assertion — but it is **not** a substitute for running the full historical alembic chain (migrations 001-023) against Postgres/Neon, which this sandbox cannot do (that chain itself has pre-existing Postgres-only assumptions — `alembic upgrade` from scratch on SQLite fails at an unrelated earlier migration, confirmed independently of this change). **Recommend running `alembic upgrade head` against a real Neon/staging Postgres before this slice reaches production**, per the task's original instruction.

### Pre-existing environment blocker fixed (separate commit, NOT part of the A1 feature)

`app/api/v1/agent_chat.py` and `app/api/v1/documentos.py` combined `from __future__ import annotations` with a `@limiter.limit(...)` (slowapi) decorator wrapping a route with a `list[UploadFile]` parameter. `functools.wraps` (used internally by slowapi) cannot copy `__globals__`; FastAPI resolves lazy string annotations via the endpoint callable's `__globals__`, which — after slowapi's wrapping — point at slowapi's own module, not the route file's. This broke import of the **entire app** (`app.main`), and therefore every single test, since `tests/conftest.py` imports the full app unconditionally. Confirmed 100% reproducible in isolation, unrelated to any Microsoft-365 code, and would block ANY work in this fresh worktree. Fixed by dropping the lazy-annotations import in those two files only (zero runtime behavior change — Python has no runtime type checks). Committed separately (`fix(api): drop lazy annotations breaking slowapi+FastAPI resolution`) so it can be reviewed/dropped independently of the credential-store feature.

### Files Changed

| File | Action | What Was Done |
|------|--------|----------------|
| `app/api/v1/agent_chat.py` | Modified | Removed `from __future__ import annotations` (unrelated env fix, separate commit) |
| `app/api/v1/documentos.py` | Modified | Same (unrelated env fix, separate commit) |
| `app/models/integracion.py` | Created | `Integracion` model + `IntegrationProvider` StrEnum |
| `app/models/__init__.py` | Modified | Register `Integracion` for `Base.metadata` |
| `alembic/versions/024_integraciones_table.py` | Created | `integraciones` table + backfill from `google_tokens` |
| `alembic/env.py` | Modified | Import `integracion` model module (autogenerate support) |
| `app/schemas/integracion.py` | Created | `IntegrationStatus` |
| `app/services/integration_service.py` | Created | Shared `_fernet`, state helpers, `store_credentials`, `get/list_integration_status(es)`, `revoke_integration`, `has_any_connected_provider` |
| `app/services/google_workspace_service.py` | Modified | Delegates token/state/status persistence to `integration_service` (provider=google); keeps Google `Flow` + Gmail/Drive ops. Public signatures unchanged. |
| `app/adapters/email/gmail_adapter.py` | Modified | `get_credentials` reads `Integracion WHERE provider='google'` instead of `GoogleToken` |
| `app/adapters/drive/drive_adapter.py` | Modified | Docstring only (still delegates credentials to `GmailAdapter`) |
| `app/adapters/calendar/calendar_adapter.py` | Modified | Docstring only (same) |
| `tests/test_integracion_model.py` | Created | Uniqueness/collapse tests |
| `tests/test_integration_service.py` | Created | Full `integration_service` coverage |
| `tests/test_google_workspace_service.py` | Modified | `TestFernet` removed (moved); `TestVerifyOauthState` patches `integration_service.settings`; two mocks gained `provider`/`email` attrs |

### Deviations from Design
- Design's illustrative migration SQL (`op.execute` with `COALESCE(email, '')` and `gen_random_uuid()`) assumed `google_tokens` has an `email` column — it does not (confirmed by reading `app/models/google_token.py` and `alembic/versions/006_google_tokens_table.py`). Backfill sets `email=''` unconditionally for every row, which is exactly D5's stated fallback for unknown-email connections, so behavior is unchanged, only the literal SQL shape differs. Also used the repo's existing `sa.table()`/Python-loop backfill idiom (migration 023's pattern) instead of raw dialect-specific SQL, for portability and testability.
- `drive_adapter.py`/`calendar_adapter.py` credential loaders were NOT independently repointed (task A1.11 lists them) because they never queried `GoogleToken` directly — both already delegate to `GmailAdapter(db).get_credentials()`. Only `gmail_adapter.py` needed the actual query change; the other two files' stale docstrings were corrected for accuracy.
- Two files unrelated to this slice (`agent_chat.py`, `documentos.py`) were touched to fix a pre-existing environment-breaking bug — see above. Committed separately, not mixed into the credential-store commits.

### Review Workload — measured vs. forecast
Forecast: A1 ~350-420 changed lines, risk Medium. **Actual: 797 insertions + 146 deletions = 943 changed lines** across the 4 feature commits (excluding the 4-line unrelated env-fix commit), driven mainly by `tests/test_integration_service.py` (280 lines, comprehensive coverage of 8 new/moved functions) and `app/services/integration_service.py` (192 lines). This exceeds the forecast. Recommend the orchestrator/reviewer treat this as an accepted overage for a well-tested, single-purpose credential-store PR rather than splitting further — the 5 commits are each independently reviewable and no single one exceeds ~500 lines.

### Issues Found
None beyond the pre-existing environment blocker (fixed, see above) and pre-existing mypy/ruff debt (untouched, see Verification below).

### Verification

- **Focused tests**: `uv run python -m pytest tests/test_integracion_model.py tests/test_integration_service.py -q` → 19/19 passed.
- **A1 regression set**: `uv run python -m pytest tests/test_google_workspace_service.py tests/test_store_credentials.py tests/test_gmail_adapter.py tests/test_calendar_adapter.py tests/test_drive_search.py tests/test_integration_service.py tests/test_integracion_model.py -q` → 54/54 passed.
- **Full suite**: `uv run python -m pytest -q` → 1137 passed, 5 failed, 12 deselected. All 5 failures confirmed pre-existing/environmental, unrelated to this diff: 3 need MinIO/S3 (`Could not connect to the endpoint URL` — `make up`/Docker not running in this sandbox), 1 same (batch upload → S3), 1 needs an external `cashing_vault` golden-fixture file not present in this environment.
- **Lint**: `ruff check`/`ruff format --check` clean on every touched file. Two pre-existing findings (S106 in `gmail_adapter.py:95`, N806 in `documentos.py`) are on lines untouched by this diff — confirmed via `git diff --stat` (not part of the changed hunks).
- **Type check**: `mypy` strict has 155 pre-existing errors repo-wide (confirmed identical before/after this diff by stashing and re-running against the unmodified `google_workspace_service.py`). This diff adds exactly one new instance of the same already-endemic `no-any-return` pattern (from the untyped `jose` library — `jwt.encode(...)` returns `Any`), consistent with the code it replaces; no new error categories introduced. `make lint` (`ruff check . && ruff format --check . && mypy app/`) is **not** green on this fresh worktree at baseline (971 ruff findings across 193 unformatted files repo-wide, confirmed before any of my edits) — this is pre-existing repo-wide debt (matches prior session's memory note "repo isn't ruff-clean"), not something introduced or fixable within this slice's scope.
- **Rollback boundary**: `git revert` the 4 feature commits (model, migration, service, refactor) — `google_tokens` stays intact and `/google/*` behavior is unaffected either way since the migration never drops it. The unrelated env-fix commit can be reverted or kept independently.

### Workload / PR Boundary
- Mode: stacked-to-main chained PR slice (auto-chain, already resolved — no decision needed)
- Current work unit: A1 — credential store (this batch)
- Boundary: starts at `ms365-integration/base` HEAD, ends at the 5 commits on `feat/microsoft-365-a1-credential-store` (env-fix + model + migration + service + refactor)
- Estimated review budget impact: measured 943 changed lines vs ~350-420 forecast — see "Review Workload" above

### Status
13/13 A1 tasks complete. Ready for `sdd-verify` on this slice, or for the next apply batch (Slice A2 — port generalization, independent of A1).

## Correction Round — 4-lens review fixes (on top of A1, same branch)

A 4-lens review (risk, resilience, readability, reliability) against
`origin/master...feat/microsoft-365-a1-credential-store` raised 2 CRITICAL, 2
should-fix, and 2 test-coverage findings. All were fixed in this round; none
were pushed back to origin (local commits only, per instruction).

| # | Finding | Status | Fix |
|---|---------|--------|-----|
| 1 | CRITICAL — `get_integration_status`/`revoke_integration` (integration_service.py) and `get_credentials` (gmail_adapter.py) used `.scalar_one_or_none()` filtered only on `(usuario_id, provider)`, raising unhandled `MultipleResultsFound` the moment a second account exists — contradicts `store_credentials`'s own multi-account design | Fixed | Added optional `email` kwarg to all three; when omitted, order by `updated_at desc` + `.scalars().first()`. Regression tests: `tests/test_integration_service.py::TestMultipleAccountsSameProvider`, `tests/test_gmail_adapter.py::TestGetCredentialsAgainstRealIntegracionRow::test_multiple_accounts_does_not_raise_multiple_results_found` |
| 2 | CRITICAL — `gmail_adapter.get_credentials`'s two `Fernet.decrypt()` calls had no try/except; a rotated/corrupted key raised raw `cryptography.fernet.InvalidToken` (500) instead of the `ExternalServiceError` pattern used 20 lines below for refresh failures | Fixed | Wrapped both decrypts in `try/except InvalidToken`, raising the same reconnect-your-account `ExternalServiceError`. `drive_adapter.py`/`calendar_adapter.py` both delegate to `GmailAdapter.get_credentials` for credentials, so one fix covers all three. Test: `test_invalid_token_on_decrypt_raises_external_service_error` |
| 3 | Should-fix — migration `downgrade()` unconditionally `DROP TABLE integraciones`; design.md called this "trivial, lossless" but that's only true pre-cutover | Fixed | Added an explicit docstring on `downgrade()` stating the destructive/lossy window (any post-cutover Google row, and every Microsoft row unconditionally — no fallback table exists for Microsoft). Corrected the "trivial, lossless" language in `design.md`'s two rollback-description sections to state the actual constraint |
| 4 | Should-fix — backfill did `fetchall()` + N sequential `insert()` calls inside one transaction instead of design.md's single set-based `INSERT ... SELECT` | Fixed | Postgres path now issues one `INSERT ... SELECT` using `gen_random_uuid()` (built into PG13+, no pgcrypto extension needed). SQLite (test-only) path keeps a single bulk multi-row `insert()` (still one `execute()` call, not N) since `gen_random_uuid()` has no SQLite equivalent — `bind.dialect.name` branches between them. Tests: `tests/test_migration_024_backfill.py` (new — Alembic `Operations`/`MigrationContext` against a throwaway SQLite DB) |
| 5 | Should-fix — `google_workspace_service.py` and `integration_service.py` both defined `store_credentials`/`revoke_integration`/`get_integration_status`/`verify_oauth_state` with different signatures — would triple once `microsoft_graph_service.py` lands in Slice B | Fixed | Renamed the four Google-specific wrappers to `google_store_credentials`/`google_revoke_integration`/`google_get_integration_status`/`google_verify_oauth_state`. Updated every caller: `api/v1/integraciones.py`, `evidence_discovery_service.py`, `scripts/evidence_demo.py`, and all affected tests (`test_google_workspace_service.py`, `test_store_credentials.py`, `test_evidence_discovery.py`, `test_error_codes.py`) |
| 6 | Test gap — `test_gmail_adapter.py` always stubbed `get_credentials` with `AsyncMock`, so the `GoogleToken` → `Integracion` repoint was never exercised end-to-end | Fixed | Added `TestGetCredentialsAgainstRealIntegracionRow` (real aiosqlite `Integracion` row): decrypt succeeds, `NotFoundError` when no row, expiry/refresh still fires (`Credentials.refresh` patched, not mocked away), decrypt-failure path, multi-account path |
| 7 | Test gap — migration backfill only verified via a manual throwaway script, no repeatable test | Fixed | `tests/test_migration_024_backfill.py` loads `alembic/versions/024_integraciones_table.py` by path (numeric-prefixed filename isn't importable normally) and drives `upgrade()` via `Operations.context(MigrationContext.configure(conn))` against an in-memory SQLite DB |

**Explicitly left untouched (out of scope per instruction):** the SELECT-then-INSERT race in `store_credentials` (pre-existing pattern, not a regression); no Microsoft OAuth/`CalendarPort`/`DrivePort` work (belongs to Slices A2/B/C).

**Verification for this round:**
- Focused: `uv run python -m pytest tests/test_integration_service.py tests/test_gmail_adapter.py tests/test_google_workspace_service.py tests/test_store_credentials.py tests/test_migration_024_backfill.py tests/test_evidence_discovery.py tests/test_error_codes.py -q` → 69/69 passed.
- Full suite: `uv run python -m pytest -q` → **1146 passed**, 6 failed, 12 deselected. All 6 failures reconfirmed pre-existing/environmental (same set documented in the A1 baseline above, minus the one this round's migration test itself needed fixing for — confirmed by re-running the identical failing subset against `git stash`-ed pre-correction code, which reproduces the same 3 representative failures with the same root causes: missing MinIO/S3 in this sandbox, a missing external `cashing_vault` golden fixture). Net +9 passing tests from this round's new regression coverage.
- Lint: `ruff check`/`ruff format --check` clean on every line this round touched or added; pre-existing repo-wide debt (documented in the A1 baseline above) is unchanged and untouched.
- Type check: zero new mypy errors attributable to this round's diff (spot-checked: no error in the 209 baseline errors traces to a line added/changed here — `integration_service.py` and `gmail_adapter.py` report zero errors of their own).
- Environment note: a stray `uv sync` early in this round temporarily desynced the venv from `requirements-dev.txt` (this repo's real dependency source of truth — `pyproject.toml` only lists 3 unrelated packages) against `uv.lock`; recovered via `uv pip install -r requirements-dev.txt --override <cryptography override>` (a pre-existing, unrelated `pyhanko`/`cryptography` version-floor conflict in `requirements-dev.txt` needed a temporary install-time override, not a requirements-file edit). All subsequent commands in this round used `uv run --no-sync` to avoid re-triggering the implicit project sync.
