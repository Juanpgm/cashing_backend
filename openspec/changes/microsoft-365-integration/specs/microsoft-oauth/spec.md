# Microsoft OAuth2 PKCE Flow Specification

## Purpose

Microsoft 365 OAuth2 authorization-code + PKCE flow to connect a user's
Microsoft account, reusing the existing signed-JWT state pattern
(`_encode_oauth_state`) with no server-side session.

## Requirements

### Requirement: PKCE-based authorization code flow

The connect route MUST initiate Microsoft Graph OAuth2 using PKCE
(`code_verifier`/`code_challenge`), redirecting to Microsoft's authorize
endpoint with the delegated scopes `Mail.Read`, `Calendars.Read`,
`Files.Read`.

#### Scenario: User starts Microsoft connect

- GIVEN an authenticated user with no active Microsoft connection
- WHEN they call the Microsoft connect route
- THEN they are redirected to Microsoft's login/consent screen with the
  correct scopes and a PKCE code challenge

#### Scenario: User completes consent

- GIVEN the user grants consent
- WHEN Microsoft redirects to the callback with an authorization code
- THEN the callback exchanges the code and PKCE verifier for tokens

### Requirement: OAuth state reuses the signed-JWT pattern, no server session

The `state` parameter MUST be a signed JWT following the existing
`_encode_oauth_state` pattern. The flow MUST NOT rely on server-side session
storage.

#### Scenario: Tampered state is rejected

- GIVEN a callback request with a tampered/invalid state JWT
- WHEN the callback is processed
- THEN the request is rejected before any token exchange occurs

#### Scenario: Valid state proceeds

- GIVEN a callback request with a valid, unexpired state JWT
- WHEN the callback is processed
- THEN the flow proceeds to token exchange

### Requirement: Successful connection persists to `integraciones` as `provider=microsoft`

On successful token exchange, the system MUST create or update an
`integraciones` row with `provider=microsoft`, encrypted tokens, scopes,
`expires_at`, and account email, without disturbing existing rows for other
providers/accounts of the same user.

#### Scenario: Coexistence with an existing Google connection

- GIVEN a user with an existing `provider=google` integration
- WHEN they complete the Microsoft connect flow
- THEN both rows exist independently after the flow completes

#### Scenario: Reconnecting an already-connected Microsoft account

- GIVEN a user with an existing `(usuario_id, microsoft, email)` row
- WHEN they reconnect the same account
- THEN the existing row is updated, not duplicated

### Requirement: Microsoft connect/callback routes are exposed under `integraciones`

Connect and callback routes for Microsoft MUST exist and behave analogously
to the existing Google connect/callback routes.

#### Scenario: Microsoft routes exist and function

- GIVEN the integraciones router
- WHEN a client calls the Microsoft connect and callback routes
- THEN both behave analogously to their Google counterparts (redirect, then
  token exchange on callback)

## Deferred to sdd-design

- Exact route paths/parameterization (e.g., provider-parameterized vs.
  provider-specific routes).
- Azure AD app registration details and admin-consent documentation location.
