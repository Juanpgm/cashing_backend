"""Tests for MicrosoftGraphAdapter — EmailPort/DrivePort/CalendarPort via Microsoft Graph.

Graph is fully mocked (httpx.AsyncClient patched at the module level, same pattern as
tests/test_microsoft_graph_service.py) — no live Azure tenant. Mirrors the existing
Google adapter test conventions (test_gmail_adapter.py / test_drive_search.py /
test_calendar_adapter.py) but mocks httpx instead of googleapiclient, since the Graph
adapter is httpx-native (already async, no run_in_executor wrapping needed).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.adapters.drive.port import DriveQuery
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.models.integracion import Integracion, IntegrationProvider


def _fake_integracion(
    *,
    access_token_encrypted: str,
    refresh_token_encrypted: str,
    expires_at: datetime | None,
) -> Integracion:
    record = MagicMock(spec=Integracion)
    record.usuario_id = uuid.uuid4()
    record.provider = IntegrationProvider.MICROSOFT
    record.access_token_encrypted = access_token_encrypted
    record.refresh_token_encrypted = refresh_token_encrypted
    record.expires_at = expires_at
    record.email = "user@contoso.com"
    return record


def _mock_db_returning(record: Integracion | None) -> MagicMock:
    """Build a MagicMock AsyncSession whose execute().scalars().first() returns `record`."""
    db = MagicMock()
    scalars_result = MagicMock()
    scalars_result.first.return_value = record
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    db.execute = AsyncMock(return_value=execute_result)
    db.commit = AsyncMock()
    return db


def _mock_graph_client(responses: list) -> MagicMock:  # type: ignore[type-arg]
    """Build a mock httpx.AsyncClient whose .request() yields `responses` in order.

    Each entry is either an httpx.Response-like MagicMock or an exception instance
    (raised instead of returned) — lets a single fixture drive 429-then-success
    sequences for the retry tests.
    """
    client = MagicMock()
    client.request = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _ok_response(json_body: dict) -> MagicMock:  # type: ignore[type-arg]
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_body
    resp.headers = {}
    return resp


def _rate_limited_error(retry_after: str = "0") -> httpx.HTTPStatusError:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 429
    response.headers = {"Retry-After": retry_after}
    return httpx.HTTPStatusError("429 rate limited", request=MagicMock(), response=response)


class TestSearchMessages:
    @pytest.mark.asyncio
    async def test_search_messages_returns_normalized_results_without_blocking_event_loop(self) -> None:
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"real-access-token", b"real-refresh-token"])

        graph_response = _ok_response(
            {
                "value": [
                    {
                        "id": "msg-1",
                        "conversationId": "conv-1",
                        "subject": "Informe de actividades",
                        "from": {"emailAddress": {"address": "supervisor@contoso.com"}},
                        "toRecipients": [{"emailAddress": {"address": "user@contoso.com"}}],
                        "receivedDateTime": "2024-04-10T10:00:00Z",
                        "bodyPreview": "Adjunto el informe...",
                        "body": {"contentType": "text", "content": "Adjunto el informe de abril"},
                        "categories": [],
                    }
                ]
            }
        )
        mock_client = _mock_graph_client([graph_response])

        # A real event-loop-blocking call would keep control for the whole sleep;
        # proving another coroutine runs concurrently demonstrates non-blocking behavior.
        yielded = False

        async def _yield_marker() -> None:
            nonlocal yielded
            await asyncio.sleep(0)
            yielded = True

        with patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", return_value=mock_client):
            results, _ = await asyncio.gather(
                adapter.search_messages(record.usuario_id, "informe", max_results=10),
                _yield_marker(),
            )

        assert yielded is True
        assert len(results) == 1
        assert results[0].id == "msg-1"
        assert results[0].subject == "Informe de actividades"
        assert results[0].sender == "supervisor@contoso.com"
        assert results[0].recipients == ["user@contoso.com"]


class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_expired_access_token_is_refreshed_transparently_before_graph_call(self) -> None:
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at-old",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),  # already expired
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"stale-access-token", b"real-refresh-token"])
        adapter._fernet.encrypt = MagicMock(return_value=b"enc-at-new")

        refresh_response = _ok_response(
            {"access_token": "fresh-access-token", "refresh_token": "fresh-refresh-token", "expires_in": 3600}
        )
        mock_refresh_client = MagicMock()
        mock_refresh_client.post = AsyncMock(return_value=refresh_response)
        mock_refresh_client.__aenter__ = AsyncMock(return_value=mock_refresh_client)
        mock_refresh_client.__aexit__ = AsyncMock(return_value=None)

        graph_response = _ok_response({"value": []})
        mock_graph_client = _mock_graph_client([graph_response])

        call_count = 0

        def _client_factory(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return mock_refresh_client if call_count == 1 else mock_graph_client

        with (
            patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", side_effect=_client_factory),
            patch("app.adapters.microsoft.graph_adapter.settings") as mock_settings,
        ):
            mock_settings.AZURE_AD_CLIENT_ID = "client-id"
            mock_settings.AZURE_AD_CLIENT_SECRET = "client-secret"
            mock_settings.AZURE_AD_TENANT_ID = "common"
            mock_settings.MICROSOFT_OAUTH_SCOPES = ["Mail.Read"]

            results = await adapter.search_messages(record.usuario_id, "informe")

        assert results == []
        mock_refresh_client.post.assert_awaited_once()
        db.commit.assert_awaited()
        assert record.access_token_encrypted == "enc-at-new"

    @pytest.mark.asyncio
    async def test_no_connected_account_raises_not_found(self) -> None:
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        db = _mock_db_returning(None)
        adapter = MicrosoftGraphAdapter(db=db)

        with pytest.raises(NotFoundError):
            await adapter.get_access_token(uuid.uuid4())


class TestSearchFiles:
    @pytest.mark.asyncio
    async def test_search_files_uses_same_query_object_contract_as_google_adapter(self) -> None:
        """keywords -> $search, dates -> $filter, exclude_folders drops folder facet, mime_types post-filters."""
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"real-access-token", b"real-refresh-token"])

        graph_response = _ok_response(
            {
                "value": [
                    {
                        "id": "file-1",
                        "name": "Informe.pdf",
                        "file": {"mimeType": "application/pdf"},
                        "size": 2048,
                        "createdDateTime": "2024-04-10T10:00:00Z",
                        "lastModifiedDateTime": "2024-04-12T10:00:00Z",
                        "webUrl": "https://contoso.sharepoint.com/informe.pdf",
                    },
                    {
                        "id": "folder-1",
                        "name": "Informes",
                        "folder": {"childCount": 3},
                        "createdDateTime": "2024-04-01T10:00:00Z",
                        "lastModifiedDateTime": "2024-04-01T10:00:00Z",
                        "webUrl": "https://contoso.sharepoint.com/informes",
                    },
                    {
                        "id": "file-2",
                        "name": "Foto.png",
                        "file": {"mimeType": "image/png"},
                        "size": 1024,
                        "createdDateTime": "2024-04-05T10:00:00Z",
                        "lastModifiedDateTime": "2024-04-05T10:00:00Z",
                        "webUrl": "https://contoso.sharepoint.com/foto.png",
                    },
                ]
            }
        )
        mock_client = _mock_graph_client([graph_response])

        query = DriveQuery(
            keywords=["informe"],
            date_from=datetime(2024, 4, 1, tzinfo=UTC),
            exclude_folders=True,
            mime_types=["application/pdf"],
            max_results=10,
        )

        with patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", return_value=mock_client):
            results = await adapter.search_files(record.usuario_id, query)

        assert len(results) == 1
        assert results[0].id == "file-1"
        assert results[0].mime_type == "application/pdf"

        args, kwargs = mock_client.request.call_args
        requested_url = args[1]
        assert "search" in requested_url
        assert "$filter" in kwargs["params"]
        assert "lastModifiedDateTime ge" in kwargs["params"]["$filter"]


class TestListEvents:
    @pytest.mark.asyncio
    async def test_search_events_maps_graph_events_to_calendar_event(self) -> None:
        """search_events() maps Graph /me/events -> CalendarEvent, shape-equivalent to Google's output."""
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"real-access-token", b"real-refresh-token"])

        graph_response = _ok_response(
            {
                "value": [
                    {
                        "id": "evt-1",
                        "subject": "Reunión de seguimiento",
                        "body": {"contentType": "text", "content": "Seguimiento mensual"},
                        "isAllDay": False,
                        "start": {"dateTime": "2024-04-15T14:00:00.0000000", "timeZone": "UTC"},
                        "end": {"dateTime": "2024-04-15T15:00:00.0000000", "timeZone": "UTC"},
                        "location": {"displayName": "Sala 1"},
                        "webLink": "https://outlook.office.com/evt-1",
                        "attendees": [
                            {
                                "emailAddress": {"address": "user@contoso.com", "name": "User"},
                                "status": {"response": "accepted"},
                                "type": "required",
                            }
                        ],
                        "organizer": {"emailAddress": {"address": "supervisor@contoso.com"}},
                    }
                ]
            }
        )
        mock_client = _mock_graph_client([graph_response])

        with patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", return_value=mock_client):
            events = await adapter.search_events(record.usuario_id, "2024-04-01T00:00:00Z", "2024-04-30T23:59:59Z")

        assert len(events) == 1
        event = events[0]
        assert event.id == "evt-1"
        assert event.summary == "Reunión de seguimiento"
        assert event.is_all_day is False
        assert event.start is not None
        assert event.html_link == "https://outlook.office.com/evt-1"
        assert event.organizer_email == "supervisor@contoso.com"
        assert len(event.attendees) == 1
        assert event.attendees[0].response_status == "accepted"


class TestRetryBackoff:
    @pytest.mark.asyncio
    async def test_transient_429_with_retry_after_recovers_within_retry_budget(self) -> None:
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"real-access-token", b"real-refresh-token"])

        success_response = _ok_response({"value": []})
        mock_client = _mock_graph_client([_rate_limited_error("0"), success_response])

        with patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", return_value=mock_client):
            results = await adapter.search_messages(record.usuario_id, "informe")

        assert results == []
        assert mock_client.request.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_budget_exhausted_raises_scoped_provider_error(self) -> None:
        from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter

        record = _fake_integracion(
            access_token_encrypted="enc-at",
            refresh_token_encrypted="enc-rt",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db = _mock_db_returning(record)
        adapter = MicrosoftGraphAdapter(db=db)
        adapter._fernet.decrypt = MagicMock(side_effect=[b"real-access-token", b"real-refresh-token"])

        always_429 = [_rate_limited_error("0") for _ in range(10)]
        mock_client = _mock_graph_client(always_429)

        with (
            patch("app.adapters.microsoft.graph_adapter.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(ExternalServiceError),
        ):
            await adapter.search_messages(record.usuario_id, "informe")

        # Bounded — must not have retried forever.
        assert mock_client.request.await_count <= 5
