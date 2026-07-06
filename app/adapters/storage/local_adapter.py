"""Local filesystem storage adapter — development without MinIO/S3.

Files are stored under LOCAL_STORAGE_PATH/{bucket}/{key}.
Set STORAGE_PROVIDER=local in your .env to activate this adapter.

Download strategy: local first → S3 fallback (when S3 credentials exist).
Files fetched from S3 are cached locally so subsequent reads are instant.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.config import settings


class LocalStorageAdapter:
    """Stores files on the local filesystem, mirroring the StoragePort contract.

    On download: checks local path first; falls back to S3/MinIO and caches the
    result locally so the next read is instant. Uploads always go to local only.
    """

    def __init__(self, bucket: str) -> None:
        self._bucket = bucket
        self._root = (Path(settings.LOCAL_STORAGE_PATH) / bucket).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        resolved = (self._root / key).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise ValueError(f"Path traversal attempt blocked: {key}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        path = self._path(key)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, path.write_bytes, data)
        return key

    async def download(self, key: str) -> bytes:
        path = self._path(key)
        loop = asyncio.get_running_loop()

        if path.exists():
            return await loop.run_in_executor(None, path.read_bytes)

        # S3 fallback — fetch and cache locally so next read is instant.
        if settings.S3_ACCESS_KEY and settings.S3_SECRET_KEY and settings.S3_ENDPOINT_URL:
            try:
                from app.adapters.storage.s3_adapter import S3StorageAdapter

                data = await S3StorageAdapter(bucket=self._bucket).download(key)
                await loop.run_in_executor(None, path.write_bytes, data)
                return data
            except Exception:
                pass

        raise FileNotFoundError(
            f'Could not find "{key}" in local storage'
            + (" or S3 fallback" if settings.S3_ACCESS_KEY else "")
            + "."
        )

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        # Dev only: files are not served via HTTP in local mode.
        return f"/local-storage/{self._bucket}/{key}"

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, path.unlink)
