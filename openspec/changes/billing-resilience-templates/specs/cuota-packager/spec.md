# Cuota Packager Specification

## Purpose

Hardens `generar_zip_evidencias` from a placeholder into a real packager: fetches real
bytes from `StoragePort`, builds a per-organism numbered folder structure, runs a
mandatory fail-closed secret scan, and computes a LISTO/PENDIENTE gate mirroring the
human LEEME workflow.

## Requirements

### Requirement: Real bytes are packaged, not placeholders

The system MUST fetch actual document bytes from `StoragePort` for every
requisito/evidencia included in a package.

#### Scenario: Package contains real content

- GIVEN a cuenta with uploaded evidencia files in storage
- WHEN the packager builds the zip
- THEN each entry in the zip contains the real bytes fetched from `StoragePort`, not
  placeholder text

### Requirement: Per-organism numbered folder structure

The system MUST arrange package contents into numbered top-level folders following the
target organism's structure when one is available (from `template-ingestion`), and MUST
fall back to a default numbered structure when no organism-specific structure exists.

#### Scenario: Organism-specific structure applied

- GIVEN an organism with an ingested template structure
- WHEN the package is built for that organism
- THEN folders are numbered and named per that organism's structure

#### Scenario: Fallback structure used

- GIVEN an organism with no ingested template structure
- WHEN the package is built
- THEN the packager uses the default numbered folder structure

### Requirement: Mandatory fail-closed secret scan

The system MUST scan every file's bytes for secrets before emitting the zip. If any
secret is detected, the system MUST NOT emit a package.

#### Scenario: Real-leak corpus is caught

- GIVEN a PENDIENTE.txt file inside the package payload containing a Postgres connection
  string and an API key (the real-leak test corpus)
- WHEN the secret scan runs
- THEN it detects both secrets
- AND no zip is emitted
- AND the packager raises `SECRET_DETECTED_IN_PACKAGE`

#### Scenario: Clean package proceeds

- GIVEN a package payload with no secret patterns
- WHEN the secret scan runs
- THEN no findings are returned and packaging proceeds

### Requirement: LISTO/PENDIENTE gate

The system MUST classify every requisito as LISTO (evidence present and coherent) or
PENDIENTE (missing or incomplete) and MUST include this split in the package manifest.

#### Scenario: Partial package with PENDIENTE items

- GIVEN a cuenta where some requisitos have no uploaded evidence
- WHEN the packager runs in standard (non-final) mode
- THEN it emits the package with a manifest section listing PENDIENTE requisitos, without
  blocking

#### Scenario: Final radicación requires complete package

- GIVEN a cuenta being packaged in strict/final mode for radicación submission
- WHEN one or more requisitos remain PENDIENTE
- THEN the packager raises `PACKAGE_PENDIENTE` and does not finalize radicación

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `generar_zip_evidencias` | write (creates zip artifact in storage) | Hardened version; internally fetches read bytes, runs scan, applies gate |
| `obtener_estado_listo_pendiente` | read-only | Returns the LISTO/PENDIENTE split without producing a package |

## Error Codes

- `SECRET_DETECTED_IN_PACKAGE` — secret scan hit; no package emitted.
- `PACKAGE_PENDIENTE` — strict/final packaging attempted with incomplete requisitos.

## Deferred to sdd-design

- Secret-scan detector implementation (existing lib vs bespoke) — proposal §9.2.
- StoragePort sequential-download cost handling (no new port methods this change).
