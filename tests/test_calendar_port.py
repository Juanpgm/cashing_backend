"""Tests for the provider-neutral CalendarEvent/CalendarAttendee dataclasses."""

from __future__ import annotations

from datetime import date, datetime, timezone


def test_calendar_event_defaults():
    from app.adapters.calendar.port import CalendarEvent

    event = CalendarEvent(id="ev1")

    assert event.id == "ev1"
    assert event.summary == ""
    assert event.description == ""
    assert event.start is None
    assert event.end is None
    assert event.start_date is None
    assert event.end_date is None
    assert event.is_all_day is False
    assert event.location is None
    assert event.html_link == ""
    assert event.attendees == []
    assert event.organizer_email is None
    assert event.event_type == "default"


def test_calendar_event_carries_timed_fields():
    from app.adapters.calendar.port import CalendarAttendee, CalendarEvent

    start = datetime(2024, 4, 15, 9, 0, tzinfo=timezone.utc)
    end = datetime(2024, 4, 15, 10, 0, tzinfo=timezone.utc)
    event = CalendarEvent(
        id="ev1",
        summary="Reunión de seguimiento",
        description="Revisión de avances",
        start=start,
        end=end,
        html_link="https://calendar.google.com/event?eid=ev1",
        attendees=[CalendarAttendee(email="a@b.com", response_status="accepted", is_self=True)],
        organizer_email="organizer@entidad.gov.co",
        event_type="default",
    )

    assert event.start == start
    assert event.end == end
    assert event.attendees[0].email == "a@b.com"
    assert event.attendees[0].response_status == "accepted"
    assert event.attendees[0].is_self is True
    assert event.organizer_email == "organizer@entidad.gov.co"


def test_calendar_event_carries_all_day_fields():
    from app.adapters.calendar.port import CalendarEvent

    event = CalendarEvent(
        id="ev2",
        summary="Día festivo",
        start_date=date(2024, 4, 19),
        end_date=date(2024, 4, 20),
        is_all_day=True,
    )

    assert event.start is None
    assert event.start_date == date(2024, 4, 19)
    assert event.is_all_day is True


def test_calendar_attendee_defaults():
    from app.adapters.calendar.port import CalendarAttendee

    attendee = CalendarAttendee()

    assert attendee.email == ""
    assert attendee.display_name == ""
    assert attendee.response_status == ""
    assert attendee.optional is False
    assert attendee.is_self is False
