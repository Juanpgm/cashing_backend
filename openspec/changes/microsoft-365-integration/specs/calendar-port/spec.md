# Calendar Port Generalization Specification

## Purpose

Generalizes `CalendarPort` to return a neutral `CalendarEvent` dataclass
instead of raw Google JSON, mirroring the existing `DriveFile` pattern, so any
provider adapter can implement it without leaking Google's response shape.

## Requirements

### Requirement: `CalendarPort.list_events` returns a provider-neutral `CalendarEvent`

`CalendarPort` implementations MUST return `CalendarEvent` instances with
normalized fields (id, title, start, end, attendees, is_all_day,
attendance/response status, source metadata) instead of raw provider JSON.
Callers MUST access these via attributes, not provider-specific dict keys.

#### Scenario: Google adapter returns normalized events

- GIVEN `GoogleCalendarAdapter.list_events` is called
- WHEN it returns results
- THEN each result is a `CalendarEvent` instance, not a raw Google JSON dict

#### Scenario: Calendar fetch node reads normalized fields

- GIVEN the calendar fetch node receives a list of `CalendarEvent`
- WHEN it reads event data
- THEN it accesses `event.start`, `event.is_all_day`, etc., instead of
  `ev["start"]["dateTime"]`

### Requirement: Existing Google calendar behavior is preserved

This is a behavior-preserving refactor: filtering and noise-detection outcomes
for Google calendar events MUST NOT change as a result of the adapter/format
change.
(Previously: `CalendarPort` returned raw Google API JSON dicts consumed
directly by the fetch node via provider-specific keys.)

#### Scenario: Existing Google noise-detection tests unaffected

- GIVEN the existing Google calendar noise-detection test cases
- WHEN they run against the refactored adapter and port
- THEN every test produces the same pass/fail outcome as before the refactor

## Deferred to sdd-design

- Exact `CalendarEvent` field list, types, and dataclass location.
