# Apply Progress — microsoft-365-integration

## Reintegration (2026-08-07) — READ THIS FIRST, supersedes staleness below

**Context**: the sections below this one were written against an old worktree
(`cashing-backend-ms365`) that later stopped being its own git repo; its
uncommitted/unpushed source tree was snapshotted into `_ms365_preserved/` in
*this* repo before deletion. Separately — and not obvious from that snapshot
alone — the same 5-slice implementation (A1→A2→B→C1→C2) **had also been
pushed to `origin/feat/microsoft-365-sharepoint-drive`** in *this* repo, plus
extra work beyond the original 68-task scope (SharePoint `search_site_files`,
a reverted generic-IMAP-adapter experiment). That branch was real, complete,
and already had 3 correction commits (mypy fix, debug-secret masking, Azure
token malformed-response hardening) that never made it into `_ms365_preserved/`.

**What actually happened this session**: verified the pushed branch existed
and diverged from `master` by 119 commits (not from any deletion — normal
independent development on both sides). Created `feat/microsoft-365-integration-flagged`
off current master tip and ran `git merge feat/microsoft-365-sharepoint-drive`
— a real 3-way merge, not a manual file-by-file reconciliation against the
stale `_ms365_preserved/` snapshot (which is the superseded, less-complete
source and was left untouched, per instruction, pending human review/deletion).

**Conflicts resolved by hand** (4 files, all textually genuine — master had
grown features the branch never saw, and vice versa):

1. `app/services/evidence_discovery_service.py` — master added `local_only`
   mode (skips the connection gate + local-file evidence classification),
   `discovery_cache` (TTL result cache), `_contexto_usuario`. Branch added the
   provider-agnostic gate (`NO_PROVIDER_CONNECTED`, replacing `GOOGLE_NOT_CONNECTED`)
   and per-provider email/drive/calendar gather with per-provider failure
   isolation. Merged: `local_only=True` skips the gate+gather entirely
   (unchanged master semantics, now provider-agnostic wording); `local_only=False`
   runs the branch's multi-provider gate+gather; cache/contexto/local-evidence
   untouched.
2. `app/services/google_workspace_service.py` — master added `_email_from_id_token`
   (captures the user's email from the OAuth id_token) threaded through
   `store_credentials`/`get_integration_status`. Branch renamed/refactored
   `store_credentials` → `google_store_credentials`, a thin wrapper delegating
   to the new provider-agnostic `integration_service.store_credentials`.
   Merged: kept the branch's delegation architecture, added `email=email` to
   the delegated call so the master-side feature survives. Also had to
   re-add `from jose import JWTError, jwt` (dropped by the branch's rewrite,
   still needed by `_email_from_id_token`) — a real import bug the naive
   auto-merge would NOT have caught (no textual conflict, would have been a
   silent `NameError` at first callback).
3-4. `tests/test_evidence_discovery.py`, `tests/test_google_workspace_service.py`
   — matching test-side renames/mock-shape updates for the above two.

**Non-conflicting but load-bearing checks performed before trusting the merge**:
`app/api/router.py` and `app/api/deps.py` — the files the original briefing
flagged as "diverged shared wiring" — turned out to be **untouched by the
branch entirely** (`git diff` empty on both sides of the merge-base); the
Microsoft routes were added by generalizing the already-registered
`integraciones_router`'s handlers, not by touching router registration.
`app/models/__init__.py` and `app/core/config.py` auto-merged cleanly
(non-overlapping insertion points, verified before merging, not just trusted).

**Alembic chain fork fixed**: `024_integraciones_table.py`'s `down_revision`
pointed at `023_documento_requisito_vinculos`, but master's tracked chain had
moved on to `034_cuenta_cobro_consecutivo_ds` without ever consuming revision
024 (that number was simply skipped on master). Inserting 024 unchanged would
have forked the chain into two heads (024 and 025 both children of 023).
Repointed 024's `down_revision` to `034_cuenta_cobro_consecutivo_ds` (append
at the tail) instead of renumbering 11 intervening migrations. **Not applied
to any real database in this session** — `alembic upgrade head` was not run
against Postgres/Neon; the original A1 slice's Neon-substitute verification
(SQLite + real Alembic Operations API, see below) still stands as the only
verification this migration's `upgrade()`/`downgrade()` logic itself has had.

**New task, not in the original 68**: `MS365_INTEGRATION_ENABLED: bool = False`
in `app/core/config.py` — a hard requirement from the reintegration brief,
"must land coded but not active." The branch's routes were NOT gated by any
flag (they'd have gone live on merge). Since `/integraciones/{provider}/*`
is a single already-registered router shared with the live Google routes
(confirmed above — no separate MS365 router exists to gate registration of),
gating happens per-request in `app/api/v1/integraciones.py` via
`_require_ms365_enabled(provider)`, called at the top of
`integration_connect`/`integration_callback`/`integration_status`/`integration_revoke`
— raises `NotFoundError` (404) when `provider == IntegrationProvider.MICROSOFT`
and the flag is off. Google is untouched (checked explicitly: `_require_ms365_enabled`
is a no-op for `IntegrationProvider.GOOGLE`). Without `/connect`/`/callback`
reachable, no user can ever create a `provider=microsoft` row in `integraciones`,
which transitively makes the provider-agnostic discovery gate/gather loop in
`evidence_discovery_service.py` and the `MicrosoftGraphAdapter` unreachable
in practice too, even though those layers have no flag check of their own by
design (they're meant to be exercised directly by tests, per the brief).
Added `tests/test_integraciones_api.py::TestMs365Disabled` (new) asserting
the 404s and that Google is unaffected; the rest of that file's classes gained
an autouse fixture flipping the flag on (that file's whole purpose is testing
the Microsoft OAuth wiring). `.env.example` — could not read/edit it, blocked
by this sandbox's permission settings (directory-level deny on `.env.example`);
the Pydantic Settings default (`False`) is authoritative regardless, but a
human should manually add a documented, defaulted-empty `MS365_INTEGRATION_ENABLED=false`
line there for operator visibility. **This is the one explicit gap left by
this session — flagged, not silently skipped.**

**Verification**: focused MS365-relevant suite (`-k "microsoft or integracion
or integraciones or evidence_discovery or google_workspace or store_credentials
or calendar or drive"`) 134/134 passed. Full repo suite: **1934 passed, 0
failed, 14 deselected** (`live_llm`, opt-in only), 647s. Zero regressions —
the historical "5 pre-existing environmental failures" baseline mentioned in
earlier sections below is gone (resolved by the 119 intervening master
commits, unrelated to this work).

**Explicit human-call gaps** (cannot be closed by an agent):
- Real Microsoft Graph OAuth end-to-end (actual Azure AD app registration,
  real user consent, real token exchange against `login.microsoftonline.com`)
  has never been exercised — only mocked/unit-tested. `AZURE_AD_CLIENT_ID`/
  `AZURE_AD_CLIENT_SECRET` are empty by default; nothing in this session
  fabricated or simulated real credentials.
- `alembic upgrade head` was not run against a real Postgres/Neon instance in
  this session (see A1's original Neon-substitute note below — still the
  only verification this migration has had).
- `.env.example` needs a human edit (documented above) — sandbox-blocked here.
- `_ms365_preserved/` was intentionally left in place, per instruction,
  pending a human confirming this reintegration is correct — safe to delete
  once that's done (it is now the strictly worse/superseded source; nothing
  in this session drew from it for the final merged code, only for the initial
  briefing/orientation).

**Not yet done**: commit is local only (`feat/microsoft-365-integration-flagged`,
merge commit `0125016`, off current `master` tip `cd3e73f`), not pushed, no
PR opened. `fix/robust-document-extraction-ocr` and
`feat/secop-contrato-disambiguacion` were not touched, per instruction.

---

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

## Slice A2 — Port generalization (PR 2, this batch)

**Branch**: `feat/microsoft-365-a2-port-generalization` (off A1 tip `3e8a538`, worktree `cashing-backend-ms365`), 4 commits, NOT pushed.
**Mode**: Strict TDD
**Status**: 15/15 A2 tasks complete (A2.1-A2.15). Only Google-code-touching slice; full existing Google suite green throughout.

### TDD Cycle Evidence

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| A2.1/A2.2 | `tests/test_calendar_port.py` (new) | Unit | N/A (new) | Written (ImportError) | 4/4 passed | defaults / timed fields / all-day fields / attendee defaults (4 cases) | Clean |
| A2.3/A2.4 | `tests/test_calendar_adapter.py` | Unit (mocked Google service) | 3/3 passing before (baseline) | Written (dict-access `AttributeError`, 4 failures) | 10/10 passed | all-day mapping, attendees+organizer mapping, `get_event` mapping (3 new cases + updated existing) | Clean |
| A2.5/A2.6/A2.7 | `tests/test_drive_calendar_fetch.py` (calendar half), `tests/test_integraciones_test_routes.py` (new) | Unit + httpx integration | 9/9 (calendar+drive) passing before | Written (`AttributeError: 'CalendarEvent' object has no attribute 'get'`, then route dict-access failures) | 9/9 + 3/3 route tests passed | all-day event, keyword-query passthrough, missing-summary route default (3 route cases) | Clean |
| A2.8/A2.9/A2.10 | `tests/test_drive_search.py` | Unit (mocked Google service) | 3/3 passing before | Written (string-vs-DriveQuery mismatch, 2 failures) | 5/5 passed | date-range translation, empty-keywords omits clause (2 new cases) | Clean |
| A2.11/A2.12/A2.13 | `tests/test_drive_calendar_fetch.py` (drive half), `tests/test_integraciones_test_routes.py` | Unit + httpx integration | 7/7 (drive) passing before | Written (`AttributeError: 'str' object has no attribute 'exclude_folders'`) | 10/10 + drive-route case passed | `list[DriveQuery]` shape + one-keyword-per-query granularity (1 new case) | Clean |

### Test Summary
- Total tests written/updated: 4 new files/additions (`test_calendar_port.py`: 4 new, `test_integraciones_test_routes.py`: 3 new) + updates to `test_calendar_adapter.py` (+3 new, 1 updated), `test_drive_search.py` (+2 new, 2 updated), `test_drive_calendar_fetch.py` (+1 new, 5 updated)
- Total tests passing (A2-scoped regression set): 28/28 (`test_calendar_port.py`, `test_calendar_adapter.py`, `test_drive_search.py`, `test_drive_calendar_fetch.py`, `test_integraciones_test_routes.py`)
- Merge-gate set (`-k "calendar or drive or evidence or integracion"`): **147/147 passed**
- Full repo suite: **1160 passed**, 5 failed (pre-existing, unrelated — same category as A1's baseline), 12 deselected (`live_llm`)
- Approval tests: existing Google calendar/drive suites (28 tests total across the touched files) served as the approval-test baseline before each refactor step
- Pure functions created/extended: `_parse_event` (calendar_adapter.py), `_translate_query` (drive_adapter.py), `build_drive_queries` (drive_fetch.py, now dataclass-returning)

### Files Changed

| File | Action | What Was Done |
|------|--------|----------------|
| `app/adapters/calendar/port.py` | Modified | Added `CalendarEvent`/`CalendarAttendee` dataclasses; `CalendarPort.search_events`/`get_event` now return them |
| `app/adapters/calendar/calendar_adapter.py` | Modified | New `_parse_event()` maps raw Google event JSON → `CalendarEvent`/`CalendarAttendee` once, internally |
| `app/agent/nodes/calendar_fetch.py` | Modified | `_event_start()`/`_extract_event_metadata()` consume `CalendarEvent` attributes; metadata dict still emits the `self`/`responseStatus` attendee shape `evidence_filter.is_noise_calendar` expects |
| `app/adapters/drive/port.py` | Modified | Added `DriveQuery` dataclass; `DrivePort.search_files` signature changed to `(usuario_id, query: DriveQuery)` |
| `app/adapters/drive/drive_adapter.py` | Modified | New `_translate_query()` maps `DriveQuery` → Google Drive query syntax (keywords OR-fan, date range, folder exclude, mime filter) |
| `app/agent/nodes/drive_fetch.py` | Modified | `build_drive_queries()` returns `list[DriveQuery]`; dedup now keys on a normalized field tuple (DriveQuery isn't hashable) |
| `app/api/v1/integraciones.py` | Modified | `test_calendar`/`test_drive` routes consume `CalendarEvent`/`DriveQuery` instead of raw dicts/strings |
| `tests/test_calendar_port.py` | Created | Dataclass shape/defaults tests for `CalendarEvent`/`CalendarAttendee` |
| `tests/test_calendar_adapter.py` | Modified | Updated dict-access assertions to attribute access; added all-day/attendees/organizer/`get_event` mapping tests |
| `tests/test_drive_search.py` | Modified | Updated raw-string calls to `DriveQuery`; added date-range and empty-keywords translation tests |
| `tests/test_drive_calendar_fetch.py` | Modified | Updated calendar/drive fixtures to `CalendarEvent`/`DriveQuery`; added dataclass-shape assertions |
| `tests/test_integraciones_test_routes.py` | Created | httpx tests for `/integraciones/calendar/test` and `/integraciones/drive/test` (no prior test existed for either route) |

### Deviations from Design
- **`CalendarAttendee.is_self: bool = False` added** — not in design D3's field list (`email`, `display_name`, `response_status`, `optional`). `evidence_filter.is_noise_calendar` (downstream, Slice C2 file but already live) reads `metadata["attendees"][i]["self"]` to detect "the connected user declined this event" as a noise signal. Design D3 didn't carry this Google-specific "is this attendee the calendar owner" flag onto `CalendarAttendee`, which would have silently broken that noise-detection branch (attendee dicts would lose `self` entirely once sourced from the dataclass instead of raw Google JSON). Marked `ponytail:` in the dataclass — Graph has no equivalent, defaults `False`, revisit only if Slice C1's Microsoft calendar heuristic needs an analogous self-detection signal.
- **`build_drive_queries()` builds one `DriveQuery` per extracted keyword/generic term (same granularity as the old per-string list), not literally "one `DriveQuery` per obligation" as design.md's D4 prose states.** Reason: `drive_fetch_node` truncates each obligation's query list to `settings.EVIDENCE_QUERIES_PER_OBLIGACION` (default 3) *before* dedup — with the old per-string queries, that truncation kept only the 3 keyword-derived queries and silently dropped the 5 generic-term queries for the per-obligation path (the generic terms were only ever reachable via the no-obligations fallback). Collapsing to one combined `DriveQuery(keywords=[...all terms...])` per obligation, as design.md's prose suggests, would have started including generic terms in the per-obligation search path where they were previously truncated away — a real (if minor) behavior change forbidden by this slice's merge gate. Kept the granular structure to make the refactor strictly behavior-preserving; noted here per design-deviation reporting requirement, not silently deviated.
- `CalendarEvent.summary`/`description` default to `""` per design (not `None`); each caller applies its own placeholder text (`"(evento sin título)"` in `calendar_fetch.py`, `"(sin título)"` in the `test_calendar` route) — matches design D3's stated intent exactly, no deviation, called out here only because it's easy to misread as inconsistent.

### Issues Found
None. No regressions in the existing Google calendar/drive/discovery suites; mypy error count on touched files improved slightly (see Verification).

### Verification

- **Focused tests**: `uv run --no-sync python -m pytest tests/test_calendar_port.py tests/test_calendar_adapter.py tests/test_drive_search.py tests/test_drive_calendar_fetch.py tests/test_integraciones_test_routes.py -q` → 28/28 passed.
- **Merge gate (A2.14)**: `uv run --no-sync python -m pytest tests/ -q -k "calendar or drive or evidence or integracion"` → 147/147 passed.
- **Full suite**: `uv run --no-sync python -m pytest -q` → 1160 passed, 5 failed, 12 deselected. All 5 failures confirmed pre-existing/environmental and unrelated to this diff (`test_agent_chat_robustness.py` x2, `test_agent_chat_service.py`, `test_checklist_api.py`, `test_obligaciones_golden_ejemplos.py` — same category documented in A1's baseline: missing MinIO/S3 and an external golden fixture, none of which import calendar/drive/integraciones code).
- **Lint**: `ruff check`/`ruff format --check` clean on every file this slice touched (`app/adapters/calendar/port.py`, `app/adapters/calendar/calendar_adapter.py`, `app/adapters/drive/port.py`, `app/adapters/drive/drive_adapter.py`, `app/agent/nodes/calendar_fetch.py`, `app/agent/nodes/drive_fetch.py`, `app/api/v1/integraciones.py`, and all 5 touched/new test files). Repo-wide `ruff check .` shows 968 pre-existing findings across files this slice never touched — same documented pre-existing debt as A1's baseline, not introduced or fixable within this slice's scope.
- **Type check**: mypy on the 7 touched source files: **206 errors** (this branch) vs **209 errors** (A1-tip baseline, same 7-file target set, verified via a temporary git worktree at commit `3e8a538` using the main repo's venv) — net **-3 errors, -1 file with errors**, no new error categories. All remaining errors are pre-existing (`_build_service` untyped-call warnings, `Missing type arguments for generic type "dict"` on lines unchanged by this diff, unrelated `secop_service.py`/`document_service.py`/`extraction.py` debt).
- **Rollback boundary**: `git revert` the 3 feature commits (calendar port/adapter, calendar_fetch node, drive port/adapter/node/routes) + 1 formatting commit — behavior-preserving refactor, no schema/migration involved, Google discovery/adapter behavior reverts to its A1-tip state exactly.

### Workload / PR Boundary
- Mode: stacked-to-main chained PR slice (auto-chain, already resolved — no decision needed)
- Current work unit: A2 — port generalization (this batch)
- Boundary: starts at A1 tip `3e8a538`, ends at the 4 commits on `feat/microsoft-365-a2-port-generalization`
- Estimated review budget impact: forecast ~380-450 changed lines, Medium risk. **Actual: 574 insertions + 110 deletions = 684 changed lines** (`git diff --stat 3e8a538..HEAD`) — exceeds the forecast, similar overage pattern to A1 (943 vs 350-420), driven mainly by the 5 touched/new test files (372 of the 574 insertions) needed to cover both the new dataclasses and the behavior-preservation regression cases the merge gate requires. Each of the 4 commits is independently reviewable and none exceeds ~250 lines; recommend accepting as a well-tested single-purpose refactor PR rather than splitting further, consistent with A1's precedent.

### Status
15/15 A2 tasks complete. Ready for `sdd-verify` on this slice, or for the next apply batch (Slice B — Microsoft OAuth + generalized routes, depends on A1 only — A2 was independent).

## A2 Correction Round (4R review fixes)

A 4-lens review (risk, resilience, readability, reliability) ran against
`feat/microsoft-365-a1-credential-store...feat/microsoft-365-a2-port-generalization`.
One bounded correction transaction applied the confirmed findings as 5 atomic work-unit commits.

| Ledger ID (severity/lens) | Location | Fix | Commit |
|---|---|---|---|
| resilience CRITICAL | `app/adapters/calendar/calendar_adapter.py:112` | `search_events` wraps each `_parse_event(item)` call in its own try/except (`ValueError, KeyError, TypeError`), logs+skips the malformed item, keeps the rest — matches `drive_fetch_node`'s established per-item isolation pattern | `fe9bd91` |
| risk WARNING | `app/adapters/drive/drive_adapter.py:210` | `_translate_query` keyword sanitization now strips backslashes alongside single quotes (Drive query grammar uses `\` as the escape char inside `'...'` literals) | `cda3fcb` |
| readability SUGGESTION (should-fix) | `app/adapters/calendar/port.py` | `CalendarAttendee.is_self` and `CalendarEvent.summary` docstrings/comments now explain the Google-only exception and add the missing Google/Graph cross-reference, matching the pattern already used by `html_link`/`event_type`/`response_status` | `e984cbd` |
| test-coverage gap | `tests/test_drive_calendar_fetch.py` | New `test_drive_fetch_truncation_keeps_keyword_queries_over_generic_terms` pins the ordering property `drive_fetch_node`'s `EVIDENCE_QUERIES_PER_OBLIGACION` truncation depends on | `590a815` |
| test-coverage gap | `tests/test_drive_calendar_fetch.py` | New `test_declined_rsvp_is_noise_end_to_end` / `test_accepted_rsvp_is_not_noise_end_to_end` drive the real `_parse_event` → `_extract_event_metadata` → `is_noise_calendar` pipeline instead of only hand-built half-tests | `ba321ca` |

Out of scope per correction instructions (not touched): `tests/test_drive_port.py` file-organization suggestion; anything Microsoft/OAuth/credential-store related.

### Correction Verification
- Focused: `pytest tests/test_calendar_adapter.py tests/test_drive_search.py tests/test_drive_calendar_fetch.py tests/test_calendar_port.py -q` → 30/30 passed (was 28/28 before this round; +1 fault-isolation test, +1 backslash-escaping test, +3 new regression tests replacing/extending the coverage-gap items — net +9 test functions across the 3 modified test files).
- Full suite: `pytest -q` → 1165 passed, 5 failed (same 5 pre-existing/environmental failures as the A2 baseline — confirmed unchanged by running them against a `git stash` of this correction's diff, identical `FileNotFoundError` on a missing external fixture path), 12 deselected.
- Lint: `ruff check`/`ruff format --check` clean on all 6 files this round touched. `mypy app/adapters/calendar/ app/adapters/drive/` → 43 errors, identical count/content to the pre-correction baseline (verified via `git stash`) — no new mypy errors introduced.
- Rollback boundary: each of the 5 commits (`fe9bd91`, `cda3fcb`, `e984cbd`, `590a815`, `ba321ca`) is independently revertible without touching unrelated work.

## Slice B — Microsoft OAuth + generalized routes (PR 3, this batch)

**Branch**: `feat/microsoft-365-b-oauth-routes` (off A2 tip `6664a73`, same worktree `cashing-backend-ms365`), NOT pushed.
**Mode**: Strict TDD
**Status**: 13/13 B tasks complete (B.1-B.13).

### TDD Cycle Evidence

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| B.3/B.4 | `tests/test_microsoft_graph_service.py::TestBuildAuthorizationUrl` | Unit | N/A (new) | Written (ModuleNotFoundError) | 3/3 passed | no-client-id error / full URL shape / distinct PKCE challenge per call (3 cases) | Clean |
| B.7/B.8 | `tests/test_microsoft_graph_service.py::TestExchangeCode` | Unit (mocked httpx.AsyncClient) | N/A (new) | Written (ModuleNotFoundError) | 4/4 passed | happy path / missing client id / token-endpoint HTTP error / Graph `/me` failure degrades to `email=""` without failing the exchange (4 cases) | Clean |
| B.6/B.10 | `tests/test_microsoft_graph_service.py::TestHandleOAuthCallback` | Unit (mocked db + service calls) | N/A (new) | Written | 1/1 passed | single scenario — pure wiring/dispatch, no branching logic to triangulate | ➖ Single (structural glue) |
| B.1/B.2 | `tests/test_integraciones_api.py::TestProviderPathValidation` | Integration (httpx.AsyncClient) | N/A (new route shape) | Written (404 — routes didn't exist) | 3/3 passed | unknown provider on connect / on status / static-second-segment collision case (3 cases) | Clean |
| B.2 (connect) | `tests/test_integraciones_api.py::TestConnect` | Integration | 0 pre-existing route tests (none existed for `/google/connect` at API level before this slice) | Written | 2/2 passed | google (regression) + microsoft (new) — both dispatch paths | Clean |
| B.5/B.6 | `tests/test_integraciones_api.py::TestCallback` | Integration | Same | Written | 4/4 passed | google regression / microsoft new / tampered state / state-provider≠path-provider mismatch (4 cases) | Clean |
| B.12 | `tests/test_integraciones_api.py::TestStatus` | Integration | Same | Written | 3/3 passed | google unconnected / microsoft unconnected / microsoft connected with scope-derived flags (3 cases) | Clean |
| B.7 (coexistence) | `tests/test_integraciones_api.py::TestRevoke` | Integration | Same | Written | 2/2 passed | google 404-when-disconnected / revoke microsoft leaves google connected (2 cases) | Clean |
| B.9 | `tests/test_integraciones_api.py::TestReconnect` | Integration | Same | Written | 1/1 passed | reconnect-same-account collapses to 1 row — single scenario per spec | ➖ Single |

### Test Summary
- Total tests written (new): 23 (`test_microsoft_graph_service.py`: 8, `test_integraciones_api.py`: 15)
- Total tests passing (B-scoped regression set): 61/61 (`test_microsoft_graph_service.py`, `test_integraciones_api.py`, `test_integraciones_test_routes.py`, `test_google_workspace_service.py`, `test_integration_service.py`)
- Full repo suite: **1188 passed**, 5 failed (pre-existing/environmental, identical set to A1/A2's documented baseline — MinIO/S3 not running, one external golden fixture missing), 12 deselected (`live_llm`)
- Approval tests (refactoring): N/A — no existing Google route had prior API-level test coverage to use as an approval baseline; `TestConnect`/`TestCallback`/`TestStatus`/`TestRevoke`'s "google" cases serve as the first-ever regression pin for those routes, added in the same commit as the generalization
- Pure functions created: `_generate_pkce_pair` (microsoft_graph_service.py)

### Files Changed

| File | Action | What Was Done |
|------|--------|----------------|
| `app/core/config.py` | Modified | Added `AZURE_AD_CLIENT_ID`/`AZURE_AD_CLIENT_SECRET`/`AZURE_AD_TENANT_ID`/`AZURE_AD_REDIRECT_URI`/`MICROSOFT_OAUTH_SCOPES` Pydantic Settings (no hardcoded secrets) |
| `app/services/microsoft_graph_service.py` | Created | `build_authorization_url()` (PKCE S256), `exchange_code()` (token exchange + best-effort Graph `/me` email lookup), `handle_oauth_callback()` — mirrors `google_workspace_service.py`'s OAuth shape, delegates all crypto/state/persistence to `integration_service` |
| `app/services/integration_service.py` | Modified | Extended `_SCOPE_MARKERS` with Microsoft's `Mail.Read`/`Files.Read`/`Calendars.Read` markers — `_derive_enabled_flags`/`_to_status` (from A1) needed no changes, already generic |
| `app/api/v1/integraciones.py` | Modified | Replaced `/google/connect\|callback\|status\|revoke` with `/{provider}/connect\|callback\|status\|revoke`, `provider` typed as the existing `IntegrationProvider` StrEnum (path-param validation, no new "Provider" type introduced); callback cross-checks decoded state provider against path provider before dispatch; `status`/`revoke` now call `integration_service` directly (already provider-generic, no per-provider wrapper needed) |
| `tests/test_microsoft_graph_service.py` | Created | PKCE URL building, token exchange (+ email lookup, + error paths), callback wiring |
| `tests/test_integraciones_api.py` | Created | First-ever API-level (httpx.AsyncClient) coverage for connect/callback/status/revoke — both providers, plus provider-path validation and state/provider mismatch defense |

### Deviations from Design
- **`test_calendar`/`test_drive` NOT generalized to `{provider}`**, despite tasks.md Reconciliation #3 saying both should be in this slice. The user's explicit Slice B scope instruction excludes `MicrosoftGraphAdapter` (Slice C1 only) — generalizing these probe routes for `provider=microsoft` would require calling an adapter that doesn't exist yet. Kept both Google-only, unchanged; to be generalized in Slice C1 alongside the adapter itself.
- **No new "Microsoft OAuth request/callback schemas" in `app/schemas/integracion.py`** (task B.12 literally lists this). Not needed: the connect/callback routes take no Pydantic request body (query params only, same as Google today), and the existing `GoogleConnectURLResponse{authorization_url, state}` shape is already provider-neutral — reused as-is for both providers rather than duplicating an identical schema under a new name.
- **No separate `Provider` FastAPI-path enum type.** `IntegrationProvider` (already `google | microsoft`, defined in `app/models/integracion.py` since A1) is used directly as the path-parameter type. FastAPI validates path segments against `Enum` types natively — introducing a second enum with the same two members would be pure duplication.
- **`/{provider}/status` now returns `IntegrationStatus` (provider-discriminated, `mail_enabled`/`drive_enabled`/`calendar_enabled`) instead of the old Google-only `GoogleIntegrationStatus` (`gmail_enabled`/`drive_enabled`, no `calendar_enabled`) for `provider=google` too.** This is exactly what design.md's Interfaces section specifies ("`IntegrationStatus` ... replaces hardcoded `GoogleIntegrationStatus` shape") and what task B.12 requires (scope-derived flags for both providers via one shared schema/route). **Flagged as a risk**: this is a JSON field-rename for the existing `/integraciones/google/status` response (`gmail_enabled` → `mail_enabled`, plus a new `calendar_enabled` field) — any frontend code reading `gmail_enabled` from that endpoint needs a coordinated update. The *URL* keeps resolving identically (no redirect-URI/path change), which is what the hard backward-compatibility requirement was about, but the response *shape* changes per design's explicit interface contract. `GoogleIntegrationStatus` itself is untouched and still used internally by `google_get_integration_status`/`handle_oauth_callback` — it is simply no longer the API response model for the generalized `/status` route.
- **Reconnect coexistence relies on `email=""` collapsing to one row per `(usuario_id, provider)`**, same fallback Google already uses (`google_store_credentials` never passes an `email` kwarg either) — Microsoft's real email IS captured (via Graph `/me`, per spec's "persists ... account email" requirement and task B.8) and passed to `store_credentials`, so multi-account-per-provider (A1's locked goal) works correctly once a real email is available; the reconnect test uses the same email both times, which is the realistic path (a user reconnecting the same Microsoft account gets the same `mail`/`userPrincipalName` back from Graph).

### Issues Found
None. No regressions in any existing Google OAuth/discovery/adapter suite.

### Verification

- **Focused tests**: `uv run --no-sync python -m pytest tests/test_microsoft_graph_service.py tests/test_integraciones_api.py -q` → 23/23 passed.
- **B regression set**: `uv run --no-sync python -m pytest tests/test_microsoft_graph_service.py tests/test_integraciones_api.py tests/test_integraciones_test_routes.py tests/test_google_workspace_service.py tests/test_integration_service.py -q` → 61/61 passed.
- **Broader regression** (`-k "integracion or google or microsoft or drive or calendar or evidence"`): 210/210 passed.
- **Full suite**: `uv run --no-sync python -m pytest -q` → 1188 passed, 5 failed, 12 deselected. All 5 failures confirmed pre-existing/environmental and identical to A1/A2's documented baseline set (`test_agent_chat_robustness.py` x2, `test_agent_chat_service.py`, `test_checklist_api.py`, `test_obligaciones_golden_ejemplos.py` — MinIO/S3 not running, one external golden fixture missing; none import integraciones/OAuth code).
- **Lint**: `ruff check`/`ruff format --check` clean on every file this slice touched or created (`app/core/config.py`, `app/services/microsoft_graph_service.py`, `app/services/integration_service.py`, `app/api/v1/integraciones.py`, `tests/test_microsoft_graph_service.py`, `tests/test_integraciones_api.py`). Repo-wide debt is unchanged/untouched (same category documented in A1/A2's baseline, not introduced or fixable within this slice's scope).
- **Type check**: mypy on the 4 touched/created source files: 2 pre-existing errors on lines this diff didn't add (`config.py`'s `parse_cors_origins` validator, `integration_service.py`'s `verify_oauth_state` — both confirmed identical via `git stash` at the equivalent pre-diff line numbers, only line-number drift from new settings/lines added earlier in the file). `microsoft_graph_service.py` and the new routes in `integraciones.py` introduce zero new mypy errors.
- **Rollback boundary**: revert the 3 feature commits (config settings, `microsoft_graph_service.py` + scope markers, route generalization) — `google_tokens`/`integraciones` schema untouched (no migration in this slice), `/google/*` routes revert to their literal-path A2-tip shape exactly.

### Workload / PR Boundary
- Mode: stacked-to-main chained PR slice (auto-chain, already resolved — no decision needed)
- Current work unit: B — Microsoft OAuth + generalized routes (this batch)
- Boundary: starts at A2 tip `6664a73`, ends at the 3 feature commits on `feat/microsoft-365-b-oauth-routes` (+ 1 docs commit for this apply-progress update)
- Estimated review budget impact: forecast ~450-550 changed lines, High risk. **Actual measured below in the commit log** — driven mainly by the two new test files (23 new tests) needed to cover both the new PKCE/token-exchange service and the first-ever API-level regression pins for the Google OAuth routes this slice touches.

### Status
13/13 B tasks complete. Ready for `sdd-verify` on this slice, or for the next apply batch (Slice C1 — `MicrosoftGraphAdapter`, depends on A2 + B).

## Slice C1 — MicrosoftGraphAdapter (PR 4, this batch)

**Branch**: `feat/microsoft-365-c1-graph-adapter` (off B tip `68cee5d`, same worktree `cashing-backend-ms365`), NOT pushed.
**Mode**: Strict TDD
**Status**: 12/12 C1 tasks complete (C1.1-C1.12).

### TDD Cycle Evidence

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| C1.1/C1.2 | `tests/test_microsoft_graph_adapter.py::TestSearchMessages` | Unit (mocked `httpx.AsyncClient`) | N/A (new) | Written (`ModuleNotFoundError`) | 1/1 passed | non-blocking-event-loop assertion via `asyncio.gather` with a yielding sibling coroutine (1 case — scenario is single-path per spec) | Clean |
| C1.3/C1.4 | `tests/test_microsoft_graph_adapter.py::TestTokenRefresh` | Unit (mocked `httpx.AsyncClient` x2 — token endpoint + Graph endpoint, mocked DB row) | 1/1 (prior batch) passing before adding this | Written (assertion failures — no refresh logic existed) | 2/2 passed | expired-token refresh-then-call / no-connected-account raises `NotFoundError` (2 cases) | Clean |
| C1.5/C1.6 | `tests/test_microsoft_graph_adapter.py::TestSearchFiles` | Unit (mocked `httpx.AsyncClient`) | 2/2 passing before | Written (`ModuleNotFoundError` then `KeyError` on request-args assertion) | 1/1 passed | single scenario exercises all 4 contract rules at once (keywords→$search, date→$filter, folder-facet drop, mime post-filter) — matches the spec's single named scenario | Clean |
| C1.7/C1.8 | `tests/test_microsoft_graph_adapter.py::TestListEvents` | Unit (mocked `httpx.AsyncClient`) | 3/3 passing before | Written | 1/1 passed | single scenario per spec — shape-equivalence to Google's `CalendarEvent` output (summary/start/html_link/organizer/attendees) | Clean |
| C1.9/C1.10/C1.11 | `tests/test_microsoft_graph_adapter.py::TestRetryBackoff` | Unit (mocked `httpx.AsyncClient`, sequenced `side_effect`) | 4/4 passing before | Written | 2/2 passed | transient-429-then-success (2 requests) / all-429-exhausted (bounded ≤5 requests, scoped `ExternalServiceError`, no crash) (2 cases) | Clean |

### Test Summary
- Total tests written (new): 7 (`tests/test_microsoft_graph_adapter.py`)
- Total tests passing (C1-scoped focused set): 7/7
- Broader regression (`-k "calendar or drive or integracion or microsoft or evidence"`): **191/191 passed**
- Full repo suite: see Verification below
- Approval tests: N/A — new adapter, no prior behavior to preserve
- Pure functions created: `_parse_message`, `_parse_drive_file`, `_parse_event`, `_translate_drive_query`, `_escape_odata_literal`, `_parse_retry_after`, `_parse_graph_datetime` (all in `graph_adapter.py`)

### Files Changed

| File | Action | What Was Done |
|------|--------|----------------|
| `app/adapters/microsoft/__init__.py` | Created | Package marker |
| `app/adapters/microsoft/graph_adapter.py` | Created | `MicrosoftGraphAdapter` — full `EmailPort`/`DrivePort`/`CalendarPort` implementations against Microsoft Graph via `httpx.AsyncClient`; per-`(usuario_id, provider=microsoft)` token load + transparent refresh; bounded retry/backoff on 429 (honoring `Retry-After`) and bounded `@odata.nextLink` page walk shared by all 3 ports via `_request`/`_paginate` |
| `tests/test_microsoft_graph_adapter.py` | Created | Mocked-Graph coverage for all 5 spec scenarios (search_messages non-blocking, token refresh, drive query contract, calendar mapping, 429 retry + exhaustion) |
| `openspec/changes/microsoft-365-integration/tasks.md` | Modified | C1.1-C1.12 marked `[x]`, deviations noted inline |

### Deviations from Design
- **`run_in_executor` not used** (C1.2's literal wording) — the adapter uses `httpx.AsyncClient` directly, which is already async and non-blocking; there is no synchronous Graph SDK call to wrap (unlike the Google adapters, which wrap the synchronous `googleapiclient` library). The C1.1 test proves non-blocking behavior directly (a sibling coroutine advances via `asyncio.gather` while the Graph call is in flight) rather than by asserting an executor was used — the spec's actual requirement ("no synchronous Graph call blocks the event loop") is satisfied structurally, not through the executor mechanism named in the task text.
- **Token refresh (C1.4) reads/writes the `Integracion` row directly** rather than routing through `integration_service.store_credentials`. `store_credentials` is an upsert keyed on `(usuario_id, provider, email)` designed for the OAuth callback path (new connection or reconnection); a mid-call token refresh only needs to update `access_token_encrypted`/`refresh_token_encrypted`/`expires_at` on the *existing* row already resolved by `get_access_token`, with no email/upsert semantics involved — mirrors `gmail_adapter.get_credentials`'s established refresh-in-place pattern exactly.
- **`CalendarPort.list_events` does not exist** — the real protocol (Slice A2) exposes `search_events(usuario_id, time_min, time_max, ...)` and `get_event(usuario_id, event_id, ...)`. The task list / spec text's `list_events()` wording is informal; implemented against the actual protocol methods. `search_events` uses Graph's `/me/calendarView` (the windowed-query equivalent of Google's `timeMin`/`timeMax`), not `/me/events` literally, since plain `/me/events` has no time-window filter and would require an unbounded `$filter` string — `calendarView` is Graph's documented equivalent for this exact use case.
- **`DrivePort`/`EmailPort` implemented in full** (not just the methods C1.2/C1.6 name) — `list_files`/`get_file`/`download_file`/`delete_file` and `get_message`/`get_attachment`/`send_message` are implemented against their direct Graph/OneDrive equivalents so `MicrosoftGraphAdapter` is a real, structurally-complete Protocol implementer (matches design.md's File Changes table: "implementing EmailPort/DrivePort/CalendarPort via Graph"). Only `search_messages`/`search_files`/`search_events` have dedicated RED tests per this slice's explicit scope; the remaining methods are direct, low-complexity Graph-endpoint mappings with no branching logic warranting a dedicated test in this slice (consistent with ponytail's "trivial one-liners need no test" — each is a single Graph call + one parse function already covered by the parsing tests above).
- **Email query translation is not spec-locked** (unlike `DriveQuery`/`CalendarEvent`, which the spec pins exactly) — `search_messages` passes the free-text query straight to Graph's `$search` parameter with a `ConsistencyLevel: eventual` header (Graph's documented requirement for `$search` on `/me/messages`). No Gmail-style query-syntax translation exists to mirror, since Gmail's `q` parameter and Graph's `$search` are both free-text.

### Issues Found
None. Pre-existing uncommitted changes to `app/services/microsoft_graph_service.py` and `tests/test_microsoft_graph_service.py` were present in the worktree **before** this branch was created (from an unfinished Slice B follow-up, unrelated to C1) — left untouched and NOT included in any C1 commit, per instruction to only touch this slice's scope.

### Verification

- **Focused tests**: `uv run --no-sync python -m pytest tests/test_microsoft_graph_adapter.py -q` → 7/7 passed.
- **C1 regression set** (`-k "calendar or drive or integracion or microsoft or evidence"`): `uv run --no-sync python -m pytest tests/ -q -k "calendar or drive or integracion or microsoft or evidence"` → 191/191 passed.
- **Full suite**: `uv run --no-sync python -m pytest -q` → **1204 passed**, 5 failed, 12 deselected (`live_llm`), 1258.23s. All 5 failures confirmed pre-existing/environmental, identical set to A1/A2/B's documented baseline (`test_agent_chat_robustness.py` x2, `test_agent_chat_service.py`, `test_checklist_api.py`, `test_obligaciones_golden_ejemplos.py` — MinIO/S3 not running, one external golden fixture missing; none import Microsoft/Graph code). Net +16 passing tests vs B's 1188 baseline (7 new C1 tests + tests picked up from the pre-existing uncommitted Slice-B follow-up left in the worktree, not part of this diff — see Issues Found).
- **Lint**: `ruff check`/`ruff format --check` clean on `app/adapters/microsoft/__init__.py`, `app/adapters/microsoft/graph_adapter.py`, `tests/test_microsoft_graph_adapter.py`. Repo-wide `ruff check .` shows 968 pre-existing findings — identical count to A2/B's documented baseline, confirming this slice introduced zero new repo-wide findings.
- **Type check**: `mypy app/adapters/microsoft/` reports the same 7 pre-existing errors as the unmodified baseline, all in unrelated files (`app/core/db_ssl.py`, `app/core/config.py`, `app/core/database.py`, `app/models/*.py`) reached via import graph — zero errors attributed to `graph_adapter.py` itself.
- **Rollback boundary**: delete/revert `app/adapters/microsoft/` and `tests/test_microsoft_graph_adapter.py` — no other adapter or node depends on `MicrosoftGraphAdapter` yet (Slice C2 is the first consumer), so this slice is independently revertible with zero blast radius on Google or Microsoft OAuth code.

### Workload / PR Boundary
- Mode: stacked-to-main chained PR slice (auto-chain, already resolved — no decision needed)
- Current work unit: C1 — MicrosoftGraphAdapter (this batch)
- Boundary: starts at B tip `68cee5d`, ends at the C1 commit(s) on `feat/microsoft-365-c1-graph-adapter`
- Estimated review budget impact: forecast ~500-600 changed lines, High risk. One new adapter file (~430 lines) + one new test file (~370 lines) + tasks.md/apply-progress.md updates — within forecast, single self-contained PR (no correction round needed, unlike A1/A2 which needed a 4R follow-up).

### Status
12/12 C1 tasks complete. Ready for `sdd-verify` on this slice, or for the next apply batch (Slice C2 — provider-agnostic gate + noise heuristics, depends on C1).

## Slice C2 — Provider-agnostic gate + noise heuristics (PR 5, this batch)

**Branch**: `feat/microsoft-365-c2-gate-heuristics` (off C1 tip `cdc777d`, same worktree `cashing-backend-ms365`), NOT pushed. 4 commits (3 feature/docs commits + 1 correction commit below).
**Mode**: Strict TDD (RED confirmed via `git stash` of implementation files only, re-running the new/updated tests against pre-C2 code before restoring).
**Status**: 14/15 C2 tasks complete (C2.1-C2.2, C2.4-C2.15). C2.3 is **N/A** — see Deviations.

### TDD Cycle Evidence

| Task | Test File | Layer | RED (confirmed via stash) | GREEN | Notes |
|------|-----------|-------|------|-------|-------|
| C2.1/C2.2 | `tests/test_evidence_discovery.py::test_descubrir_evidencias_requires_a_connected_provider`, `::test_descubrir_evidencias_succeeds_microsoft_only_connected` | Unit (mocked adapters) | `AttributeError: no attribute 'integration_service'` | Passed | Gate now calls `integration_service.has_any_connected_provider`/`list_integration_statuses` |
| C2.4/C2.5 | `tests/test_evidence_discovery.py::test_descubrir_evidencias_merges_both_providers_and_isolates_failure`, `tests/test_drive_calendar_fetch.py::test_drive_fetch_microsoft_provider_uses_graph_adapter_and_appends`, `::test_calendar_fetch_microsoft_provider_uses_graph_adapter_and_appends`, `::test_drive_fetch_preserves_existing_evidencias_on_provider_error` | Unit + integration-style (mocked adapters) | Failed (no `provider` kwarg / no `MicrosoftGraphAdapter` patch target under old signature) | Passed | `drive_fetch_node`/`calendar_fetch_node` gained a `provider` param; APPEND (not overwrite) into `state`; per-provider try/except in the service loop |
| C2.6 | `tests/test_evidence_discovery.py::test_descubrir_evidencias_dedupes_duplicate_evidence_across_providers` | Unit (spy on matcher) | Failed (`evidence_dedup_node` not wired; `MicrosoftGraphAdapter` not patchable in `eds`) | Passed | Wired the pre-existing (until now unused-here) `evidence_dedup_node` into `descubrir_evidencias`'s chain, right after `evidence_orchestrator_node`, before `evidence_filter_node` |
| C2.7/C2.8 | `tests/test_evidence_filter.py::test_score_ms_other_inference_classification_is_noise_likely`, `::test_score_ms_ambiguous_email_passes_to_llm`, `::test_score_ms_whitelisted_domain_never_filtered` | Unit | `ImportError: cannot import name 'score_non_personal_ms_email'` | Passed | |
| C2.9/C2.10 | `::test_is_noise_ms_calendar_flags_allday_with_no_response`, `::test_is_noise_ms_calendar_keeps_confirmed_meeting` | Unit | `ImportError` | Passed | |
| C2.11/C2.12 | `::test_is_noise_ms_drive_filters_folder_like_item`, `::test_is_noise_ms_drive_keeps_real_file` | Unit | `ImportError` | Passed | |
| C2.13/C2.14 | `::test_dispatch_drops_ms_email_other_inference_classification`, `::test_dispatch_keeps_ms_calendar_confirmed_meeting`, `::test_dispatch_drops_ms_drive_folder_item`, `::test_dispatch_mixed_google_and_microsoft_batch_scored_by_own_heuristic`, `::test_dispatch_defaults_legacy_items_without_provider_to_google` | Unit | Mixed — `test_dispatch_drops_ms_drive_folder_item` genuinely RED; the email/calendar single-assertion dispatch tests initially had a test-design flaw (see below) | Passed (after fixing the flaw) | `_heuristic_is_noise` dispatches by `(source, provider)`, `meta.get("provider") or "google"` default |

**RED verification methodology**: `git stash push --keep-index -- <8 implementation files>` (never touching the pre-existing uncommitted security-fix diff on `integraciones.py`/`microsoft_graph_service.py`/their tests, which was never part of the stash), ran the full new/updated test set → 22 failures confirmed, then `git stash pop` to restore. Caught and fixed a real test-design flaw this way: `test_dispatch_drops_ms_email_other_inference_classification` and the mixed-batch test's `ms_other` case both used `sender="newsletter@service.com"`, which Google's own pre-existing `_AUTO_PREFIXES_NORMALIZED` heuristic already flags as noise (the literal string `"newsletter"` is in that frozenset) — so both tests passed "by accident" under the OLD code, without ever exercising the new Microsoft dispatch path. Fixed by changing the sender to `asistente@empresa.com` (a generic address that triggers no Google heuristic on its own), re-confirmed genuine RED, then GREEN.

### Test Summary
- Total tests written (new): 24 (`tests/test_evidence_filter.py`: +16, `tests/test_drive_calendar_fetch.py`: +3, `tests/test_evidence_discovery.py`: +4 net — 1 renamed/rewritten + 3 new)
- Focused C2 set (`tests/test_evidence_filter.py tests/test_drive_calendar_fetch.py tests/test_evidence_discovery.py tests/test_error_codes.py`): **84/84 passed**
- Broader regression (`-k "calendar or drive or integracion or microsoft or evidence"`): **210/210 passed**
- Full repo suite: **1223 passed**, 5 failed, 12 deselected (`live_llm`). All 5 failures are the exact pre-existing/environmental baseline documented since A1 (`test_agent_chat_robustness.py` x2, `test_agent_chat_service.py`, `test_checklist_api.py`, `test_obligaciones_golden_ejemplos.py` — MinIO/S3 not running, one external golden fixture missing; none import Microsoft/evidence-discovery code). Net +19 passing tests vs C1's 1204 baseline.
- One genuine regression found and fixed mid-slice: `tests/journey/test_full_radicacion_journey.py` and both gate-touching call sites in `test_evidence_discovery.py`/`test_error_codes.py` patched `eds.gws.google_get_integration_status`, which no longer exists once the gate moved to `integration_service` — all three files updated to patch the new gate functions (confirmed via a full-suite run that caught the journey-test breakage before this slice was considered done).

### Files Changed

| File | Action | What Was Done |
|------|--------|----------------|
| `app/core/exceptions.py` | Modified | `GOOGLE_NOT_CONNECTED` → `NO_PROVIDER_CONNECTED` (rename, no alias per Reconciliation #1) |
| `app/adapters/email/port.py` | Modified | `EmailMessage` gained `web_link: str = ""` (Graph's `webLink`, needed for a valid Outlook permalink — Gmail leaves it `""`, unused, since the service builds Gmail's permalink from `id` instead) |
| `app/adapters/microsoft/graph_adapter.py` | Modified | `_parse_message` now sets `web_link` from Graph's `webLink` and stashes `inferenceClassification` into `headers` (mirrors how Gmail's real headers dict is read by the noise scorer) |
| `app/agent/prompts/evidence_filter.py` | Modified | Added `score_non_personal_ms_email`, `is_noise_ms_calendar`, `is_noise_ms_drive` |
| `app/agent/nodes/evidence_filter.py` | Modified | `_heuristic_is_noise` dispatches by `(source, provider)`, default `"google"` for legacy items |
| `app/agent/nodes/calendar_fetch.py` | Modified | `calendar_fetch_node(state, provider=GOOGLE)` — provider-aware adapter (`GoogleCalendarAdapter`/`MicrosoftGraphAdapter`), APPENDS to `state["calendar_evidencias"]` instead of overwriting, tags each item with `provider`, preserves `existing` on error (was previously `[]` on any adapter error, silently wiping other providers' results) |
| `app/agent/nodes/drive_fetch.py` | Modified | Same shape as `calendar_fetch.py`, for `drive_fetch_node`/`DriveAdapter` |
| `app/services/evidence_discovery_service.py` | Modified | Gate calls `integration_service.has_any_connected_provider`; `_gather_gmail_evidence` renamed `_gather_email_evidence` (provider param, provider-aware adapter/heuristic/query-builder/permalink); new `_build_email_queries`/`_email_permalink` helpers; `descubrir_evidencias` loops over every connected provider for email/drive/calendar, isolating each provider's failure in its own `try/except`; wires the pre-existing `evidence_dedup_node` into the chain before `evidence_filter_node` |
| `tests/test_evidence_filter.py` | Modified | +16 tests: MS heuristic unit tests + `_heuristic_is_noise` dispatch tests (mixed batch, legacy-default) |
| `tests/test_drive_calendar_fetch.py` | Modified | +3 tests: provider-aware append/tag behavior for both fetch nodes, per-provider failure isolation |
| `tests/test_evidence_discovery.py` | Modified | Gate/merge/dedupe tests rewritten around the new `integration_service`-based gate; `+4` new scenarios (Microsoft-only success, both-providers-merge-with-isolated-failure, cross-provider dedup, provider-agnostic zero-connected) |
| `tests/test_error_codes.py` | Modified | `GOOGLE_NOT_CONNECTED` test → `NO_PROVIDER_CONNECTED`, patch target updated |
| `tests/journey/test_full_radicacion_journey.py` | Modified | Same gate-patch-target fix (caught by the full-suite run, not by the focused C2 set) |
| `openspec/changes/microsoft-365-integration/tasks.md` | Modified | C2.1-C2.15 marked, C2.3 annotated N/A |

### Deviations from Design

1. **C2.3 (local_only regression test) is N/A in this codebase lineage — not implemented, not skipped silently.** `local_only` does not exist anywhere in this worktree/branch chain (`ms365-integration/base` → A1 → A2 → B → C1 → C2): `evidence_discovery_service.descubrir_evidencias` here has no `local_only` parameter, no `_evidencias_subidas`/`local_evidence` wiring, nothing to bypass. It exists only in the separate `cashing-backend` repo's `master` branch (confirmed via `git log --all --oneline -- app/services/evidence_discovery_service.py` in this worktree — no `local_only` commit anywhere in this repo's history — and by reading `master`'s actual `evidence_discovery_service.py` in the other checkout, which has a full `local_only: bool = False` kwarg + local-file pipeline added by a commit later than this SDD change's base cut). There is no regression to guard against; writing a test for a parameter that doesn't exist would be fabricated, not a real RED/GREEN cycle. **Recommendation**: before this scenario becomes real, either rebase this feature chain (`ms365-integration/base`..C2) onto `master`, or backport the `local_only`/`_evidencias_subidas` commit — out of scope for this slice per its own instructions (touch only C2's assigned work).
2. **Email query translation for Microsoft is not spec-locked, so a plain keyword query was used instead of reusing Gmail's operator-laden query strings verbatim.** `build_obligation_queries` embeds Gmail-specific syntax (`subject:`, `after:`, `before:`, `-category:...`); Microsoft Graph's `$search` is free-text only (already established in Slice C1's `MicrosoftGraphAdapter.search_messages`). Sending Gmail's literal operator string to Graph's `$search` would search for that exact phrase and reliably match nothing. `_build_email_queries` instead builds a plain space-joined keyword query for Microsoft (mirrors `calendar_fetch.py`'s pre-existing `_build_calendar_query` pattern, which already does the same simplification for the `q` param both adapters share). Documented as a deviation per the microsoft-graph-adapter spec's own "Deferred to sdd-design: exact retry/backoff params, executor wrapping, Graph SDK vs raw HTTP choice" — query-syntax translation quality was never a locked requirement.
3. **`EmailMessage.web_link: str = ""` added** (email/port.py) — needed because `EvidenceLink.link` (the final API response schema) validates a real http(s) URL; Gmail's permalink is built by the service from `message.id` alone (unchanged), but Microsoft Graph's message resource has no equivalent deterministic ID-based deep-link scheme, so the adapter's real `webLink` (or an `OUTLOOK_PERMALINK` fallback if Graph omits it) is threaded through as a new optional field, defaulting to `""` (a no-op for Gmail).
4. **`is_noise_ms_calendar` does not check for a self-declined RSVP**, unlike `is_noise_calendar`. `CalendarAttendee.is_self` is a Google-only field (Slice A2 already documented this — Graph has no reliable equivalent), so the Microsoft calendar heuristic only mirrors the `isAllDay` + "no attendee response recorded" check, which does have a direct Graph equivalent (`responseStatus.response`).
5. **`is_noise_ms_drive(mime_type)` checks for an empty `mime_type`** rather than a Graph `folder` facet directly, because by the time an item reaches this heuristic it has already been normalized to `DriveFile` (whose `mime_type` is only populated from the Graph `file` facet, absent on folders) — this is defense-in-depth anyway, since `MicrosoftGraphAdapter.search_files` already excludes folders server-side via `query.exclude_folders`.
6. **Cross-provider dedup reuses the existing `evidence_dedup_node` (SHA-256 content hash)** rather than adding new merge/dedup logic — it existed in the codebase already (used by the full agent-graph `/chat` evidence-mode path) but was never wired into `descubrir_evidencias`'s manually-chained pipeline. Inserted right after `evidence_orchestrator_node`/before `evidence_filter_node`; since `matched_evidence` doesn't exist yet at that point in the chain, `evidence_dedup_node`'s own fallback (`all_matched if all_matched else deduped_raw`) correctly degrades to plain `evidence_raw` deduplication.

### Issues Found
One real regression introduced mid-slice and fixed before completion: removing the `gws` import from `evidence_discovery_service.py` (no longer needed once the gate moved to `integration_service`) broke `tests/journey/test_full_radicacion_journey.py`, which still patched `eds.gws.google_get_integration_status` — not caught by the focused C2 test set (that journey test isn't in `test_evidence_filter.py`/`test_drive_calendar_fetch.py`/`test_evidence_discovery.py`/`test_error_codes.py`), only by the full-suite run. Fixed by updating the journey test's gate patch to the new `integration_service` functions. This is exactly why the full-suite run (not just the focused set) is part of this slice's verification, consistent with every prior slice's practice.

### Verification

- **Focused tests**: `uv run --no-sync python -m pytest tests/test_evidence_filter.py tests/test_drive_calendar_fetch.py tests/test_evidence_discovery.py tests/test_error_codes.py -q` → 84/84 passed.
- **C2 regression set** (`-k "calendar or drive or integracion or microsoft or evidence"`): `uv run --no-sync python -m pytest tests/ -q -k "calendar or drive or integracion or microsoft or evidence"` → 210/210 passed.
- **Full suite**: `uv run --no-sync python -m pytest -q` → **1223 passed**, 5 failed, 12 deselected. All 5 failures confirmed pre-existing/environmental, identical set to A1/A2/B/C1's documented baseline.
- **Lint**: `ruff check`/`ruff format --check` run per-file on all 14 touched/created files. All touched app files are ruff-check-clean on the lines this slice authored; the handful of remaining findings (in `app/core/exceptions.py`, `app/agent/prompts/evidence_filter.py`, `app/agent/nodes/evidence_filter.py`, `app/services/evidence_discovery_service.py`) were individually confirmed via `git diff`/`git stash` to sit on lines this slice never touched — same pre-existing repo-wide debt documented since A1. `ruff format --check` similarly flags whole-file reformatting on files that were never `ruff format`-ted before this change (large pre-existing frozensets, long-line signatures elsewhere in the same files) — confirmed via per-hunk `--diff` inspection that every line this slice actually authored is already correctly formatted; only pre-existing content triggers the reformat. One real formatting fix WAS applied to code this slice wrote: `drive_fetch_node`'s new signature collapsed to one line (was needlessly wrapped across 3 lines) and one `SIM103` simplification in the new `is_noise_ms_calendar` (`return any(...)` instead of `if ...: return True / return False`).
- **Type check**: `mypy` on the 8 touched/created files, compared line-for-line against a `git stash`-restored pre-C2 baseline of the same 8-file target set: baseline 24 errors → post-C2 29 errors, **net +5, zero new error categories** (all 5 new instances are `Missing type arguments for generic type "dict"`/`"list[dict]"`, the same pre-existing repo-wide style already used by every sibling function in these exact files — e.g. `existing: list[dict] = state.get(...)` in the two fetch nodes, `emails_by_id: dict[str, dict] = {}`-style locals in the new email-gather code, `metadata: dict` in the new `is_noise_ms_calendar`). No error in `app/adapters/microsoft/graph_adapter.py`, `app/adapters/email/port.py`, or `app/core/exceptions.py` (0 baseline, 0 post).
- **Rollback boundary**: revert the 8 `app/` files + `tasks.md` — no schema/migration involved; `local_only`/Google-only gate behavior for the pre-existing (pre-C2) codebase is unaffected since this slice only generalizes behavior that already required a connected provider (Google before, any provider now). No other slice depends on C2 (final slice in the chain).

### Correction Round (independent re-verification, this session)

Re-verified the above (committed) C2 work independently before reporting it done: re-ran the focused set (84/84), the broader regression set (210/210), and ruff/mypy on all 8 touched `app/` files. Ruff findings (7, all pre-existing per `git blame`) matched the documented claim. **Found one real gap the prior verification missed**: `mypy` on `evidence_discovery_service.py` reported a genuinely NEW `func-returns-value` error (not in the documented "zero new error categories" claim) on the new query-dedup line — `not (q in seen_q or seen_q.add(q))` relies on `set.add()`'s `None` return, a correct but mypy-flagged idiom, and no sibling instance of this exact pattern exists elsewhere in the codebase to call it "pre-existing style". Fixed by rewriting as an explicit loop (functionally identical, order-preserving dedup):

```python
seen_q: set[str] = set()
unique_queries: list[str] = []
for q in queries:
    if q and q not in seen_q:
        seen_q.add(q)
        unique_queries.append(q)
```

Re-confirmed: `tests/test_evidence_discovery.py` 13/13, broader regression 210/210, `mypy app/services/evidence_discovery_service.py` no longer reports `func-returns-value` (only the same pre-existing `dict`-type-arg/`no-any-return`/`typeddict-item` findings), `ruff check`/`ruff format --diff` clean on the changed lines. Committed separately as a small fixup on top of the 3 C2 commits (see commit log).

### Workload / PR Boundary
- Mode: stacked-to-main chained PR slice (auto-chain, already resolved — no decision needed)
- Current work unit: C2 — provider-agnostic gate + noise heuristics (final batch, PR 5/5)
- Boundary: starts at C1 tip `cdc777d`, ends at the 4 commits (3 feature/docs + 1 correction fixup) on `feat/microsoft-365-c2-gate-heuristics`
- Estimated review budget impact: forecast ~350-420 changed lines, Medium risk. **Actual: 769 insertions + 110 deletions = 879 changed lines** (`git diff --stat` of the 3 feature/docs commits, including `tasks.md`) — exceeds forecast, consistent with every prior slice's documented overage pattern (driven mainly by the 4 touched/extended test files, which needed ~530 of those lines to cover the new dispatch/merge/dedup/heuristic behavior).

### Status
14/15 C2 tasks complete (C2.3 is N/A — see Deviations #1). This is the final slice in the microsoft-365-integration chain (A1 → A2 → B → C1 → C2, all now implemented). Ready for `sdd-verify` on this slice / the full chain, or for commit + review.
