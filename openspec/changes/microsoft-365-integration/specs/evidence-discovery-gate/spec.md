# Evidence Discovery Provider-Agnostic Gate Specification

## Purpose

Generalizes `descubrir_evidencias`'s connection gate from hardcoded Google to
"at least one connected provider," and runs discovery across all connected
providers, merging results into a single `evidence_raw` collection.

## Requirements

### Requirement: Provider-agnostic connection gate

Non-`local_only` evidence discovery MUST succeed if the user has at least one
connected provider (google or microsoft). It MUST NOT require Google
specifically.
(Previously: gated exclusively on `google_workspace_service.get_integration_status`,
raising `GOOGLE_NOT_CONNECTED` for any non-Google-connected user.)

#### Scenario: User connected only to Microsoft

- GIVEN a user with a connected Microsoft account and no Google connection
- WHEN they run non-`local_only` evidence discovery
- THEN discovery proceeds without a not-connected error

#### Scenario: User connected to neither provider

- GIVEN a user with no connected providers
- WHEN they run non-`local_only` evidence discovery
- THEN discovery raises `NO_PROVIDER_CONNECTED`, listing available providers
  to connect

#### Scenario: `local_only` bypasses the gate

- GIVEN a user with no connected providers
- WHEN they run evidence discovery in `local_only` mode
- THEN discovery proceeds without the gate applying (unchanged behavior)

### Requirement: Discovery runs across all connected providers and merges results

When multiple providers are connected, discovery MUST run each connected
provider's adapters and merge results into a single `evidence_raw` collection.

#### Scenario: User connected to both Google and Microsoft

- GIVEN a user with both providers connected
- WHEN evidence discovery runs
- THEN `evidence_raw` contains items sourced from both providers

#### Scenario: Duplicate evidence across providers is deduplicated

- GIVEN the same underlying item surfaced by two providers
- WHEN discovery merges results
- THEN the existing dedup logic prevents a duplicate persisted evidence
  record

### Requirement: One provider's failure does not abort discovery for others

A failure in one connected provider's fetch MUST NOT prevent evidence
collected from other connected providers from being returned.

#### Scenario: Microsoft fails, Google succeeds

- GIVEN Microsoft Graph calls fail after exhausting retries while Google
  succeeds
- WHEN evidence discovery completes
- THEN Google-sourced evidence is returned and the Microsoft failure is
  surfaced as a partial-result signal, not a hard crash

## Deferred to sdd-design

- Exact merge/error-aggregation implementation and whether gate logic lives
  in the service layer or a graph node.
