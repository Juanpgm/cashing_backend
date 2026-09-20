# Quality Gate and Test Infrastructure Specification

Retroactive spec. Both repos. Task ids refer to `tasks.md`.

## Purpose

Regressions are stopped locally before merge, and tests run deterministically without client data or external networks.

## Requirements

### Requirement: Local pre-merge gate is the official gate (0.5, 1.10, 4.8)

While GitHub Actions is unusable, each repo MUST provide a local gate (backend `scripts/pre-merge.ps1`, frontend `npm run gate`) that mirrors CI and prints `GATE OK <sha> <counts> dirty=<yes|no>` only when every fatal step passes. A versioned `pre-push` hook MUST run it; `SKIP_GATE=1` and `--no-verify` are the only bypasses. A PR template MUST require the `GATE OK` line. CI workflows are retained as dormant specification.

#### Scenario: Failing step
- WHEN tests, coverage (below 70%), type-check, build or mocked e2e fail
- THEN the gate exits non-zero and never prints `GATE OK`

#### Scenario: Dirty tree or focused test
- WHEN the tree is dirty THEN `dirty=yes` is reported
- AND a stray `test.only` fails the gate; a stale dev server on :3000 is refused

#### Scenario: Zero-`.env` checkout
- THEN the Postgres test script runs against the infra compose overlay without a `.env`

### Requirement: Test builders (4.1)

Backend tests MUST have `factory-boy` builders for `Usuario`, `Contrato`, `CuentaCobro`, `RequisitoCuenta` and `Evidencia` producing unique values across the suite.

#### Scenario: Cascade
- WHEN a `CuentaCobro` is built with default sub-factories
- THEN its `Contrato` and `Usuario` are persisted

### Requirement: Synthetic fixtures only (4.4)

E2E specs MUST NOT depend on `context/SYJ` or `context/DAGMA`. Fixtures under `e2e/fixtures/` MUST be structurally valid for their type (PDFs with a trailer unless flagged `corrupt`), enforced by a validity test and a client-data leak guard.

#### Scenario: Truncated PDF fixture
- WHEN a non-corrupt fixture lacks `%%EOF`
- THEN the validity test fails

### Requirement: Shared seed/auth helpers and layout (4.5, 4.7a)

Register/login/seed logic MUST live in shared `e2e/helpers`. Specs MUST be organized into `e2e/journey`, `e2e/edge-cases` and `e2e/smoke`, with the 429-retrying seeders. Local runs MAY disable the rate limiter only via opt-in `RATE_LIMIT_ENABLED=false` (default `True`).

#### Scenario: Default rate limit
- WHEN `RATE_LIMIT_ENABLED` is unset
- THEN the limiter is enabled

### Requirement: Edge-case suite (4.7b, 4.2)

The suite MUST cover double-submit, radicar-after-enviada, incomplete checklist, invalid evidence format, corrupt document, SECOP down, Google disconnected, malformed LLM response (isolated `FAKE_LLM_SCRIPT=malformed` run), mobile 390px and axe. Backend concurrency tests MUST cover double radicar, double create-same-month/credit debit, and double classification-job enqueue.

#### Scenario: Concurrent classification enqueue
- WHEN two enqueues race for one cuenta
- THEN one job runs and a first-ever phantom-insert conflict is retried once, then fails with a dedicated error

### Requirement: Production smoke is read-only (4.9)

The prod smoke suite MUST fail any non-GET request to a non-localhost host except login (same-origin) and token refresh, via a mechanical guard with its own tests. Without credentials, authenticated specs MUST skip with a clear message while `/health` still runs. Radicar, uploads, classification, credits, OAuth, Wompi and agent chat MUST NOT be exercised in production.

#### Scenario: Write attempt
- WHEN a test issues a POST to a remote host
- THEN the run fails naming method and URL

#### Scenario: Localhost base URL
- WHEN `PLAYWRIGHT_BASE_URL` is localhost or unset
- THEN the local `webServer` is booted

## Out of Scope (tracked follow-ups)

- Live-backend Playwright as `npm run gate -- --live-e2e` (runbook only); restoring GitHub Actions.
- Authenticated prod smoke run (needs user-provided URL/account).
- `assert_budget` floor mode; `LLM_PROVIDER=fake` local-env test-isolation gap; hardcoded `SYJ`/`DAGMA` entity-name literals.
