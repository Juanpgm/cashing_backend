"""Tests for GmailAdapter — concurrency bounding and 429 retry behavior.

These cover the regression where search_messages fired one get_message() per
search hit concurrently, tripping Gmail's per-user concurrency cap with
HTTP 429 "Too many concurrent requests for user".
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.email.gmail_adapter import (
    _GMAIL_MAX_CONCURRENCY,
    _GMAIL_MAX_RETRIES,
    GmailAdapter,
    _is_rate_limit_error,
)
from app.core.exceptions import ExternalServiceError
from googleapiclient.errors import HttpError as GoogleHttpError


def _http_error(status: int, message: str = "error") -> GoogleHttpError:
    """Build a googleapiclient HttpError with a given HTTP status and message."""
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps({"error": {"message": message, "code": status}}).encode()
    return GoogleHttpError(resp=resp, content=content)


def _make_adapter() -> GmailAdapter:
    """Construct a GmailAdapter without running __init__ (no DB / Fernet needed)."""
    adapter = GmailAdapter.__new__(GmailAdapter)
    return adapter


class TestIsRateLimitError:
    def test_detects_429_status(self) -> None:
        assert _is_rate_limit_error(_http_error(429)) is True

    def test_detects_too_many_concurrent_requests_message(self) -> None:
        # The real Gmail 429 body — verify message-based detection as a fallback.
        err = _http_error(403, "Too many concurrent requests for user.")
        # status 403 but message matches → still rate-limited
        assert _is_rate_limit_error(err) is True

    def test_detects_rate_limit_exceeded_message(self) -> None:
        err = _http_error(403, "rateLimitExceeded")
        assert _is_rate_limit_error(err) is True

    def test_non_rate_limit_error_is_false(self) -> None:
        assert _is_rate_limit_error(_http_error(500, "Internal error")) is False


class TestExecuteWithRetry:
    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self) -> None:
        adapter = _make_adapter()
        calls = {"n": 0}

        def fn() -> dict:
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(429, "Too many concurrent requests for user.")
            return {"ok": True}

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await adapter._execute_with_retry(fn)

        assert result == {"ok": True}
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self) -> None:
        adapter = _make_adapter()
        calls = {"n": 0}

        def fn() -> dict:
            calls["n"] += 1
            raise _http_error(429, "rateLimitExceeded")

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(GoogleHttpError):
            await adapter._execute_with_retry(fn)

        assert calls["n"] == _GMAIL_MAX_RETRIES

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_propagates_without_retry(self) -> None:
        adapter = _make_adapter()
        calls = {"n": 0}

        def fn() -> dict:
            calls["n"] += 1
            raise _http_error(500, "Internal error")

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(GoogleHttpError):
            await adapter._execute_with_retry(fn)

        assert calls["n"] == 1  # propagated immediately, no retry


class TestSearchMessagesConcurrency:
    @pytest.mark.asyncio
    async def test_bounds_concurrent_fetches(self) -> None:
        """search_messages must never exceed _GMAIL_MAX_CONCURRENCY in-flight fetches."""
        adapter = _make_adapter()
        adapter.get_credentials = AsyncMock(return_value=MagicMock())

        service = MagicMock()
        message_ids = [{"id": str(i)} for i in range(20)]
        service.users().messages().list().execute.return_value = {"messages": message_ids}
        adapter._build_service = MagicMock(return_value=service)

        in_flight = 0
        max_in_flight = 0

        async def fake_fetch(_service, message_id: str) -> MagicMock:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return MagicMock(id=message_id)

        adapter._fetch_message = fake_fetch

        result = await adapter.search_messages(uuid.uuid4(), "after:2026/03/30", max_results=20)

        assert len(result) == 20
        assert max_in_flight <= _GMAIL_MAX_CONCURRENCY
        assert max_in_flight > 1  # confirms it does run concurrently, just bounded

    @pytest.mark.asyncio
    async def test_fetches_credentials_once(self) -> None:
        """Credentials (DB lookup + refresh) are fetched a single time, not per message."""
        adapter = _make_adapter()
        adapter.get_credentials = AsyncMock(return_value=MagicMock())

        service = MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        }
        adapter._build_service = MagicMock(return_value=service)
        adapter._fetch_message = AsyncMock(return_value=MagicMock())

        await adapter.search_messages(uuid.uuid4(), "q", max_results=3)

        adapter.get_credentials.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_builds_fresh_service_per_fetch(self) -> None:
        """Each message fetch builds its OWN service — httplib2.Http is not thread-safe,
        so a shared socket across concurrent threads causes SSL RECORD_LAYER_FAILURE."""
        adapter = _make_adapter()
        adapter.get_credentials = AsyncMock(return_value=MagicMock())

        service = MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        }
        # Each get(...).execute() returns a minimal raw message that _parse_message handles.
        service.users().messages().get().execute.return_value = {
            "id": "x",
            "threadId": "t",
            "payload": {"headers": [], "body": {}, "parts": []},
            "snippet": "",
            "labelIds": [],
        }
        build_service = MagicMock(return_value=service)
        adapter._build_service = build_service

        await adapter.search_messages(uuid.uuid4(), "q", max_results=3)

        # 1 build for the list call + 1 per message fetch (3) = 4 distinct services/sockets.
        assert build_service.call_count == 4

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_messages(self) -> None:
        adapter = _make_adapter()
        adapter.get_credentials = AsyncMock(return_value=MagicMock())

        service = MagicMock()
        service.users().messages().list().execute.return_value = {}
        adapter._build_service = MagicMock(return_value=service)
        adapter._fetch_message = AsyncMock()

        result = await adapter.search_messages(uuid.uuid4(), "q")

        assert result == []
        adapter._fetch_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_http_error_wrapped(self) -> None:
        adapter = _make_adapter()
        adapter.get_credentials = AsyncMock(return_value=MagicMock())

        service = MagicMock()
        service.users().messages().list().execute.side_effect = _http_error(500, "boom")
        adapter._build_service = MagicMock(return_value=service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")
