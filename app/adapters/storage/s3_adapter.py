"""S3-compatible storage adapter for Cloudflare R2 and MinIO."""

from __future__ import annotations

import asyncio
from functools import partial

import boto3
from botocore.config import Config

from app.core.config import settings


class S3StorageAdapter:
    """Works with any S3-compatible service (Cloudflare R2, MinIO, AWS S3)."""

    def __init__(self, bucket: str | None = None) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        self._bucket = bucket or settings.S3_BUCKET_EVIDENCIAS

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            partial(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            ),
        )
        return key

    async def download(self, key: str) -> bytes:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            partial(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            ),
        )
        return response["Body"].read()

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        loop = asyncio.get_running_loop()
        url: str = await loop.run_in_executor(
            None,
            partial(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            ),
        )
        return url

    async def delete(self, key: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            partial(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=key,
            ),
        )


def get_storage() -> S3StorageAdapter:
    """Factory — returns the storage adapter based on config.

    Since R2, MinIO, and S3 are all S3-compatible, a single adapter suffices.
    """
    return S3StorageAdapter()
