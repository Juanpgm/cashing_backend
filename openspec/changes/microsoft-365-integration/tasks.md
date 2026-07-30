# Tasks: microsoft-365-integration

## Reconciliation Notes (design open questions → resolved by spec)

1. **`GOOGLE_NOT_CONNECTED` alias** — design D6 left it open. Spec
   (`evidence-discovery-gate`) only asserts `NO_PROVIDER_CONNECTED`; no alias
   scenario exists. Resolved: **no alias** — replace, don't dual-raise (Slice C2).
2. **Azure AD scopes** — spec (`microsoft-oauth`) locks `Mail.Read`,
   `Calendars.Read`, `Files.Read` (+ `offline_access`/`User.Read` per design
   rollout). Admin-consent-per-tenant remains a deployment/doc concern, not code
   — tracked as a Slice B follow-up note, not a task.
3. **`test_calendar`/`test_drive` route shape** — D1's action list
   (`connect|callback|status|revoke|test`) already includes `test` as a
   provider-parameterized action. Resolved: both go `{provider}`-generalized in
   Slice B, consumed via A2's dataclasses.
4. **`IntegrationStatus` per-source flags for Microsoft** — not scenario-locked
   by spec. Resolved at apply time: derive `mail_enabled`/`drive_enabled`/
   `calendar_enabled` from granted scope membership (task 3.12).

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | A1 ~350-420 / A2 ~380-450 / B ~450-550 / C1 ~500-600 / C2 ~350-420 (5 slices, ~2,000-2,400 total) |
| 400-line budget risk | Medium per A1/A2/C2 · High for B and C1 standalone |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 (A1) → PR 2 (A2) → PR 3 (B) → PR 4 (C1) → PR 5 (C2) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium

**Design's 4-slice cut is confirmed, with Slice C split further**: `MicrosoftGraphAdapter`
(3 ports + retry/backoff + token refresh, mocked-Graph tests) is a large, independently
verifiable unit on its own — bundling the gate + 3 heuristic functions into the same PR
risks exceeding 400 changed lines. Split into **C1 (adapter)** and **C2 (gate + merge +
heuristics)**, C2 depending on C1. Final order: **A1 → A2 → B → C1 → C2**. A1/A2 have no
dependency on each other (confirmed — disjoint files, design D-note "either order works");
A1 is kept first only as the default tie-break, not a discovered dependency.

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| A1 | `integraciones` table + migration/backfill + `integration_service` + repoint Google credential loaders | PR 1 → main | `uv run pytest tests/test_integration_service.py tests/test_integracion_model.py -v` | `alembic upgrade head` against Neon + manual Google-connected-user status check | Revert model/migration/service files; `google_workspace_service` reads `google_tokens` again |
| A2 | `CalendarEvent`/`DriveQuery` dataclasses + Google adapter/node updates | PR 2 → main | `uv run pytest tests/test_calendar_adapter.py tests/test_drive_adapter.py tests/agent/test_calendar_fetch*.py tests/agent/test_drive_fetch*.py -v` (full existing Google suite must stay green) | Manual `test_calendar`/`test_drive` route hit against a real connected Google account | Git revert of port/adapter/node diff — behavior-preserving, no schema change |
| B | Microsoft OAuth (PKCE) + `{provider}` routes + `IntegrationStatus`/config | PR 3 → main | `uv run pytest tests/test_microsoft_graph_service.py tests/test_integraciones_api.py -v` | `httpx.AsyncClient` hitting `/integraciones/{provider}/connect|callback|status` for both `google` and `microsoft` | Revert routes/service/schema files; `/google/*` still resolves via `{provider}` dispatch untouched |
| C1 | `MicrosoftGraphAdapter` (Email/Drive/Calendar via Graph, retry/backoff) | PR 4 → main | `uv run pytest tests/test_microsoft_graph_adapter.py -v` (Graph fully mocked) | N/A — no live Graph tenant in CI; mocked HTTP fixtures are the harness | Delete/revert `app/adapters/microsoft/graph_adapter.py`; no other adapter depends on it yet |
| C2 | Provider-agnostic gate + evidence merge + MS noise heuristics | PR 5 → main | `uv run pytest tests/test_evidence_discovery_gate.py tests/test_evidence_filter_ms.py -v` | Manual `descubrir_evidencias` run for a Microsoft-only-connected test user | Revert gate/heuristic diff; `local_only` path and Google-only gate behavior unaffected |

---

## Slice A1 — Credential store (PR 1, migration, API-stable, no Microsoft code)

- [x] A1.1 [RED] `tests/test_integracion_model.py`: `Integracion`/`IntegrationProvider` — unique `(usuario_id, provider, email)`; empty-email collapses to one row per `(user, provider)` (integraciones spec, Req 1 & D5).
- [x] A1.2 [GREEN] Create `app/models/integracion.py` — `Integracion` model + `IntegrationProvider` StrEnum; `email` NOT NULL `server_default=''`.
- [x] A1.3 [RED] Migration test (Neon): create `integraciones` + backfill `google_tokens` rows preserves encrypted tokens/expiry, no re-consent (integraciones spec scenario "Existing connected Google user keeps working"). — Neon unreachable from this sandbox; verified instead via alembic's real Operations/MigrationContext API against a throwaway SQLite DB (see apply-progress.md). Recommend a real Neon run before production.
- [x] A1.4 [GREEN] `alembic/versions/024_integraciones_table.py` — `create_table` + `UniqueConstraint` + `ix_integraciones_usuario_provider` + CHECK constraint + Python-loop backfill (`provider='google'`, `email=''` — `google_tokens` has no email column); no drop of `google_tokens`.
- [x] A1.5 [RED] `tests/test_integration_service.py`: `store_credentials` upserts by `(usuario_id, provider, email)`; reconnect updates in place, no duplicate (integraciones spec scenario).
- [x] A1.6 [GREEN] Create `app/services/integration_service.py` — `_fernet()`, `encode_oauth_state`/`verify_oauth_state` (provider-aware claim), `store_credentials`, `get_integration_status`, `list_integration_statuses`, `revoke_integration`.
- [x] A1.7 [RED] Test `has_any_connected_provider` — `False` at zero rows, `True` at ≥1.
- [x] A1.8 [GREEN] Implement `has_any_connected_provider` in `integration_service.py`.
- [x] A1.9 [RED] Test `IntegrationStatus` shape (provider discriminator, per-source flags); no-connections case returns unconnected without error (integraciones spec scenarios).
- [x] A1.10 [GREEN] Create `app/schemas/integracion.py` — `IntegrationStatus`.
- [x] A1.11 [GREEN] Repoint `google_workspace_service.py`, `gmail_adapter.py` credential loaders to `integraciones WHERE provider='google'`; delegate token/state/status to `integration_service`. (`drive_adapter.py`/`calendar_adapter.py` never queried `GoogleToken` directly — both already delegate credential loading to `GmailAdapter`; only their docstrings were corrected.)
- [x] A1.12 Regression gate: full existing Google OAuth/discovery/adapter suite green unchanged (54/54); `/google/*` routes/behavior untouched.
- [x] A1.13 Verify: `ruff check`/`ruff format --check` clean on all touched files; mypy strict shows only pre-existing repo debt (verified no new categories introduced); full suite 1137 passed / 5 pre-existing-environment failures (S3/MinIO not running, one external golden fixture missing) / 12 deselected (live_llm). See apply-progress.md for exact commands.

## Slice A2 — Port generalization (PR 2, no migration, ONLY Google-code-touching slice) — STATUS: COMPLETE (15/15 tasks)

Branch: `feat/microsoft-365-a2-port-generalization` (off A1 tip `3e8a538`, worktree `cashing-backend-ms365`), 4 commits, NOT pushed.

- [x] A2.1 [RED] Test `CalendarEvent`/`CalendarAttendee` dataclasses exist with fields per design D3 (calendar-port spec Req 1). `tests/test_calendar_port.py` (new).
- [x] A2.2 [GREEN] `app/adapters/calendar/port.py` — add `CalendarEvent`/`CalendarAttendee`; `search_events`/`get_event` return them + `q` param. Deviation: added `CalendarAttendee.is_self: bool` (not in design D3's field list) — required to preserve `evidence_filter.is_noise_calendar`'s `attendees[].self` check, which design D3 didn't account for.
- [x] A2.3 [RED] Test `GoogleCalendarAdapter` maps Google JSON → `CalendarEvent` using existing fixtures (calendar-port spec scenario "Google adapter returns normalized events"). Extended `tests/test_calendar_adapter.py` with all-day, attendees/organizer, and `get_event` mapping cases.
- [x] A2.4 [GREEN] `calendar_adapter.py` — map to `CalendarEvent` via new `_parse_event()`.
- [x] A2.5 [RED] Test `calendar_fetch.py` reads `event.start`/`event.is_all_day` (not raw dict keys); existing noise-detection cases produce identical pass/fail (calendar-port spec scenario "existing Google noise-detection tests unaffected"). Updated `tests/test_drive_calendar_fetch.py` calendar tests to build `CalendarEvent`/`CalendarAttendee` fixtures and assert the preserved `self`/`responseStatus` metadata shape.
- [x] A2.6 [GREEN] `app/agent/nodes/calendar_fetch.py` — consume `CalendarEvent`; `_event_start()` prefers `start` iso, else `start_date` iso, else `""`.
- [x] A2.7 [GREEN] `integraciones.py` `test_calendar` route — consume the dataclass. New `tests/test_integraciones_test_routes.py` (httpx, no prior test existed for this route).
- [x] A2.8 [RED] Test `DriveQuery` dataclass shape; Google adapter translates an equivalent query object to prior Google-syntax behavior (drive-port spec scenarios). Extended `tests/test_drive_search.py`.
- [x] A2.9 [GREEN] `app/adapters/drive/port.py` — add `DriveQuery`.
- [x] A2.10 [GREEN] `drive_adapter.py` — `search_files(usuario_id, query: DriveQuery)` via new `_translate_query()`: keywords OR-fan-out, date clause, `exclude_folders`, `mime_types`, `trashed=false` unchanged.
- [x] A2.11 [RED] Test `drive_fetch.build_drive_queries` returns `list[DriveQuery]` (not strings); dedup key is a normalized field tuple. Updated `tests/test_drive_calendar_fetch.py`.
- [x] A2.12 [GREEN] `app/agent/nodes/drive_fetch.py` — build `DriveQuery` objects, one per extracted keyword (≤3) + one per generic term (same granularity as the old per-string queries, to keep `EVIDENCE_QUERIES_PER_OBLIGACION` truncation behavior-identical — see Deviations in apply-progress.md for why "one `DriveQuery` per obligation" per design.md prose was not implemented literally).
- [x] A2.13 [GREEN] `integraciones.py` `test_drive` route — `DriveQuery(keywords=[])` probe.
- [x] A2.14 **Merge gate**: full existing Google adapter + `calendar_fetch` + `drive_fetch` + discovery suite green, unchanged pass/fail outcomes (design's stated acceptance gate for this slice). 147/147 passed (`-k "calendar or drive or evidence or integracion"`).
- [x] A2.15 Verify: `ruff check`/`ruff format --check` clean on touched files; mypy on touched files improved (206 errors vs 209 baseline on the same file set, net -3, no new categories); full suite 1160 passed / 5 pre-existing-environment failures (unchanged from A1's baseline set) / 12 deselected (`live_llm`). Repo-wide `make lint`/`ruff check .` reflects pre-existing, unrelated repo-wide debt (968 findings), consistent with A1's documented baseline — not introduced or fixable within this slice's scope.

## Slice B — Microsoft OAuth + generalized routes (PR 3, depends on A1)

- [x] B.1 [RED] Test `Provider` path-enum validation — unknown provider fails cleanly; existing `/google/*` URLs still resolve as `provider=google` (design D1 rationale). `tests/test_integraciones_api.py::TestProviderPathValidation`.
- [x] B.2 [GREEN] `app/api/v1/integraciones.py` — `{provider}/connect|callback|status|revoke` routes behind an `IntegrationProvider` (StrEnum, already defined in `app/models/integracion.py` — reused directly as the path-enum dependency, no separate `Provider` type introduced) path-param dependency; Google's redirect URI unchanged. **Deviation**: `test` (the `/calendar/test`/`/drive/test` probes) intentionally NOT generalized in this slice — see note below.
- [x] B.3 [RED] Test PKCE authorization URL redirects to Microsoft's authorize endpoint with `Mail.Read`/`Calendars.Read`/`Files.Read` + code challenge (microsoft-oauth spec scenario "User starts Microsoft connect"). `tests/test_microsoft_graph_service.py::TestBuildAuthorizationUrl`.
- [x] B.4 [GREEN] Create `app/services/microsoft_graph_service.py` — `build_authorization_url()` (PKCE verifier/challenge, S256), scopes from `Settings`.
- [x] B.5 [RED] Test tampered state rejected before token exchange; valid unexpired state proceeds; state-provider/path-provider mismatch rejected (microsoft-oauth spec scenarios + extra defense-in-depth case). `tests/test_integraciones_api.py::TestCallback`.
- [x] B.6 [GREEN] Wire Microsoft connect/callback through `integration_service.encode_oauth_state`/`verify_oauth_state` (provider claim already in A1); the generalized callback route calls `integration_service.verify_oauth_state` directly (3-tuple) instead of a provider-specific wrapper, and cross-checks the decoded provider against the path provider before dispatch.
- [x] B.7 [RED] Test code+verifier exchange → tokens (+ account email via Graph `/me`); `store_credentials(provider=microsoft)` coexists with an existing `provider=google` row untouched (microsoft-oauth spec scenario "Coexistence with an existing Google connection"). `tests/test_microsoft_graph_service.py::TestExchangeCode`, `tests/test_integraciones_api.py::TestRevoke::test_revoking_microsoft_does_not_affect_google`.
- [x] B.8 [GREEN] `microsoft_graph_service.py` — `exchange_code(code, code_verifier)` → access/refresh/`expires_in`/scopes/email (`email` via a best-effort Graph `/me` call — failure defaults to `""`, does not fail the exchange).
- [x] B.9 [RED] Test reconnecting the same Microsoft account updates the existing row, no duplicate. `tests/test_integraciones_api.py::TestReconnect`.
- [x] B.10 [GREEN] Callback route → `integration_service.store_credentials(provider=microsoft)` via `microsoft_graph_service.handle_oauth_callback`.
- [x] B.11 [RED] `httpx.AsyncClient` integration test: connect/callback/status/revoke for `google` (regression) and `microsoft`. `tests/test_integraciones_api.py` (15 tests).
- [x] B.12 [GREEN] `app/core/config.py` — Azure AD client id/secret/tenant/redirect/scopes settings (no hardcoded secrets); `app/services/integration_service.py` — extended `_SCOPE_MARKERS` with Microsoft's `Mail.Read`/`Files.Read`/`Calendars.Read` markers so `mail_enabled`/`drive_enabled`/`calendar_enabled` derive from granted scopes at read time (already-generic `_derive_enabled_flags`/`_to_status` from A1 needed no changes). **Deviation**: no new "Microsoft OAuth request/callback schemas" added to `app/schemas/integracion.py` — the connect/callback routes take no request body (query params only, same as Google today), and the existing provider-neutral `GoogleConnectURLResponse{authorization_url, state}` shape is reused as-is for both providers' connect response (no Microsoft-specific fields needed).
- [x] B.13 Verify: `ruff check`/`ruff format --check` clean on every touched/created file; mypy on touched files shows only pre-existing errors (confirmed identical line-for-line via `git stash` diff, no new errors); full suite 1188 passed / 5 pre-existing-environment failures (same set as A1/A2's baseline: MinIO/S3 not running, one external golden fixture missing) / 12 deselected (`live_llm`); confirmed `/google/*` end-to-end behavior unchanged (regression tests in `tests/test_integraciones_api.py`).

**Deviation note (test_calendar/test_drive)**: Reconciliation #3 above proposed generalizing `test_calendar`/`test_drive` to `{provider}` in this slice. NOT implemented — the user's explicit Slice B scope instruction excludes `MicrosoftGraphAdapter` (Slice C1: "Slice B only builds the OAuth/credential-acquisition path, not the adapters that USE those credentials to fetch data"). Parameterizing these probe routes for `provider=microsoft` would require calling an adapter that does not exist yet; stubbing a fake response would test nothing real. `/integraciones/calendar/test` and `/integraciones/drive/test` remain Google-only, unchanged, to be generalized in Slice C1 alongside `MicrosoftGraphAdapter` itself.

## Slice C1 — MicrosoftGraphAdapter (PR 4, depends on A2 + B)

- [x] C1.1 [RED] Test `search_messages` against mocked Graph `/me/messages` returns normalized results without blocking the event loop (microsoft-graph-adapter spec scenario).
- [x] C1.2 [GREEN] Create `app/adapters/microsoft/graph_adapter.py` — `EmailPort.search_messages` via Graph, `run_in_executor`/async HTTP. **Deviation**: used `httpx.AsyncClient` directly (already async, non-blocking) instead of `run_in_executor` — no synchronous Graph SDK is used, so there is no blocking call to wrap.
- [x] C1.3 [RED] Test expired access token is refreshed transparently before a Graph call (spec scenario).
- [x] C1.4 [GREEN] Token refresh per `(usuario_id, provider=microsoft)` using `integraciones` + `integration_service`. **Deviation**: refresh reads/writes the `Integracion` row directly (mirrors `gmail_adapter.get_credentials`'s in-place refresh pattern) instead of routing through `integration_service.store_credentials` — only access/refresh token + expiry change on an existing row, no upsert-by-email needed here.
- [x] C1.5 [RED] Test `search_files(DriveQuery)` uses the same contract as the Google adapter: keywords → `$search`, dates → `$filter`, `exclude_folders` → drop `folder` facet, `mime_types` → post-filter (spec scenario "OneDrive search uses same query object contract").
- [x] C1.6 [GREEN] `graph_adapter.py` — `DrivePort.search_files`/`upload_file`/`get_or_create_folder`/`make_shareable` against OneDrive. Full `DrivePort` protocol implemented (`list_files`/`get_file`/`download_file`/`delete_file` too) for structural conformance, not just the 4 explicitly named methods.
- [x] C1.7 [RED] Test `list_events()` maps Graph `/me/events` → `CalendarEvent`, shape-equivalent to the Google adapter's output (spec scenario). **Deviation**: the real `CalendarPort` protocol (from Slice A2) exposes `search_events`/`get_event`, not `list_events` — the task/spec text uses `list_events` informally. Implemented against the actual protocol method names.
- [x] C1.8 [GREEN] `graph_adapter.py` — `CalendarPort.search_events`/`get_event` via Graph `/me/calendarView` (windowed) and `/me/events/{id}`.
- [x] C1.9 [RED] Test transient 429 with `Retry-After` recovers within the retry budget, no error surfaced (spec scenario "Transient 429 recovers on retry").
- [x] C1.10 [RED] Test retry budget exhausted → scoped provider error, no crash, bounded pagination (spec scenario "Retry budget exhausted").
- [x] C1.11 [GREEN] Bounded retry/backoff honoring `Retry-After` + bounded page walk across all 3 port methods.
- [x] C1.12 Verify: `make lint` + `make test` (Graph fully mocked, no live tenant). See apply-progress.md for exact results.

## Slice C2 — Provider-agnostic gate + noise heuristics (PR 5, depends on C1)

- [x] C2.1 [RED] Test gate raises `NO_PROVIDER_CONNECTED` only at zero connected providers; succeeds with Microsoft-only connected (evidence-discovery-gate spec scenarios).
- [x] C2.2 [GREEN] `app/core/exceptions.py` — add `NO_PROVIDER_CONNECTED` (replaces `GOOGLE_NOT_CONNECTED`, no alias — see Reconciliation #1); `evidence_discovery_service.py` gate calls `integration_service.has_any_connected_provider`.
- [ ] C2.3 [RED] Regression test: `local_only=True` still bypasses the gate unchanged. **N/A in this codebase lineage** — `local_only` does not exist anywhere in this worktree/branch chain (`ms365-integration/base` → A1 → A2 → B → C1 → C2); it exists only on the separate `cashing-backend` repo's `master`, added there after this SDD change's base was cut (confirmed via `git log --all -- app/services/evidence_discovery_service.py` and grepping both trees). There is nothing to regress-test. See apply-progress.md for full analysis; recommend rebasing this feature chain onto master (or backporting `local_only`) before this scenario becomes applicable.
- [x] C2.4 [RED] Test discovery merges `evidence_raw` from both providers when both connected; one provider's failure doesn't abort the other's results (spec scenarios "both connected" / "Microsoft fails, Google succeeds").
- [x] C2.5 [GREEN] `evidence_discovery_service.py` + `calendar_fetch.py`/`drive_fetch.py`/Gmail-gather step — provider-aware adapter instantiation per connected provider; append normalized items with `metadata.provider` set; isolate per-provider failures.
- [x] C2.6 [RED] Test duplicate evidence across providers deduplicated by existing dedup logic (spec scenario).
- [x] C2.7 [RED] Test `score_non_personal_ms_email` flags "other" `inferenceClassification` as noise-likely; ambiguous email passes to the LLM, not silently discarded (microsoft-noise-heuristics spec scenarios).
- [x] C2.8 [GREEN] `app/agent/prompts/evidence_filter.py` — `score_non_personal_ms_email`.
- [x] C2.9 [RED] Test `is_noise_ms_calendar` flags all-day-with-no-response as noise; confirmed meeting not flagged (spec scenarios).
- [x] C2.10 [GREEN] `is_noise_ms_calendar`.
- [x] C2.11 [RED] Test `is_noise_ms_drive` filters OneDrive folder/system items (spec scenario).
- [x] C2.12 [GREEN] `is_noise_ms_drive`.
- [x] C2.13 [RED] Test `_heuristic_is_noise` dispatch by `(source, provider)` — mixed batch scored by each item's own heuristic; Google items unchanged (spec scenario "Mixed Google/Microsoft batch scored correctly").
- [x] C2.14 [GREEN] `app/agent/nodes/evidence_filter.py` — dispatch by `(source, provider)`, default `provider="google"` for legacy items.
- [x] C2.15 Verify: `make lint` + `make test`; full regression across `evidence_discovery`, `evidence_filter`, `calendar_fetch`, `drive_fetch` suites.

---

## Review Workload Forecast (Per-Slice Recap)

| Slice | PR | Est. lines | Risk | Depends on |
|-------|-----|-----------|------|------------|
| A1 | Credential store + migration | ~350-420 | Medium | none |
| A2 | Port generalization | ~380-450 | Medium (behavior-preserving, high blast radius if regressed) | none (independent of A1) |
| B | Microsoft OAuth + routes | ~450-550 | High | A1 |
| C1 | MicrosoftGraphAdapter | ~500-600 | High | A2, B |
| C2 | Gate + merge + heuristics | ~350-420 | Medium | C1 |

Decision needed before apply: No (auto-chain, stacked-to-main already resolved). Each PR
requires its own merge-gate verification (Slice A2/C1's Google-suite-green gate is
explicit) before the next slice branches off it.
