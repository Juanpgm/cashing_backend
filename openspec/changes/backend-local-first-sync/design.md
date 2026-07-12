# Design: backend-local-first-sync

## Technical Approach

Backend foundation for a local-first client, built strictly along `model → schema →
service → api → test`. All change tracking, cursor assignment, conflict detection and
the presigned handshake live in the **service layer** — no session-event magic, no SQL
in routers, no new job runner. One real Alembic migration `024_*` carries every schema
change (verified on Neon, since `create_all` no-ops column additions on existing tables).

The design turns one insight into the backbone of the whole slice: because `/sync/pull`
is **always per-user**, per-user monotonic ordering is the ONLY ordering that matters.
That lets a per-user counter row assigned under `SELECT..FOR UPDATE` solve the
commit-visibility problem for free (no clock/lag tuning) and behave identically on
aiosqlite. Every other decision follows from it.

## Architecture Decisions

### D1 — Cursor: per-user counter row, not global BIGSERIAL

| Option | Tradeoff | Decision |
|---|---|---|
| Global `BIGSERIAL` seq + safety-lag window | Out-of-order commit visibility; lag tuning; SQLite autoincrement only on INTEGER PK (seq is not PK) | Rejected |
| `REPEATABLE READ` snapshot discipline | Correct but couples pull to isolation level; brittle on aiosqlite | Rejected |
| **Per-user counter row `sync_cursor(usuario_id PK, last_seq)` locked `FOR UPDATE`** | Assigns `seq` inside the write txn under a per-user row lock → concurrent writes for the SAME user serialize, so seq order == commit order within that user's stream | **Chosen** |

`seq` is a plain `BigInteger` on `change_log`, populated by the service from
`sync_cursor.last_seq + 1`. It is monotonic **per usuario_id**, not globally — which is
all pull needs. No Postgres sequence, no BIGSERIAL, so `024_*` adds only plain columns
and the aiosqlite test path is trivial (`FOR UPDATE` is a harmless no-op; tests are
single-threaded). Contention is negligible: a mobile user rarely has concurrent writes.

### D2 — Write interception: explicit service helper, not `after_flush`

| Option | Tradeoff | Decision |
|---|---|---|
| SQLAlchemy `after_flush` session event | Fires for non-syncable entities; owner `usuario_id` for `Actividad`/`Obligacion` is only reachable via joins; invisible magic vs. repo convention | Rejected |
| **`sync_service.record_change(db, usuario_id, entity_type, entity_id, op, payload)` called from each mutating service** | One explicit call per interception point; matches "all writes go through services"; payload built by a per-entity whitelist serializer | **Chosen** |

`record_change` (a) locks the caller's `sync_cursor` row `FOR UPDATE`, (b) increments
`last_seq`, (c) inserts a `change_log` row in the **same** session/transaction as the
entity write. Payloads are built by per-entity serializers (`_serialize_contrato`, …)
that **whitelist columns** — never dump the ORM row blindly, never include another
user's data; `usuario_id` is always the owner. `op=delete` payloads are minimal
(`{entity_id}`).

### D3 — Greenfield conflict detection via client `base_seq`

No version column exists. The client stores, per entity, the `seq` of the last
`change_log` row it saw (`base_seq`; `base_updated_at` is the fallback anchor). On push,
the server computes `server_latest_seq = max(change_log.seq WHERE entity_id=…)`:

- `server_latest_seq <= base_seq` → no concurrent server change → **apply**.
- Concurrent change AND a protected **money field** differs → **server-wins**: discard
  client edit, return a conflict record (`reason="money_conflict"`, `server_value`).
- Concurrent change on **checklist** state → **last-writer-wins**: apply client edit
  (links are additive; primary slot never silently overwritten).

Protected money fields: `CuentaCobro.valor`, `Contrato.valor_total`,
`Contrato.valor_adicion`, `Contrato.valor_mensual`, and any `Actividad` monto that backs
a value. Conflict counts (KPI #4) are emitted via `structlog` + surfaced in the response;
`change_log` stays entity-ops-only.

### D4 — Presigned: confirm-then-fetch reuses the pipeline via extracted core

`upload_document` (`document_service.py:605-975`) today does `storage.upload` **and** the
post-storage pipeline (dedup, text/OCR, vision/contract, obligación extraction, checklist
link) in one function. To reuse it WITHOUT a duplicate row, extract the post-storage body
into `_process_uploaded_document(db, doc, content, …)` operating on an EXISTING row. Then:

- Legacy path: `storage.upload` → create row → `_process_uploaded_document`.
- Confirm path: row already exists (`estado=pendiente_confirmacion`, bytes already in R2)
  → `storage.download(key)` → verify `sha256` → set `estado=confirmed` +
  `sha256/size/content_type` → `_process_uploaded_document`. `sha256` makes confirm
  idempotent (re-confirm with same hash returns the existing processed row, no reprocess).

### D5 — SoftDeleteMixin + tombstone propagation

Add `SoftDeleteMixin` to the 6 entities. `eliminar_documento` (`document_service.py:1086`)
changes `db.delete(doc)` → `doc.soft_delete()` + `record_change(op=delete)`; R2 bytes are
still deleted (privacy #5), the row survives only as a tombstone. All syncable read
queries must add `deleted_at IS NULL` (audit existing queries). Tombstones reach the client
only through the incremental `change_log` delta (`op=delete`); the initial snapshot excludes
deleted rows.

### D6 — Three-tier taxonomy

Split `_NIVEL_USUARIO = {"CEDULA","RUT","HV"}` out of `_NIVEL_CONTRATO`
(checklist_service.py:63 → keeps `CONTRATO,RPC,FICHA_TECNICA,ACTA_INICIO`; add new `HV`
code). Add `es_nivel_usuario()`. Usuario-level doc scope = `usuario_id set, contrato_id
NULL, cuenta_cobro_id NULL`. Auto-fulfillment pool (checklist_service.py:985-996) gains a
third pool `docs_usuario` (owner + both FKs NULL); `_pool_para` returns usuario → contrato
→ cuenta. Uploading a CEDULA/RUT/HV through any cuenta promotes it to usuario-level (both
FKs NULL) so it auto-satisfies every future contrato.

## Data Flow

```
PULL (since=0)  → sync_service.snapshot_working_set(user, active_window)
                  → active Contratos + CuentaCobros (≤ SYNC_ACTIVE_WINDOW_MONTHS) + children
                  → {entities, cursor = current max sync_cursor.last_seq}

PULL (since=N)  → SELECT * FROM change_log
                  WHERE usuario_id=:uid AND seq > :N ORDER BY seq ASC LIMIT :page
                  → {changes[], next_cursor = max(seq), has_more}

WRITE (any svc) → mutate entity ─┐
                                 ├─ same txn ─→ record_change() → sync_cursor FOR UPDATE → change_log row
                  commit ────────┘

PUSH            → per change: compare base_seq vs server_latest_seq
                  → apply | server-wins discard(+conflict) | last-writer-wins
                  → {results[{accepted,new_seq} | {rejected,reason,server_value}], cursor}

UPLOAD          → POST /documentos/presigned-upload → row(pendiente_confirmacion) + PUT url
                  → client PUT → R2
                  → POST /documentos/{id}/confirmar → download back → verify sha256
                  → _process_uploaded_document (SAME pipeline) → estado=confirmed
```

## File Changes

| File | Action | Description |
|---|---|---|
| `app/models/change_log.py` | Create | Append-only `change_log` + `sync_cursor` models |
| `app/models/documento_fuente.py` | Modify | `SoftDeleteMixin`; `sha256`,`size`,`content_type`,`estado` cols |
| `app/models/cuenta_cobro.py` | Modify | `motivo_rechazo` (StrEnum), `motivo_rechazo_nota` |
| `app/models/{actividad,obligacion,evidencia,documento_cuenta_cobro,requisito_cuenta}.py` | Modify | `SoftDeleteMixin` |
| `app/schemas/sync.py` | Create | Pull/push request+response, conflict record |
| `app/schemas/documento.py` | Modify | presigned-upload + confirmar + usuario-listing |
| `app/services/sync_service.py` | Create | `record_change`, `snapshot_working_set`, `pull`, `push`, `reap_orphaned_uploads` |
| `app/services/document_service.py` | Modify | Extract `_process_uploaded_document`; presigned + confirmar; `eliminar_documento` soft-delete |
| `app/services/checklist_service.py` | Modify | `_NIVEL_USUARIO` split, `es_nivel_usuario`, `docs_usuario` pool, `listar_documentos_usuario` |
| `app/adapters/storage/{port,s3_adapter}.py` | Modify | `presigned_upload_url()` (PUT) |
| `app/api/v1/sync.py` | Create | `GET /sync/pull`, `POST /sync/push` |
| `app/api/v1/documentos.py` | Modify | `/presigned-upload`, `/{id}/confirmar`, `/usuario` |
| `alembic/versions/024_*.py` | Create | Explicit `op.create_table`/`op.add_column`; Neon-verified |

## Interfaces / Contracts

```python
class ChangeOp(StrEnum): INSERT="insert"; UPDATE="update"; DELETE="delete"

class ChangeLog(UUIDMixin, Base):        # created_at only; no updated_at
    usuario_id: UUID (FK usuarios.id, indexed)
    entity_type: str(50); entity_id: UUID
    op: ChangeOp
    payload: JSON | None                 # jsonb on PG; whitelisted per entity
    seq: BigInteger                       # per-user monotonic, from sync_cursor
    created_at: datetime
    __table_args__ = (Index("ix_change_log_pull", "usuario_id", "seq"),)

class SyncCursor(Base):
    usuario_id: UUID (PK, FK usuarios.id)
    last_seq: BigInteger default 0

class EstadoDocumentoFuente(StrEnum): PENDIENTE_CONFIRMACION="pendiente_confirmacion"; CONFIRMED="confirmed"

# StoragePort
async def presigned_upload_url(self, key: str, content_type: str = "application/octet-stream",
                               expires_in: int = 3600) -> str: ...
# S3 impl: generate_presigned_url("put_object", Params={Bucket,Key,ContentType}, ExpiresIn)
```

Push envelope:
```json
{ "changes": [ {"entity_type":"cuenta_cobro","entity_id":"…","op":"update","base_seq":123,"fields":{…}} ] }
→ { "results":[ {"entity_id":"…","status":"accepted","new_seq":140},
                {"entity_id":"…","status":"rejected","reason":"money_conflict","server_value":{"valor":"…"}} ],
    "cursor":140 }
```

## Testing Strategy (strict TDD, aiosqlite)

| Layer | What | Approach |
|---|---|---|
| Unit | `record_change` assigns monotonic per-user seq; whitelist payload has no cross-user data | aiosqlite; two users interleaved |
| Unit | Pull `seq > since` never skips/dupes; snapshot windowed to active | factory-boy fixtures |
| Unit | Push: money server-wins discard+conflict; checklist last-writer-wins; base_seq compare | aiosqlite |
| Unit | Confirm reuses `_process_uploaded_document`, no duplicate row; sha256 idempotency; reap TTL | `moto[s3]` |
| Unit | `es_nivel_usuario` split; `docs_usuario` auto-fulfills new contrato; `eliminar_documento` soft-delete emits tombstone | aiosqlite |
| Integration | `/sync/pull`,`/sync/push`,`/documentos/presigned-upload`,`/confirmar`,`/usuario` | `httpx.AsyncClient` + `moto[s3]` |
| Migration | `024_*` ALTER/CREATE applies cleanly | **Real Postgres (Neon)**, not only aiosqlite |

## Migration / Rollout

Single `024_*`: `create_table` `change_log`+`sync_cursor` (+`ix_change_log_pull`);
`add_column` `deleted_at` on 6 tables; `sha256`/`size`/`content_type`/`estado` on
`documentos_fuente` (`estado` `server_default='confirmed'` to backfill existing rows);
`motivo_rechazo`/`motivo_rechazo_nota` on `cuentas_cobro`; PG enum types
(`change_op`,`estado_documento_fuente`,`motivo_rechazo`). Backfill `sync_cursor` with one
row per existing usuario (`last_seq=0`). Reap runs opportunistically at the start of
`/documentos/presigned-upload` and `/sync/pull` (indexed, bounded); a scheduled runner is
future work (no job runner in scope).

## Open Questions

- [ ] Final `MotivoRechazo` catalog values — finalized in `sdd-spec`.
- [ ] `SYNC_ACTIVE_WINDOW_MONTHS` default (6) confirmation and whether snapshot children
      (Actividad/Evidencia) inherit the parent's window.
- [ ] Whether reap should also emit an `op=delete` tombstone for orphaned
      `pendiente_confirmacion` rows the client never saw (likely no — client-local only).
