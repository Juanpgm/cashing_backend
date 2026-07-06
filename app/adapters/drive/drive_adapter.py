"""Google Drive adapter — implementation of DrivePort."""

from __future__ import annotations

import asyncio
import io
import re
import uuid
from datetime import datetime

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError as GoogleHttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drive.port import DriveFile
from app.adapters.email.gmail_adapter import GmailAdapter
from app.core.exceptions import ExternalServiceError

logger = structlog.get_logger("adapters.drive")

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveAdapter:
    """Google Drive implementation of DrivePort.

    Reuses GmailAdapter for credential management — both use the same GoogleToken.
    All Google API calls run via run_in_executor to stay non-blocking.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._auth = GmailAdapter(db)  # credential management is shared

    def _build_service(self, creds):  # type: ignore[no-untyped-def]
        return build("drive", "v3", credentials=creds, cache_discovery=False)

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
        loop = asyncio.get_running_loop()

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

        raw = await loop.run_in_executor(None, _upload)
        logger.info("drive_file_uploaded", file_id=raw["id"], name=name, user_id=str(usuario_id))
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
        loop = asyncio.get_running_loop()

        # Escape single quotes in name for Drive query
        safe_name = name.replace("'", "\\'")
        query = f"name='{safe_name}' and mimeType='{FOLDER_MIME}' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        def _search() -> dict:  # type: ignore[type-arg]
            return service.files().list(q=query, fields="files(id,name)").execute()

        result = await loop.run_in_executor(None, _search)
        files = result.get("files", [])
        return files[0]["id"] if files else None

    async def _create_folder(
        self,
        usuario_id: uuid.UUID,
        name: str,
        parent_id: str | None,
    ) -> str:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        metadata: dict[str, object] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]

        def _create() -> dict:  # type: ignore[type-arg]
            return service.files().create(body=metadata, fields="id").execute()

        result = await loop.run_in_executor(None, _create)
        logger.info("drive_folder_created", name=name, folder_id=result["id"])
        return result["id"]

    # ── List / Get ───────────────────────────────────────────────────────────

    async def list_files(
        self,
        usuario_id: uuid.UUID,
        folder_id: str,
        query: str | None = None,
    ) -> list[DriveFile]:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

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

        result = await loop.run_in_executor(None, _list)
        return [self._parse_file(f) for f in result.get("files", [])]

    async def search_files(
        self,
        usuario_id: uuid.UUID,
        query: str,
        max_results: int = 20,
    ) -> list[DriveFile]:
        """Search across the user's entire Drive (no parent folder constraint).

        ``query`` is a Google Drive query fragment (e.g. ``name contains 'informe'``).
        Requires the ``drive.readonly`` scope to reach files the app did not create.
        """
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        q = "trashed=false"
        if query:
            q += f" and ({query})"

        def _search() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .list(
                    q=q,
                    pageSize=max_results,
                    orderBy="modifiedTime desc",
                    fields="files(id,name,mimeType,size,createdTime,modifiedTime,webViewLink,webContentLink,parents)",
                )
                .execute()
            )

        try:
            result = await loop.run_in_executor(None, _search)
        except GoogleHttpError as exc:
            raise ExternalServiceError("Drive", f"Error buscando archivos: {exc}") from exc
        files = [self._parse_file(f) for f in result.get("files", [])]
        logger.info("drive_search", user_id=str(usuario_id), query=query, count=len(files))
        return files

    async def get_file(self, usuario_id: uuid.UUID, file_id: str) -> DriveFile:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _get() -> dict:  # type: ignore[type-arg]
            return (
                service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents",
                )
                .execute()
            )

        return self._parse_file(await loop.run_in_executor(None, _get))

    async def download_file(self, usuario_id: uuid.UUID, file_id: str) -> bytes:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _download() -> bytes:
            request = service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        return await loop.run_in_executor(None, _download)

    # ── Sharing ──────────────────────────────────────────────────────────────

    async def make_shareable(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
        role: str = "reader",
    ) -> str:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _share() -> str:
            service.permissions().create(
                fileId=file_id,
                body={"type": "anyone", "role": role},
            ).execute()
            return service.files().get(fileId=file_id, fields="webViewLink").execute()["webViewLink"]

        link = await loop.run_in_executor(None, _share)
        logger.info("drive_file_shared", file_id=file_id, role=role)
        return link

    async def delete_file(self, usuario_id: uuid.UUID, file_id: str) -> None:
        creds = await self._auth.get_credentials(usuario_id)
        service = self._build_service(creds)
        loop = asyncio.get_running_loop()

        def _trash() -> None:
            service.files().update(fileId=file_id, body={"trashed": True}).execute()

        await loop.run_in_executor(None, _trash)
        logger.info("drive_file_trashed", file_id=file_id)

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _parse_file(self, raw: dict) -> DriveFile:  # type: ignore[type-arg]
        def _parse_dt(val: str) -> datetime:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))

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
