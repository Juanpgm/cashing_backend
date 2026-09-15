"""Network-failure-injection tests for GoogleCalendarAdapter — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md ~L222).

`search_events` already wraps `GoogleHttpError` AND skips malformed items
per-item (the reference pattern the Gmail/Drive fixes in this slice mirror).
`get_event` had NO error wrapping at all — bug fixed here, matching
`search_events`'s existing transport-error + malformed-payload handling.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import httplib2
import pytest
from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
from app.core.exceptions import ExternalServiceError
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError as GoogleHttpError


def _http_error(status: int, message: str = "error", uri: str | None = None) -> GoogleHttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps({"error": {"message": message, "code": status}}).encode()
    return GoogleHttpError(resp=resp, content=content, uri=uri)


def _make_adapter(service: MagicMock) -> GoogleCalendarAdapter:
    adapter = GoogleCalendarAdapter.__new__(GoogleCalendarAdapter)
    adapter._auth = MagicMock()

    async def _get_credentials(*_args: object, **_kwargs: object) -> MagicMock:
        return MagicMock()

    adapter._auth.get_credentials = _get_credentials
    adapter._build_service = MagicMock(return_value=service)
    return adapter


class TestGetEventFailures:
    @pytest.mark.asyncio
    async def test_401_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_403_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = _http_error(403, "forbidden")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_404_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = _http_error(404, "not found")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_5xx_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = _http_error(500, "boom")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = TimeoutError("socket timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_transport_os_error_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = OSError("connection reset")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_dns_failure_is_wrapped(self) -> None:
        """httplib2.ServerNotFoundError is NOT an OSError subclass — review r1
        finding P2b."""
        service = MagicMock()
        service.events().get().execute.side_effect = httplib2.ServerNotFoundError("Unable to find server")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_lazy_token_refresh_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().get().execute.side_effect = RefreshError("token expired")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_malformed_response_is_wrapped_not_raw(self) -> None:
        """A malformed dateTime string must not raise a raw ValueError from
        `datetime.fromisoformat` inside `_parse_event`."""
        service = MagicMock()
        service.events().get().execute.return_value = {
            "id": "ev1",
            "start": {"dateTime": "not-a-real-datetime"},
            "end": {},
        }
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_event(uuid.uuid4(), "ev1")

    @pytest.mark.asyncio
    async def test_well_formed_response_still_parses_successfully(self) -> None:
        service = MagicMock()
        service.events().get().execute.return_value = {
            "id": "ev1",
            "summary": "Reunión",
            "htmlLink": "https://calendar.google.com/event?eid=ev1",
        }
        adapter = _make_adapter(service)

        result = await adapter.get_event(uuid.uuid4(), "ev1")

        assert result.id == "ev1"
        assert result.summary == "Reunión"


class TestUserFacingMessageDoesNotLeakRawSdkText:
    """`str(GoogleHttpError)` includes the FULL request URI — Calendar search
    `q=` terms — and `domain_error_handler` returns `ExternalServiceError.detail`
    to the client VERBATIM, no redaction. Review r1 finding P3a."""

    @pytest.mark.asyncio
    async def test_http_error_message_does_not_leak_search_query_or_uri(self) -> None:
        service = MagicMock()
        sensitive_query = "reunion-confidencial-cliente-x"
        service.events().list().execute.side_effect = _http_error(
            500,
            "boom",
            uri=f"https://www.googleapis.com/calendar/v3/calendars/primary/events?q={sensitive_query}",
        )
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError) as exc_info:
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z", q=sensitive_query)

        detail = str(exc_info.value)
        assert "http" not in detail.lower()
        assert "googleapis" not in detail.lower()
        assert "q=" not in detail
        assert sensitive_query not in detail
        # NOT the exception class name here on purpose: GoogleHttpError's class
        # name is literally "HttpError", which would reintroduce the "http"
        # substring this test forbids.
        assert "Google Calendar no está disponible" in detail

    @pytest.mark.asyncio
    async def test_transport_error_message_does_not_leak_raw_exception_text(self) -> None:
        service = MagicMock()
        service.events().list().execute.side_effect = RefreshError(
            "refresh failed for internal-service-account@project.iam.gserviceaccount.com"
        )
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError) as exc_info:
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

        detail = str(exc_info.value)
        assert "internal-service-account" not in detail
        assert "RefreshError" in detail


class TestSearchEventsFailureMatrixRegression:
    """Re-verifies the existing behavior search_events already had (reference
    pattern) — transport errors and per-item malformed tolerance."""

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        """Previously a documented gap (`search_events` only caught
        `GoogleHttpError`, symmetric to Gmail's `search_messages`) — fixed in
        review r1 finding P2b via the shared `GOOGLE_TRANSPORT_ERRORS` tuple."""
        service = MagicMock()
        service.events().list().execute.side_effect = TimeoutError("timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

    @pytest.mark.asyncio
    async def test_dns_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().list().execute.side_effect = httplib2.ServerNotFoundError("Unable to find server")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

    @pytest.mark.asyncio
    async def test_lazy_token_refresh_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().list().execute.side_effect = RefreshError("token expired")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

    @pytest.mark.asyncio
    async def test_401_is_wrapped(self) -> None:
        service = MagicMock()
        service.events().list().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

    @pytest.mark.asyncio
    async def test_malformed_item_is_skipped_not_raised(self) -> None:
        service = MagicMock()
        service.events().list().execute.return_value = {
            "items": [
                {"id": "ev1", "summary": "ok"},
                {"id": "ev2", "start": {"dateTime": "bad-date"}},
            ]
        }
        adapter = _make_adapter(service)

        result = await adapter.search_events(uuid.uuid4(), "2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z")

        assert len(result) == 1
        assert result[0].id == "ev1"
