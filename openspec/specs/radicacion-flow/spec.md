# Radicacion Flow Specification

Retroactive spec (written after implementation; amended 2026-09-19 to the merged state: backend `81aab19`, frontend `11bf4c4`; originally backend `a730acf`, frontend `db45c9f`). Task ids refer to `tasks.md`.

## Purpose

A user can take a cuenta de cobro from `borrador` to `enviada` through the guided wizard, safely and idempotently, with only Spanish user-facing errors.

## Requirements

### Requirement: Wizard step 7 owns radicar (1.1, 3.5)

The wizard's final step MUST call `POST /radicar` and render the real post-radicar state (estado, fecha, package link). The checklist summary card MUST NOT offer radicar. The legacy `/cuentas-cobro/[id]/radicacion` page SHOULD retain its own radicar action. The button MUST be disabled while the cuenta-estado query is loading or errored, and while a radicar request is in flight.

#### Scenario: Successful radicar
- GIVEN a cuenta in `borrador` with a complete checklist
- WHEN the user clicks "Radicar cuenta" in step 7
- THEN exactly one `POST /radicar` is sent and the UI shows the real estado, fecha and package link

#### Scenario: Triple rapid click
- WHEN the user clicks "Radicar cuenta" three times before the first response
- THEN exactly one request is sent

#### Scenario: Backend rejects (checklist / coherence)
- GIVEN the backend returns `CHECKLIST_INCOMPLETE` or `COHERENCE_CHECK_FAILED`
- THEN the UI names the pendientes or the coherence finding and the button stays retryable

#### Scenario: Network failure
- WHEN the request fails without a response
- THEN no success state is shown and the button remains retryable

#### Scenario: Estado query unresolved
- GIVEN the cuenta-estado query is loading or has failed
- THEN the radicar button is disabled

### Requirement: Radicar is idempotent and lock-safe (1.2)

`radicar` MUST transition `borrador` or `rechazada` to `enviada` at most once, using a row lock plus a conditional update that also requires the cuenta to be not soft-deleted. A call on an already-`enviada` cuenta MUST return the existing result, not 422. Terminal states (`pagada`) MUST still be rejected.

#### Scenario: Concurrent double radicar
- GIVEN a radicable `borrador` cuenta
- WHEN two `radicar` calls run concurrently
- THEN exactly one transitions the cuenta, neither duplicates side effects, both return success

#### Scenario: Radicar after enviada / rechazada / pagada
- WHEN `radicar` is called on `enviada` THEN the existing result is returned
- AND on `rechazada` THEN it is allowed; on `pagada` THEN it is rejected

### Requirement: `requisitos_modo` NULL stays an explicit unresolved state (1.3)

NULL MUST continue to mean "checklist mode not yet chosen" (it drives `requisitos_definidos=false`). Refresh-SECOP and auto-vincular-documentos, via REST and agent tools, MUST NOT materialize a standard checklist on an unresolved cuenta.

#### Scenario: Refresh on unresolved cuenta
- GIVEN a cuenta with NULL `requisitos_modo`
- WHEN refresh-secop or auto-vincular is invoked (REST or agent tool)
- THEN no checklist rows are created and `GET /checklist` still reports `requisitos_definidos=false`

### Requirement: The standard checklist materializes identity documents on the first cuota only (5.2)

For a cuenta that is not its contrato's first cuota, the standard checklist MUST NOT materialize or show CEDULA, RUT, RPC or CDP; CONTRATO MUST be hidden when a shared contract-level document exists and the contrato already has obligaciones, and MUST reappear when there is no shared document or the contrato has zero obligaciones; ACTA_INICIO (and FICHA_TECNICA, nivel-contrato) MUST keep applying. One shared rule (`requisito_aplica_a_cuenta` / `listar_filas_visibles`) MUST be used by the checklist view, the resumen, the radicar gate, the constancia PDF and the radicación package. A row that already carries a document, a SECOP link or a manual decision (`cumplido_manual` / `no_aplica`) MUST stay visible and its file MUST keep shipping in the package. When a contrato has no active first cuenta, exactly one surviving cuenta (the earliest) MUST be treated as first. A settled cuenta (`enviada`, `aprobada`, `pagada`) MUST NEVER have requisitos materialized for it or deleted from it by a checklist redefinition. Step 5 ("Formatos e informes") of `stepper-state` MUST report `complete` only when both INFORME_ACTIVIDADES and INFORME_SUPERVISION rows are satisfied and MUST expose the missing ones in `detail.informes_pendientes`; a missing informe row is not pending.

#### Scenario: Second cuota
- GIVEN a contrato whose first cuota exists and a later cuota
- WHEN the later cuota's checklist is built
- THEN CEDULA, RUT, RPC and CDP are absent and ACTA_INICIO is present

#### Scenario: CONTRATO on a later cuota
- WHEN a shared contract document exists and the contrato has obligaciones THEN CONTRATO is absent
- AND WHEN there is no shared document, or the contrato has zero obligaciones, THEN CONTRATO is present, and uploading it does not make it vanish from the package that needs it

#### Scenario: Content is never hidden
- GIVEN a legacy CEDULA row on a later cuota that already has a linked document or a manual decision
- THEN the row stays visible and its document remains in the package

#### Scenario: First cuota was deleted
- GIVEN a contrato with no active first cuenta
- THEN the earliest surviving cuenta is treated as first and keeps its identity documents; deleting cuota 1 promotes the next open cuenta

#### Scenario: Settled cuenta and checklist redefinition
- GIVEN an `aprobada` cuenta with its mandatory documents
- WHEN its checklist is redefined
- THEN the delete is refused and `radicacion_lista` does not flip to true on an account with no real soportes

#### Scenario: Informes gate on step 5
- WHEN an obligatory informe row is not yet satisfied
- THEN step 5 is incomplete and `detail.informes_pendientes` names it; a cuenta with no informe rows yet is not blocked by them

### Requirement: Domain exceptions map through the class hierarchy (1.5a)

`domain_to_http` MUST resolve the HTTP status by walking the exception's MRO; the first mapped ancestor wins; an unmapped root `DomainError` MUST yield 500.

#### Scenario: New subclass
- GIVEN a new subclass of a mapped error with no map entry
- THEN it receives its parent's status

### Requirement: No raw backend text or codes reach the UI (1.4, 1.5b, 5.6)

Every backend error code MUST have a Spanish message in a checked-in list, enforced by a test that fails on any gap. Unknown codes MUST fall back to a generic Spanish message, never the raw string. Error `detail` MUST be read only via `extractApiError` / `hasApiErrorDetail` (enforced by lint, repo-wide: the only exempt file is `lib/api.ts`, where those helpers live), handling string, object, array, empty and missing shapes and codeless network errors. This holds for `app/**`, `components/**` **and `lib/**`**: `lib/checklist-api.ts` `extractUploadErrorMessage` was the last hand-rolled reader and was migrated in 5.6 (an array `detail` no longer shows the raw `body.files:` loc prefix; an object or junk `detail` yields the generic Spanish fallback, not raw JSON).

#### Scenario: Unknown or hostile code
- WHEN the code is unrecognized (including `"constructor"`)
- THEN the generic message is shown

#### Scenario: Malformed detail
- WHEN `detail` is `[]`, an object array, or absent
- THEN no blank banner and no `[object Object]` is rendered

#### Scenario: Upload error message (lib layer)
- WHEN a file upload fails with a 422 array `detail`, an object `detail`, or junk `detail`
- THEN the message comes from `extractApiError` (no raw loc prefix, no raw JSON), a 429 keeps its own friendly message, and a response with no `detail` and no `error` falls back to a Spanish generic

### Requirement: Single cuenta per contrato-month under concurrency (4.2a)

Concurrent creates for the same `(contrato, mes, anio)` MUST yield one cuenta; the loser MUST receive `CUENTA_MES_DUPLICADA` and its credit deduction MUST be rolled back.

#### Scenario: Double create
- WHEN two creates race for the same month
- THEN one succeeds, the other gets `CUENTA_MES_DUPLICADA`, and credits are debited once

## Out of Scope (tracked follow-ups)

- Gating checklist mutators and `PATCH /checklist/{codigo}` by estado, with a read-only mode for `enviada` (P1, from 1.6).
- DB constraint for `numero_cuota` / `posicion=primera` (4.2a); running-window classification-enqueue gap (4.2b).
- `cruzar` / semaforo manual re-analysis in the wizard (3.2a, deferred).
- String-array `detail` joining (a string-array `detail` is no longer joined in `lib/checklist-api.ts` after 5.6; no backend producer); `detail: ""` yielding a message-less error (`lib/api.ts:189`, pre-existing); residual raw `detail` on known codes (1.1, 1.4). The ESLint `.detail` selector is repo-wide and untyped (a narrower `data.detail` selector would avoid false positives).
- `document_service.py` contract-replacement branch can leave a stale `CARGADO` row with no artifact on Postgres (not reproducible on SQLite); no frontend e2e drives the "archivo"/"texto" checklist inference modes (5.2 follow-ups).
