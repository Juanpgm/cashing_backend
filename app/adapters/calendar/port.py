"""Calendar port — interface for Google Calendar operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol


@dataclass
class CalendarAttendee:
    """Provider-neutral attendee of a `CalendarEvent`."""

    email: str = ""
    display_name: str = ""
    response_status: str = ""  # Google responseStatus / Graph status.response
    optional: bool = False
    is_self: bool = False  # ponytail: Google-only "self" flag; Graph has no equivalent (defaults False)


@dataclass
class CalendarEvent:
    """Provider-neutral calendar event, mirrors the `DriveFile` dataclass pattern.

    Carries every field the existing Google-specific raw-dict readers touch
    (calendar_fetch node, `test_calendar` route, attendance metadata extraction),
    so any provider adapter can implement `CalendarPort` without leaking its
    response shape to callers.
    """

    id: str
    summary: str = ""  # "" when the provider omits it; callers default their own placeholder text
    description: str = ""
    start: datetime | None = None  # timed events: aware datetime
    end: datetime | None = None
    start_date: date | None = None  # all-day events: no time component
    end_date: date | None = None
    is_all_day: bool = False  # True when only the date (no time) was present
    location: str | None = None
    html_link: str = ""  # Google htmlLink / Graph webLink
    attendees: list[CalendarAttendee] = field(default_factory=list)
    organizer_email: str | None = None
    event_type: str = "default"  # Google eventType; Graph has no equivalent -> "default"


class CalendarPort(Protocol):
    """Abstract interface for calendar event reading (DB-backed credentials)."""

    async def search_events(
        self,
        usuario_id: uuid.UUID,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 50,
        q: str | None = None,
    ) -> list[CalendarEvent]:
        """List calendar events in the given RFC3339 time range.

        Args:
            q: Optional free-text search query to bias results.

        Returns a list of provider-neutral `CalendarEvent` instances sorted by start time.
        """
        ...

    async def get_event(
        self,
        usuario_id: uuid.UUID,
        event_id: str,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """Fetch a single calendar event by ID."""
        ...
