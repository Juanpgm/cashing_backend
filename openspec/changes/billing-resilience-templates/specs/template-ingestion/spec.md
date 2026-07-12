# Template Ingestion Specification

## Purpose

Ingests an institutional informe template (DOCX/PDF) per organism, extracts its
structure (columns, sections, anexo references), and persists it so
`adaptive-informe-generation` and `cuota-packager` can adapt their output. Degrades
gracefully to current flat behavior when extraction fails.

## Requirements

### Requirement: Template structure is extracted and persisted

The system MUST extract structural elements from an ingested DOCX/PDF template — column
layout, section list, and literal anexo references (e.g., "Ver Anexo: Carpeta
/5. EVIDENCIAS/A1") — and persist them keyed to the organism.

#### Scenario: Successful extraction

- GIVEN a well-formed DOCX template for an organism
- WHEN it is ingested
- THEN column layout, sections, and anexo references are extracted and persisted for
  that organism

#### Scenario: Anexo reference preserved verbatim

- GIVEN a template containing "Ver Anexo: Carpeta /5. EVIDENCIAS/A1"
- WHEN extraction runs
- THEN this reference string is preserved verbatim in the persisted structure

### Requirement: Graceful degradation on extraction failure

The system MUST fall back to the current flat/default informe behavior when template
extraction fails or produces low-confidence results, without blocking ingestion of the
underlying contract/organism record.

#### Scenario: Unreadable template degrades safely

- GIVEN a scanned, low-quality PDF template that cannot be reliably parsed
- WHEN ingestion is attempted
- THEN no structure is persisted for that organism
- AND `adaptive-informe-generation` falls back to the default layout for that organism
- AND the ingestion call does not raise a hard error

### Requirement: Resilient reading reuses the existing fallback chain

The system SHOULD reuse the existing resilient document-reading fallback chain (used
for CONTRATO vision extraction) when parsing degraded/scanned template files.

#### Scenario: Vision fallback used for scanned template

- GIVEN a template file that fails standard text extraction
- WHEN ingestion falls back to the vision-based reader
- THEN extraction is retried through that chain before declaring failure

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `ingerir_plantilla_organismo` | write | Extracts and persists template structure for an organism |
| `obtener_plantilla_organismo` | read-only | Returns the persisted structure (or none, if not ingested) |

## Error Codes

No new error code; extraction failure is a graceful-degradation path, not an error
state.

## Deferred to sdd-design

- Storage model for the extracted structure — new model vs extending `Plantilla`
  (currently HTML→PDF cuenta-de-cobro only) — proposal §9.5.
- Per-organism selection key (no `organismo` field exists on `RequisitoDocumento` today)
  — proposal §9.6.
