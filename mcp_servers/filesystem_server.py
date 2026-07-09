"""MCP Filesystem Server — indexes local files and folders for the agent (Phase 7).

Exposes tools:
  - list_folder(path): list contents of a folder
  - read_file(path): read a text file (UTF-8, max 50 KB)
  - search_files(folder, pattern): glob-match filenames
  - index_folder(path): walk a tree and return metadata for all files

Run standalone:
    python -m mcp_servers.filesystem_server

Or mount via mcp_config.json.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import structlog
from mcp.server.fastmcp import FastMCP

logger = structlog.get_logger("mcp.filesystem")

mcp = FastMCP("cashin-filesystem")

# Security: restrict to user-configured allowed roots
_DEFAULT_ALLOWED_ROOTS: list[str] = [
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Desktop"),
]

MAX_FILE_BYTES = 50 * 1024  # 50 KB read limit


def _is_allowed(path: str, allowed_roots: list[str]) -> bool:
    """Check that path is under an allowed root (path traversal protection)."""
    resolved = os.path.realpath(path)
    for root in allowed_roots:
        root_resolved = os.path.realpath(root)
        try:
            Path(resolved).relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def list_folder(path: str, allowed_roots: list[str] | None = None) -> dict:
    """List contents of a local folder.

    Returns a dict with 'entries' (list of {name, type, size_bytes}).
    """
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    if not _is_allowed(path, roots):
        return {"error": f"Path not in allowed roots: {path}"}

    if not os.path.isdir(path):
        return {"error": f"Not a directory: {path}"}

    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            entry_type = "directory" if os.path.isdir(full) else "file"
            size = os.path.getsize(full) if entry_type == "file" else 0
            entries.append({"name": name, "type": entry_type, "size_bytes": size})
    except PermissionError as exc:
        return {"error": str(exc)}

    return {"path": path, "entries": entries}


def read_file(path: str, allowed_roots: list[str] | None = None) -> dict:
    """Read a text file (UTF-8, max 50 KB).

    Returns a dict with 'content' (str) or 'error'.
    """
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    if not _is_allowed(path, roots):
        return {"error": f"Path not in allowed roots: {path}"}

    if not os.path.isfile(path):
        return {"error": f"Not a file: {path}"}

    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        return {"error": f"File too large ({size} bytes > {MAX_FILE_BYTES})"}

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return {"path": path, "content": content, "size_bytes": size}
    except OSError as exc:
        return {"error": str(exc)}


def search_files(folder: str, pattern: str, allowed_roots: list[str] | None = None) -> dict:
    """Glob-match filenames under a folder (recursive).

    Returns a dict with 'matches' (list of absolute paths).
    """
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    if not _is_allowed(folder, roots):
        return {"error": f"Path not in allowed roots: {folder}"}

    if not os.path.isdir(folder):
        return {"error": f"Not a directory: {folder}"}

    glob_pattern = os.path.join(folder, "**", pattern)
    matches = glob.glob(glob_pattern, recursive=True)
    # Filter to only files (not dirs) and sort
    file_matches = sorted(p for p in matches if os.path.isfile(p))
    return {"folder": folder, "pattern": pattern, "matches": file_matches}


def index_folder(path: str, allowed_roots: list[str] | None = None, max_files: int = 200) -> dict:
    """Walk a directory tree and return metadata for all files.

    Returns a dict with 'files' (list of {path, name, ext, size_bytes}).
    Stops after max_files to prevent runaway indexing.
    """
    roots = allowed_roots or _DEFAULT_ALLOWED_ROOTS
    if not _is_allowed(path, roots):
        return {"error": f"Path not in allowed roots: {path}"}

    if not os.path.isdir(path):
        return {"error": f"Not a directory: {path}"}

    files: list[dict] = []
    truncated = False
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in sorted(filenames):
            if len(files) >= max_files:
                truncated = True
                break
            full = os.path.join(dirpath, fname)
            ext = Path(fname).suffix.lower()
            files.append(
                {
                    "path": full,
                    "name": fname,
                    "ext": ext,
                    "size_bytes": os.path.getsize(full),
                }
            )
        if truncated:
            break

    return {"path": path, "files": files, "truncated": truncated, "count": len(files)}


# ── FastMCP tools (thin wrappers over the pure functions above) ──────────────


@mcp.tool()
async def filesystem_list_folder(path: str) -> dict:  # type: ignore[type-arg]
    """List the direct contents of a local folder (must be within allowed roots).

    Args:
        path: Absolute path to the folder to list. Must resolve under one of the
            allowed roots (~/Documents, ~/Downloads, ~/Desktop) or it is rejected.

    Returns:
        Dict with 'path' and 'entries' (list of {name, type, size_bytes}), or
        {'error': ...} if the path is disallowed or not a directory.
    """
    return list_folder(path)


@mcp.tool()
async def filesystem_read_file(path: str) -> dict:  # type: ignore[type-arg]
    """Read the text content of a local file (UTF-8, max 50 KB).

    Args:
        path: Absolute path to the file. Must resolve under an allowed root and be
            no larger than 50 KB.

    Returns:
        Dict with 'path', 'content' (str) and 'size_bytes', or {'error': ...} if the
        path is disallowed, missing, or too large.
    """
    return read_file(path)


@mcp.tool()
async def filesystem_search_files(folder: str, pattern: str) -> dict:  # type: ignore[type-arg]
    """Search recursively for files matching a glob pattern under a folder.

    Args:
        folder: Absolute path to the root folder to search (must be under an allowed root).
        pattern: Glob pattern to match filenames, e.g. "*.pdf" or "informe_*.docx".

    Returns:
        Dict with 'folder', 'pattern' and 'matches' (list of absolute file paths), or
        {'error': ...} if the folder is disallowed or not a directory.
    """
    return search_files(folder, pattern)


@mcp.tool()
async def filesystem_index_folder(path: str) -> dict:  # type: ignore[type-arg]
    """Walk a folder tree and return file metadata (never file content).

    Args:
        path: Absolute path to the root folder to index (must be under an allowed root).
            Indexing stops after 200 files to prevent runaway walks.

    Returns:
        Dict with 'path', 'files' (list of {path, name, ext, size_bytes}), 'count' and
        'truncated' (True if the 200-file cap was hit), or {'error': ...} if disallowed.
    """
    return index_folder(path)


# ── Legacy in-process tool registry (kept for non-MCP callers) ───────────────

TOOLS: dict[str, dict] = {
    "filesystem_list_folder": {
        "description": "List the contents of a local folder",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the folder"},
            },
            "required": ["path"],
        },
        "handler": lambda args: list_folder(args["path"]),
    },
    "filesystem_read_file": {
        "description": "Read a local text file (UTF-8, max 50 KB)",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["path"],
        },
        "handler": lambda args: read_file(args["path"]),
    },
    "filesystem_search_files": {
        "description": "Search for files matching a glob pattern under a folder",
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "description": "Root folder to search in"},
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.pdf'"},
            },
            "required": ["folder", "pattern"],
        },
        "handler": lambda args: search_files(args["folder"], args["pattern"]),
    },
    "filesystem_index_folder": {
        "description": "Index all files in a folder tree (returns metadata, not content)",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Root folder to index"},
            },
            "required": ["path"],
        },
        "handler": lambda args: index_folder(args["path"]),
    },
}


def call_tool(name: str, arguments: dict) -> dict:
    """Dispatch a tool call by name and return the result."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return tool["handler"](arguments)
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run()
