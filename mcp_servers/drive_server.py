"""Google Drive MCP Server — exposes Drive tools to AI agents.

Tools:
- upload_file: Upload a file to the user's Drive
- list_files: List files in a Drive folder
- create_folder: Create a new folder in Drive
- make_shareable: Generate a shareable link for a file/folder

Proxies requests to the CashIn FastAPI backend with Bearer auth.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("CASHIN_API_URL", "http://localhost:8000/api/v1")
BEARER_TOKEN = os.getenv("CASHIN_BEARER_TOKEN", "")

mcp = FastMCP("cashin-drive")

_HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}


@mcp.tool()
async def upload_file(
    local_path: str,
    drive_folder_id: str = "",
    filename: str = "",
) -> dict:  # type: ignore[type-arg]
    """Upload a local file to Google Drive.

    Args:
        local_path: Absolute path to the local file to upload.
        drive_folder_id: Google Drive folder ID (empty = My Drive root).
        filename: Override filename (uses original name if empty).

    Returns:
        Dict with drive_file_id, name, web_view_link.
    """
    path = Path(local_path)
    if not path.exists():
        return {"error": f"File not found: {local_path}"}

    name = filename or path.name
    content = path.read_bytes()
    encoded = base64.b64encode(content).decode()

    payload = {
        "name": name,
        "content_base64": encoded,
        "folder_id": drive_folder_id,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/drive/subir",
            headers={**_HEADERS, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def list_files(
    folder_id: str = "",
    query: str = "",
    max_results: int = 20,
) -> list[dict]:  # type: ignore[type-arg]
    """List files in a Google Drive folder.

    Args:
        folder_id: Google Drive folder ID (empty = My Drive root).
        query: Optional search query to filter files.
        max_results: Maximum number of files to return (1-100).

    Returns:
        List of file dicts with id, name, mimeType, modifiedTime, webViewLink.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/integraciones/drive/archivos",
            headers=_HEADERS,
            params={
                "folder_id": folder_id,
                "query": query,
                "max_results": min(max_results, 100),
            },
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def create_folder(
    name: str,
    parent_folder_id: str = "",
) -> dict:  # type: ignore[type-arg]
    """Create a new folder in Google Drive.

    Args:
        name: Folder name.
        parent_folder_id: Parent folder ID (empty = My Drive root).

    Returns:
        Dict with drive_folder_id, name, web_view_link.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/drive/carpetas",
            headers={**_HEADERS, "Content-Type": "application/json"},
            json={"name": name, "parent_id": parent_folder_id},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def make_shareable(
    file_id: str,
    role: str = "reader",
) -> dict:  # type: ignore[type-arg]
    """Make a Google Drive file or folder shareable via link.

    Args:
        file_id: Google Drive file or folder ID.
        role: Permission role: 'reader', 'commenter', or 'writer'.

    Returns:
        Dict with shareable_link.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/drive/compartir",
            headers={**_HEADERS, "Content-Type": "application/json"},
            json={"file_id": file_id, "role": role},
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
