# Contract Addition Events Specification

## Purpose

Records contract Adición (amendment) as a first-class tracked event — new RPC/CDP
identifiers, `valor_adicion`, and prórroga (term extension) — replacing the current
single scalar `valor_adicion` field. Feeds cuota position/final detection and the
coherence validator (R6).

## Requirements

### Requirement: Adición is recorded as an event

The system MUST record each Adición as a discrete event carrying: new RPC/CDP
identifiers (when issued), `valor_adicion`, and prórroga extension (when granted),
preserving the full history for a contract.

#### Scenario: Adición with new RPC/CDP recorded

- GIVEN a contract receiving an Adición that issues a new RPC/CDP
- WHEN the event is recorded
- THEN the new RPC/CDP, `valor_adicion`, and event timestamp are persisted and
  associated with the contract

#### Scenario: Multiple Adición events are preserved

- GIVEN a contract with two Adición events over its lifetime
- WHEN both are recorded
- THEN both events remain queryable in order; the second does not overwrite the first

### Requirement: Prórroga affects final-cuota detection

The system MUST account for a recorded prórroga when determining whether a cuota is the
contract's final cuota.

#### Scenario: Prórroga extends expected cuota count

- GIVEN a contract whose Adición event includes a prórroga
- WHEN cuota position is evaluated after the prórroga
- THEN the cuota previously expected to be final is no longer treated as final by
  default

### Requirement: Adición events are visible to the coherence validator

The system MUST expose recorded Adición events (new RPC/CDP, effective date) so rule R6
(`radicacion-coherence-validator`) can compare them against generated clause text.

#### Scenario: Validator detects stale clause after Adición

- GIVEN a contract with a recorded Adición introducing a new RPC/CDP
- WHEN a cuota generated after the Adición still references the old RPC/CDP
- THEN the validator's R6 check flags the mismatch (see `radicacion-coherence-validator`)

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `registrar_adicion_contrato` | write | Records a new Adición event for a contract |
| `listar_adiciones_contrato` | read-only | Returns the ordered event history for a contract |

## Error Codes

No new error code introduced; invalid event data (missing required fields) uses
existing validation error handling.

## Deferred to sdd-design

- Dedicated event table vs generic event/audit pattern; exact prórroga-to-position
  interaction rule — proposal §9.4.
