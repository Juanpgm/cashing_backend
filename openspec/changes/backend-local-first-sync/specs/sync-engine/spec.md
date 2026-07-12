# Sync Engine Specification

## Purpose

Delta-sync contract (`change_log`, `GET /sync/pull`, `POST /sync/push`) letting an
offline-capable client hold an active working set and reconcile deterministically.

## Requirements

### Requirement: Delta pull contract

The system MUST expose `GET /sync/pull?since=<cursor>&limit=<n>` returning
`change_log` rows for the caller's active working set (see Requirement: Active
working set scoping), ordered by `seq` ascending, respecting a commit-visibility
guard so no committed row is skipped due to out-of-order transaction commits.

Response MUST include: `changes[]` (each: `entity_type`, `entity_id`,
`op` in `insert|update|delete`, `payload`, `seq`), `next_cursor` (highest
safely-visible `seq`), `has_more`. Default page size MUST be bounded (e.g. 500)
and MAY be overridden by `limit` up to a server-enforced max.

#### Scenario: First pull with no cursor

- GIVEN a user with `since` omitted
- WHEN they call `GET /sync/pull`
- THEN the response returns their active working set up to `next_cursor`, paginated if needed

#### Scenario: Incremental pull

- GIVEN a user previously received `next_cursor=118`
- WHEN they call `GET /sync/pull?since=118`
- THEN only rows with `seq > 118` inside the commit-visibility guard are returned

#### Scenario: Uncommitted lower seq is not skipped

- GIVEN `seq=119` has not yet committed while `seq=120` already has
- WHEN a client pulls `since=115`
- THEN `next_cursor` MUST NOT advance past 118 until `seq=119`'s commit is visible

### Requirement: Delta push contract and conflict envelope

`POST /sync/push` MUST accept a batch of mutations, each with `entity_type`,
`entity_id`, `op`, `payload`, and `base_seq` (client's last-known server state for
that entity). For every mutation the system MUST return `accepted` (with the new
`seq`) or `rejected` (with `reason=changed_while_offline` and the authoritative
current server value) — never silent partial application.

#### Scenario: Non-conflicting push accepted

- GIVEN a checklist-state mutation whose `base_seq` matches current server `seq`
- WHEN `/sync/push` processes it
- THEN it is applied and returned as `accepted` with a new `seq`

#### Scenario: Money field changed while offline

- GIVEN `CuentaCobro.valor` changed server-side after the client's `base_seq`
- WHEN the client pushes an offline edit to `valor`
- THEN the system MUST reject it with `changed_while_offline` and the server's authoritative `valor`, and MUST NOT apply the client value

### Requirement: Active working set scoping

Pull MUST be scoped per user to: all non-expired `Contrato` rows owned by the
user, plus `CuentaCobro` rows within `SYNC_ACTIVE_WINDOW_MONTHS` (default 6,
configurable), plus their directly-owned dependent syncable entities.

#### Scenario: Old cuenta excluded from pull

- GIVEN a `CuentaCobro` last updated 9 months ago and the default 6-month window
- WHEN the owner calls `/sync/pull`
- THEN that `CuentaCobro` and its `change_log` rows are excluded

### Requirement: Syncable entity set

The syncable set (push + pull) MUST be exactly: `Contrato`, `CuentaCobro`,
`Actividad`, `Obligacion`, `DocumentoFuente` (metadata only), `Evidencia`,
`DocumentoCuentaCobro`, `RequisitoCuenta`. `SecopContrato`/`SecopProceso`/
`SecopDocumento` and the `RequisitoDocumento` catalog MUST be excluded from
push and, if pulled at all, MUST be pull-only reference data never sourced
from `change_log`.

#### Scenario: SECOP cache rejected from push

- GIVEN a client pushes a mutation with `entity_type=SecopDocumento`
- WHEN `/sync/push` receives it
- THEN the system MUST reject that mutation as an invalid entity type

### Requirement: Tombstone propagation

Every entity in the syncable set MUST support soft delete (`SoftDeleteMixin`),
and every delete MUST write a `change_log` row with `op=delete` instead of a
hard `DELETE`. `document_service.eliminar_documento` MUST perform a soft delete.

#### Scenario: Deleting a document tombstones it

- GIVEN a user deletes a `DocumentoFuente`
- WHEN the delete completes
- THEN `deleted_at` is set (row not removed) and a `change_log` row with `op=delete` is created for that `entity_id`

### Requirement: Conflict resolution policy

Checklist state changes MUST resolve last-writer-wins. Money fields
(`CuentaCobro.valor`, `Contrato.valor_total`, `Contrato.valor_adicion`,
`Contrato.valor_mensual`) and actividades backing a monto MUST resolve
server-wins, surfaced to the client per the conflict envelope above.

#### Scenario: Checklist edit applies last-writer-wins

- GIVEN two offline checklist edits to the same requisito with different `base_seq`
- WHEN both are pushed
- THEN the later-applied edit wins and both mutations are `accepted` (no rejection)

### Requirement: KPI instrumentation via change_log

`change_log` MUST be queryable, without a separate analytics pipeline, to derive:
time-to-complete a cuenta de cobro, rejection rate by `motivo_rechazo`, `sha256`
dedup hit count, and `/sync/push` server-wins discard count.

#### Scenario: Server-wins discard is observable

- GIVEN `/sync/push` rejects a money-field mutation with `changed_while_offline`
- WHEN a KPI query inspects sync conflict counts
- THEN that rejection is countable from persisted state
