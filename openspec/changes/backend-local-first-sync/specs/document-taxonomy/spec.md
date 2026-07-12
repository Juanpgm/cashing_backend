# Document Taxonomy Specification (Three-Tier)

## Purpose

Split documents into usuario / contrato / cuenta tiers so usuario-level
documents (cédula, RUT, HV) auto-fulfill requisitos on new contratos instead
of being re-uploaded.

## Requirements

### Requirement: Three-tier classification

Every `DocumentoFuente` MUST be classified into exactly one tier by its FK
state: usuario-level (`contrato_id IS NULL AND cuenta_cobro_id IS NULL`),
contrato-level (`contrato_id IS NOT NULL AND cuenta_cobro_id IS NULL`), or
cuenta-level (`cuenta_cobro_id IS NOT NULL`).

#### Scenario: Cédula classified as usuario-level

- GIVEN a `DocumentoFuente` of type CEDULA with no `contrato_id` or `cuenta_cobro_id`
- WHEN its tier is evaluated
- THEN it is classified usuario-level

### Requirement: List usuario-level documents

`GET /documentos/usuario` MUST return all `DocumentoFuente` rows owned by the
authenticated user with `contrato_id IS NULL AND cuenta_cobro_id IS NULL`.

#### Scenario: Usuario documents listed

- GIVEN a user has one usuario-level RUT and one contrato-level informe
- WHEN they call `GET /documentos/usuario`
- THEN only the RUT is returned

### Requirement: Usuario-level auto-fulfillment on new contratos

When a new `Contrato` is created, the checklist auto-fulfillment pool MUST
include the user's usuario-level CEDULA/RUT/HV documents so matching
requisitos are satisfied automatically, without requiring re-upload.

#### Scenario: New contrato auto-satisfies CEDULA requisito

- GIVEN a user already has a usuario-level CEDULA document
- WHEN they create a new `Contrato` whose checklist includes a CEDULA requisito
- THEN that requisito is auto-fulfilled from the existing document without a new upload

#### Scenario: Contrato-level document does not leak across contratos

- GIVEN a contrato-level informe tied to `Contrato A`
- WHEN checklist auto-fulfillment runs for `Contrato B`
- THEN the `Contrato A` informe MUST NOT satisfy any requisito of `Contrato B`
