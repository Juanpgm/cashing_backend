# Integraciones Credential Table Specification

## Purpose

Generalized multi-provider, multi-account credential storage (`integraciones`)
and its status schema, replacing the single-provider `google_tokens` table and
`GoogleIntegrationStatus`, with a zero-re-consent migration path for existing
Google users.

## Requirements

### Requirement: Generalized table supports multi-provider, multi-account credentials

The system MUST store OAuth credentials in an `integraciones` table with a
`provider` discriminator (`google`|`microsoft`), Fernet-encrypted tokens,
`scopes`, `expires_at`, `email`, and a uniqueness constraint on
`(usuario_id, provider, email)`. The system MUST allow one user to hold Google
and Microsoft credentials simultaneously, and multiple accounts per provider.

#### Scenario: User connects Microsoft while already connected to Google

- GIVEN a user with an existing `integraciones` row for `provider=google`
- WHEN the user completes the Microsoft connect flow
- THEN a second `integraciones` row is created for `provider=microsoft` and the
  Google row is untouched

#### Scenario: Reconnecting an existing account updates instead of duplicating

- GIVEN an existing `integraciones` row for `(usuario_id, provider, email)`
- WHEN the user reconnects the same account
- THEN the existing row is updated in place, no duplicate row is created

### Requirement: IntegrationStatus schema exposes a provider discriminator

The system MUST expose an `IntegrationStatus` schema/response that includes a
`provider` field and per-connection details (email, scopes, expires_at,
connected flag), replacing `GoogleIntegrationStatus`.

#### Scenario: Status for a user connected to both providers

- GIVEN a user with connected Google and Microsoft accounts
- WHEN the integration status endpoint is called
- THEN the response includes one entry per provider with `provider` set
  correctly on each

#### Scenario: Status for a user with no connections

- GIVEN a user with no `integraciones` rows
- WHEN the integration status endpoint is called
- THEN the response indicates no providers are connected, without error

### Requirement: Backfill preserves existing Google connections with zero re-consent

The system MUST create the `integraciones` table and backfill every existing
`google_tokens` row into it with `provider=google`, preserving the encrypted
token material and expiry, WITHOUT requiring the affected user to
re-authenticate.

#### Scenario: Existing connected Google user keeps working after migration

- GIVEN a user with a pre-migration `google_tokens` row
- WHEN the migration/backfill runs and the user later calls a Google-backed
  evidence tool
- THEN the call succeeds using the backfilled credentials, with no OAuth
  redirect required

### Requirement: `google_tokens` remains read-only until cutover is verified

The migration MUST NOT delete or mutate `google_tokens` rows. `google_tokens`
MUST remain intact and readable until the `integraciones` cutover is verified.

#### Scenario: Rollback after migration

- GIVEN the migration has run and `integraciones` is later rolled back
- WHEN `integraciones` is dropped
- THEN `google_tokens` still contains all original rows and Google-backed
  features keep working

## Deferred to sdd-design

- Exact migration/backfill SQL and cutover sequencing.
- Whether `GoogleIntegrationStatus` is removed, aliased, or kept as a
  provider-scoped view of `IntegrationStatus`.
