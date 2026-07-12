# Adaptive Informe Generation Specification

## Purpose

Generates cuota informes using per-organism layout (from `template-ingestion`) and a
progressive narrative built from prior cuotas' context. Every generated informe is
explicitly labeled a borrador.

## Requirements

### Requirement: Per-organism layout selection

The system MUST select the informe layout based on the organism's ingested template
structure when available, and MUST fall back to the current default layout when no
organism-specific structure exists.

#### Scenario: DAGMA 2-column layout

- GIVEN an organism identified as DAGMA with an ingested 2-column template structure
- WHEN an informe is generated for a DAGMA contract
- THEN the informe is rendered in the DAGMA 2-column layout

#### Scenario: COEMPRESAR 3-column layout with anexo refs

- GIVEN an organism identified as COEMPRESAR with an ingested 3-column template
  structure containing anexo references
- WHEN an informe is generated for a COEMPRESAR contract
- THEN the informe is rendered in the 3-column layout and includes the literal anexo
  references from the ingested template

#### Scenario: No organism template — default layout

- GIVEN a contract for an organism with no ingested template
- WHEN an informe is generated
- THEN the current default 4-column layout is used

### Requirement: Progressive narrative uses prior cuota context

The system MUST generate the narrative for cuota N using context from cuotas 1..N-1 of
the same contract.

#### Scenario: Cuota 3 references prior cuotas

- GIVEN a contract with cuotas 1 and 2 already generated
- WHEN cuota 3's informe is generated
- THEN its narrative is built using the recorded context of cuotas 1 and 2

#### Scenario: Cuota 1 has no prior context

- GIVEN a contract's first cuota
- WHEN its informe is generated
- THEN the narrative is generated without prior-cuota context (none exists)

### Requirement: Output is always labeled borrador

The system MUST label every generated informe output as borrador (draft), regardless of
organism, layout, or completeness.

#### Scenario: Draft label present on every informe

- GIVEN any generated informe, complete or partial
- WHEN it is returned to the caller
- THEN it carries an explicit borrador label

### Requirement: One-time obligations blank after cuota 1

The system MUST omit content for `solo_primera_cuenta` obligations in generated
informes for any cuota where `posicion != primera`.

#### Scenario: One-time obligation blank in cuota 2

- GIVEN a one-time obligation filled in cuota 1's informe
- WHEN cuota 2's informe is generated
- THEN that obligation's section is blank

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `generar_informe_cuota` | write (produces/stores an informe draft) | Existing `informe_service` generator, extended with organism/position/prior-context params |

## Error Codes

No new error code; falls back to the default layout rather than failing.

## Deferred to sdd-design

- Exact prior-cuota context window (all N-1 cuotas vs a bounded window) and how "always
  draft" is surfaced to API/tool callers — proposal §9.7.
