"""Tests for the DB-backed Google Calendar adapter (evidence from events)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_calendar_service(items: list[dict]):
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {"items": items}
    return service


@pytest.mark.asyncio
async def test_search_events_returns_items():
    """search_events loads the user's credentials and returns Calendar event resources."""
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter

    items = [
        {
            "id": "ev1",
            "summary": "Reunión de seguimiento contrato",
            "htmlLink": "https://calendar.google.com/event?eid=ev1",
            "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        },
    ]
    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_calendar_service(items)):
        events = await adapter.search_events(
            uuid.uuid4(),
            time_min="2024-04-01T00:00:00Z",
            time_max="2024-04-30T23:59:59Z",
        )

    assert len(events) == 1
    assert events[0].summary.startswith("Reunión")
    assert events[0].html_link.startswith("https://calendar.google.com")
    assert events[0].start.isoformat() == "2024-04-15T09:00:00-05:00"


@pytest.mark.asyncio
async def test_search_events_passes_time_range():
    """The RFC3339 time window and singleEvents flag are forwarded to the API."""
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter

    service = _fake_calendar_service([])
    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=service):
        await adapter.search_events(
            uuid.uuid4(),
            time_min="2024-04-01T00:00:00Z",
            time_max="2024-04-30T23:59:59Z",
            max_results=25,
        )

    _, kwargs = service.events.return_value.list.call_args
    assert kwargs["timeMin"] == "2024-04-01T00:00:00Z"
    assert kwargs["timeMax"] == "2024-04-30T23:59:59Z"
    assert kwargs["singleEvents"] is True
    assert kwargs["maxResults"] == 25


@pytest.mark.asyncio
async def test_search_events_empty():
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter

    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_calendar_service([])):
        events = await adapter.search_events(
            uuid.uuid4(), time_min="2024-04-01T00:00:00Z", time_max="2024-04-30T23:59:59Z"
        )

    assert events == []


@pytest.mark.asyncio
async def test_search_events_maps_all_day_event():
    """An event with only a ``date`` (no ``dateTime``) maps to CalendarEvent.is_all_day."""
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter

    items = [{"id": "ev2", "summary": "Día festivo", "start": {"date": "2024-04-19"}, "end": {"date": "2024-04-20"}}]
    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_calendar_service(items)):
        events = await adapter.search_events(
            uuid.uuid4(), time_min="2024-04-01T00:00:00Z", time_max="2024-04-30T23:59:59Z"
        )

    assert events[0].is_all_day is True
    assert events[0].start is None
    assert events[0].start_date.isoformat() == "2024-04-19"


@pytest.mark.asyncio
async def test_search_events_maps_attendees_and_organizer():
    """Attendee ``self``/``responseStatus`` and organizer email map onto CalendarEvent."""
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter

    items = [
        {
            "id": "ev1",
            "summary": "Reunión",
            "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
            "attendees": [
                {"self": True, "responseStatus": "declined"},
                {"self": False, "email": "supervisor@entidad.gov.co", "responseStatus": "accepted"},
            ],
            "organizer": {"email": "organizador@entidad.gov.co"},
            "eventType": "default",
        }
    ]
    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_calendar_service(items)):
        events = await adapter.search_events(
            uuid.uuid4(), time_min="2024-04-01T00:00:00Z", time_max="2024-04-30T23:59:59Z"
        )

    event = events[0]
    assert event.attendees[0].is_self is True
    assert event.attendees[0].response_status == "declined"
    assert event.attendees[1].email == "supervisor@entidad.gov.co"
    assert event.organizer_email == "organizador@entidad.gov.co"
    assert event.event_type == "default"


@pytest.mark.asyncio
async def test_get_event_returns_calendar_event():
    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
    from app.adapters.calendar.port import CalendarEvent

    service = MagicMock()
    service.events.return_value.get.return_value.execute.return_value = {
        "id": "ev1",
        "summary": "Reunión",
        "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        "htmlLink": "https://calendar.google.com/event?eid=ev1",
    }
    adapter = GoogleCalendarAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=service):
        event = await adapter.get_event(uuid.uuid4(), event_id="ev1")

    assert isinstance(event, CalendarEvent)
    assert event.id == "ev1"
    assert event.html_link.startswith("https://calendar.google.com")
