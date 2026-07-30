"""Microsoft Graph adapter — implements EmailPort, DrivePort, and CalendarPort.

Mirrors the Google adapters' structure (gmail_adapter.py / drive_adapter.py /
calendar_adapter.py): same port interfaces, same error-handling via
`ExternalServiceError`, same per-`(usuario_id, provider)` credential loading from
`Integracion`. Unlike the Google adapters (which wrap the synchronous
`googleapiclient` library via `run_in_executor`), this adapter talks to Graph
through `httpx.AsyncClient`, which is already async and never blocks the event
loop — no executor wrapping needed.

Token refresh reads/writes the `integraciones` row directly (mirrors
`gmail_adapter.get_credentials`'s refresh-in-place pattern) rather than going
through `integration_service.store_credentials`, since only the access/refresh
token + expiry change here — the row already exists (issued by
`microsoft_graph_service.handle_oauth_callback`, Slice B).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.calendar.port import CalendarAttendee, CalendarEvent
from app.adapters.drive.port import DriveFile, DriveQuery
from app.adapters.email.port import EmailMessage
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError
from app.models.integracion import Integracion, IntegrationProvider

logger = structlog.get_logger("adapters.microsoft_graph")

_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# ponytail: naive fixed budget, upgrade to a jitter/circuit-breaker strategy only if
# Graph 429s measurably outpace this in production.
_MAX_RETRIES = 4
_DEFAULT_RETRY_AFTER_SECONDS = 1.0
_MAX_PAGES = 5  # bounded page walk — never follow @odata.nextLink unboundedly

FOLDER_FACET = "folder"


def _parse_retry_after(value: str | None) -> float:
    if not value:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        return float(value)
    except ValueError:
        return _DEFAULT_RETRY_AFTER_SECONDS


def _parse_graph_datetime(value: str) -> datetime:
    """Graph returns naive ISO datetimes (paired with a separate `timeZone` field).

    Evidence discovery only compares/sorts these, so treating them as UTC is a safe,
    conservative simplification (no cross-timezone arithmetic is performed downstream).
    """
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_message(raw: dict[str, Any]) -> EmailMessage:
    """Map a raw Graph `/me/messages` resource to the neutral `EmailMessage`."""
    from_addr = ((raw.get("from") or {}).get("emailAddress") or {}).get("address", "")
    recipients = [(r.get("emailAddress") or {}).get("address", "") for r in raw.get("toRecipients") or []]
    received = raw.get("receivedDateTime")
    date_val = _parse_graph_datetime(received) if received else datetime.now(UTC)
    body = raw.get("body") or {}
    content_type = (body.get("contentType") or "text").lower()
    content = body.get("content", "") or ""
    return EmailMessage(
        id=raw.get("id", ""),
        thread_id=raw.get("conversationId", "") or "",
        subject=raw.get("subject", "") or "(sin asunto)",
        sender=from_addr,
        recipients=recipients,
        date=date_val,
        body_plain=content if content_type == "text" else raw.get("bodyPreview", "") or "",
        body_html=content if content_type == "html" else None,
        snippet=raw.get("bodyPreview", "") or "",
        attachments=[],  # attachments require a separate call — see get_attachment
        labels=raw.get("categories") or [],
        headers={},
    )


def _parse_drive_file(raw: dict[str, Any]) -> DriveFile:
    """Map a raw Graph `driveItem` resource to the neutral `DriveFile`."""

    def _parse_dt(val: str | None) -> datetime:
        if not val:
            return datetime(2000, 1, 1, tzinfo=UTC)
        return _parse_graph_datetime(val)

    file_facet = raw.get("file") or {}
    parent_id = (raw.get("parentReference") or {}).get("id")
    return DriveFile(
        id=raw.get("id", ""),
        name=raw.get("name", "") or "",
        mime_type=file_facet.get("mimeType", ""),
        size_bytes=int(raw.get("size", 0) or 0),
        created_at=_parse_dt(raw.get("createdDateTime")),
        modified_at=_parse_dt(raw.get("lastModifiedDateTime")),
        web_view_link=raw.get("webUrl", "") or "",
        download_link=raw.get("@microsoft.graph.downloadUrl"),
        parents=[parent_id] if parent_id else [],
    )


def _parse_event(raw: dict[str, Any]) -> CalendarEvent:
    """Map a raw Graph `/me/events` resource to the neutral `CalendarEvent`."""
    is_all_day = bool(raw.get("isAllDay", False))
    start_raw = raw.get("start") or {}
    end_raw = raw.get("end") or {}
    start_dt = end_dt = None
    start_date = end_date = None
    if is_all_day:
        if start_raw.get("dateTime"):
            start_date = date.fromisoformat(start_raw["dateTime"][:10])
        if end_raw.get("dateTime"):
            end_date = date.fromisoformat(end_raw["dateTime"][:10])
    else:
        if start_raw.get("dateTime"):
            start_dt = _parse_graph_datetime(start_raw["dateTime"])
        if end_raw.get("dateTime"):
            end_dt = _parse_graph_datetime(end_raw["dateTime"])

    organizer_email = ((raw.get("organizer") or {}).get("emailAddress") or {}).get("address")
    attendees = [
        CalendarAttendee(
            email=(a.get("emailAddress") or {}).get("address", ""),
            display_name=(a.get("emailAddress") or {}).get("name", ""),
            response_status=(a.get("status") or {}).get("response", ""),
            optional=a.get("type") == "optional",
        )
        for a in raw.get("attendees") or []
    ]
    body = raw.get("body") or {}
    return CalendarEvent(
        id=raw.get("id", ""),
        summary=raw.get("subject", "") or "",
        description=body.get("content", "") or "",
        start=start_dt,
        end=end_dt,
        start_date=start_date,
        end_date=end_date,
        is_all_day=is_all_day,
        location=(raw.get("location") or {}).get("displayName"),
        html_link=raw.get("webLink", "") or "",
        attendees=attendees,
        organizer_email=organizer_email,
        event_type="default",  # Graph has no equivalent to Google's eventType
    )


def _escape_odata_literal(value: str) -> str:
    """Escape a value for interpolation inside a single-quoted OData literal."""
    return value.replace("'", "''")


def _translate_drive_query(query: DriveQuery) -> tuple[str | None, str | None]:
    """Translate a `DriveQuery` into Graph's `$search` text + `$filter` clause.

    keywords -> $search (Graph's search is already OR-ish across name/content);
    date_from/date_to -> $filter on lastModifiedDateTime; exclude_folders and
    mime_types are applied as post-filters (see search_files) because Graph's
    `folder` facet and `file.mimeType` aren't filterable server-side alongside
    a free-text $search in the same request.
    """
    search_text = " ".join(query.keywords) if query.keywords else None
    filter_parts: list[str] = []
    if query.date_from:
        filter_parts.append(f"lastModifiedDateTime ge {query.date_from.isoformat()}")
    if query.date_to:
        filter_parts.append(f"lastModifiedDateTime le {query.date_to.isoformat()}")
    filter_clause = " and ".join(filter_parts) if filter_parts else None
    return search_text, filter_clause


class MicrosoftGraphAdapter:
    """Microsoft Graph implementation of EmailPort, DrivePort, and CalendarPort.

    Credentials are per-`(usuario_id, provider=microsoft)`, loaded from the shared
    `Integracion` table (see app/models/integracion.py). All Graph HTTP calls run
    through `_request`, which bounds retries on 429 (honoring `Retry-After`) and
    raises a scoped `ExternalServiceError` on exhaustion, and `_paginate`, which
    bounds `@odata.nextLink` walks to `_MAX_PAGES`.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._fernet = Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())

    # ── Credential management ────────────────────────────────────────────────

    async def get_access_token(self, usuario_id: uuid.UUID) -> str:
        """Load, decrypt, and refresh (if expired) the Graph access token for a user."""
        result = await self._db.execute(
            select(Integracion)
            .where(Integracion.usuario_id == usuario_id, Integracion.provider == IntegrationProvider.MICROSOFT)
            .order_by(Integracion.updated_at.desc())
        )
        record = result.scalars().first()
        if not record:
            raise NotFoundError("Cuenta de Microsoft no conectada. Ve a /api/v1/integraciones/microsoft/connect")

        try:
            access_token = self._fernet.decrypt(record.access_token_encrypted.encode()).decode()
            refresh_token = self._fernet.decrypt(record.refresh_token_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise ExternalServiceError(
                "Microsoft Graph",
                "No se pudo desencriptar el token almacenado (clave de cifrado rotada o "
                "corrupta) — reconectá tu cuenta de Microsoft en /integraciones",
            ) from exc

        expires_at = record.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is None or expires_at > datetime.now(UTC):
            return access_token

        new_tokens = await self._refresh_access_token(refresh_token)
        record.access_token_encrypted = self._fernet.encrypt(new_tokens["access_token"].encode()).decode()
        if new_tokens.get("refresh_token"):
            record.refresh_token_encrypted = self._fernet.encrypt(new_tokens["refresh_token"].encode()).decode()
        record.expires_at = datetime.now(UTC) + timedelta(seconds=new_tokens.get("expires_in", 3600))
        await self._db.commit()
        logger.info("microsoft_token_refreshed", user_id=str(usuario_id))
        return str(new_tokens["access_token"])

    async def _refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        token_url = _TOKEN_URL_TEMPLATE.format(tenant=settings.AZURE_AD_TENANT_ID)
        data = {
            "client_id": settings.AZURE_AD_CLIENT_ID,
            "client_secret": settings.AZURE_AD_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(settings.MICROSOFT_OAUTH_SCOPES),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(token_url, data=data)
                resp.raise_for_status()
                result: dict[str, Any] = resp.json()
                return result
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                "Microsoft Graph",
                f"Token vencido — reconectá tu cuenta de Microsoft en /integraciones: {exc}",
            ) from exc

    # ── HTTP with bounded retry/backoff + bounded pagination ─────────────────

    async def _request(
        self,
        usuario_id: uuid.UUID,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one Graph HTTP call, retrying up to `_MAX_RETRIES` on 429."""
        access_token = await self.get_access_token(usuario_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await client.request(
                        method, url, params=params, json=json_body, content=content, headers=headers
                    )
                    resp.raise_for_status()
                    return resp
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 429 or attempt == _MAX_RETRIES - 1:
                        raise ExternalServiceError(
                            "Microsoft Graph", f"HTTP {exc.response.status_code}: {exc}"
                        ) from exc
                    last_exc = exc
                    delay = _parse_retry_after(exc.response.headers.get("Retry-After"))
                    logger.warning("graph_rate_limited_retry", attempt=attempt + 1, delay=delay, url=url)
                    await asyncio.sleep(delay)
                except httpx.RequestError as exc:
                    raise ExternalServiceError("Microsoft Graph", f"Error de red: {exc}") from exc
        raise ExternalServiceError("Microsoft Graph", f"Reintentos agotados: {last_exc}")

    async def _paginate(
        self,
        usuario_id: uuid.UUID,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        cap: int = 20,
        extra_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Collect Graph `value` pages, bounded to `_MAX_PAGES` (no unbounded page walk)."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        next_params: dict[str, Any] | None = params
        for _ in range(_MAX_PAGES):
            if not next_url:
                break
            resp = await self._request(usuario_id, "GET", next_url, params=next_params, extra_headers=extra_headers)
            payload = resp.json()
            items.extend(payload.get("value", []))
            if len(items) >= cap:
                return items[:cap]
            next_url = payload.get("@odata.nextLink")
            next_params = None  # nextLink already carries the full query string
        return items[:cap]

    # ── EmailPort ─────────────────────────────────────────────────────────────

    async def search_messages(
        self,
        usuario_id: uuid.UUID,
        query: str,
        max_results: int = 20,
    ) -> list[EmailMessage]:
        url = f"{_GRAPH_BASE_URL}/me/messages"
        params: dict[str, Any] = {"$top": min(max_results, 50)}
        extra_headers = None
        if query:
            params["$search"] = f'"{query}"'
            extra_headers = {"ConsistencyLevel": "eventual"}
        items = await self._paginate(usuario_id, url, params=params, cap=max_results, extra_headers=extra_headers)
        messages = [_parse_message(item) for item in items]
        logger.info("microsoft_email_search", user_id=str(usuario_id), count=len(messages))
        return messages

    async def get_message(self, usuario_id: uuid.UUID, message_id: str) -> EmailMessage:
        resp = await self._request(usuario_id, "GET", f"{_GRAPH_BASE_URL}/me/messages/{message_id}")
        return _parse_message(resp.json())

    async def get_attachment(self, usuario_id: uuid.UUID, message_id: str, attachment_id: str) -> bytes:
        resp = await self._request(
            usuario_id, "GET", f"{_GRAPH_BASE_URL}/me/messages/{message_id}/attachments/{attachment_id}"
        )
        content_bytes = resp.json().get("contentBytes", "")
        return base64.b64decode(content_bytes) if content_bytes else b""

    async def send_message(
        self,
        usuario_id: uuid.UUID,
        to: list[str],
        subject: str,
        body_html: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> str:
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        }
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentType": mime_type,
                    "contentBytes": base64.b64encode(content).decode(),
                }
                for filename, content, mime_type in attachments
            ]
        await self._request(usuario_id, "POST", f"{_GRAPH_BASE_URL}/me/sendMail", json_body={"message": message})
        logger.info("microsoft_email_sent", user_id=str(usuario_id), to=to, subject=subject)
        return ""  # Graph sendMail returns 202 Accepted with no body/id

    # ── DrivePort ─────────────────────────────────────────────────────────────

    async def search_files(self, usuario_id: uuid.UUID, query: DriveQuery) -> list[DriveFile]:
        """Search across the user's OneDrive using the same `DriveQuery` contract as Google."""
        search_text, filter_clause = _translate_drive_query(query)
        params: dict[str, Any] = {"$top": query.max_results}
        if filter_clause:
            params["$filter"] = filter_clause
        if search_text:
            url = f"{_GRAPH_BASE_URL}/me/drive/root/search(q='{_escape_odata_literal(search_text)}')"
        else:
            url = f"{_GRAPH_BASE_URL}/me/drive/root/children"

        items = await self._paginate(usuario_id, url, params=params, cap=query.max_results)

        files: list[DriveFile] = []
        for item in items:
            if query.exclude_folders and FOLDER_FACET in item:
                continue
            if query.mime_types:
                mime = (item.get("file") or {}).get("mimeType", "")
                if mime not in query.mime_types:
                    continue
            files.append(_parse_drive_file(item))
        logger.info("onedrive_search", user_id=str(usuario_id), keywords=query.keywords, count=len(files))
        return files

    async def upload_file(
        self,
        usuario_id: uuid.UUID,
        name: str,
        content: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> DriveFile:
        path_segment = f"items/{folder_id}:/{name}:" if folder_id else f"root:/{name}:"
        url = f"{_GRAPH_BASE_URL}/me/drive/{path_segment}/content"
        resp = await self._request(usuario_id, "PUT", url, content=content, extra_headers={"Content-Type": mime_type})
        raw = resp.json()
        logger.info("onedrive_file_uploaded", file_id=raw.get("id"), name=name, user_id=str(usuario_id))
        return _parse_drive_file(raw)

    async def get_or_create_folder(
        self,
        usuario_id: uuid.UUID,
        path: list[str],
        parent_id: str | None = None,
    ) -> str:
        current_parent = parent_id
        for folder_name in path:
            folder_id = await self._find_folder(usuario_id, folder_name, current_parent)
            if not folder_id:
                folder_id = await self._create_folder(usuario_id, folder_name, current_parent)
            current_parent = folder_id
        return current_parent  # type: ignore[return-value]

    async def _find_folder(self, usuario_id: uuid.UUID, name: str, parent_id: str | None) -> str | None:
        children_url = (
            f"{_GRAPH_BASE_URL}/me/drive/items/{parent_id}/children"
            if parent_id
            else f"{_GRAPH_BASE_URL}/me/drive/root/children"
        )
        items = await self._paginate(usuario_id, children_url, params={"$top": 50}, cap=50)
        for item in items:
            if item.get("name") == name and FOLDER_FACET in item:
                return str(item["id"])
        return None

    async def _create_folder(self, usuario_id: uuid.UUID, name: str, parent_id: str | None) -> str:
        url = (
            f"{_GRAPH_BASE_URL}/me/drive/items/{parent_id}/children"
            if parent_id
            else f"{_GRAPH_BASE_URL}/me/drive/root/children"
        )
        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        resp = await self._request(usuario_id, "POST", url, json_body=body)
        result = resp.json()
        logger.info("onedrive_folder_created", name=name, folder_id=result.get("id"))
        return str(result["id"])

    async def list_files(self, usuario_id: uuid.UUID, folder_id: str, query: str | None = None) -> list[DriveFile]:
        params: dict[str, Any] = {}
        if query:
            params["$filter"] = query
        items = await self._paginate(
            usuario_id, f"{_GRAPH_BASE_URL}/me/drive/items/{folder_id}/children", params=params, cap=200
        )
        return [_parse_drive_file(item) for item in items]

    async def get_file(self, usuario_id: uuid.UUID, file_id: str) -> DriveFile:
        resp = await self._request(usuario_id, "GET", f"{_GRAPH_BASE_URL}/me/drive/items/{file_id}")
        return _parse_drive_file(resp.json())

    async def download_file(self, usuario_id: uuid.UUID, file_id: str) -> bytes:
        resp = await self._request(usuario_id, "GET", f"{_GRAPH_BASE_URL}/me/drive/items/{file_id}/content")
        return resp.content

    async def make_shareable(self, usuario_id: uuid.UUID, file_id: str, role: str = "reader") -> str:
        link_type = "view" if role == "reader" else "edit"
        body = {"type": link_type, "scope": "anonymous"}
        resp = await self._request(
            usuario_id, "POST", f"{_GRAPH_BASE_URL}/me/drive/items/{file_id}/createLink", json_body=body
        )
        link = str(resp.json()["link"]["webUrl"])
        logger.info("onedrive_file_shared", file_id=file_id, role=role)
        return link

    async def delete_file(self, usuario_id: uuid.UUID, file_id: str) -> None:
        await self._request(usuario_id, "DELETE", f"{_GRAPH_BASE_URL}/me/drive/items/{file_id}")
        logger.info("onedrive_file_deleted", file_id=file_id)

    # ── CalendarPort ──────────────────────────────────────────────────────────

    async def search_events(
        self,
        usuario_id: uuid.UUID,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
        max_results: int = 50,
        q: str | None = None,
    ) -> list[CalendarEvent]:
        """List events in the RFC3339 window via Graph `/me/events` (`/me/calendarView` window filter)."""
        url = f"{_GRAPH_BASE_URL}/me/calendarView"
        params: dict[str, Any] = {
            "startDateTime": time_min,
            "endDateTime": time_max,
            "$top": min(max_results, 50),
            "$orderby": "start/dateTime",
        }
        extra_headers = None
        if q:
            params["$search"] = f'"{q}"'
            extra_headers = {"ConsistencyLevel": "eventual"}

        items = await self._paginate(usuario_id, url, params=params, cap=max_results, extra_headers=extra_headers)
        events: list[CalendarEvent] = []
        for item in items:
            try:
                events.append(_parse_event(item))
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "microsoft_calendar_event_parse_failed",
                    event_id=item.get("id"),
                    user_id=str(usuario_id),
                    error=str(exc),
                )
                continue
        logger.info("microsoft_calendar_search", user_id=str(usuario_id), count=len(events), q=q)
        return events

    async def get_event(self, usuario_id: uuid.UUID, event_id: str, calendar_id: str = "primary") -> CalendarEvent:
        resp = await self._request(usuario_id, "GET", f"{_GRAPH_BASE_URL}/me/events/{event_id}")
        return _parse_event(resp.json())
