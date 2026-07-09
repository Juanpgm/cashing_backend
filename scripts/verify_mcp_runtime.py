"""End-to-end MCP runtime check — spawns each server over real stdio and does the handshake.

Unlike verify_mcp_tools.py (which only imports the module in-process), this launches every
server as a SUBPROCESS exactly like a real MCP client would, performs the JSON-RPC
`initialize` handshake, lists tools, and shuts it down. This is what proves the servers
actually RUN without crashing — not just that they import.

Run: PYTHONPATH=. uv run python scripts/verify_mcp_runtime.py

Exit 0 only if every server starts, initializes, and returns a non-empty tool list.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BACKEND_DIR / "mcp_servers" / "mcp_config.json"

# Per-server startup timeout (seconds). Cold Python start on Windows can be slow.
STARTUP_TIMEOUT = 30.0


def _module_to_script(module: str) -> Path:
    """mcp_servers.gmail_server -> <backend>/mcp_servers/gmail_server.py"""
    return BACKEND_DIR / (module.replace(".", os.sep) + ".py")


async def _probe(name: str, module: str) -> tuple[str, bool, str]:
    """Spawn one server over stdio, initialize, list tools. Returns (name, ok, detail)."""
    script = _module_to_script(module)
    if not script.exists():
        return name, False, f"script not found: {script}"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    # Deterministic dummy config so no server blocks waiting on real secrets at startup.
    env.setdefault("CASHIN_BEARER_TOKEN", "runtime-check-token")
    env.setdefault("CASHIN_API_URL", "http://localhost:8000/api/v1")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(script)],
        env=env,
        cwd=str(BACKEND_DIR),
    )

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=STARTUP_TIMEOUT)
                result = await asyncio.wait_for(session.list_tools(), timeout=STARTUP_TIMEOUT)
                tools = result.tools
                if not tools:
                    return name, False, "initialized but exposed 0 tools"
                names = ", ".join(t.name for t in tools)
                return name, True, f"{len(tools)} tools: {names}"
    except asyncio.TimeoutError:
        return name, False, f"timed out after {STARTUP_TIMEOUT}s during handshake"
    except Exception as exc:  # noqa: BLE001 — we want to report any crash cause
        return name, False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    servers = config.get("servers", {})
    enabled = [(n, s["module"]) for n, s in servers.items() if s.get("enabled", True)]

    print(f"Probing {len(enabled)} MCP servers over real stdio transport...\n")

    # Sequential (not parallel) so a crash's stderr is easy to attribute.
    results = []
    for name, module in enabled:
        res = await _probe(name, module)
        results.append(res)
        mark = "OK  " if res[1] else "FAIL"
        print(f"  [{mark}] {res[0]:<12} {res[2]}")

    print(f"\n{'=' * 60}")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"FAIL — {len(failed)}/{len(results)} server(s) did not run cleanly.")
        return 1
    print(f"OK — all {len(results)} MCP servers start, initialize, and list tools without crashing.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
