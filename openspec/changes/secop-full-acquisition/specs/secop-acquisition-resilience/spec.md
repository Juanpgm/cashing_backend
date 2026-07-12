# SECOP Acquisition Resilience Specification

## Purpose

Hardens SECOP II Socrata acquisition against transient failures: bounded
retry/backoff on 429/5xx, non-crashing surfacing of a missing
`SECOP_APP_TOKEN`, and preservation of existing partial-result/cache-safety
guarantees as new datasets and the scraper are wired in.

## Requirements

### Requirement: Bounded retry with backoff and jitter on 429/5xx

Socrata queries (`_query_socrata` and its callers) MUST retry on HTTP 429 and
5xx responses using bounded exponential backoff with jitter, up to a fixed
maximum attempt count, before treating the call as failed.

#### Scenario: Transient 429 recovers on retry

- GIVEN Socrata returns 429 on the first attempt and 200 on the second
- WHEN the query executes
- THEN the retry succeeds and the caller receives the data with no error
  surfaced

#### Scenario: Retries exhausted

- GIVEN Socrata returns 429 or 5xx on every attempt up to the maximum
- WHEN retries are exhausted
- THEN the call fails as `ExternalServiceError` (or, in fan-out contexts,
  contributes the dataset id to `datasets_con_error`) rather than hanging or
  crashing the request

### Requirement: Missing SECOP_APP_TOKEN is a warning, not a crash

The system MUST detect an empty/missing `SECOP_APP_TOKEN` and surface it as a
health/config warning; it MUST NOT prevent the application from starting or
turn into an unhandled exception at request time.

#### Scenario: Token unset at startup

- GIVEN `SECOP_APP_TOKEN` is empty
- WHEN the application starts
- THEN it starts successfully and a config/health warning is available

#### Scenario: Token unset causes an auth failure at request time

- GIVEN `SECOP_APP_TOKEN` is empty and Socrata rejects the unauthenticated
  request
- WHEN a SECOP query is made
- THEN the resulting error message references the missing token as the likely
  cause, instead of an opaque HTTP error

### Requirement: Partial-result semantics via `datasets_con_error` preserved

Adding new datasets and retry logic MUST NOT change the existing contract: a
fan-out query MUST still return whatever datasets succeeded and list the
failed ones in `datasets_con_error`, never fail the whole request because one
dataset errored.

#### Scenario: One of several datasets fails after exhausting retries

- GIVEN a fan-out across multiple datasets where one fails all retries
- WHEN the fan-out completes
- THEN results from the successful datasets are returned and the failed
  dataset's id appears in `datasets_con_error`

### Requirement: Cache is never poisoned by a failed refresh

A refresh attempt that fails or returns zero rows (whether due to exhausted
retries or a genuine empty result) MUST NOT delete or blank out previously
cached rows.

#### Scenario: Refresh fails after retries exhausted

- GIVEN previously cached SECOP rows exist for a contract
- WHEN a refresh attempt exhausts retries and fails
- THEN the previously cached rows remain intact and are returned to the caller

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `verificar_configuracion_secop` | read | Surfaces `SECOP_APP_TOKEN` presence/health warning for support/diagnostics |

## Error Codes

No new domain error code for retry exhaustion — reuses existing
`ExternalServiceError`. The token/health warning is informational, not an
exception.

## Deferred to sdd-design

- Exact backoff schedule (base delay, max attempts, jitter algorithm).
- Where the config/health warning surfaces (dedicated endpoint vs. existing
  health check).
