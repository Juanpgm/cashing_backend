"""Tests for GET /integraciones/calendar/test and /integraciones/drive/test.

Both routes previously read raw Google JSON dicts; they now consume the
provider-neutral CalendarEvent/DriveFile dataclasses via DriveQuery.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.calendar.port import CalendarEvent
from app.adapters.drive.port import DriveFile


@pytest.mark.asyncio
async def test_calendar_test_route_maps_calendar_event(client, test_user):
    event = CalendarEvent(
        id="ev1",
        summary="Reunión de seguimiento",
        start=datetime.fromisoformat("2024-04-15T09:00:00-05:00"),
        end=datetime.fromisoformat("2024-04-15T10:00:00-05:00"),
        location="Sala 1",
        html_link="https://calendar.google.com/event?eid=ev1",
    )

    with patch(
        "app.adapters.calendar.calendar_adapter.GoogleCalendarAdapter.search_events",
        AsyncMock(return_value=[event]),
    ):
        resp = await client.get(
            "/api/v1/integraciones/calendar/test",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["events"][0]
    assert item["summary"] == "Reunión de seguimiento"
    assert item["start"] == "2024-04-15T09:00:00-05:00"
    assert item["location"] == "Sala 1"
    assert item["html_link"].startswith("https://calendar.google.com")


@pytest.mark.asyncio
async def test_calendar_test_route_defaults_missing_summary(client, test_user):
    event = CalendarEvent(id="ev2", html_link="https://calendar.google.com/event?eid=ev2")

    with patch(
        "app.adapters.calendar.calendar_adapter.GoogleCalendarAdapter.search_events",
        AsyncMock(return_value=[event]),
    ):
        resp = await client.get(
            "/api/v1/integraciones/calendar/test",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    item = resp.json()["events"][0]
    assert item["summary"] == "(sin título)"
    assert item["start"] == ""


@pytest.mark.asyncio
async def test_drive_test_route_maps_drive_file(client, test_user):
    now = datetime(2024, 4, 12, tzinfo=None)
    drive_file = DriveFile(
        id="f1",
        name="Informe.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        created_at=now,
        modified_at=now,
        web_view_link="https://drive.google.com/file/d/f1/view",
    )

    with patch(
        "app.adapters.drive.drive_adapter.DriveAdapter.search_files",
        AsyncMock(return_value=[drive_file]),
    ) as mock_search:
        resp = await client.get(
            "/api/v1/integraciones/drive/test",
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["files"][0]["id"] == "f1"

    # The probe passes a DriveQuery object, not a raw Google query string.
    from app.adapters.drive.port import DriveQuery

    _, kwargs = mock_search.call_args
    assert isinstance(kwargs["query"], DriveQuery)
    assert kwargs["query"].keywords == []
