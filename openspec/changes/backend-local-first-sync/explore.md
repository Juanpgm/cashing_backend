# Exploration — backend-local-first-sync

Status: complete. Scope: `cashing-backend` only. Enables a local-first / offline mobile client (ADR-12..15 in the Obsidian vault).

## Current state

Stack: FastAPI + SQLAlchemy 2.0 async (asyncpg) + PostgreSQL 16. Flow `api/v1/` → service → DB, with ports injected via `app/api/deps.py`. Mixins (`app/models/base.py`): `UUIDMixin` (uuid4 generated in Python — good for offline client-created IDs), `TimestampMixin` (`created_at`/`updated_at`, `onupdate=func.now()`), `SoftDeleteMixin` (`deleted_at`). **No model has a version / optimistic-concurrency column.**

## Q1 — Change tracking (delta-sync source)

**`AuditLog` is dead code and NOT usable as the sync source.** `app/core/audit.py:66-86` (`log_audit_event`) only calls structlog — it never inserts into `audit_logs`. Zero `AuditLog(` write sites repo-wide. The model has no entity/op/payload/sequence.

**Recommendation: dedicated `change_log` table** — `id (uuid)`, `usuario_id`, `entity_type`, `entity_id`, `op (insert/update/delete)`, `payload (jsonb)`, `seq (bigint monotonic)`, `created_at`. Populated from the service layer (single clean interception point per the `model→schema→service→api→test` convention).

Cursor options:
- **A. app/DB-assigned monotonic `seq` (BIGSERIAL) — RECOMMENDED.** Simple `WHERE seq > :since ORDER BY seq ASC LIMIT :page`. Works on aiosqlite for tests. Caveat: concurrent-transaction commit ordering can expose a lower `seq` after a higher one → add a commit-visibility guard (short safety-lag window or REPEATABLE READ snapshot), not just `seq > :since`.
- B. Postgres `xmin` — rejected: 32-bit wraparound, not monotonic across VACUUM FREEZE, unusable across tables, breaks aiosqlite test convention.
- C. `updated_at` as cursor — rejected: timestamp ties miss/duplicate rows at the boundary; no tombstone representation.

## Q2 — Sync surface

New `app/api/v1/sync.py` (`GET /sync/pull`, `POST /sync/push`) → `app/services/sync_service.py`. Per-user scope via `get_current_user` (`deps.py:22-36`); every syncable query filters by `user.id`.

Syncable working set (owner path → `usuario_id`):
| Entity | SoftDelete today? |
|---|---|
| Contrato | ✅ |
| CuentaCobro | ✅ |
| Actividad | ❌ (tombstone gap) |
| Obligacion | ❌ |
| DocumentoFuente (metadata) | ❌ — `document_service.eliminar_documento` does a literal `db.delete` |
| Evidencia | ❌ |
| DocumentoCuentaCobro / DocumentoRequisitoVinculo / DocumentoChecklistCandidato | ❌ |
| RequisitoCuenta | ❌ |

NOT syncable: `SecopContrato/SecopProceso/SecopDocumento` (global server-side cache, never pushed). `RequisitoDocumento` catalog = global reference (pull-only if at all, never push).

## Q3 — Conflict resolution

No version column exists; only signal is `updated_at`. Money fields that must NEVER be silently overwritten:
- `CuentaCobro.valor` — `Numeric(15,2)`
- `Contrato.valor_total`, `valor_adicion`, `valor_mensual` — `Numeric(15,2)`

Policy: checklist = last-writer-wins (safe — links are additive, primary slot never overwritten). montos/actividades = server-wins + surface. **Conflict DETECTION is greenfield** — `sync/push` must compare a client `base_seq`/`base_updated_at` vs server state per entity; this is new surface, not a refinement.

## Q4 — Storage / presigned upload

`StoragePort` has `upload`, `download`, `presigned_url` (GET-only), `delete`. **No presigned PUT.** Adding `presigned_upload_url()` = mirror `presigned_url` with `generate_presigned_url("put_object", ...)`.

The hard part is the metadata handshake: `document_service.upload_document` (`document_service.py:605-975`) runs ownership checks, dedup, text extraction + OCR, vision/contract extraction (auto-creates Contrato), and obligación extraction BEFORE `storage.upload`. A naive direct-PUT skips steps 4-6.

Handshake options:
- **A. Confirm-then-fetch-and-process — RECOMMENDED.** `POST /documentos/presigned-upload` → `{presigned_upload_url, documento_fuente_id}` (row `estado=pendiente_confirmacion`) → client PUTs to R2 → `POST /documentos/{id}/confirmar` with `sha256`+`size`+`content_type` → backend downloads the object back and runs the SAME pipeline. Reuses 100% of existing logic; `sha256` gives idempotent confirm. Cost: double R2 round-trip (bounded — 10MB file cap).
- B. Metadata-only + background job — rejected for now: no job runner exists (no Celery/RQ/APScheduler); results become eventually-consistent needing new polling/ws surface.

`DocumentoFuente` needs new nullable columns: `sha256`, `size (BigInteger)`, `content_type`.

## Q5 — Migrations

Versions `001`..`023`, sequential 3-digit prefix, linear `down_revision` chain. Boot (`app/main.py:38-100`) is a deliberate two-step: `create_all` (creates missing tables, NEVER alters existing columns) then Alembic reconciliation (stamp if fresh, upgrade if versioned).

**Risk**: adding columns to EXISTING tables (`sha256`/`size`/`content_type` on documento_fuente, `motivo_rechazo` on cuenta_cobro) or new tables requires a real numbered migration `024_*` with explicit `op.add_column`/`op.create_table`. `create_all` silently no-ops column additions on any env that already has the table → passes local (stale create_all DB) but fails/misbehaves on Neon/Railway. Test `024_*` against real Postgres, not just SQLite.

## Q6 — Document taxonomy (ADR-14)

Model supports 3 FK states, but code queries only 2 tiers. No query filters `contrato_id IS NULL AND cuenta_cobro_id IS NULL` (usuario-level). Critically, `checklist_service.auto_vincular_documentos_fuente` pool (`checklist_service.py:985-996`) excludes usuario-level → a cédula uploaded once would NOT auto-satisfy a new contract's `CEDULA` requisito. The `_NIVEL_CONTRATO` frozenset (`checklist_service.py:63`) conflates contract- and usuario-level into one tier.

Minimal work: (1) `listar_documentos_usuario` service/endpoint (`usuario_id == user_id AND contrato_id IS NULL`); (2) split a third "usuario" tier (CEDULA, RUT, HV) out of `_NIVEL_CONTRATO` and include it in the auto-fulfillment pool for new contratos.

## Q7 — SECOP (ADR-15)

**Already matches ADR-15 — no backend change needed.** `secop_service` reads cache-first (24h/2h TTL), live Socrata only on `refresh=True`/stale. `checklist_service.detectar_desde_secop` scans only cached `SecopDocumento` (zero live HTTP in the scan path). Detection only sets `estado=DETECTADO` + `confianza_deteccion`, auto-links only when `PENDIENTE` and score ≥ 0.700, never overwrites user links, fully overridable. Non-authoritative, non-blocking. (Optional future: expose confianza/DETECTADO count as a KPI — out of scope.)

## Affected areas

- `app/models/documento_fuente.py` — add `sha256`, `size`, `content_type` + `SoftDeleteMixin`
- `app/models/cuenta_cobro.py` — add `motivo_rechazo`
- `app/models/actividad.py`, `obligacion.py`, `evidencia.py`, `documento_cuenta_cobro.py`, `requisito_cuenta.py` — add `SoftDeleteMixin`
- NEW `app/models/change_log.py` (append-only, monotonic `seq`)
- NEW `app/services/sync_service.py`, `app/api/v1/sync.py`
- `app/adapters/storage/port.py` + `s3_adapter.py` — add `presigned_upload_url()`
- `app/services/document_service.py:605-975` — presigned-confirm reuses this pipeline (Option A)
- `app/services/checklist_service.py:63,985-996` — three-tier taxonomy split
- NEW `alembic/versions/024_*.py` — real ALTER/CREATE

## Risks

1. Migration discipline — `create_all` no-ops column adds on existing tables across envs; write & test `024_*` on real Postgres.
2. Cursor ordering — BIGSERIAL cursors need a commit-visibility guard.
3. Conflict detection is greenfield (no version/ETag today).
4. Presigned-upload double-egress (bounded by 10MB cap) — flag as cost line item.
5. ADR-14 gap deeper than two-tier code models — real feature work.

## Recommendation

Proceed to `sdd-propose` with: (1) `change_log` + sync API, (2) `SoftDeleteMixin` on the 6 entities above, (3) `DocumentoFuente.sha256/size/content_type` + `presigned_upload_url()` with confirm-then-fetch handshake, (4) `CuentaCobro.motivo_rechazo`, (5) usuario-level query + three-tier checklist split, (6) ADR-15 explicit no-op in scope notes. All via one real Alembic migration `024_*`.
