# Rejection Reason Specification

## Purpose

Structured, analyzable rejection cause on `CuentaCobro` to measure and
eventually reduce rejection rate (KPI #2 — Menos rechazos).

## Requirements

### Requirement: MotivoRechazo enum catalog

`CuentaCobro` MUST gain `motivo_rechazo` typed as a `MotivoRechazo` StrEnum
with values: `FALTA_RPC`, `SEG_SOCIAL_VENCIDA`, `INFORME_INCOMPLETO`,
`VALOR_INCORRECTO`, `DOCUMENTO_ILEGIBLE`, `DOCUMENTO_FALTANTE`, `OTRO`. The
field MUST be nullable (set only when a cuenta is rejected).

#### Scenario: Rejection sets structured reason

- GIVEN a `CuentaCobro` transitions to a rejected state
- WHEN the rejector supplies `motivo_rechazo=SEG_SOCIAL_VENCIDA`
- THEN the enum value is persisted and readable on the record

#### Scenario: Invalid enum value rejected

- GIVEN a rejection request with `motivo_rechazo="INVALID"`
- WHEN the API validates the payload
- THEN the request MUST be rejected with a validation error before persistence

### Requirement: Optional free-text note

`CuentaCobro.motivo_rechazo_nota` MUST be an optional free-text field
independent of the enum, for human context, and MUST NOT be used for KPI
aggregation.

#### Scenario: Note without enum is invalid

- GIVEN a rejection request with only `motivo_rechazo_nota` and no `motivo_rechazo`
- WHEN the API validates the payload
- THEN the request MUST be rejected (nota alone is not a valid rejection)

### Requirement: KPI feed

`motivo_rechazo` MUST be aggregable directly from `CuentaCobro` (and/or
`change_log` update events) to compute rejection rate by cause, without
additional instrumentation.

#### Scenario: Rejection rate by cause is computable

- GIVEN multiple rejected cuentas with varying `motivo_rechazo`
- WHEN a KPI query groups by `motivo_rechazo`
- THEN counts per cause are derivable directly from persisted data
