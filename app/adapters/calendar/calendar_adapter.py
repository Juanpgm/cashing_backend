"""Google Calendar adapter — reads events as contractual evidence.

Reuses GmailAdapter for OAuth credential management (same `Integracion` row), so
all Google Workspace adapters share one token store with auto-refresh. All
Google API calls run via run_in_executor to stay non-blocking.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from typing import Any

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.calendar.port import CalendarAttendee, CalendarEvent
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("adapters.calendar")


def _parse_event(raw: dict[str, Any]) -> CalendarEvent:
    """Map a raw Google Calendar event resource to the neutral `CalendarEvent`."""
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    start_dt = datetime.fromisoformat(start["dateTime"]) if "dateTime" in start else None
    end_dt = datetime.fromisoformat(end["dateTime"]) if "dateTime" in end else None
    start_date = date.fromisoformat(start["date"]) if "date" in start else None
    end_date = date.fromisoformat(end["date"]) if "date" in end else None
    organizer = raw.get("organizer") or {}
    attendees = [
        CalendarAttendee(
            email=a.get("email", ""),
            display_name=a.get("displayName", ""),
            response_status=a.get("responseStatus", ""),
            optional=a.get("optional", False),
            is_self=a.get("self", False),
        )
        for a in raw.get("attendees") or []
    ]
    return CalendarEvent(
        id=raw.get("id", ""),
        summary=raw.get("summary", "") or "",
        description=raw.get("description", "") or "",
        start=start_dt,
        end=end_dt,
        start_date=start_date,
        end_date=end_date,
        is_all_day=start_date is not None and start_dt is None,
        location=raw.get("location"),
        html_link=raw.get("htmlLink", "") or "",
        attendees=attendees,
        organizer_email=organizer.get("email"),
        event_type=raw.get("eventType") or "default",
    )


class GoogleCalendarAdapter:
    """Implements CalendarPort using the Google Calendar API v3 (DB-backed credentials)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._auth = GmailAdapter(db)  # credential management is shared

    def _build_service(self, creds):  # type: ignore[no-untyped-def]
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    async def search_events(
        self,
        usuario_id: uuid.UUID,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 50,
        q: str | None = None,
    ) -> list[CalendarEvent]:
        """List events in the RFC3339 time range for the user's connected account.

        Args:
            q: Optional free-text search query (Google Calendar API ``q`` parameter).
               Use to bias results toward obligation-related events.

        Returns a list of `CalendarEvent` instances sorted by start time.
        """
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _list() -> dict:  # type: ignore[type-arg]
            params: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": max_results,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if q:
                params["q"] = q
            return service.events().list(**params).execute()

        try:
            result = await loop.run_in_executor(None, _list)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Calendar", f"Error consultando eventos: {exc}") from exc
        raw_items: list[dict[str, Any]] = result.get("items", [])
        events: list[CalendarEvent] = []
        for item in raw_items:
            try:
                events.append(_parse_event(item))
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "calendar_event_parse_failed",
                    event_id=item.get("id"),
                    user_id=str(usuario_id),
                    error=str(exc),
                )
                continue
        logger.info("calendar_search", user_id=str(usuario_id), count=len(events), q=q)
        return events

    async def get_event(
        self,
        usuario_id: uuid.UUID,
        event_id: str,
        calendar_id: str = "primary",
    ) -> CalendarEvent:
        """Fetch a single Google Calendar event by its ID."""
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _get() -> dict:  # type: ignore[type-arg]
            return service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        raw = await loop.run_in_executor(None, _get)
        return _parse_event(raw)
