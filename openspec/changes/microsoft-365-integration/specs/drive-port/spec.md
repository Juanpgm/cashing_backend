# Drive Port Generalization Specification

## Purpose

Generalizes `DrivePort.search_files` to accept a structured, provider-neutral
query object instead of a raw Google Drive query string, and establishes the
seam for a future semantic-search layer (not built in this change).

## Requirements

### Requirement: Structured query object replaces raw query string

`DrivePort.search_files` MUST accept a structured query object (fields:
keywords, date range, mime/type, obligation search term) instead of a
Google-syntax query string. Each adapter MUST translate the query object into
its own provider's native query language internally.

#### Scenario: Drive fetch node builds a query object

- GIVEN the drive fetch node needs to search for evidence files
- WHEN it calls `search_files`
- THEN it passes a structured query object, not a raw string

#### Scenario: Google adapter translates the query object equivalently

- GIVEN a query object equivalent to a previously used raw Google query string
- WHEN `GoogleDriveAdapter.search_files` receives it
- THEN it translates the object into Google Drive query syntax and returns
  results equivalent to the pre-refactor behavior

### Requirement: Query object is provider-agnostic and forward-compatible

The query object MUST NOT encode Google-specific operators or syntax, so a
future semantic-search layer (embeddings vs. obligación text) can extend it
without a breaking change. Building that semantic-search layer is out of scope
for this change.

#### Scenario: Query object shape is identical across providers

- GIVEN a query object built for a Microsoft adapter and one built for the
  Google adapter with equivalent search intent
- WHEN both are constructed
- THEN they use the same query object shape; only the provider-side
  translation differs

## Deferred to sdd-design

- Exact query object schema/dataclass fields and the translation function's
  location per adapter.
