"""Network-failure-injection tests for GmailAdapter — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md ~L222).

Covers timeout / 401 / 403 / 429 / 5xx / malformed-payload matrices for the
methods that previously leaked raw exceptions at the API boundary:

- `get_attachment` had no error wrapping at all (bug — fixed here).
- `_fetch_message` called `_parse_message(raw)` OUTSIDE the guarded
  try/except, so a malformed payload (missing `payload`/`id`) raised a raw
  `KeyError` instead of the domain `ExternalServiceError` (bug — fixed here).
- `search_messages` / `send_message` already wrap `GoogleHttpError` — those
  are re-verified here, plus one characterization test documenting the known,
  intentionally-unfixed gap: raw transport errors (`TimeoutError`/`OSError`)
  are neither retried nor mapped for those two methods (see
  `GmailAdapter._execute_with_retry` — only 429 `GoogleHttpError` is retried;
  widening that is a design change out of scope for this slice).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.exceptions import ExternalServiceError
from googleapiclient.errors import HttpError as GoogleHttpError


def _http_error(status: int, message: str = "error") -> GoogleHttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps({"error": {"message": message, "code": status}}).encode()
    return GoogleHttpError(resp=resp, content=content)


def _make_adapter() -> GmailAdapter:
    adapter = GmailAdapter.__new__(GmailAdapter)
    return adapter


def _adapter_with_service(service: MagicMock) -> GmailAdapter:
    adapter = _make_adapter()
    adapter.get_credentials = AsyncMock(return_value=MagicMock())
    adapter._build_service = MagicMock(return_value=service)
    return adapter


class TestGetAttachmentFailures:
    """`get_attachment` had zero error wrapping before this slice — every one
    of these raised the raw googleapiclient/transport exception straight
    through the API boundary."""

    @pytest.mark.asyncio
    async def test_401_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _adapter_with_service(service)

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_403_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = _http_error(403, "forbidden")
        adapter = _adapter_with_service(service)

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_5xx_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = _http_error(503, "unavailable")
        adapter = _adapter_with_service(service)

        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = TimeoutError("socket timed out")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_transport_os_error_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = OSError("connection reset")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_429_still_retries_then_succeeds(self) -> None:
        """The existing 429 retry path (`_execute_with_retry`) must keep working
        once get_attachment gains error wrapping around it."""
        service = MagicMock()
        calls = {"n": 0}

        def _execute():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(429, "rateLimitExceeded")
            return {"data": "aGVsbG8"}  # base64url "hello"-ish payload

        service.users().messages().attachments().get().execute.side_effect = _execute
        adapter = _adapter_with_service(service)

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

        assert isinstance(result, bytes)
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_malformed_response_missing_data_key_returns_empty_bytes(self) -> None:
        """Not a bug: `result.get("data", "")` already tolerates a missing key."""
        service = MagicMock()
        service.users().messages().attachments().get().execute.return_value = {}
        adapter = _adapter_with_service(service)

        result = await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

        assert result == b""


class TestFetchMessageMalformedPayload:
    """`_fetch_message` used to call `_parse_message(raw)` OUTSIDE the try/except
    guarding the HTTP call — a malformed payload raised a raw `KeyError`/`TypeError`
    instead of the domain `ExternalServiceError` every other adapter error uses."""

    @pytest.mark.asyncio
    async def test_missing_payload_key_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        # Real Gmail always includes "payload"; a malformed/partial response omits it.
        service.users().messages().get().execute.return_value = {"id": "m1", "threadId": "t1"}
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_missing_id_key_is_wrapped_as_external_service_error(self) -> None:
        service = MagicMock()
        service.users().messages().get().execute.return_value = {
            "threadId": "t1",
            "payload": {"headers": [], "body": {}, "parts": []},
        }
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_payload_wrong_type_is_wrapped_as_external_service_error(self) -> None:
        """`payload` present but not a dict (e.g. `None`) must not raise a raw
        AttributeError/TypeError from `.get()` deep inside `_extract_body`."""
        service = MagicMock()
        service.users().messages().get().execute.return_value = {"id": "m1", "payload": None}
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_well_formed_payload_still_parses_successfully(self) -> None:
        """Regression guard: moving the parse inside the try/except must not
        change the happy path."""
        service = MagicMock()
        service.users().messages().get().execute.return_value = {
            "id": "m1",
            "threadId": "t1",
            "payload": {"headers": [], "body": {}, "parts": []},
            "snippet": "hola",
            "labelIds": [],
        }
        adapter = _adapter_with_service(service)

        result = await adapter.get_message(uuid.uuid4(), "m1")

        assert result.id == "m1"
        assert result.snippet == "hola"

    @pytest.mark.asyncio
    async def test_http_error_during_fetch_still_wrapped(self) -> None:
        """Regression guard: the existing GoogleHttpError wrapping around the
        HTTP call itself must survive the refactor."""
        service = MagicMock()
        service.users().messages().get().execute.side_effect = _http_error(500, "boom")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")


class TestSearchMessagesAndSendMessageFailureMatrix:
    """Re-verifies the existing GoogleHttpError wrapping on the two other
    write/search paths, plus documents the known unfixed gap for transport
    errors (see module docstring)."""

    @pytest.mark.asyncio
    async def test_search_401_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_search_5xx_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.side_effect = _http_error(500, "boom")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_search_malformed_response_missing_messages_key_returns_empty(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.return_value = {"resultSizeEstimate": 0}
        adapter = _adapter_with_service(service)

        result = await adapter.search_messages(uuid.uuid4(), "q")

        assert result == []

    @pytest.mark.asyncio
    async def test_send_401_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().send().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.send_message(uuid.uuid4(), ["a@b.com"], "subj", "<p>hi</p>")

    @pytest.mark.asyncio
    async def test_send_5xx_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().send().execute.side_effect = _http_error(502, "bad gateway")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.send_message(uuid.uuid4(), ["a@b.com"], "subj", "<p>hi</p>")

    @pytest.mark.asyncio
    async def test_known_gap_search_transport_timeout_still_leaks_raw(self) -> None:
        """Characterization test, not a fix: `search_messages` only catches
        `GoogleHttpError`. A raw transport error (`TimeoutError`) is neither
        retried nor mapped — matches the documented gap in
        `_execute_with_retry`. Widening the 429-only retry/wrap to transport
        errors here is a design change out of scope for this slice (see
        report's Deviations section)."""
        service = MagicMock()
        service.users().messages().list().execute.side_effect = TimeoutError("socket timed out")
        adapter = _adapter_with_service(service)

        with pytest.raises(TimeoutError):
            await adapter.search_messages(uuid.uuid4(), "q")


class TestEmailSearchRouteFailureHandling:
    """POST /integraciones/email/search — the route already has a blanket
    `except (HTTPException, DomainError): raise` / `except Exception ->
    HTTPException(500, ...)` around the service call. This proves an adapter
    failure surfaces as a structured JSON error response, never a hang or a
    bare traceback."""

    @pytest.mark.asyncio
    async def test_adapter_error_returns_structured_json_error_not_a_hang(self, client, test_user) -> None:
        """search_messages already wraps GoogleHttpError as ExternalServiceError
        (its own contract, unchanged by this slice) — mocking at that seam
        (rather than the raw googleapiclient call, which search_messages'
        wrapping would immediately re-wrap anyway) verifies the route
        propagates the domain exception to the global DomainError handler
        instead of hanging or leaking a raw traceback."""
        with patch(
            "app.adapters.email.gmail_adapter.GmailAdapter.search_messages",
            AsyncMock(side_effect=ExternalServiceError("Gmail", "Error buscando correos: boom")),
        ):
            resp = await client.post(
                "/api/v1/integraciones/email/search",
                json={"query": "subject:acta"},
                headers=test_user["headers"],
            )

        assert resp.status_code == 502
        body = resp.json()
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_unwrapped_transport_error_still_returns_structured_500_not_a_hang(self, client, test_user) -> None:
        """Even an exception the adapter does NOT wrap (the documented
        search_messages transport-error gap) must still surface as a
        structured JSON 500 through the route's own `except Exception`
        catch-all, never a hang or a bare unhandled-exception page."""
        with patch(
            "app.adapters.email.gmail_adapter.GmailAdapter.search_messages",
            AsyncMock(side_effect=TimeoutError("socket timed out")),
        ):
            resp = await client.post(
                "/api/v1/integraciones/email/search",
                json={"query": "subject:acta"},
                headers=test_user["headers"],
            )

        assert resp.status_code == 500
        assert "detail" in resp.json()

    @pytest.mark.asyncio
    async def test_credentials_not_found_returns_structured_404_not_a_hang(self, client, test_user) -> None:
        """No Google account connected at all — get_credentials raises
        NotFoundError, a DomainError the route re-raises unchanged."""
        resp = await client.post(
            "/api/v1/integraciones/email/search",
            json={"query": "subject:acta"},
            headers=test_user["headers"],
        )

        assert resp.status_code == 404
        assert "detail" in resp.json()
