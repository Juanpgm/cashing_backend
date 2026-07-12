# Requisito Comprehension Specification

## Purpose

Strengthens requirement-document understanding to extract structured requisitos (not a
flat list) and orchestrates radicación-prep end-to-end: checklist generation, coherence
validation, and packaging.

## Requirements

### Requirement: Structured requisito extraction

The system MUST extract structured requisitos from an ingested requirement document
(DOCX/PDF/text), including at minimum: name, category, `solo_primera_cuenta` flag, and
whether it supports autogeneration — not a flat unstructured list.

#### Scenario: Structured extraction from a requirement document

- GIVEN a requirement document listing several requisitos with one-time and recurring
  obligations
- WHEN it is ingested
- THEN each requisito is extracted with its structured fields, including which are
  `solo_primera_cuenta`

### Requirement: Checklist generation from structured requisitos

The system MUST generate the account's checklist from the structured requisitos,
building on the existing non-persisted-preview pattern.

#### Scenario: Checklist preview before persistence

- GIVEN structured requisitos extracted for a new cuenta
- WHEN a checklist preview is requested
- THEN the preview reflects the structured requisitos without being persisted until
  confirmed

### Requirement: Radicación-prep orchestration

The system MUST orchestrate, in order, checklist verification, coherence validation,
and packaging when preparing a cuenta for radicación, and MUST stop before packaging if
coherence validation reports a HARD finding.

#### Scenario: Full successful orchestration

- GIVEN a cuenta with a complete checklist and no coherence findings
- WHEN radicación-prep is invoked
- THEN checklist verification, coherence validation, and packaging run in order
- AND the result reports the package location and LISTO/PENDIENTE status

#### Scenario: Coherence failure halts orchestration before packaging

- GIVEN a cuenta whose coherence validation returns a HARD finding
- WHEN radicación-prep is invoked
- THEN packaging is never attempted
- AND the orchestration result surfaces `COHERENCE_CHECK_FAILED` with the findings

#### Scenario: Secret detection halts orchestration

- GIVEN a cuenta that passes coherence validation but whose package payload contains a
  detectable secret
- WHEN radicación-prep reaches the packaging step
- THEN no package is emitted
- AND the orchestration result surfaces `SECRET_DETECTED_IN_PACKAGE`

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `inferir_requisitos_estructurados` | read-only (LLM inference over document) | Structured extraction, extends `requisito_inference_service.inferir_requisitos` |
| `preparar_radicacion` | write (orchestrates checklist + validator + packager) | Calls `validar_coherencia_cuenta`, `generar_zip_evidencias`/`obtener_estado_listo_pendiente` |

## Error Codes

Reuses `COHERENCE_CHECK_FAILED` and `SECRET_DETECTED_IN_PACKAGE` from the capabilities
it orchestrates; introduces no new codes.

## Deferred to sdd-design

- Exact orchestration tool signature and how partial success (e.g., checklist
  incomplete but caller wants a preview) is surfaced.
