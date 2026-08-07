"""Drive port (interface) — abstract contract for cloud storage of documents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class DriveQuery:
    """Provider-neutral search query for `DrivePort.search_files`.

    Each adapter translates this into its own native query syntax internally
    (Google Drive query fragments, Graph `$search`/`$filter`, etc.), so callers
    never construct provider-specific query strings themselves. Kept free of
    provider-specific operators so a future semantic-search layer can extend
    it without a breaking change.
    """

    keywords: list[str] = field(default_factory=list)  # OR-matched across name + full text
    date_from: datetime | None = None  # inclusive modified/lastModified lower bound
    date_to: datetime | None = None  # inclusive upper bound
    exclude_folders: bool = True  # drop folder/system items
    mime_types: list[str] | None = None  # optional positive type filter (None = any)
    max_results: int = 20


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    web_view_link: str
    parents: list[str] = field(default_factory=list)
    download_link: str | None = None


class DrivePort(Protocol):
    """Abstract Drive interface — implemented by Google Drive or any compatible provider.

    Keeps the agent and services decoupled from Google SDK specifics.
    """

    async def upload_file(
        self,
        usuario_id: uuid.UUID,
        name: str,
        content: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> DriveFile:
        """Upload a file and return its metadata. Optionally place in a folder."""
        ...

    async def get_or_create_folder(
        self,
        usuario_id: uuid.UUID,
        path: list[str],
        parent_id: str | None = None,
    ) -> str:
        """Traverse or create nested folders. Returns leaf folder_id."""
        ...

    async def list_files(
        self,
        usuario_id: uuid.UUID,
        folder_id: str,
        query: str | None = None,
    ) -> list[DriveFile]:
        """List files inside a folder, with optional filter query."""
        ...

    async def search_files(
        self,
        usuario_id: uuid.UUID,
        query: DriveQuery,
    ) -> list[DriveFile]:
        """Search across the user's entire Drive (no folder scope).

        ``query`` is a provider-neutral `DriveQuery`; the adapter translates it
        into its own native query language. Requires the ``drive.readonly``
        scope to see files the app did not create.
        """
        ...

    async def get_file(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
    ) -> DriveFile:
        """Get metadata for a single file."""
        ...

    async def download_file(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
    ) -> bytes:
        """Download file bytes by ID."""
        ...

    async def make_shareable(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
        role: str = "reader",
    ) -> str:
        """Grant public link access. Returns shareable web view URL."""
        ...

    async def delete_file(
        self,
        usuario_id: uuid.UUID,
        file_id: str,
    ) -> None:
        """Move file to trash."""
        ...
