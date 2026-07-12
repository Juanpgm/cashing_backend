# Cuota Position Model Specification

## Purpose

Introduces a stored (not inferred) cuota position on `CuentaCobro`: `numero_cuota`,
`posicion` (primera/recurrente/final), and an explicit `informe_final` flag. Replaces
the purely positional `_is_first_cuenta()` heuristic and governs one-time-obligation
semantics.

## Requirements

### Requirement: Position is stored, not inferred

The system MUST persist `numero_cuota`, `posicion`, and `informe_final` on each cuota
record. The system MUST NOT rely on runtime positional inference (smallest anio/mes) to
determine "first cuenta" once this field is populated.

#### Scenario: First cuota is marked explicitly

- GIVEN a new cuota created as the first for its contract
- WHEN it is persisted
- THEN `posicion = primera` and `numero_cuota = 1` are stored

#### Scenario: Final cuota is explicit, not "latest so far"

- GIVEN a contract nearing completion
- WHEN the final cuota is created
- THEN `informe_final = true` is stored explicitly on that cuota
- AND no other cuota for the same contract has `informe_final = true`

### Requirement: One-time obligations follow cuota position

The system MUST treat requisitos flagged `solo_primera_cuenta` as required only when
`posicion = primera` and MUST treat them as not-applicable (blank) for any other
position.

#### Scenario: One-time obligation required in cuota 1

- GIVEN a requisito flagged `solo_primera_cuenta`
- WHEN the checklist is evaluated for the cuota with `posicion = primera`
- THEN the requisito is required

#### Scenario: One-time obligation blank thereafter

- GIVEN the same requisito
- WHEN the checklist is evaluated for a cuota with `posicion = recurrente` or `final`
- THEN the requisito is not required and is not shown as pending

### Requirement: Position conflicts are rejected at write time

The system MUST reject a write that would produce an inconsistent position for a
contract (e.g., two cuotas both marked `informe_final = true`, or a second
`posicion = primera` for the same contract).

#### Scenario: Duplicate final cuota rejected

- GIVEN a contract that already has a cuota with `informe_final = true`
- WHEN a second cuota attempts to persist `informe_final = true`
- THEN the write is rejected with `CUOTA_POSITION_CONFLICT`

### Requirement: Existing rows are backfilled

The system MUST assign a `posicion`/`numero_cuota` value to pre-existing cuota rows so
no cuota is left without a position after this change ships.

#### Scenario: Legacy cuotas receive a position

- GIVEN cuotas created before this change with no stored position
- WHEN the backfill runs
- THEN every legacy cuota has a non-null `posicion` and `numero_cuota` consistent with
  its historical chronological order per contract

## Tool Surface (`TOOL_REGISTRY`)

No new tool. Position fields are read/written by existing cuenta-de-cobro service tools
(`crear_cuenta_cobro`, `obtener_cuenta_cobro`), which MUST include the new fields in
their outputs.

## Error Codes

- `CUOTA_POSITION_CONFLICT` — write would violate position invariants for the contract.

## Deferred to sdd-design

- Whether `numero_cuota` derives from a new `Contrato.numero_cuotas` field or is stored
  per-cuota only, and the exact backfill mechanism — proposal §9.3.
