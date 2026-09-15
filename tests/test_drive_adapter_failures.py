"""Network-failure-injection tests for DriveAdapter — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md ~L222).

Before this slice, ONLY `search_files` wrapped `GoogleHttpError` into the domain
`ExternalServiceError`. Every other method (`upload_file`, `_find_folder`,
`_create_folder`, `list_files`, `get_file`, `download_file`, `make_shareable`,
`delete_file`) leaked the raw googleapiclient/transport exception straight
through the API boundary — a generic 500 instead of the documented 502
`ExternalServiceError` contract. `_parse_file`'s `raw["id"]` also raised a raw
`KeyError` on a malformed/partial file payload.

Matrix per method: transport timeout, 401/403, 5xx, and (where a list is
built) malformed-item skip-and-log, mirroring `GoogleCalendarAdapter.search_events`'s
existing per-item tolerance.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from app.adapters.drive.drive_adapter import DriveAdapter
from app.core.exceptions import ExternalServiceError
from googleapiclient.errors import HttpError as GoogleHttpError


def _http_error(status: int, message: str = "error") -> GoogleHttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    content = json.dumps({"error": {"message": message, "code": status}}).encode()
    return GoogleHttpError(resp=resp, content=content)


def _make_adapter(service: MagicMock) -> DriveAdapter:
    adapter = DriveAdapter.__new__(DriveAdapter)
    adapter._auth = MagicMock()
    adapter._auth.get_credentials = MagicMock(return_value=None)

    async def _get_credentials(*_args: object, **_kwargs: object) -> MagicMock:
        return MagicMock()

    adapter._auth.get_credentials = _get_credentials
    adapter._build_service = MagicMock(return_value=service)
    return adapter


_VALID_FILE = {
    "id": "f1",
    "name": "informe.pdf",
    "mimeType": "application/pdf",
    "size": "1024",
    "createdTime": "2024-01-01T00:00:00Z",
    "modifiedTime": "2024-01-02T00:00:00Z",
    "webViewLink": "https://drive.google.com/file/d/f1/view",
}


class TestUploadFileFailures:
    @pytest.mark.asyncio
    async def test_401_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.upload_file(uuid.uuid4(), "a.pdf", b"x", "application/pdf")

    @pytest.mark.asyncio
    async def test_5xx_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.side_effect = _http_error(503, "unavailable")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.upload_file(uuid.uuid4(), "a.pdf", b"x", "application/pdf")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.side_effect = TimeoutError("socket timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.upload_file(uuid.uuid4(), "a.pdf", b"x", "application/pdf")

    @pytest.mark.asyncio
    async def test_malformed_response_missing_id_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.return_value = {"name": "a.pdf"}  # no "id"
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.upload_file(uuid.uuid4(), "a.pdf", b"x", "application/pdf")

    @pytest.mark.asyncio
    async def test_well_formed_response_still_uploads_successfully(self) -> None:
        service = MagicMock()
        service.files().create().execute.return_value = dict(_VALID_FILE)
        adapter = _make_adapter(service)

        result = await adapter.upload_file(uuid.uuid4(), "informe.pdf", b"x", "application/pdf")

        assert result.id == "f1"
        assert result.name == "informe.pdf"


class TestFindAndCreateFolderFailures:
    @pytest.mark.asyncio
    async def test_find_folder_403_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().list().execute.side_effect = _http_error(403, "forbidden")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter._find_folder(uuid.uuid4(), "Contrato-001", None)

    @pytest.mark.asyncio
    async def test_find_folder_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().list().execute.side_effect = OSError("connection reset")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter._find_folder(uuid.uuid4(), "Contrato-001", None)

    @pytest.mark.asyncio
    async def test_create_folder_5xx_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.side_effect = _http_error(500, "boom")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter._create_folder(uuid.uuid4(), "Contrato-001", None)

    @pytest.mark.asyncio
    async def test_create_folder_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().create().execute.side_effect = TimeoutError("timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter._create_folder(uuid.uuid4(), "Contrato-001", None)


class TestListFilesFailures:
    @pytest.mark.asyncio
    async def test_401_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().list().execute.side_effect = _http_error(401, "unauthorized")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.list_files(uuid.uuid4(), "folder1")

    @pytest.mark.asyncio
    async def test_5xx_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().list().execute.side_effect = _http_error(502, "bad gateway")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.list_files(uuid.uuid4(), "folder1")

    @pytest.mark.asyncio
    async def test_malformed_item_is_skipped_not_raised(self) -> None:
        """Mirrors GoogleCalendarAdapter.search_events: one bad item must not
        drop the whole result set, and must not crash the request."""
        service = MagicMock()
        service.files().list().execute.return_value = {
            "files": [dict(_VALID_FILE), {"name": "sin-id.pdf"}]  # second item missing "id"
        }
        adapter = _make_adapter(service)

        result = await adapter.list_files(uuid.uuid4(), "folder1")

        assert len(result) == 1
        assert result[0].id == "f1"

    @pytest.mark.asyncio
    async def test_empty_files_key_returns_empty_list(self) -> None:
        service = MagicMock()
        service.files().list().execute.return_value = {}
        adapter = _make_adapter(service)

        result = await adapter.list_files(uuid.uuid4(), "folder1")

        assert result == []


class TestSearchFilesFailureMatrix:
    """search_files already wrapped GoogleHttpError before this slice — extends
    it to transport errors and per-item malformed tolerance, consistent with
    every other Drive method fixed here."""

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        from app.adapters.drive.port import DriveQuery

        service = MagicMock()
        service.files().list().execute.side_effect = TimeoutError("socket timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.search_files(uuid.uuid4(), DriveQuery(keywords=[]))

    @pytest.mark.asyncio
    async def test_malformed_item_is_skipped_not_raised(self) -> None:
        from app.adapters.drive.port import DriveQuery

        service = MagicMock()
        service.files().list().execute.return_value = {"files": [dict(_VALID_FILE), {"name": "sin-id.pdf"}]}
        adapter = _make_adapter(service)

        result = await adapter.search_files(uuid.uuid4(), DriveQuery(keywords=[]))

        assert len(result) == 1


class TestGetFileFailures:
    @pytest.mark.asyncio
    async def test_404_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().get().execute.side_effect = _http_error(404, "not found")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_file(uuid.uuid4(), "f1")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().get().execute.side_effect = TimeoutError("timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_file(uuid.uuid4(), "f1")

    @pytest.mark.asyncio
    async def test_malformed_response_missing_id_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().get().execute.return_value = {"name": "sin-id.pdf"}
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.get_file(uuid.uuid4(), "f1")


class TestDownloadFileFailures:
    @pytest.mark.asyncio
    async def test_403_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().get_media.side_effect = _http_error(403, "forbidden")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.download_file(uuid.uuid4(), "f1")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().get_media.side_effect = TimeoutError("timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.download_file(uuid.uuid4(), "f1")


class TestMakeShareableFailures:
    @pytest.mark.asyncio
    async def test_5xx_is_wrapped(self) -> None:
        service = MagicMock()
        service.permissions().create().execute.side_effect = _http_error(500, "boom")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.make_shareable(uuid.uuid4(), "f1")

    @pytest.mark.asyncio
    async def test_transport_os_error_is_wrapped(self) -> None:
        service = MagicMock()
        service.permissions().create().execute.side_effect = OSError("connection reset")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.make_shareable(uuid.uuid4(), "f1")


class TestDeleteFileFailures:
    @pytest.mark.asyncio
    async def test_404_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().update().execute.side_effect = _http_error(404, "not found")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.delete_file(uuid.uuid4(), "f1")

    @pytest.mark.asyncio
    async def test_transport_timeout_is_wrapped(self) -> None:
        service = MagicMock()
        service.files().update().execute.side_effect = TimeoutError("timed out")
        adapter = _make_adapter(service)

        with pytest.raises(ExternalServiceError):
            await adapter.delete_file(uuid.uuid4(), "f1")
