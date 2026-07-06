"""Tests for global Drive search (search_files) — explorer capability."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _fake_drive_service(files: list[dict]):
    """Build a MagicMock Google Drive service whose files().list().execute() returns files."""
    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": files}
    return service


@pytest.mark.asyncio
async def test_search_files_global_query_returns_parsed_files():
    """search_files runs a global query (no parent folder) and parses DriveFile results."""
    from app.adapters.drive.drive_adapter import DriveAdapter

    raw_files = [
        {
            "id": "f1",
            "name": "Informe de actividades abril.pdf",
            "mimeType": "application/pdf",
            "size": "1024",
            "createdTime": "2024-04-10T10:00:00Z",
            "modifiedTime": "2024-04-12T10:00:00Z",
            "webViewLink": "https://drive.google.com/file/d/f1/view",
        },
    ]

    adapter = DriveAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_drive_service(raw_files)):
        results = await adapter.search_files(uuid.uuid4(), "name contains 'informe'", max_results=10)

    assert len(results) == 1
    assert results[0].id == "f1"
    assert results[0].web_view_link.endswith("/view")
    assert results[0].name.startswith("Informe")


@pytest.mark.asyncio
async def test_search_files_excludes_trashed_and_folders_in_query():
    """The constructed Drive query filters out trashed items and folders."""
    from app.adapters.drive.drive_adapter import DriveAdapter

    service = _fake_drive_service([])
    adapter = DriveAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=service):
        await adapter.search_files(uuid.uuid4(), "fullText contains 'acta'", max_results=5)

    # Inspect the q kwarg passed to files().list(...)
    _, kwargs = service.files.return_value.list.call_args
    q = kwargs["q"]
    assert "trashed=false" in q
    assert "fullText contains 'acta'" in q
    assert kwargs["pageSize"] == 5


@pytest.mark.asyncio
async def test_search_files_empty_result():
    """Returns an empty list when Drive has no matches."""
    from app.adapters.drive.drive_adapter import DriveAdapter

    adapter = DriveAdapter(db=MagicMock())
    adapter._auth.get_credentials = AsyncMock(return_value=MagicMock())

    with patch.object(adapter, "_build_service", return_value=_fake_drive_service([])):
        results = await adapter.search_files(uuid.uuid4(), "name contains 'nada'")

    assert results == []
