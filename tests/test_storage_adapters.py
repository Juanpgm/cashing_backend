"""Tests for `StoragePort.list_objects` (radicacion-stepper, work unit B5):
a storage-prefix read used by `GET /paquete` to check package existence
without downloading or packaging. Covers both adapters — `LocalStorageAdapter`
(real filesystem, `tmp_path`) and `S3StorageAdapter` (moto-mocked S3, per the
`moto[s3]` dev dependency already declared for this purpose).
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from app.adapters.storage.local_adapter import LocalStorageAdapter
from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.core.config import settings
from moto import mock_aws

pytestmark = pytest.mark.asyncio


# ── LocalStorageAdapter ───────────────────────────────────────────────────


async def test_local_list_objects_returns_empty_when_prefix_has_no_files(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    adapter = LocalStorageAdapter(bucket="test-bucket")

    result = await adapter.list_objects("paquetes/usuario-x/cuenta-y/")

    assert result == []


async def test_local_upload_interrupted_write_leaves_original_file_untouched(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RESILIENCE-001: `upload()` writes to a sibling temp file then
    atomically renames onto the final path (`os.replace`), so a write that
    crashes mid-way never corrupts (or partially overwrites) bytes a
    concurrent `download()` could already be reading."""
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    adapter = LocalStorageAdapter(bucket="test-bucket")
    key = "paquetes/u1/c1/paquete.zip"
    await adapter.upload(key, b"original-bytes", "application/zip")

    def _boom(self: Path, data: bytes) -> int:
        # Simulate a real interrupted write: some bytes actually land on
        # whichever path is targeted, then it crashes — mirroring a disk-full
        # or killed-process mid-write, not a clean no-op failure.
        with self.open("wb") as f:
            f.write(data[: len(data) // 2])
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(Path, "write_bytes", _boom)

    with pytest.raises(OSError):
        await adapter.upload(key, b"new-bytes-that-must-never-land", "application/zip")

    assert await adapter.download(key) == b"original-bytes"


async def test_local_upload_leaves_no_leftover_temp_file_after_success(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    adapter = LocalStorageAdapter(bucket="test-bucket")

    await adapter.upload("paquetes/u1/c1/paquete.zip", b"1234", "application/zip")

    leftovers = list(Path(str(tmp_path)).rglob("*.tmp-*"))
    assert leftovers == []


async def test_local_list_objects_returns_key_and_size_for_matching_prefix(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    adapter = LocalStorageAdapter(bucket="test-bucket")

    await adapter.upload("paquetes/u1/c1/evidencias-CTR-1-2024-01.zip", b"1234", "application/zip")
    await adapter.upload("paquetes/u1/other-cuenta/evidencias-CTR-1-2024-01.zip", b"12", "application/zip")

    result = await adapter.list_objects("paquetes/u1/c1/")

    assert len(result) == 1
    assert result[0].key == "paquetes/u1/c1/evidencias-CTR-1-2024-01.zip"
    assert result[0].size_bytes == 4


# ── S3StorageAdapter ──────────────────────────────────────────────────────


async def test_s3_list_objects_returns_only_keys_under_prefix() -> None:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket-s3")
        adapter = S3StorageAdapter(bucket="test-bucket-s3")

        await adapter.upload("paquetes/u1/c1/evidencias-CTR-1-2024-01.zip", b"1234", "application/zip")
        await adapter.upload("paquetes/u1/other-cuenta/evidencias-CTR-1-2024-01.zip", b"12", "application/zip")

        result = await adapter.list_objects("paquetes/u1/c1/")

    assert len(result) == 1
    assert result[0].key == "paquetes/u1/c1/evidencias-CTR-1-2024-01.zip"
    assert result[0].size_bytes == 4


async def test_s3_list_objects_returns_empty_when_prefix_has_no_objects() -> None:
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-bucket-s3-empty")
        adapter = S3StorageAdapter(bucket="test-bucket-s3-empty")

        result = await adapter.list_objects("paquetes/nobody/nothing/")

    assert result == []
