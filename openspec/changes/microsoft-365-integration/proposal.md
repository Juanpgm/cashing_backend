# Proposal — microsoft-365-integration

Status: proposed. Scope: `cashing-backend` only. Adds a second evidence provider
(Microsoft 365 via Graph API: Outlook Mail + Outlook Calendar + OneDrive) behind the
existing Ports & Adapters seams, and performs the provider-generalization the codebase
needs before ANY second adapter can exist. No exploration artifact — decisions were
supplied directly.

Depends on nothing upstream. Motivated by roadmap Phase 8 (Additional Integrations),
today marked Pending in `CLAUDE.md`.

---

## 1. Intent — problem and why now

Evidence discovery is hardwired to Google. `descubrir_evidencias`
(`app/services/evidence_discovery_service.py:268-390`) gates every non-`local_only`
run on `google_workspace_service.get_integration_status` and raises
`GOOGLE_NOT_CONNECTED`. Contractors who live in Outlook/OneDrive (a large share of
Colombian public-sector and enterprise contractors) simply cannot produce automated
evidence — their proof of obligation fulfillment sits in Microsoft 365, invisible to
the pipeline.

Adding Microsoft is blocked by three structural facts, not by the adapter itself:

- **Credentials are single-provider, single-account.** `google_tokens`
  (`app/models/google_token.py:14-27`) has a UNIQUE constraint on `usuario_id` — one
  row per user. A user cannot hold Google AND Microsoft, nor two accounts of one
  provider. `GoogleIntegrationStatus` (`app/schemas/google_workspace.py:27-35`) has no
  `provider` discriminator.
- **`CalendarPort` leaks Google JSON.** `search_events`/`get_event`
  (`app/adapters/calendar/port.py:9-30`) return `list[dict[str, Any]]` = raw Google
  Calendar resources; `calendar_fetch.py` reads `ev["start"]["dateTime"]`,
  `ev["attendees"]`, `ev["eventType"]` directly. Graph returns a different shape.
- **`DrivePort.search_files(query)` leaks Google query syntax.**
  (`app/adapters/drive/port.py:24-105`) `drive_fetch.py` builds raw
  `name contains '...' and modifiedTime >= '...'` strings. Graph/OneDrive speaks a
  different query language entirely.

Why now: this is greenfield (grep confirms zero `outlook`/`microsoft`/`onedrive`
references), and the port debt only compounds. We fix the seams and land the first
second-provider in one coherent slice, so provider #3 (IMAP, Dropbox…) is a pure
adapter add.

Success looks like: a user connects a Microsoft account (independently of, or
alongside, an existing Google connection); `descubrir_evidencias` runs against
whichever provider(s) the user has connected, feeding the SAME normalized
`evidence_raw` shape into the unchanged LLM classification layer; and existing
connected Google users keep working with zero re-consent, behind one real Alembic
migration.

## 2. Scope

### In scope (cashing-backend)

1. **Generalized credential store (multi-provider, multi-account).** Replace the
   single-row `google_tokens` table with a generalized `integraciones` table carrying
   a `provider` discriminator (`google` | `microsoft`), per-provider
   `access_token_encrypted`/`refresh_token_encrypted` (Fernet), `scopes`,
   `expires_at`, `email`. Uniqueness moves to `(usuario_id, provider, email)` so a user
   holds a Google account and a Microsoft account (and future providers)
   simultaneously. Migrate + backfill existing `google_tokens` rows.

2. **Port generalization (prerequisite, in this slice).**
   - `CalendarPort` → return a neutral `CalendarEvent` dataclass (mirroring the
     existing `DriveFile` dataclass pattern), not raw provider JSON. Update
     `calendar_fetch.py` to consume the dataclass. `GoogleCalendarAdapter` maps Google
     JSON → `CalendarEvent`.
   - `DrivePort.search_files(query: str)` → structured provider-neutral query object
     (`keywords`, `date_from`/`date_to`, `mime`/type filter, obligation-linked search
     term). Each adapter translates it to its native query language. Update
     `drive_fetch.py` to build the query object instead of a Google string.

3. **`MicrosoftGraphAdapter`(s)** implementing the existing (now-generalized) ports:
   - `EmailPort` — Outlook Mail via Graph `/me/messages`.
   - `DrivePort` — OneDrive via Graph `/me/drive` (translate the query object to Graph
     `$search`/`$filter`).
   - `CalendarPort` — Outlook Calendar via Graph `/me/events` → `CalendarEvent`.
   - Tokens loaded per `(usuario_id, provider=microsoft)`, auto-refresh on expiry, all
     Graph calls wrapped in `run_in_executor` (or async HTTP) per the anti-pattern rule.

4. **Microsoft OAuth2 flow.** Graph authorization-code + PKCE, reusing the existing
   signed-JWT `state` pattern (`_encode_oauth_state`, carrying `usuario_id` +
   `code_verifier`, no server session store). New provider-scoped routes under
   `app/api/v1/integraciones.py` (connect/callback/status/revoke/test), OR generalized
   `{provider}` routes — decided in `sdd-design`.

5. **Provider-agnostic connection gate.** Generalize the `GOOGLE_NOT_CONNECTED` gate in
   `descubrir_evidencias` to "at least one provider connected", and run discovery
   across every connected provider, merging results into the single `evidence_raw`
   list. `local_only=True` continues to bypass the gate entirely.

6. **Microsoft noise heuristics (pre-LLM layer).** Add Microsoft-source counterparts
   feeding the same normalized shape:
   - Email: a Graph-equivalent of `score_non_personal_email`
     (`evidence_filter.py:241-327`) reading Outlook categories/`inferenceClassification`
     instead of Gmail `CATEGORY_PROMOTIONS` labels.
   - Calendar: a Graph-equivalent of `is_noise_calendar` (506-522) reading Graph
     `attendees`/`responseStatus`/`isAllDay`.
   - Drive: a Graph-equivalent of `is_noise_drive` (525-527) — folder/system-item
     filter for OneDrive items. Dispatch heuristics by `source`/`provider`; the
     source-agnostic `WORK_NOISE_SYSTEM_PROMPT` LLM layer is unchanged.

7. **`IntegrationStatus` schema** with a `provider` discriminator (and per-source
   enabled flags), replacing the hardcoded `GoogleIntegrationStatus` shape.

8. **One real Alembic migration** (create `integraciones`, backfill from
   `google_tokens`, keep/deprecate old table per rollback plan), verified on real
   Postgres, not only aiosqlite.

### Out of scope

- **Semantic-search evidence layer** (content extraction + embeddings matching evidence
  against obligación text). This is the north-star `why` motivating the structured
  query object, but building it is future work — see §3.4.
- IMAP generic email, Dropbox/Box, SFTP — separate follow-up integrations. Noted in
  §3.5 for context only, not built here.
- WhatsApp / social-media evidence sources — permanently out (legal/evidentiary risk).
- Frontend integration UI for Microsoft connect — separate `cashing-frontend` change.
- MCP server tools for Outlook/OneDrive/Outlook-Calendar — follow-up (adapters land
  first; MCP proxies through the backend later).
- New background job runner — Graph discovery stays in the existing synchronous
  orchestration path.

## 3. Locked product decisions

### 3.1 Full Microsoft 365 parity, one consent
Outlook Mail + Outlook Calendar + OneDrive under a single OAuth2 consent, mirroring the
existing Gmail + Calendar + Drive parity. Not a mail-only MVP.

### 3.2 Multi-provider redesign happens now
The generalized `integraciones` table lands in THIS slice. A parallel
`microsoft_tokens` table was explicitly rejected. A user may hold Google and Microsoft
connections at once; the schema leaves room for future providers.

### 3.3 Port generalization is in-scope, not deferred
`CalendarEvent` dataclass and the `DrivePort` query object are prerequisites, not
follow-ups — a second adapter cannot be written cleanly without them, and they are the
only reason to touch the Google adapter/nodes in this slice.

### 3.4 Query object is the semantic-search seam (future scope)
The structured query object is designed so a future semantic layer can sit ABOVE
provider search (provider keyword search misses evidence when contractors name files
inconsistently). This proposal only ships the query object + provider translation; the
embeddings/content-matching layer is explicitly Not Now.

### 3.5 Future integrations (context only)
IMAP generic email, Dropbox/Box, SFTP are the recommended next providers once the
generalized seam exists. Listed for direction; not scoped, not designed here.

## 4. High-level approach

Per `model → schema → service → api → test`:

1. **model** — NEW `app/models/integracion.py` (`provider` discriminator, encrypted
   tokens, `scopes`, `expires_at`, `email`, uniqueness `(usuario_id, provider,
   email)`). Deprecate `google_token.py` after backfill.
2. **schema** — `IntegrationStatus` with `provider`; Microsoft OAuth request/callback
   schemas; the `DrivePort` query object; the `CalendarEvent` dataclass.
3. **service** — generalize `google_workspace_service` into a provider-agnostic
   integration service (or a thin `microsoft_graph_service` sibling reusing the shared
   token-encryption/JWT-state helpers); provider-agnostic connection gate in
   `evidence_discovery_service`; Microsoft noise heuristics dispatched by source.
4. **adapter** — `MicrosoftGraphAdapter` for `EmailPort`/`DrivePort`/`CalendarPort`;
   update `GoogleCalendarAdapter` + `DriveAdapter` to the generalized signatures.
5. **api** — Microsoft (or generalized `{provider}`) connect/callback/status/revoke/test
   routes in `integraciones.py`.
6. **migration** — one real migration: create `integraciones`, backfill `google_tokens`
   (provider=`google`), cut reads over, then drop/retire old table.
7. **test** — adapter unit tests (mocked Graph), service tests (aiosqlite), OAuth-flow
   + status API integration tests (`httpx.AsyncClient`), migration verified on real
   Postgres.

Detailed table columns, route shape (per-provider vs. generalized `{provider}`), query-object
fields, and the backfill cutover order are deferred to `sdd-design`/`sdd-spec`.

## 5. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Token-table migration breaks existing connected Google users | Med | Backfill `google_tokens` → `integraciones` (provider=`google`) in the same migration; keep old table read-only until cutover verified; no re-consent required. Verify on real Postgres. |
| Graph API throttling (429 / retry-after) | Med | Respect `Retry-After`, bounded page sizes, honor existing per-run evidence caps; keep discovery synchronous but resilient. `ponytail:` naive backoff, upgrade to token-bucket if throughput matters. |
| Azure AD app registration + admin-consent complexity | Med | Register a multi-tenant Azure AD app; document required delegated Graph scopes (Mail.Read, Calendars.Read, Files.Read); surface consent errors as domain exceptions. Config via Settings/.env, no hardcoded secrets. |
| Port generalization touches working Google paths (regression) | Med | `CalendarEvent`/query-object changes are mechanical adapter+node updates; existing Google tests must pass unchanged behavior before Microsoft adapter merges. |
| Microsoft noise heuristics under-tuned vs. mature Google ones | Low | Heuristics are pre-filters over the same LLM layer; conservative thresholds, leave a tuning knob. Wrong pre-filter only over-includes into the LLM, never silently drops to a hard error. |
| Two providers connected → duplicate/overlapping evidence | Low | Merge into one `evidence_raw`; existing dedup/filter stages operate on the normalized shape. |

## 6. Rollback plan

- Migration is create + backfill (additive); keep `google_tokens` intact (read-only)
  until the `integraciones` cutover is verified in prod. Rollback = point reads back at
  `google_tokens` and drop `integraciones`; no data loss because backfill is a copy.
- Microsoft adapter/routes are new surface — disable via config/feature flag or revert
  the routes without affecting Google discovery.
- Port generalization is the only Google-touching change; it is behavior-preserving and
  covered by existing tests, so revert is a straight git revert of the adapter/node
  edits.

## 7. Rough work breakdown and size

| Area | Rough surface |
|---|---|
| `integraciones` model + migration + backfill | medium |
| `IntegrationStatus` schema + `provider` discriminator | small |
| `CalendarEvent` dataclass + `CalendarPort`/adapter/node update | small–medium |
| `DrivePort` query object + adapter/node update | medium |
| `MicrosoftGraphAdapter` (Email/Drive/Calendar) | **large** |
| Microsoft OAuth2 flow (PKCE + JWT state) + routes | medium |
| Provider-agnostic gate in `descubrir_evidencias` | small |
| Microsoft noise heuristics (3 source types) | medium |
| tests across all of the above | medium–large |

**Size estimate: this will exceed a ~400-line PR.** Delivery strategy `ask-on-risk` →
recommend chained/stacked PRs, e.g. Slice A: port generalization (`CalendarEvent` +
query object) + `integraciones` table + migration/backfill. Slice B: Microsoft OAuth
flow + routes + `IntegrationStatus`. Slice C: `MicrosoftGraphAdapter`(s) +
provider-agnostic gate + Microsoft noise heuristics. Precise slicing decided in
`sdd-tasks`.

## 8. Language contract

Prose is English. Spanish domain nouns preserved verbatim: cuenta de cobro, contrato,
obligación, evidencia, requisito. Provider/API identifiers (Graph, OneDrive, Outlook,
Gmail, Drive) and existing code identifiers stay as-is.
