# External Adapter Resilience Specification

Retroactive spec (slice 4.3, PR #92, plus 2.8). Applies to Gmail, Drive, Calendar and the LLM adapter.

## Purpose

Failures of external services are mapped to stable domain errors with sanitized, actionable Spanish messages, and never abort unrelated work.

## Requirements

### Requirement: Uniform error mapping across Google adapters (4.3)

Every Gmail, Drive and Calendar adapter method MUST map SDK errors and transport errors (`GOOGLE_TRANSPORT_ERRORS`, including non-`OSError` ones) to `ExternalServiceError` (502). The Drive adapter MUST apply this to all methods, not only search.

#### Scenario: 5xx / timeout / DNS failure
- WHEN any adapter method hits a 5xx, a timeout or a non-`OSError` transport error
- THEN a 502 `ExternalServiceError` is raised, not a raw SDK exception

#### Scenario: 4xx
- WHEN Google returns 403 or 404
- THEN the message includes the HTTP status and a per-class hint

### Requirement: Revoked grants are distinguished (4.3)

`RefreshError` MUST be handled before the transport tuple and MUST yield `GOOGLE_REAUTH_REQUIRED` (502) with a reconnect message. A test MUST pin that `RefreshError` is not in the transport tuple.

#### Scenario: Revoked token
- WHEN the refresh is rejected
- THEN `GOOGLE_REAUTH_REQUIRED` is returned, not a transient-outage message

### Requirement: Malformed payloads never leak or crash (4.3)

Gmail parsing MUST guard missing keys, malformed base64 and `"data": null`. One malformed message MUST be skipped and logged (with an aggregate warning) without dropping the batch. User-facing details MUST NOT contain raw SDK text or request URIs; raw text goes to structured logs only.

#### Scenario: One bad message in a batch
- GIVEN 10 messages, one malformed
- THEN 9 are returned and a warning is logged

#### Scenario: Malformed attachment
- WHEN attachment data is invalid base64 or null
- THEN a mapped domain error is raised, not `binascii.Error`/`TypeError`

### Requirement: LLM failure chain (4.3, 1.8)

The LLM adapter MUST walk its 3-model fallback chain on real `litellm` timeout, 4xx, 5xx and malformed-response errors, recording fallback depth. Raw LLM exception text MUST NOT reach a user-facing 422.

#### Scenario: Primary model 5xx
- WHEN the first model fails
- THEN the next is tried and `fallback_depth` reflects it

#### Scenario: All models fail
- THEN one sanitized domain error is returned within the turn timeout

### Requirement: Evidence discovery fails open (4.3)

Evidence discovery MUST continue with the providers that succeed when one fails.

#### Scenario: Gmail down, Drive up
- THEN Drive evidence is still returned

## Out of Scope (tracked follow-ups)

- Gmail 429 backoff for transient transport errors; surfacing `GOOGLE_REAUTH_REQUIRED` on `POST /evidencias/descubrir`.
- `domain_error_handler` returning `detail` verbatim in production; Gmail dict-payload guards at 2 sites.
- Credit refund path for failed `consumes_credits` tools.
