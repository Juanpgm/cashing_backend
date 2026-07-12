# Proposal — backend-local-first-sync

Status: proposed. Scope: `cashing-backend` only. Backend-only slice that makes a
local-first / offline mobile client possible. The frontend Dexie/IndexedDB client
is a SEPARATE follow-up change and is explicitly OUT of scope here.

Depends on: `openspec/changes/backend-local-first-sync/explore.md` (engram topic
`sdd/backend-local-first-sync/explore`). Encodes ADR-12..15 from the team vault.

---

## 1. Intent — problem and why now

The CashIn backend today is online-only: every read and write assumes a live
connection to Railway/Neon. Contractors preparing a **cuenta de cobro** frequently
work in the field (site visits, low-connectivity municipalities) and re-upload the
same **cédula**, **RUT**, or **contrato** documents over and over because nothing is
resident on-device and documents are siloed per contrato. When a **radicación** is
rejected, the reason is not captured in a structured way, so we cannot measure or
reduce rejections.

This change lays the backend foundation to hit four Norte objectives from the vault:

- **#2 Menos rechazos** — capture a structured `motivo_rechazo` on `CuentaCobro` so
  the rejection rate becomes measurable and, later, preventable.
- **#3 Offline** — a delta-sync protocol (`change_log` + `/sync/pull` + `/sync/push`)
  so a client can hold an active working set locally and reconcile when back online.
- **#4 No re-subir** — three-level document taxonomy (usuario / contrato / cuenta)
  plus `sha256` dedup so a **cédula**/**RUT**/HV uploaded once auto-fulfills every
  future contrato, and identical files are recognized instead of re-processed.
- **#5 Privacidad** — files stay metadata-only on device and upload direct-to-R2 via
  presigned PUT, with a server-side reap policy for uploads that never confirm.

Success looks like: a client can pull its active working set with a single cursor,
push offline edits and have them applied with a deterministic conflict policy,
upload a document without routing bytes through the API, and every one of these
events is observable for KPIs — with **one** real Alembic migration and no regression
to the existing extraction/OCR/obligación pipeline.

## 2. Scope

### In scope (cashing-backend)

1. **Delta-sync engine**
   - NEW append-only `change_log` table: `id (uuid)`, `usuario_id`, `entity_type`,
     `entity_id`, `op (insert/update/delete)`, `payload (jsonb)`, `seq (bigint
     monotonic)`, `created_at`. Populated from the **service layer** (single clean
     interception point per `model → schema → service → api → test`).
   - NEW `app/services/sync_service.py` + `app/api/v1/sync.py` exposing
     `GET /sync/pull?since=<cursor>` and `POST /sync/push`.
   - Cursor = monotonic `seq` **with a commit-visibility guard** (safety-lag window
     or snapshot discipline). NOT `xmin`, NOT `updated_at`, NOT `AuditLog`
     (`AuditLog` is confirmed dead code — never inserts).
   - Offline working set = **ACTIVE only** (see §3.1).

2. **Tombstones** — add `SoftDeleteMixin` to the 6 entities still hard-deleting:
   `Actividad`, `Obligacion`, `DocumentoFuente`, `Evidencia`,
   `DocumentoCuentaCobro`, `RequisitoCuenta`. Convert
   `document_service.eliminar_documento`'s literal `db.delete` to a soft delete so
   deletions can propagate as `op=delete` change-log rows instead of vanishing.

3. **Presigned direct-to-R2 upload**
   - Add `presigned_upload_url()` (presigned PUT) to `StoragePort` and `S3Adapter`,
     mirroring the existing GET-only `presigned_url`.
   - **Confirm-then-fetch-and-process** handshake (ADR-13):
     `POST /documentos/presigned-upload` → `{presigned_upload_url,
     documento_fuente_id}` with the row in `estado=pendiente_confirmacion` →
     client PUTs bytes to R2 → `POST /documentos/{id}/confirmar` with `sha256` +
     `size` + `content_type` → backend downloads the object back and runs the SAME
     `document_service.upload_document` pipeline (ownership, dedup, text extraction,
     OCR, vision/contract extraction, obligación extraction). The pipeline is
     REUSED, never skipped. `sha256` makes confirm idempotent.
   - Add nullable columns `sha256`, `size (BigInteger)`, `content_type` to
     `DocumentoFuente`.
   - **Server-side reap policy** for `DocumentoFuente` rows stuck in
     `pendiente_confirmacion` that never confirm (TTL-based expiry/cleanup), mirroring
     the client-side outbox TTL (§3.4).

4. **Three-level document taxonomy (ADR-14)**
   - Add `listar_documentos_usuario` query/endpoint
     (`usuario_id == user_id AND contrato_id IS NULL AND cuenta_cobro_id IS NULL`).
   - Split a third **usuario** tier (CEDULA, RUT, HV) out of the `_NIVEL_CONTRATO`
     frozenset (`checklist_service.py:63`) and include it in the auto-fulfillment
     pool (`checklist_service.py:985-996`) so usuario-level docs auto-satisfy the
     requisitos of NEW contratos.

5. **Rejection reason (KPI #2)**
   - Add `CuentaCobro.motivo_rechazo` as a `MotivoRechazo` StrEnum plus optional
     `motivo_rechazo_nota` free-text (see §3.3).

6. **One real Alembic migration `024_*`** — every schema change above via explicit
   `op.add_column` / `op.create_table`. Tested against real Postgres (Neon), not
   only aiosqlite, because `create_all` no-ops column additions on existing tables.

7. **ADR-15 SECOP — explicit in-scope VERIFICATION, not new work.** `secop_service`
   is already cache-first (24h/2h TTL), non-authoritative, non-blocking; the
   checklist scan reads only cached `SecopDocumento` with zero live HTTP. This change
   states that as a verified invariant and makes NO SECOP backend change.

8. **KPI instrumentation (no separate pipeline)** — `change_log` doubles as the
   product-event stream (see §4).

### Out of scope

- Frontend Dexie/IndexedDB client, offline UI, client-side encrypted outbox
  implementation — **separate follow-up change** (`cashing-frontend`).
- Conflict-resolution UI. Backend only surfaces a "changed while offline" signal.
- Any SECOP backend change (already compliant).
- A background job runner (Celery/RQ/APScheduler) — the confirm handshake is
  synchronous by design precisely to avoid introducing one now.
- Real-time push (websockets/SSE) for sync — pull-based only.
- Exposing SECOP `confianza`/DETECTADO counts as a KPI (future).
- Migrating older history onto the device — pull-on-demand only (§3.1).

## 3. Locked product decisions

### 3.1 Offline working set = ACTIVE only
The device holds only the **active** working set: vigent `Contrato`s plus recent
`CuentaCobro`s. Proposed recency window: **last 6 months**, exposed as a tunable
setting (e.g. `SYNC_ACTIVE_WINDOW_MONTHS`, default 6). Older history is
pull-on-demand, never resident. This keeps device footprint and — critically — the
conflict surface small. `sync_service` filters the working set by this window plus
per-user ownership.

### 3.2 Conflict policy
- **Money fields = server-wins + notify.** If a protected money field changed on the
  server while the client was offline, the client's offline edit is **discarded** and
  the client is notified ("changed while offline"). No merge UI. Protected fields:
  `CuentaCobro.valor`, `Contrato.valor_total`, `Contrato.valor_adicion`,
  `Contrato.valor_mensual` (all `Numeric(15,2)`). Same treatment for **actividades**
  that back a monto.
- **Checklist state = last-writer-wins** — safe, because document links are additive
  and the primary slot is never silently overwritten.
- Conflict DETECTION is **greenfield**: `POST /sync/push` compares a client-supplied
  `base_seq` (or `base_updated_at`) against current server state per entity. No
  version/ETag column exists today, so this is new surface, designed in `sdd-design`.

### 3.3 `motivo_rechazo` = structured enum + optional nota
Define a `MotivoRechazo` StrEnum catalog, e.g. `FALTA_RPC`, `SEG_SOCIAL_VENCIDA`,
`INFORME_INCOMPLETO`, `VALOR_INCORRECTO`, `OTRO` (final catalog finalized in
`sdd-spec`). Plus optional `motivo_rechazo_nota` free-text. The enum feeds KPI #2
(rejection rate by cause) directly; the nota is for the human context, not analytics.

### 3.4 Encrypted upload outbox — backend implication
The client-side encrypted outbox (purge-on-logout + ~7-day TTL) is frontend work and
OUT of scope. Its BACKEND implication IS in scope: `DocumentoFuente` rows created in
`estado=pendiente_confirmacion` that never receive a `confirmar` call must be reaped
by a server-side expiry policy (TTL, aligned with the client ~7-day window) so
orphaned presigned uploads do not accumulate.

## 4. KPI instrumentation — change_log as the event source

No separate analytics pipeline. The append-only `change_log` is the product-event
stream, queryable per `usuario_id` and `entity_type`/`op`/`seq`:

- **Time-to-complete a cuenta de cobro** — first insert → radicación transition,
  derived from ordered `change_log` rows.
- **Rejection rate by cause** — `CuentaCobro.motivo_rechazo` enum aggregated from
  update events.
- **Dedup hits** — `sha256` collisions detected during confirm/upload.
- **Sync conflict counts** — server-wins discards recorded during `/sync/push`.

This is a design note; building KPI dashboards/queries is not part of this slice —
only the instrumentation substrate (the columns and the change_log) is.

## 5. High-level approach

Per `model → schema → service → api → test`:

1. **model** — NEW `app/models/change_log.py` (append-only, monotonic `seq`);
   `SoftDeleteMixin` on the 6 entities; `sha256`/`size`/`content_type` on
   `DocumentoFuente`; `motivo_rechazo`/`motivo_rechazo_nota` on `CuentaCobro`.
2. **schema** — `MotivoRechazo` StrEnum; sync pull/push request+response schemas;
   presigned-upload + confirmar schemas; usuario-document listing schema.
3. **service** — `sync_service` (working-set pull windowed to active; push with
   greenfield conflict detection + server-wins money guard); change-log writes
   inserted at the service interception points for syncable entities;
   `document_service` presigned-upload + confirm (fetch-back → existing pipeline) +
   `eliminar_documento` hard→soft; `checklist_service` three-tier split +
   `listar_documentos_usuario`; reap policy for stale `pendiente_confirmacion`.
4. **api** — `app/api/v1/sync.py` (`/sync/pull`, `/sync/push`); document endpoints
   `/documentos/presigned-upload`, `/documentos/{id}/confirmar`,
   `/documentos/usuario`.
5. **adapter** — `presigned_upload_url()` on `StoragePort` + `S3Adapter`.
6. **migration** — one real `024_*` with explicit ALTER/CREATE, verified on Neon.
7. **test** — service unit tests (aiosqlite) + API integration tests
   (`httpx.AsyncClient`, `moto[s3]`); `024_*` exercised against real Postgres.

Detailed data model, cursor guard mechanism, and conflict algorithm are deferred to
`sdd-design`/`sdd-spec` — not decided here.

## 6. Risks and mitigations

1. **Migration discipline.** `create_all` silently no-ops column additions on any env
   that already has the table → passes on a stale local DB, breaks on Neon/Railway.
   *Mitigation:* one explicit numbered migration `024_*` with `op.add_column`/
   `op.create_table`; run and verify against real Postgres before merge, not just
   aiosqlite.
2. **Cursor commit-ordering.** A BIGSERIAL `seq` can become visible out of commit
   order under concurrency, so a naive `seq > :since` can skip rows.
   *Mitigation:* commit-visibility guard — safety-lag window or snapshot discipline —
   specified in `sdd-design`.
3. **Greenfield conflict detection.** No version/ETag column exists; `/sync/push`
   must build comparison from `base_seq`/`base_updated_at`. *Mitigation:* narrow the
   surface — server-wins on the four money fields only, last-writer-wins elsewhere,
   active-only working set to shrink the conflict window.
4. **Presigned double-egress.** Confirm-then-fetch downloads the object back to run
   the pipeline → double R2 round-trip. *Mitigation:* bounded by the existing 10MB
   file cap; `sha256` makes confirm idempotent so retries don't re-process; flag as a
   known cost line item (acceptable vs. building a job runner).
5. **ADR-14 depth.** The taxonomy gap is real feature work (auto-fulfillment pool),
   not a cosmetic query. *Mitigation:* keep it minimal — one query + one frozenset
   split + pool inclusion; no reshaping of the checklist engine.
6. **Orphaned pendiente_confirmacion rows.** Presigned rows that never confirm would
   accumulate. *Mitigation:* server-side TTL reap policy in scope (§3.4).

## 7. Rough work breakdown and size

| Area | Rough surface |
|---|---|
| `change_log` model + migration piece | small |
| `SoftDeleteMixin` on 6 entities + `eliminar_documento` soft-delete | small–medium |
| `sync_service` pull (active window) + push (conflict + server-wins) | **large** |
| `app/api/v1/sync.py` | small–medium |
| `presigned_upload_url()` adapter + port | small |
| presigned-upload + confirmar endpoints/service (fetch-back → pipeline) | medium |
| `DocumentoFuente` sha256/size/content_type | small |
| three-tier taxonomy split + `listar_documentos_usuario` | medium |
| `CuentaCobro.motivo_rechazo` + `MotivoRechazo` enum | small |
| reap policy for stale pendiente_confirmacion | small |
| one Alembic `024_*` (all changes) | medium |
| tests across all of the above | medium–large |

**Size estimate: this will almost certainly exceed a ~400-line PR.** The sync engine
alone (pull windowing + greenfield conflict detection + change-log write
interception) is a large, cohesive unit. Delivery strategy is `ask-on-risk`, so this
is a flag: recommend **chained/stacked PRs**, e.g.

- Slice A: `change_log` + `SoftDeleteMixin` tombstones + migration `024_*`.
- Slice B: `sync_service` + `/sync` endpoints (conflict policy).
- Slice C: presigned upload + confirm handshake + `DocumentoFuente` columns + reap.
- Slice D: three-tier taxonomy + `motivo_rechazo` + KPI substrate.

The precise slicing and PR boundaries are decided in `sdd-tasks`, not here.

## 8. Language contract

Prose is English. Spanish domain nouns are preserved verbatim: cuenta de cobro,
contrato, obligación, requisito, radicación, SECOP, RPC, CDP, cédula, RUT, motivo de
rechazo. Identifiers and enum values stay as-is.
