"""Storage port (interface) for file persistence."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class StorageObjectInfo:
    """Metadata for a single object returned by `StoragePort.list_objects` or
    `StoragePort.stat` — no bytes are read, just key/size/mtime (radicacion-stepper,
    work unit B5). `last_modified` is `None` only if an adapter genuinely can't
    determine it — callers doing age-gated deletes must treat that as "too young"."""

    key: str
    size_bytes: int
    last_modified: datetime | None = None


class StoragePort(Protocol):
    """Abstract storage interface — implemented by R2, MinIO, or GCS adapters."""

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload bytes and return the storage key."""
        ...

    async def download(self, key: str) -> bytes:
        """Download file content by key."""
        ...

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a time-limited presigned download URL."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a file by key."""
        ...

    async def list_objects(self, prefix: str) -> list[StorageObjectInfo]:
        """List objects under a key prefix — key + size only, no data read."""
        ...

    async def stat(self, key: str) -> StorageObjectInfo | None:
        """Metadata for a SINGLE known key, or `None` if it doesn't exist.

        Prefer this over `list_objects(prefix)` + filter for an
        existence/size check on a key you already know exactly (e.g. `GET
        /paquete`'s deterministic package key) — cheaper than scanning an
        entire prefix for one entry.
        """
        ...
