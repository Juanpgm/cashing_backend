"""Storage port (interface) for file persistence."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StorageObjectInfo:
    """Metadata for a single object returned by `StoragePort.list_objects` —
    no bytes are read, just the key and size (radicacion-stepper, work unit B5)."""

    key: str
    size_bytes: int


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
        """List objects under a key prefix — key + size only, no data read.

        Used for existence/metadata checks (e.g. `GET /paquete`) without
        downloading or packaging anything.
        """
        ...
