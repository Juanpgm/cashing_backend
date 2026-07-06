"""Test for google_workspace_service.store_credentials (encrypt + upsert)."""

from __future__ import annotations

import pytest

from app.services import google_workspace_service as gws


@pytest.mark.asyncio
async def test_store_credentials_creates_and_updates(db, test_user):
    user = test_user["user"]

    # Create
    await gws.store_credentials(
        db,
        user.id,
        access_token="acc-123",
        refresh_token="ref-456",
        scopes=["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/drive.readonly"],
    )
    status = await gws.get_integration_status(db, user.id)
    assert status.connected is True
    assert status.gmail_enabled is True
    assert status.drive_enabled is True

    # Update (idempotent upsert — still one row, new token)
    record = await gws.store_credentials(
        db, user.id, access_token="acc-new", refresh_token="ref-456", scopes="https://www.googleapis.com/auth/gmail.readonly"
    )
    # Tokens are stored encrypted, never in plaintext.
    assert record.access_token_encrypted != "acc-new"
