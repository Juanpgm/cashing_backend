"""Network-failure-injection tests for GmailAdapter — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md ~L222).

Covers timeout / 401 / 403 / 429 / 5xx / malformed-payload matrices for the
methods that previously leaked raw exceptions at the API boundary:

- `get_attachment` had no error wrapping at all (bug — fixed here).
- `_fetch_message` called `_parse_message(raw)` OUTSIDE the guarded
  try/except, so a malformed payload (missing `payload`/`id`) raised a raw
  `KeyError` instead of the domain `ExternalServiceError` (bug — fixed here).
- `search_messages` / `send_message` already wrap `GoogleHttpError` — those
  are re-verified here.
- `search_messages`, `send_message`, `get_attachment`, and `_fetch_message`'s
  HTTP call now ALSO wrap the shared `GOOGLE_TRANSPORT_ERRORS` tuple
  (`app.adapters.google_errors`): DNS failures (`httplib2.ServerNotFoundError`)
  and lazy token-refresh failures (`google.auth.exceptions.RefreshError`/
  `TransportError`) previously escaped as a raw 500 (review r1, finding P2b).
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httplib2
import pytest
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.exceptions import ExternalServiceError
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError as GoogleHttpError


def _http_error(status: int, message: str = "error", uri: str | None = None) -> GoogleHttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps({"error": {"message": message, "code": status}}).encode()
    return GoogleHttpError(resp=resp, content=content, uri=uri)


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
    async def test_dns_failure_is_wrapped_as_external_service_error(self) -> None:
        """httplib2.ServerNotFoundError is NOT an OSError subclass — review r1
        finding P2b."""
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = httplib2.ServerNotFoundError(
            "Unable to find the server"
        )
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")

    @pytest.mark.asyncio
    async def test_lazy_token_refresh_failure_is_wrapped_as_external_service_error(self) -> None:
        """RefreshError can be raised lazily inside `.execute()` by the
        underlying transport auto-refreshing an expired token — not just in
        `get_credentials`'s explicit `creds.refresh(...)` call. Review r1
        finding P2b."""
        service = MagicMock()
        service.users().messages().attachments().get().execute.side_effect = RefreshError("token expired")
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

    @pytest.mark.asyncio
    async def test_invalid_base64_data_is_wrapped_not_raw_binascii_error(self) -> None:
        """`data` present but not valid base64 must raise the domain
        `ExternalServiceError` (502), never a raw `binascii.Error` (500) —
        see radicacion-sin-friccion phase 4.3 review, finding P2a."""
        service = MagicMock()
        # "A" is a single valid base64-alphabet char: appending the code's own
        # "==" padding yields "A==" (3 chars), which is not a multiple of 4 —
        # `binascii.Error: number of data characters (1) cannot be 1 more than
        # a multiple of 4`. Characters outside the alphabet (e.g. "!") are
        # silently *discarded* by `urlsafe_b64decode(validate=False)`, so they
        # do NOT reproduce the bug — this is the genuinely malformed case.
        service.users().messages().attachments().get().execute.return_value = {"data": "A"}
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_attachment(uuid.uuid4(), "msg1", "att1")


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
    async def test_invalid_base64_body_is_wrapped_not_raw_binascii_error(self) -> None:
        """A message body part with `data` that is not valid base64 must raise
        the domain `ExternalServiceError`, never a raw `binascii.Error` — see
        radicacion-sin-friccion phase 4.3 review, finding P2a. Mirrors Drive
        `_parse_file` / Calendar `_parse_event`, which already guard `ValueError`."""
        service = MagicMock()
        # See TestGetAttachmentFailures.test_invalid_base64_data_is_wrapped_...
        # for why "A" (not "!!!...!!!") is the genuinely malformed value.
        service.users().messages().get().execute.return_value = {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "headers": [],
                "mimeType": "text/plain",
                "body": {"data": "A"},
                "parts": [],
            },
        }
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_http_error_during_fetch_still_wrapped(self) -> None:
        """Regression guard: the existing GoogleHttpError wrapping around the
        HTTP call itself must survive the refactor."""
        service = MagicMock()
        service.users().messages().get().execute.side_effect = _http_error(500, "boom")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_dns_failure_during_fetch_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().get().execute.side_effect = httplib2.ServerNotFoundError("Unable to find server")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_message(uuid.uuid4(), "m1")

    @pytest.mark.asyncio
    async def test_lazy_token_refresh_failure_during_fetch_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().get().execute.side_effect = RefreshError("token expired")
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
    async def test_search_transport_timeout_is_wrapped(self) -> None:
        """Previously a documented gap (`search_messages` only caught
        `GoogleHttpError`) — fixed in review r1 finding P2b via the shared
        `GOOGLE_TRANSPORT_ERRORS` tuple."""
        service = MagicMock()
        service.users().messages().list().execute.side_effect = TimeoutError("socket timed out")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_search_dns_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.side_effect = httplib2.ServerNotFoundError("Unable to find server")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_search_lazy_token_refresh_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.side_effect = RefreshError("token expired")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_send_dns_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().send().execute.side_effect = httplib2.ServerNotFoundError("Unable to find server")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.send_message(uuid.uuid4(), ["a@b.com"], "subj", "<p>hi</p>")

    @pytest.mark.asyncio
    async def test_send_lazy_token_refresh_failure_is_wrapped(self) -> None:
        service = MagicMock()
        service.users().messages().send().execute.side_effect = RefreshError("token expired")
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.send_message(uuid.uuid4(), ["a@b.com"], "subj", "<p>hi</p>")


class TestSearchMessagesPerMessageTolerance:
    """`search_messages` used to abort the WHOLE search when a single message
    in the batch was malformed: `asyncio.gather(*tasks)` had no per-item
    tolerance, and `_fetch_message` raised `ExternalServiceError` for a
    malformed payload same as for a transport/HTTP failure — so one bad
    message silently zeroed an entire evidence-discovery batch. Review r1
    finding P2c: a malformed message is now skipped-and-logged, while a
    genuine transport/HTTP failure of any single fetch still surfaces."""

    def _raw_message(self, message_id: str) -> dict:  # type: ignore[type-arg]
        return {
            "id": message_id,
            "threadId": "t1",
            "payload": {"headers": [], "body": {}, "parts": []},
            "snippet": "ok",
            "labelIds": [],
        }

    @pytest.mark.asyncio
    async def test_one_malformed_message_is_skipped_and_logged_rest_returned(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
        }

        def _get(**kwargs: str) -> MagicMock:
            requested_id = kwargs["id"]
            mock = MagicMock()
            if requested_id == "m2":
                mock.execute.return_value = {"id": "m2"}  # missing "payload" -> malformed
            else:
                mock.execute.return_value = self._raw_message(requested_id)
            return mock

        service.users().messages().get.side_effect = _get
        adapter = _adapter_with_service(service)

        with patch("app.adapters.email.gmail_adapter.logger.warning") as mock_warning:
            result = await adapter.search_messages(uuid.uuid4(), "q")

        assert {m.id for m in result} == {"m1", "m3"}
        logged_events = [call.args[0] for call in mock_warning.call_args_list]
        assert "gmail_message_malformed" in logged_events
        malformed_call = next(call for call in mock_warning.call_args_list if call.args[0] == "gmail_message_malformed")
        assert malformed_call.kwargs.get("message_id") == "m2"

    @pytest.mark.asyncio
    async def test_one_transport_failure_still_raises_external_service_error(self) -> None:
        """Transport/HTTP failures are NOT silently hidden — only genuinely
        malformed payloads are skip-and-logged."""
        service = MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
        }

        def _get(**kwargs: str) -> MagicMock:
            requested_id = kwargs["id"]
            mock = MagicMock()
            if requested_id == "m2":
                mock.execute.side_effect = _http_error(503, "unavailable")
            else:
                mock.execute.return_value = self._raw_message(requested_id)
            return mock

        service.users().messages().get.side_effect = _get
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_messages(uuid.uuid4(), "q")

    @pytest.mark.asyncio
    async def test_all_messages_malformed_returns_empty_list_no_exception(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}, {"id": "m2"}]}

        def _get(**kwargs: str) -> MagicMock:
            mock = MagicMock()
            mock.execute.return_value = {"id": kwargs["id"]}  # missing "payload" always
            return mock

        service.users().messages().get.side_effect = _get
        adapter = _adapter_with_service(service)

        result = await adapter.search_messages(uuid.uuid4(), "q")

        assert result == []


class TestUserFacingMessageDoesNotLeakRawSdkText:
    """`str(GoogleHttpError)` includes the FULL request URI — Gmail search
    `q=` terms — and `domain_error_handler` returns `ExternalServiceError.detail`
    to the client VERBATIM, no redaction. Review r1 finding P3a: the
    client-facing detail must carry a generic message plus at most the
    exception's class name; the raw text goes to structlog only."""

    @pytest.mark.asyncio
    async def test_http_error_message_does_not_leak_search_query_or_uri(self) -> None:
        service = MagicMock()
        sensitive_query = "contrato-CTR-2024-secreto-001"
        service.users().messages().list().execute.side_effect = _http_error(
            500,
            "boom",
            uri=f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={sensitive_query}&maxResults=20",
        )
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError) as exc_info:
            await adapter.search_messages(uuid.uuid4(), sensitive_query)

        detail = str(exc_info.value)
        assert "http" not in detail.lower()
        assert "googleapis" not in detail.lower()
        assert "q=" not in detail
        assert sensitive_query not in detail
        # NOT `type(exc).__name__` here on purpose: GoogleHttpError's class name
        # is literally "HttpError", which would reintroduce the "http" substring
        # this test forbids — see google_errors.raise_external_service_error's
        # include_exc_type=False for GoogleHttpError call sites.
        assert "no está disponible" in detail  # sanity: generic message present, not garbled

    @pytest.mark.asyncio
    async def test_transport_error_message_does_not_leak_raw_exception_text(self) -> None:
        service = MagicMock()
        service.users().messages().list().execute.side_effect = RefreshError(
            "refresh failed for internal-service-account@project.iam.gserviceaccount.com"
        )
        adapter = _adapter_with_service(service)

        with pytest.raises(ExternalServiceError) as exc_info:
            await adapter.search_messages(uuid.uuid4(), "q")

        detail = str(exc_info.value)
        assert "internal-service-account" not in detail
        assert "RefreshError" in detail


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
        """Safety net, not a characterization of a known gap: `search_messages`
        now wraps `TimeoutError` itself (review r1 finding P2b), but this test
        mocks the adapter METHOD directly to simulate an exception type the
        adapter does not (or does not yet) wrap — the route's own
        `except Exception` catch-all must still surface a structured JSON 500,
        never a hang or a bare unhandled-exception page."""
        with patch(
            "app.adapters.email.gmail_adapter.GmailAdapter.search_messages",
            AsyncMock(side_effect=RuntimeError("totally unmapped failure")),
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
