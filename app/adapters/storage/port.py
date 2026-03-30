"""Storage port (interface) for file persistence."""

from typing import Protocol


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
