# Design: microsoft-365-integration

## Technical Approach

Add Microsoft 365 (Graph: Outlook Mail + Outlook Calendar + OneDrive) as a second
evidence provider behind the existing Ports & Adapters seams, following
`model → schema → service → api → test`. The work splits cleanly along one axis: the
**credential/provider generalization** (a data + service concern) is orthogonal to the
**port generalization** (`CalendarEvent` + `DriveQuery`, an adapter-shape concern). Neither
depends on the other, and both are prerequisites for the `MicrosoftGraphAdapter`. Every
schema change lands in one real Alembic migration verified on real Postgres (Neon), since
`create_all` no-ops table changes on existing DBs.

The guiding constraint that shapes most decisions: **OAuth redirect URIs are provider-
registered fixed strings.** `/integraciones/google/callback` is already registered in Google
Cloud; `/integraciones/microsoft/callback` will be registered in Azure AD. The route shape
therefore cannot be a purely internal choice — it is externally pinned per provider, which
makes a validated `{provider}` path parameter the natural (and non-breaking) generalization
rather than a rename.

The single safety-critical change is the port generalization, because it is the ONLY edit
that touches currently-working Google code paths. It is designed to be behavior-preserving
and guarded by the existing Google test suite passing unchanged before any Microsoft code
merges.

## Architecture Decisions

### D1 — Route shape: validated `{provider}` path parameter (Open Decision #1)

| Option | Tradeoff | Decision |
|---|---|---|
| Per-provider explicit routes (`/google/*`, `/microsoft/*`) | Duplicates 5 handlers per provider; provider #3 is another copy; but matches registered redirect URIs literally | Rejected as the primary shape |
| Fully generic `/integraciones/{provider}/{action}` free-form | One handler set; but `{provider}` unvalidated can shadow existing static segments (`evidencias`, `email`, `drive`, `calendar`) and mis-route | Rejected |
| **`/integraciones/{provider}/connect\|callback\|status\|revoke\|test` with `provider` validated against an enum dependency** | One handler set parameterized by provider; existing `/integraciones/google/*` URLs keep resolving as `provider=google` (zero redirect-URI reconfiguration, zero frontend change); unknown providers fail with a domain error, never mis-route | **Chosen** |

Rationale: the existing Google routes are literally `provider=google` under this scheme, so the
Google Cloud redirect URI and every frontend call (`/google/status`, `/google/connect`,
`/google/callback`, `/google/revoke`) keep working untouched. Microsoft is purely additive
(`provider=microsoft`, registered in Azure AD). A `Provider` enum dependency
(`google | microsoft`) validates the path segment, so a stray `/integraciones/evidencias/status`
fails validation cleanly instead of passing `"evidencias"` into a handler. The action segments
(`connect/callback/status/revoke/test`) do not collide with existing static second-segments
(`evidencias/email/drive/calendar`), so no route ambiguity is introduced. Per-provider OAuth
`Flow` construction still lives in each provider's service module (D2); the route is a thin
dispatcher keyed on `provider`.

### D2 — Service split: shared helpers + per-provider modules, not one god-service nor duplicated crypto (Open Decision #2)

| Option | Tradeoff | Decision |
|---|---|---|
| Fully generalize `google_workspace_service` into one provider-agnostic `integration_service` | Large risky rewrite of working, security-critical Google code; one file owns two providers' OAuth flows | Rejected |
| Keep `google_workspace_service`, add sibling `microsoft_graph_service` that re-implements token-encryption/JWT-state | Duplicates the Fernet + signed-JWT-state logic — the exact security-critical code you least want copy-pasted | Rejected |
| **Extract shared provider-agnostic helpers into `integration_service.py`; keep `google_workspace_service` (Google OAuth Flow + Gmail/Drive send-upload); add `microsoft_graph_service` (Graph OAuth + Graph calls)** | Crypto/state/store logic lives once; each provider module is additive and owns only its OAuth `Flow` + API specifics; matches how adapters already share credential logic | **Chosen** |

`integration_service.py` (new) owns the provider-agnostic, security-sensitive core:

- `_fernet()` (moved from `google_workspace_service`).
- `encode_oauth_state(usuario_id, code_verifier, provider)` / `verify_oauth_state(state) -> (usuario_id, code_verifier, provider)` — the existing signed-JWT state, generalized to carry `provider` in the claims so one callback dispatcher can route.
- `store_credentials(db, usuario_id, provider, *, access_token, refresh_token, scopes, expires_in, email)` — Fernet-encrypt + upsert into `integraciones` keyed on `(usuario_id, provider, email)`.
- `revoke_integration(db, usuario_id, provider)`.
- `get_integration_status(db, usuario_id, provider) -> IntegrationStatus` and `list_integration_statuses(db, usuario_id) -> list[IntegrationStatus]`.
- `has_any_connected_provider(db, usuario_id) -> bool` — the generalized gate helper (D6).

`google_workspace_service` keeps its Google `Flow` construction, Gmail search/send, and Drive
upload, but delegates token persistence/state/status to `integration_service` and passes
`provider="google"`. `microsoft_graph_service` (new) mirrors it for Graph: builds the Azure AD
authorization-code+PKCE URL, exchanges the code, and exposes Graph Mail/Calendar/Drive service
calls used by the adapter — reusing the shared helpers, never re-implementing crypto.

### D3 — `CalendarEvent` dataclass (safety-critical port change)

`CalendarPort` today returns `list[dict[str, Any]]` = raw Google Calendar resources, and three
call sites read Google-specific keys directly: `calendar_fetch.py` (`ev["start"]["dateTime"]`,
`summary`, `description`, `htmlLink`, `attendees`, `organizer`, `eventType`, all-day detection),
the `test_calendar` route in `integraciones.py` (`_extract_dt`, `summary`, `location`,
`htmlLink`), and `_extract_event_metadata`. The neutral dataclass must carry every field those
readers touch, so the Google adapter maps once and both nodes/routes consume the dataclass. It
mirrors the existing `DriveFile` dataclass pattern (a frozen-ish `@dataclass` in the port
module).

```python
@dataclass
class CalendarEvent:
    id: str
    summary: str                       # "" when Google omits it; readers today default "(sin título)"
    description: str = ""
    start: datetime | None = None      # timed events: start.dateTime → aware datetime
    end: datetime | None = None
    start_date: date | None = None     # all-day events: start.date (no time component)
    end_date: date | None = None
    is_all_day: bool = False           # True when only .date was present (Graph: isAllDay)
    location: str | None = None
    html_link: str = ""                # Google htmlLink / Graph webLink
    attendees: list[CalendarAttendee] = field(default_factory=list)
    organizer_email: str | None = None
    event_type: str = "default"        # Google eventType; Graph has no equivalent → "default"

@dataclass
class CalendarAttendee:
    email: str = ""
    display_name: str = ""
    response_status: str = ""          # Google responseStatus / Graph status.response
    optional: bool = False
```

Notes:
- `start`/`end` (timed) and `start_date`/`end_date` (all-day) are kept as distinct typed fields
  rather than a stringly-typed union, so noise heuristics can reason about all-day vs timed
  without re-parsing. `_event_start()` in `calendar_fetch.py` becomes: prefer `start` isoformat,
  else `start_date` isoformat, else `""`.
- `attendees` is a typed sub-dataclass, not `list[dict]`, because both `is_noise_calendar` and
  the Microsoft heuristic need `response_status`. The Google adapter maps Google's
  `attendees[].responseStatus`; the Graph adapter maps `attendees[].status.response`.
- `CalendarPort.search_events` gains the `q: str | None = None` param that the real
  `GoogleCalendarAdapter` already accepts (the current Protocol is stale — it omits `q` and the
  `search_events`/`get_event` return type). Return type becomes `list[CalendarEvent]`;
  `get_event -> CalendarEvent`.

### D4 — `DrivePort` structured query object (safety-critical port change)

`drive_fetch.build_drive_queries()` today emits **multiple raw Google query strings** per
obligation (`(name contains 'k' or fullText contains 'k') and modifiedTime >= '...' and mimeType
!= folder`). The structured object moves keyword/date/type intent out of Google syntax; each
adapter translates it to its native language. The node builds ONE `DriveQuery` per obligation
(a keyword list, not N strings); the adapter is responsible for the OR-fan-out.

```python
@dataclass
class DriveQuery:
    keywords: list[str] = field(default_factory=list)   # OR-matched across name + full text
    date_from: datetime | None = None                    # inclusive modified/lastModified lower bound
    date_to: datetime | None = None                      # inclusive upper bound
    exclude_folders: bool = True                          # drop folder/system items
    mime_types: list[str] | None = None                  # optional positive type filter (None = any)
    max_results: int = 20
```

Translation contract (documented on the port, enforced per adapter):
- **Google** (`DriveAdapter.search_files`): `keywords` → `(name contains 'k1' or fullText contains 'k1' or name contains 'k2' …)`; `date_from/date_to` → `modifiedTime >= '…' and modifiedTime <= '…'`; `exclude_folders` → `and mimeType != 'application/vnd.google-apps.folder'`; always `and trashed = false` (adapter already adds this). Quote-escaping of keywords stays in the adapter.
- **Graph/OneDrive** (`MicrosoftGraphAdapter.search_files`): `keywords` → `/me/drive/root/search(q='k1 k2')` (Graph search is already OR-ish across name/content); `date_from/date_to` → `$filter=lastModifiedDateTime ge … and le …`; `exclude_folders` → drop items where `folder` facet is present; `mime_types` → post-filter on `file.mimeType`.

`DrivePort.search_files` signature changes from `(usuario_id, query: str, max_results)` to
`(usuario_id, query: DriveQuery)` (max_results folds into the object). `drive_fetch.py`'s
`build_drive_queries` returns `list[DriveQuery]` instead of `list[str]`; the dedup key becomes a
normalized tuple of the query fields rather than the raw string. `test_drive` route builds a
`DriveQuery(keywords=[])` for its "most recent files" probe.

### D5 — `integraciones` table: schema, uniqueness refinement, cutover order (Open Decision #3)

Final columns (`app/models/integracion.py`, `Integracion` model, singular Spanish per naming
convention, `__tablename__ = "integraciones"`):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `UUIDMixin` |
| `usuario_id` | UUID FK `usuarios.id`, indexed | NOT unique alone (multi-provider) |
| `provider` | `Enum(IntegrationProvider, native_enum=False)` → VARCHAR(20)+CHECK | `google \| microsoft`; `native_enum=False` keeps it portable to aiosqlite and avoids a PG-enum migration/enum-alter dance |
| `access_token_encrypted` | Text NOT NULL | Fernet |
| `refresh_token_encrypted` | Text NOT NULL | Fernet; Graph returns one when `offline_access` scope is granted |
| `scopes` | String(1000) | widened from Google's 500 — Graph scope strings run longer |
| `expires_at` | DateTime(tz) nullable | |
| `email` | String(320) **NOT NULL, server_default=''** | see uniqueness refinement below |
| `created_at` / `updated_at` | | `TimestampMixin` |

**Uniqueness — refined from the proposal's `(usuario_id, provider, email)`:** keep the
three-column `UniqueConstraint(usuario_id, provider, email)` **but make `email` NOT NULL with
`server_default=''`** instead of nullable. Rationale: Postgres treats NULLs as distinct, so a
nullable `email` would let a user accumulate duplicate `(user, google, NULL)` rows — silently
defeating the constraint exactly for legacy rows whose email is unknown (pre-migration-029
Google tokens). With `email` defaulting to `''`, an unknown-email connection collapses to a
single row per `(user, provider)` (degrading gracefully to one-account-per-provider, which is
all the current UI actually drives), while known emails still support the locked multi-account
goal. Add a plain index on `(usuario_id, provider)` for the common status/gate lookups.

`IntegrationProvider` is a `StrEnum` (`GOOGLE = "google"`, `MICROSOFT = "microsoft"`).

**Migration cutover order (one migration, additive):**
1. `op.create_table("integraciones")` + `UniqueConstraint(usuario_id, provider, email)` + `ix_integraciones_usuario_provider`.
2. Backfill from `google_tokens` via `op.execute`: `INSERT INTO integraciones (id, usuario_id, provider, access_token_encrypted, refresh_token_encrypted, scopes, expires_at, email, created_at, updated_at) SELECT gen_random_uuid(), usuario_id, 'google', access_token_encrypted, refresh_token_encrypted, scopes, expires_at, COALESCE(email, ''), created_at, updated_at FROM google_tokens`. (`op.execute` in a migration is the sanctioned place for SQL — the app-layer no-raw-SQL rule does not apply here. `gen_random_uuid()` is available on Neon/pgcrypto.)
3. Code cutover (Slice A): `integration_service.store_credentials/get_integration_status/revoke` and the three Google adapters' credential loaders read/write `integraciones WHERE provider='google'` instead of `google_tokens`.
4. **`google_tokens` is kept intact and read-only — NOT dropped in this migration.**

**Drop vs keep (Open Decision #3 tail):** keep `google_tokens` read-only indefinitely through
this change; the `DROP TABLE` is a **separate follow-up migration** gated on verified prod
cutover. Rationale: an additive create+backfill migration's rollback is lossless **only if run
before any post-cutover write** (point reads back at `google_tokens`, drop `integraciones` — no
data reconstruction needed, because the backfill was a copy taken before cutover). Once the code
cutover is live, downgrade is destructive: any Google row created/refreshed after cutover, and
every Microsoft row unconditionally (no fallback table exists for Microsoft at all), is lost —
see the `downgrade()` docstring in `alembic/versions/024_integraciones_table.py`. Dropping in the
same migration makes the down-path lossy from the start and forces a data-reconstructing
down-migration. The table is one row per user — keeping it costs effectively nothing as a
rollback safety net, but it is a safety net for the pre-cutover window only.

### D6 — Provider-agnostic connection gate + evidence merge

The gate in `descubrir_evidencias` (`evidence_discovery_service.py:311-318`) currently calls
`gws.google_get_integration_status` and raises `GOOGLE_NOT_CONNECTED` when Google is not connected. It
generalizes to: `if not local_only and not await integration_service.has_any_connected_provider(db, usuario_id): raise ExternalServiceError(...)` with a new provider-agnostic code
`NO_PROVIDER_CONNECTED` (keep `GOOGLE_NOT_CONNECTED` as a still-raised alias where only Google is
relevant, to avoid breaking existing callers/tests that assert on it — decided in `sdd-spec`).
`local_only=True` continues to bypass the gate entirely (unchanged).

Discovery then runs the fetch nodes **for each connected provider** and merges into the single
`evidence_raw` list the orchestrator already builds. Concretely, the fetch nodes
(`drive_fetch_node`, `calendar_fetch_node`, and the Gmail-gather step) become provider-aware:
they instantiate the Google adapter when Google is connected and the Microsoft adapter when
Microsoft is connected, appending normalized items (same `{source, title, content, link, date,
metadata}` dict shape) to the same lists. Two providers connected → both contribute; existing
dedup/filter stages operate on the normalized shape unchanged. Each evidence item's `metadata`
gains a `provider` key so the noise layer can dispatch (D7).

### D7 — Microsoft noise heuristics: dispatch by `(source, provider)`, functions beside Google's

`_heuristic_is_noise` (`evidence_filter.py:70-92`) dispatches by `source`
(email/calendar/drive) into `score_non_personal_email` / `is_noise_calendar` / `is_noise_drive`
(in `app/agent/prompts/evidence_filter.py`). Microsoft counterparts live **beside** the Google
ones in the same module, and dispatch keys on `(source, provider)` read from
`item["metadata"]["provider"]` (defaulting to `"google"` so existing Google items are untouched):

- Email: `score_non_personal_ms_email(...)` reading Outlook `categories` / `inferenceClassification` (Focused vs Other) instead of Gmail `CATEGORY_PROMOTIONS` labels.
- Calendar: `is_noise_ms_calendar(...)` reading Graph `attendees[].status.response` / `isAllDay` / `showAs` — but since `CalendarEvent` (D3) already normalizes attendees + all-day, much of this can share the neutral heuristic; the Microsoft-specific part is only the fields Graph exposes that Google doesn't.
- Drive: `is_noise_ms_drive(...)` — OneDrive folder/system-item filter (the `folder` facet + system paths) rather than Google's `application/vnd.google-apps.folder` MIME check.

Dispatch is an additive branch; the source-agnostic `WORK_NOISE_SYSTEM_PROMPT` LLM layer is
unchanged. Heuristics stay conservative (a wrong pre-filter over-includes into the LLM, never
hard-drops), with a tuning knob left in place. `ponytail:` naive thresholds, upgrade only if
Microsoft precision measurably lags Google's.

## Data Flow

```
CONNECT   → GET /integraciones/{provider}/connect
            → provider service builds PKCE + signed state{usuario_id, cv, provider}
            → GoogleConnectURLResponse{authorization_url, state}

CALLBACK  → GET /integraciones/{provider}/callback?code&state
            → integration_service.verify_oauth_state(state) → (usuario_id, cv, provider)
            → provider service exchanges code → tokens
            → integration_service.store_credentials(provider) → integraciones (Fernet)
            → 303 redirect {FRONTEND_URL}/integraciones?{provider}=connected

STATUS    → GET /integraciones/{provider}/status → IntegrationStatus{provider, connected, sources…}

DISCOVER  → POST /integraciones/evidencias/descubrir
            gate: local_only? bypass : has_any_connected_provider(user) else raise NO_PROVIDER_CONNECTED
            for each connected provider:
              gmail/outlook mail  → EmailPort   → normalized email items
              drive/onedrive      → DrivePort.search_files(DriveQuery) → normalized drive items
              calendar/outlook    → CalendarPort.search_events → CalendarEvent → normalized items
            → merge into evidence_raw (each item.metadata.provider set)
            → orchestrator → filter (heuristic dispatch by (source,provider) → LLM) → matcher → justify
            → EvidenceDiscoveryResponse (unchanged shape)
```

## File Changes

| File | Action | Description |
|---|---|---|
| `app/models/integracion.py` | Create | `Integracion` + `IntegrationProvider` StrEnum; unique `(usuario_id, provider, email)`, `email` NOT NULL default `''` |
| `app/models/google_token.py` | Keep | Untouched; read-only after cutover; dropped in a later migration |
| `app/schemas/integracion.py` | Create | `IntegrationStatus` (provider discriminator + per-source enabled flags); Microsoft OAuth request/callback schemas; `Provider` path enum |
| `app/schemas/google_workspace.py` | Modify | `GoogleIntegrationStatus` retained or aliased; `CalendarEventItem`/test schemas map from `CalendarEvent` |
| `app/adapters/calendar/port.py` | Modify | Add `CalendarEvent`/`CalendarAttendee` dataclasses; `search_events`/`get_event` return them; add `q` param to match adapter |
| `app/adapters/calendar/calendar_adapter.py` | Modify | Map Google JSON → `CalendarEvent`; read creds from `integraciones` (provider=google) |
| `app/adapters/drive/port.py` | Modify | Add `DriveQuery`; `search_files(usuario_id, query: DriveQuery)` |
| `app/adapters/drive/drive_adapter.py` | Modify | Translate `DriveQuery` → Google query string; creds from `integraciones` |
| `app/adapters/email/gmail_adapter.py` | Modify | `_load_credentials` reads `integraciones` (provider=google) |
| `app/adapters/microsoft/graph_adapter.py` | Create | `MicrosoftGraphAdapter` implementing `EmailPort`/`DrivePort`/`CalendarPort` via Graph; `run_in_executor`/async HTTP; token refresh per `(usuario_id, microsoft)` |
| `app/services/integration_service.py` | Create | Shared `_fernet`, state helpers (provider-aware), `store_credentials`, `get_integration_status`, `list_integration_statuses`, `revoke_integration`, `has_any_connected_provider` |
| `app/services/google_workspace_service.py` | Modify | Delegate token/state/status to `integration_service`; keep Google Flow + Gmail/Drive ops |
| `app/services/microsoft_graph_service.py` | Create | Azure AD auth-code+PKCE URL/exchange; Graph Mail/Calendar/Drive call surface |
| `app/services/evidence_discovery_service.py` | Modify | Provider-agnostic gate; run fetch across connected providers; merge `evidence_raw` |
| `app/agent/nodes/calendar_fetch.py` | Modify | Consume `CalendarEvent`; provider-aware adapter selection |
| `app/agent/nodes/drive_fetch.py` | Modify | Build `DriveQuery` (list) instead of query strings; provider-aware |
| `app/agent/prompts/evidence_filter.py` | Modify | Add `score_non_personal_ms_email`, `is_noise_ms_calendar`, `is_noise_ms_drive` |
| `app/agent/nodes/evidence_filter.py` | Modify | `_heuristic_is_noise` dispatch by `(source, provider)` |
| `app/api/v1/integraciones.py` | Modify | `{provider}` routes with validated `Provider` enum; `test_calendar`/`test_drive` consume dataclasses |
| `app/core/config.py` | Modify | Azure AD client id/secret/redirect/scopes settings |
| `app/core/exceptions.py` | Modify | `NO_PROVIDER_CONNECTED` code |
| `alembic/versions/0XX_*.py` | Create | Create `integraciones` + backfill from `google_tokens`; Neon-verified; no drop |

## Interfaces / Contracts

```python
class IntegrationProvider(StrEnum): GOOGLE = "google"; MICROSOFT = "microsoft"

class IntegrationStatus(BaseModel):        # replaces hardcoded GoogleIntegrationStatus shape
    provider: IntegrationProvider
    connected: bool
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    # per-source enabled flags, provider-neutral names
    mail_enabled: bool = False             # gmail / outlook mail
    drive_enabled: bool = False            # drive / onedrive
    calendar_enabled: bool = False         # google calendar / outlook calendar

# integration_service (shared, provider-agnostic)
def encode_oauth_state(usuario_id: UUID, code_verifier: str, provider: IntegrationProvider) -> str: ...
def verify_oauth_state(state: str) -> tuple[UUID, str, IntegrationProvider]: ...
async def store_credentials(db, usuario_id, provider, *, access_token, refresh_token,
                            scopes, expires_in=3600, email=None) -> Integracion: ...
async def get_integration_status(db, usuario_id, provider) -> IntegrationStatus: ...
async def list_integration_statuses(db, usuario_id) -> list[IntegrationStatus]: ...
async def has_any_connected_provider(db, usuario_id) -> bool: ...

# CalendarPort (generalized) — see D3 for CalendarEvent/CalendarAttendee
async def search_events(self, usuario_id, time_min, time_max, calendar_id="primary",
                        max_results=50, q: str | None = None) -> list[CalendarEvent]: ...
async def get_event(self, usuario_id, event_id, calendar_id="primary") -> CalendarEvent: ...

# DrivePort (generalized) — see D4 for DriveQuery
async def search_files(self, usuario_id, query: DriveQuery) -> list[DriveFile]: ...
```

## Testing Strategy (strict TDD, aiosqlite; Graph mocked)

| Layer | What | Approach |
|---|---|---|
| Unit | Google adapter maps JSON → `CalendarEvent`/`DriveFile`; behavior identical to before | existing Google fixtures, assert dataclass fields |
| Unit | `DriveQuery` → Google query string translation (keywords OR-fan-out, date clause, folder exclusion) | pure function test |
| Unit | `MicrosoftGraphAdapter` maps Graph JSON → `CalendarEvent`/`DriveFile`/email dict; `DriveQuery` → `$search`/`$filter` | mocked Graph HTTP |
| Unit | `store_credentials` upserts `integraciones` per `(user, provider, email)`; empty-email collapses to one row | aiosqlite |
| Unit | `has_any_connected_provider`; gate raises `NO_PROVIDER_CONNECTED` only when zero providers | aiosqlite |
| Unit | `_heuristic_is_noise` dispatch: Google items unchanged; Microsoft items hit MS heuristics | aiosqlite/pure |
| Integration | `{provider}` connect/callback/status/revoke for google (regression) and microsoft; state carries provider | `httpx.AsyncClient` |
| Integration | Discovery merges two providers into one `evidence_raw` | mocked adapters |
| Migration | create `integraciones` + backfill from `google_tokens` (provider=google, email COALESCE) | **Real Postgres (Neon)** |

The **regression guard**: the full existing Google adapter + discovery + OAuth suites must pass
unchanged (behavior-preserving) before any Microsoft code merges — this is the acceptance gate
for the port-generalization slice.

## Migration / Rollout

One additive migration: `create_table("integraciones")` + unique + index; backfill copy from
`google_tokens` (provider=`google`, `COALESCE(email,'')`); no drop. `google_tokens` stays as a
read-only rollback net. Rollback = repoint reads at `google_tokens` + drop `integraciones`
— lossless **only if executed before any post-cutover write**; once code reads/writes
`integraciones` in prod, dropping it destroys any Google row written since cutover and every
Microsoft row unconditionally (no fallback table exists for Microsoft). Microsoft
routes/adapter are new surface — revert independently without touching
Google discovery. Azure AD app is registered multi-tenant with delegated scopes
(`Mail.Read`, `Calendars.Read`, `Files.Read`, `offline_access`, `User.Read`), configured via
Settings/.env (no hardcoded secrets). Graph 429s respect `Retry-After` with bounded page sizes.

## PR Boundary (for sdd-tasks — stacked-to-main chained PRs)

The proposal's 3-slice cut is **confirmed with one refinement**: split Slice A into two
independent PRs, because the credential-store cutover and the port generalization touch
disjoint files and have no ordering dependency on each other (either can merge first). Route
generalization stays in Slice B (it is coupled to `IntegrationStatus` + Microsoft, not to the
data layer), so Slice A leaves the API surface stable.

- **Slice A1 — credential store cutover (data/service, API-stable).** `Integracion` model +
  migration + backfill; `integration_service` shared helpers; repoint `google_workspace_service`
  + the three Google adapters' credential loaders to `integraciones (provider=google)`. Existing
  `/google/*` routes and behavior unchanged. Guard: existing Google OAuth/discovery tests green.
- **Slice A2 — port generalization (adapter shape, API-stable).** `CalendarEvent`/`DriveQuery`
  dataclasses; `GoogleCalendarAdapter`/`DriveAdapter` mapping; `calendar_fetch`/`drive_fetch`
  node updates; `test_calendar`/`test_drive` route readers. Behavior-preserving; guarded by
  existing Google tests. **This is the only Google-code-touching slice — merge it only with the
  full Google suite green.** (A1 and A2 are independent; either order works.)
- **Slice B — Microsoft OAuth + generalized routes + status schema.** `IntegrationStatus`
  schema; `{provider}` validated routes (Google now `provider=google`, no redirect-URI change);
  `microsoft_graph_service` OAuth (PKCE + provider-aware state); Azure AD settings;
  connect/callback/status/revoke for Microsoft. No discovery/adapter yet.
- **Slice C — MicrosoftGraphAdapter + provider-agnostic gate + MS noise heuristics.** The Graph
  adapter for `EmailPort`/`DrivePort`/`CalendarPort`; generalize the discovery gate + multi-
  provider fetch/merge; Microsoft noise heuristics dispatched by `(source, provider)`.

Dependency order: A1 and A2 → B → C. (C consumes A2's ports, A1's credential store, and B's
`IntegrationStatus` + `list_integration_statuses`.)

## Open Questions (for sdd-spec)

- [ ] Whether to keep `GOOGLE_NOT_CONNECTED` as a still-raised alias alongside
      `NO_PROVIDER_CONNECTED` for backward-compatible callers/tests, or migrate them.
- [ ] Exact Azure AD delegated scope list + whether admin-consent is required for target tenants.
- [ ] `IntegrationStatus` per-source flag derivation for Microsoft (scope → flag mapping).
- [ ] Whether `test_calendar`/`test_drive` probe routes stay Google-only or also go `{provider}`.
```
