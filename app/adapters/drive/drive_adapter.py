"""Google Drive adapter — implementation of DrivePort."""

from __future__ import annotations

import asyncio
import io
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

import structlog
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drive.port import DriveFile, DriveQuery
from app.adapters.email.gmail_adapter import GmailAdapter
from app.adapters.google_errors import (
    GOOGLE_TRANSPORT_ERRORS,
    raise_external_service_error,
    raise_google_http_error,
    raise_google_reauth_required,
)
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("adapters.drive")

FOLDER_MIME = "application/vnd.google-apps.folder"

_T = TypeVar("_T")


class DriveAdapter:
    """Google Drive implementation of DrivePort.

    Reuses GmailAdapter for credential management — both read the same `Integracion` row.
    All Google API calls run via run_in_executor to stay non-blocking.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._auth = GmailAdapter(db)  # credential management is shared

    def _build_service(self, creds):  # type: ignore[no-untyped-def]
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _require_key(self, raw: dict, key: str, context: str) -> str:  # type: ignore[type-arg]
        """Extract `raw[key]`, mapping a malformed/partial Drive response to
        `ExternalServiceError` instead of a raw `KeyError`/`TypeError` escaping
        past the API boundary. Used for the handful of bare indexings that
        weren't already routed through `_parse_file` (review r1, finding P3b):
        `_find_folder`'s `files[0]["id"]`, `_create_folder`'s `result["id"]`,
        and `_share`'s `["webViewLink"]`.
        """
        try:
            return raw[key]
        except (KeyError, TypeError) as exc:
            raise_external_service_error(
                logger,
                "Drive",
                "No se pudo completar la operación en Drive",
                exc,
                "drive_missing_expected_key",
                context=context,
                key=key,
            )

    async def _run(self, fn: Callable[[], _T], context: str) -> _T:
        """Run a blocking Drive API call in the executor, mapping the known
        failure modes to the domain `ExternalServiceError` (502) contract:

        - `GoogleHttpError` — 4xx/5xx from the API itself.
        - `google.auth.exceptions.RefreshError` — a lazy token refresh inside
          `.execute()` failed (revoked/expired grant): caught FIRST and mapped
          to `GOOGLE_REAUTH_REQUIRED` with the reconnect message — this is a
          permanent auth condition, never a transient outage.
        - `GOOGLE_TRANSPORT_ERRORS` (`app.adapters.google_errors`) — raw socket
          errors (`OSError`/`TimeoutError`), DNS failures
          (`httplib2.ServerNotFoundError`), and network failures during a lazy
          token refresh (`google.auth.exceptions.TransportError`) — none of
          which `run_in_executor` would otherwise wrap. `RefreshError` is
          deliberately NOT in that tuple.

        This does NOT cover every conceivable failure (e.g. a bug in `fn`
        itself raises unwrapped, by design) — see radicacion-sin-friccion
        phase 4.3 review r1, finding P2b, for the transport gap this closed.
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, fn)
        except GoogleHttpError as exc:
            # `context` (developer-authored, e.g. "Error subiendo archivo
            # 'x.pdf'") is safe to log but the raw `str(exc)` (full request
            # URI, incl. `q=` search terms) is not — see
            # `raise_google_http_error`'s own docstring for why it never
            # appends the exception's class name either.
            raise_google_http_error(
                logger,
                "Drive",
                "No se pudo completar la operación en Drive",
                exc,
                "drive_operation_http_failed",
                context=context,
            )
        except RefreshError as exc:
            raise_google_reauth_required(logger, "Drive", exc, "drive_operation_reauth_required", context=context)
        except GOOGLE_TRANSPORT_ERRORS as exc:
            raise_external_service_error(
                logger,
                "Drive",
                "No se pudo completar la operación en Drive",
                exc,
                "drive_operation_transport_failed",
                context=context,
            )

    # ── Upload ───────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        usuario_id: uuid.UUID,
        name: str,
        content: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> DriveFile:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        metadata: dict[str, object] = {"name": name}
        if folder_id:
            metadata["parents"] = [folder_id]

        buffer = io.BytesIO(content)
        media = MediaIoBaseUpload(buffer, mimetype=mime_type, chunksize=1024 * 1024, resumable=True)

        def _upload() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents",
                )
                .execute()
            )

        raw = await self._run(_upload, f"Error subiendo archivo '{name}'")
        # Log BEFORE parsing: a file that landed in Drive but returned a
        # malformed payload must still be traceable by its id — logging only
        # after `_parse_file` (which can raise) previously left zero trace
        # that the upload itself actually succeeded (review r1, finding P3b).
        # `isinstance` guard: `raw.get(...)` on a non-dict response (e.g. a
        # list) raises a raw AttributeError instead of the domain
        # ExternalServiceError `_parse_file` below would otherwise wrap it
        # into (review r2, finding P3-1).
        logger.info(
            "drive_file_uploaded",
            file_id=raw.get("id") if isinstance(raw, dict) else None,
            name=name,
            user_id=str(usuario_id),
        )
        return self._parse_file(raw)

    # ── Folder Management ────────────────────────────────────────────────────

    async def get_or_create_folder(
        self,
        usuario_id: uuid.UUID,
        path: list[str],
        parent_id: str | None = None,
    ) -> str:
        """Traverse or create each level of path. Returns deepest folder_id."""
        current_parent = parent_id
        for folder_name in path:
            folder_id = await self._find_folder(usuario_id, folder_name, current_parent)
            if not folder_id:
                folder_id = await self._create_folder(usuario_id, folder_name, current_parent)
            current_parent = folder_id
        return current_parent  # type: ignore[return-value]

    async def _find_folder(
        self,
        usuario_id: uuid.UUID,
        name: str,
        parent_id: str | None,
    ) -> str | None:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        # Escape single quotes in name for Drive query
        safe_name = name.replace("'", "\\'")
        query = f"name='{safe_name}' and mimeType='{FOLDER_MIME}' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        def _search() -> dict:  # type: ignore[type-arg]
            return service.files().list(q=query, fields="files(id,name)").execute()

        result = await self._run(_search, f"Error buscando carpeta '{name}'")
        files = result.get("files", [])
        if not files:
            return None
        return self._require_key(files[0], "id", f"Error buscando carpeta '{name}'")

    async def _create_folder(
        self,
        usuario_id: uuid.UUID,
        name: str,
        parent_id: str | None,
    ) -> str:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        metadata: dict[str, object] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]

        def _create() -> dict:  # type: ignore[type-arg]
            return service.files().create(body=metadata, fields="id").execute()

        result = await self._run(_create, f"Error creando carpeta '{name}'")
        folder_id = self._require_key(result, "id", f"Error creando carpeta '{name}'")
        logger.info("drive_folder_created", name=name, folder_id=folder_id)
        return folder_id

    # ── List / Get ───────────────────────────────────────────────────────────

    async def list_files(
        self,
        usuario_id: uuid.UUID,
        folder_id: str,
        query: str | None = None,
    ) -> list[DriveFile]:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        q = f"'{folder_id}' in parents and trashed=false"
        if query:
            q += f" and {query}"

        def _list() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .list(
                    q=q,
                    fields="files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents)",
                )
                .execute()
            )

        result = await self._run(_list, f"Error listando archivos de la carpeta '{folder_id}'")
        return self._parse_files_tolerant(result.get("files", []))

    async def search_files(
        self,
        usuario_id: uuid.UUID,
        query: DriveQuery,
    ) -> list[DriveFile]:
        """Search across the user's entire Drive (no parent folder constraint).

        ``query`` is a provider-neutral `DriveQuery`, translated here into Google
        Drive's native query syntax. Requires the ``drive.readonly`` scope to
        reach files the app did not create.
        """
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        q = "trashed=false"
        translated = self._translate_query(query)
        if translated:
            q += f" and ({translated})"

        def _search() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .list(
                    q=q,
                    pageSize=query.max_results,
                    orderBy="modifiedTime desc",
                    fields="files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,webContentLink,parents)",
                    # Files living in a Shared Drive (common for entity-provisioned
                    # Workspace accounts) are otherwise invisible to this search
                    # entirely — evidencias/discovery-fix root cause #4.
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    corpora="allDrives",
                )
                .execute()
            )

        result = await self._run(_search, "Error buscando archivos")
        files = self._parse_files_tolerant(result.get("files", []))
        logger.info("drive_search", user_id=str(usuario_id), query=query.keywords, count=len(files))
        return files

    def _translate_query(self, query: DriveQuery) -> str:
        """Translate a provider-neutral `DriveQuery` into Google Drive query syntax."""
        parts: list[str] = []
        if query.keywords:
            # Drive query grammar uses backslash as the escape char inside single-quoted
            # literals; strip both quotes and backslashes so a keyword can't corrupt quoting.
            safe_terms = [kw.replace("'", "").replace("\\", "") for kw in query.keywords]
            or_clause = " or ".join(f"name contains '{kw}' or fullText contains '{kw}'" for kw in safe_terms)
            parts.append(f"({or_clause})")
        if query.date_from or query.date_to:
            # SYMMETRIC windows, ORed (round-2 fix). The previous form paired an
            # ORed lower bound with a modifiedTime-only upper bound:
            #   (createdTime >= from or modifiedTime >= from) and modifiedTime <= to
            # For files Drive timestamps itself modifiedTime >= createdTime, so
            # `createdTime >= from` is a strict subset of `modifiedTime >= from`
            # and the OR added ZERO rows — an inert change. Worse, the case the
            # comment claimed to fix (created inside the period, edited after
            # it) was still excluded by the upper bound. Each timestamp now gets
            # its OWN complete window, so a file qualifies if EITHER its
            # creation or its last modification falls inside the period.
            def _window(field: str) -> str:
                bounds = []
                if query.date_from:
                    bounds.append(f"{field} >= '{query.date_from.isoformat()}'")
                if query.date_to:
                    bounds.append(f"{field} <= '{query.date_to.isoformat()}'")
                return "(" + " and ".join(bounds) + ")"

            parts.append(f"({_window('createdTime')} or {_window('modifiedTime')})")
        if query.exclude_folders:
            parts.append(f"mimeType != '{FOLDER_MIME}'")
        if query.mime_types:
            mime_clause = " or ".join(f"mimeType = '{m}'" for m in query.mime_types)
            parts.append(f"({mime_clause})")
        return " and ".join(parts)

    async def get_file(self, usuario_id: uuid.UUID, file_id: str) -> DriveFile:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _get() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents",
                )
                .execute()
            )

        raw = await self._run(_get, f"Error obteniendo archivo '{file_id}'")
        return self._parse_file(raw)

    async def download_file(self, usuario_id: uuid.UUID, file_id: str) -> bytes:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _download() -> bytes:
            request = service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        return await self._run(_download, f"Error descargando archivo '{file_id}'")

    # ── Sharing ──────────────────────────────────────────────────────────────

    async def make_shareable(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
        role: str = "reader",
    ) -> str:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _share() -> dict:  # type: ignore[type-arg]
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": role},
            ).execute()
            return service.files().get(fileId=file_id, fields="webViewLink").execute()

        raw = await self._run(_share, f"Error compartiendo archivo '{file_id}'")
        link = self._require_key(raw, "webViewLink", f"Error compartiendo archivo '{file_id}'")
        logger.info("drive_file_shared", file_id=file_id, role=role)
        return link

    async def delete_file(self, usuario_id: uuid.UUID, file_id: str) -> None:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)

        def _trash() -> None:
            service.files().update(fileId=file_id, body={"trashed": True}).execute()

        await self._run(_trash, f"Error eliminando archivo '{file_id}'")
        logger.info("drive_file_trashed", file_id=file_id)

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _parse_file(self, raw: dict) -> DriveFile:  # type: ignore[type-arg]
        def _parse_dt(val: str) -> datetime:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))

        try:
            return DriveFile(
                id=raw["id"],
                name=raw.get("name", ""),
                mime_type=raw.get("mimeType", ""),
                size_bytes=int(raw.get("size", 0)),
                created_at=_parse_dt(raw.get("createdTime", "2000-01-01T00:00:00Z")),
                modified_at=_parse_dt(raw.get("modifiedTime", "2000-01-01T00:00:00Z")),
                web_view_link=raw.get("webViewLink", ""),
                download_link=raw.get("webContentLink"),
                parents=raw.get("parents", []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            # `raw.get("id")` guarded by `isinstance`: a non-dict `raw` (e.g. a
            # list) raises `TypeError` above (caught), but `raw.get(...)` on
            # that same non-dict `raw` — unguarded — would raise a raw
            # `AttributeError` right here inside the except handler, escaping
            # this very function that exists to prevent a raw exception from
            # reaching the caller (review r2, finding P3-1).
            raise_external_service_error(
                logger,
                "Drive",
                "El archivo de Drive tiene un formato inesperado",
                exc,
                "drive_file_parse_failed_detail",
                file_id=raw.get("id") if isinstance(raw, dict) else None,
            )

    def _parse_files_tolerant(self, raw_files: list[dict]) -> list[DriveFile]:  # type: ignore[type-arg]
        """Parse a list of raw Drive file resources, skipping (and logging) any
        malformed item instead of dropping the whole result set — mirrors
        `GoogleCalendarAdapter.search_events`'s per-item tolerance."""
        files: list[DriveFile] = []
        for raw in raw_files:
            try:
                files.append(self._parse_file(raw))
            except ExternalServiceError as exc:
                logger.warning("drive_file_parse_failed", file_id=raw.get("id"), error=str(exc))
                continue
        return files


# ── Helper utilities ─────────────────────────────────────────────────────────


def build_contract_drive_path(
    entidad: str,
    numero_contrato: str,
    anio: int,
    mes: int,
) -> list[str]:
    """Build the standard Drive folder path for a billing period.

    Example: ["CashIn", "Alcaldía de Bogotá", "Contrato-001-2025", "2025-03"]
    """
    return [
        "CashIn",
        _slugify(entidad or "Sin Entidad"),
        f"Contrato-{_slugify(numero_contrato)}",
        f"{anio}-{mes:02d}",
    ]


def _slugify(text: str) -> str:
    """Make text safe for Drive folder names (max 50 chars)."""
    cleaned = re.sub(r"[^\w\s\-]", "", text, flags=re.UNICODE).strip()
    return cleaned[:50]
