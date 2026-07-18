"""Tests for `StoragePort.list_objects` (radicacion-stepper, work unit B5):
a storage-prefix read used by `GET /paquete` to check package existence
without downloading or packaging. Covers both adapters — `LocalStorageAdapter`
(real filesystem, `tmp_path`) and `S3StorageAdapter` (moto-mocked S3, per the
`moto[s3]` dev dependency already declared for this purpose).
"""

from __future__ import annotations

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
