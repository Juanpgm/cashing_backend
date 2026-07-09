"""Verify every MCP server imports and exposes tools with non-empty descriptions.

Run: PYTHONPATH=. uv run python scripts/verify_mcp_tools.py

Fails (exit 1) if any @mcp.tool() has an empty/whitespace description or if any
server exposes zero tools. This is what guarantees the tools are usable by an MCP
client — a tool without a description is effectively unusable by the LLM.
"""

from __future__ import annotations

import asyncio
import sys

SERVERS = [
    "mcp_servers.gmail_server",
    "mcp_servers.drive_server",
    "mcp_servers.calendar_server",
    "mcp_servers.evidence_server",
    "mcp_servers.filesystem_server",
]


async def _verify_stdio_servers() -> tuple[list[str], int]:
    import importlib

    failures: list[str] = []
    total_tools = 0

    for module_name in SERVERS:
        mod = importlib.import_module(module_name)
        mcp = getattr(mod, "mcp", None)
        if mcp is None:
            failures.append(f"{module_name}: no `mcp` FastMCP instance")
            continue

        tools = await mcp.list_tools()
        if not tools:
            failures.append(f"{module_name}: exposes 0 tools")
            continue

        print(f"\n{module_name}  ({len(tools)} tools)")
        for tool in tools:
            desc = (tool.description or "").strip()
            n_props = len((tool.inputSchema or {}).get("properties", {}))
            status = "OK " if desc else "MISSING-DESC"
            print(f"  [{status}] {tool.name}  ({n_props} params)  -> {desc[:70]}")
            total_tools += 1
            if not desc:
                failures.append(f"{module_name}.{tool.name}: empty description")

    return failures, total_tools


async def _verify_curated_server() -> tuple[list[str], int]:
    """Verify the curated backend MCP server (app.mcp.server) mirrors
    TOOL_REGISTRY exactly: same count, every tool has a non-empty description."""
    from app.mcp.server import get_mcp_server
    from app.tools.registry import TOOL_REGISTRY

    failures: list[str] = []

    mcp = get_mcp_server()
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    registry_names = set(TOOL_REGISTRY.keys())

    print(f"\napp.mcp.server (curated)  ({len(tools)} tools)")
    for tool in tools:
        desc = (tool.description or "").strip()
        n_props = len((tool.inputSchema or {}).get("properties", {}))
        status = "OK " if desc else "MISSING-DESC"
        print(f"  [{status}] {tool.name}  ({n_props} params)  -> {desc[:70]}")
        if not desc:
            failures.append(f"app.mcp.server.{tool.name}: empty description")

    if len(tools) != len(TOOL_REGISTRY):
        failures.append(
            f"app.mcp.server: tool count {len(tools)} != TOOL_REGISTRY count {len(TOOL_REGISTRY)}"
        )
    if tool_names != registry_names:
        missing = registry_names - tool_names
        extra = tool_names - registry_names
        if missing:
            failures.append(f"app.mcp.server: missing registry tools {sorted(missing)}")
        if extra:
            failures.append(f"app.mcp.server: leaked non-registry tools {sorted(extra)}")

    return failures, len(tools)


async def main() -> int:
    stdio_failures, stdio_total = await _verify_stdio_servers()
    curated_failures, curated_total = await _verify_curated_server()

    failures = stdio_failures + curated_failures
    total_tools = stdio_total + curated_total

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAIL — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"OK — {total_tools} tools across {len(SERVERS) + 1} servers, all with descriptions.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
