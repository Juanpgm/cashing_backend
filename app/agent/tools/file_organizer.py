"""File organizer tool — create structured evidence folders in storage."""

from __future__ import annotations

import uuid

import structlog

from app.adapters.storage.s3_adapter import S3StorageAdapter

logger = structlog.get_logger("agent.tools.file_organizer")


async def organize_evidence_folder(
    user_id: uuid.UUID,
    cuenta_cobro_id: uuid.UUID,
    periodo: str,
) -> str:
    """Create a well-structured folder key prefix in object storage.

    Returns the base prefix key for the organized folder.
    """
    prefix = f"usuarios/{user_id}/cuentas/{cuenta_cobro_id}/{periodo}"
    subfolders = ["contrato", "evidencias", "soportes", "cuenta_cobro_final"]

    adapter = S3StorageAdapter()
    for folder in subfolders:
        key = f"{prefix}/{folder}/.keep"
        await adapter.upload(key=key, data=b"", content_type="application/octet-stream")

    await logger.ainfo("folder_organized", prefix=prefix)
    return prefix
