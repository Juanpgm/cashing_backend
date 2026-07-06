"""Storage adapter factory — returns local or S3 adapter based on STORAGE_PROVIDER."""

from __future__ import annotations

from app.core.config import settings


def get_storage(bucket: str) -> object:
    """Return the configured storage adapter for the given bucket.

    STORAGE_PROVIDER=local  → LocalStorageAdapter (no MinIO/S3 needed, dev default)
    STORAGE_PROVIDER=minio  → S3StorageAdapter pointing at local MinIO
    STORAGE_PROVIDER=s3     → S3StorageAdapter pointing at AWS S3 / Cloudflare R2
    """
    if settings.STORAGE_PROVIDER == "local":
        from app.adapters.storage.local_adapter import LocalStorageAdapter

        return LocalStorageAdapter(bucket=bucket)

    from app.adapters.storage.s3_adapter import S3StorageAdapter

    return S3StorageAdapter(bucket=bucket)
