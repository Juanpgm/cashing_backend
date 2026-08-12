"""Local filesystem storage adapter — development without MinIO/S3.

Files are stored under LOCAL_STORAGE_PATH/{bucket}/{key}.
Set STORAGE_PROVIDER=local in your .env to activate this adapter.

Download strategy: local first → S3 fallback (when S3 credentials exist).
Files fetched from S3 are cached locally so subsequent reads are instant.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from app.adapters.storage.port import StorageObjectInfo
from app.core.config import settings


def _winsafe(path: Path) -> Path:
    """Prefix with \\\\?\\ on Windows to lift the 260-char MAX_PATH limit."""
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


class LocalStorageAdapter:
    """Stores files on the local filesystem, mirroring the StoragePort contract.

    On download: checks local path first; falls back to S3/MinIO and caches the
    result locally so the next read is instant. Uploads always go to local only.
    """

    def __init__(self, bucket: str) -> None:
        self._bucket = bucket
        self._root = (Path(settings.LOCAL_STORAGE_PATH) / bucket).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve `key` to an absolute path under `self._root` without creating
        any directories — used by read-only operations like `stat()`."""
        resolved = (self._root / key).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise ValueError(f"Path traversal attempt blocked: {key}")
        return _winsafe(resolved)

    def _path(self, key: str) -> Path:
        resolved = self._resolve(key)
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
        await loop.run_in_executor(None, self._write_atomic, path, data)
        return key

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """Write to a sibling temp file, then atomically rename onto the final
        path. `os.replace` is atomic on both POSIX and Windows for same-
        filesystem renames, so a concurrent `download()` never observes a
        torn/partial file while a write is in progress or fails midway."""
        tmp_path = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, path)
        finally:
            tmp_path.unlink(missing_ok=True)

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
            f'Could not find "{key}" in local storage' + (" or S3 fallback" if settings.S3_ACCESS_KEY else "") + "."
        )

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        # Dev only: files are not served via HTTP in local mode.
        return f"/local-storage/{self._bucket}/{key}"

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, path.unlink)

    async def list_objects(self, prefix: str) -> list[StorageObjectInfo]:
        base = (self._root / prefix).resolve()
        if not str(base).startswith(str(self._root)):
            raise ValueError(f"Path traversal attempt blocked: {prefix}")
        loop = asyncio.get_running_loop()

        def _scan() -> list[StorageObjectInfo]:
            safe_base = _winsafe(base)
            safe_root = _winsafe(self._root)
            if not safe_base.exists():
                return []
            return [
                StorageObjectInfo(
                    key=str(path.relative_to(safe_root)).replace("\\", "/"),
                    size_bytes=path.stat().st_size,
                )
                for path in sorted(safe_base.rglob("*"))
                if path.is_file()
            ]

        return await loop.run_in_executor(None, _scan)

    async def stat(self, key: str) -> StorageObjectInfo | None:
        """Metadata for a single known key — cheaper than `list_objects` +
        filter when the caller already knows the exact key (e.g. `GET
        /paquete`'s deterministic package key)."""
        path = self._resolve(key)
        loop = asyncio.get_running_loop()

        def _stat() -> StorageObjectInfo | None:
            if not path.exists():
                return None
            return StorageObjectInfo(key=key, size_bytes=path.stat().st_size)

        return await loop.run_in_executor(None, _stat)
