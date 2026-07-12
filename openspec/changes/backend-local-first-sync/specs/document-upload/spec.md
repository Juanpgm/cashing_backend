# Document Upload Specification (Presigned + Confirm)

## Purpose

Direct-to-R2 presigned upload with a confirm-then-fetch handshake that reuses
the existing extraction pipeline unmodified, plus a reap policy for orphaned
uploads.

## Requirements

### Requirement: Presigned upload initiation

`POST /documentos/presigned-upload` MUST create a `DocumentoFuente` row with
`estado=pendiente_confirmacion` and return `{presigned_upload_url,
documento_fuente_id}`. The URL MUST be a presigned PUT scoped to that object
only, honoring the same size/type constraints as direct upload (10MB cap).

#### Scenario: Successful initiation

- GIVEN an authenticated user requests a presigned upload
- WHEN `POST /documentos/presigned-upload` is called
- THEN a `pendiente_confirmacion` row is created and a presigned PUT URL + its id are returned

### Requirement: Confirm-then-fetch handshake

`POST /documentos/{id}/confirmar` MUST accept `sha256`, `size`, `content_type`;
fetch the object back from storage; and run the existing
`document_service.upload_document` pipeline (ownership, dedup, text
extraction, OCR, contract/obligación extraction) unmodified. Confirm MUST be
idempotent on `sha256` — re-confirming the same object MUST NOT reprocess it.

#### Scenario: First confirm processes the document

- GIVEN a `pendiente_confirmacion` row and a completed R2 PUT
- WHEN `confirmar` is called with the correct `sha256`/`size`/`content_type`
- THEN the object is fetched, the full pipeline runs, and the row exits `pendiente_confirmacion`

#### Scenario: Duplicate confirm is idempotent

- GIVEN a document already confirmed with `sha256=X`
- WHEN `confirmar` is called again with the same `sha256=X`
- THEN the system MUST NOT reprocess and MUST return the existing result

#### Scenario: sha256 mismatch rejected

- GIVEN a confirm request whose `sha256` does not match the uploaded object
- WHEN `confirmar` validates the object
- THEN the system MUST reject with an error and MUST NOT run the extraction pipeline

### Requirement: DocumentoFuente storage metadata

`DocumentoFuente` MUST gain nullable `sha256` (string), `size` (BigInteger),
`content_type` (string) columns, populated on confirm.

#### Scenario: Metadata persisted

- GIVEN a confirmed document
- WHEN its row is read
- THEN `sha256`, `size`, and `content_type` are populated

### Requirement: Reap policy for orphaned uploads

`DocumentoFuente` rows in `estado=pendiente_confirmacion` older than a
configurable TTL (default aligned to the client outbox's ~7 days) MUST be
excluded from active document listings and MUST eventually be reaped, without
introducing a new background job runner (reap trigger mechanism is a design
decision).

#### Scenario: Stale pending row excluded

- GIVEN a `pendiente_confirmacion` row created 10 days ago with a 7-day TTL
- WHEN a user lists their documents
- THEN that row MUST NOT appear as an active/pending document
