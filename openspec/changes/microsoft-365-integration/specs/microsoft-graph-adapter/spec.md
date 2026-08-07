# Microsoft Graph Adapter Specification

## Purpose

`MicrosoftGraphAdapter`(s) implement `EmailPort`, `DrivePort`, and
`CalendarPort` against Microsoft Graph (Outlook Mail, OneDrive, Outlook
Calendar), using per-`(usuario_id, provider=microsoft)` tokens from
`integraciones`.

## Requirements

### Requirement: `MicrosoftGraphAdapter` implements `EmailPort` via `/me/messages`

The adapter MUST implement `search_messages` against Graph `/me/messages`,
using auto-refreshing tokens scoped to `(usuario_id, provider=microsoft)`, and
MUST run Graph calls via `run_in_executor` (no synchronous Graph call blocks
the event loop).

#### Scenario: Connected Microsoft user fetches evidence email

- GIVEN a user with a connected Microsoft account
- WHEN evidence discovery calls the adapter's `search_messages`
- THEN normalized email results are returned without blocking the event loop

#### Scenario: Expired access token is refreshed transparently

- GIVEN the stored Microsoft access token is expired but the refresh token is
  valid
- WHEN the adapter makes a Graph call
- THEN it refreshes the token and completes the call without surfacing an
  error to the caller

### Requirement: `MicrosoftGraphAdapter` implements `DrivePort` via `/me/drive`

The adapter MUST implement `search_files` against Graph `/me/drive` using the
generalized query object (see Drive Port Generalization spec), and MUST
implement `upload_file`, `get_or_create_folder`, and `make_shareable` against
their OneDrive equivalents.

#### Scenario: OneDrive search uses the same query object contract as Google

- GIVEN an obligation-scoped query object
- WHEN it is passed to the Microsoft adapter's `search_files`
- THEN matching OneDrive files are returned using the same contract as the
  Google adapter

### Requirement: `MicrosoftGraphAdapter` implements `CalendarPort` via `/me/events`

The adapter MUST implement `list_events` against Graph `/me/events`, mapping
results into `CalendarEvent` (see Calendar Port Generalization spec).

#### Scenario: Connected Microsoft user's calendar events are fetched

- GIVEN a user with a connected Microsoft account and calendar events in the
  requested window
- WHEN the adapter's `list_events` is called
- THEN it returns `CalendarEvent` instances equivalent in shape to the Google
  adapter's output

### Requirement: Graph throttling does not crash evidence discovery

The adapter MUST retry on HTTP 429 with bounded backoff honoring
`Retry-After`, and MUST bound pagination (no unbounded page walk).

#### Scenario: Transient 429 recovers on retry

- GIVEN Graph returns 429 with a `Retry-After` header on the first attempt
- WHEN the adapter retries within its retry budget
- THEN the call succeeds and no error is surfaced to the caller

#### Scenario: Retry budget exhausted

- GIVEN Graph keeps returning 429 past the retry budget
- WHEN the call ultimately fails
- THEN it fails as a scoped error for that provider without crashing the
  overall evidence discovery run or blocking other connected providers

## Deferred to sdd-design

- Exact retry/backoff parameters, executor wrapping mechanics, and Graph SDK
  vs. raw HTTP client choice.
