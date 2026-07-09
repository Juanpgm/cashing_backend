"""Curated MCP server — exposes exactly `app.tools.registry.TOOL_REGISTRY`, nothing else.

Design notes (read before touching this file):

1. **Tool construction bypasses `FastMCP.tool()`/`add_tool()`.** Those build a
   tool's JSON schema by introspecting a Python function's *signature*
   (`mcp.server.fastmcp.utilities.func_metadata.func_metadata`). Our tools
   already have first-class Pydantic `input_model`/`output_model`s (with
   field descriptions, constraints, etc. — see `app.tools.registry.ToolSpec`);
   re-deriving an equivalent function signature would be more code and lossier
   than just handing the SDK the model's own `model_json_schema()`. Instead we
   hand-assemble `mcp.server.fastmcp.tools.base.Tool` objects (the SDK's
   internal tool record) and insert them directly into the `FastMCP` instance's
   `ToolManager._tools` dict. This reaches past a private attribute
   (`_tool_manager._tools`), which is the pragmatic tradeoff called out in the
   task: `Tool`/`FuncMetadata` are stable-enough building blocks within one
   `mcp` minor version, and this file is the single place that would need to
   change on an SDK upgrade.

2. **Auth**: see `app.mcp.auth.get_request_token` — isolates the
   version-sensitive part (recovering the raw HTTP Authorization header from
   FastMCP's request context).

3. **Lifespan**: `mcp_asgi_app()` returns a Starlette app whose own
   `lifespan=` (set by `FastMCP.streamable_http_app()`) is *never invoked* when
   mounted via `app.mount()` — Starlette's `Router.lifespan` only enters its
   own `lifespan_context`; it does not recurse into mounted sub-apps' lifespans.
   The `StreamableHTTPSessionManager` backing this app therefore has to be
   started/stopped explicitly by the parent FastAPI app. `app.main.lifespan`
   does this via `get_mcp_server().session_manager.run()` — see the comment
   there. This is also the pattern the SDK itself documents on
   `FastMCP.session_manager`: "exposed to enable advanced use cases like
   mounting multiple FastMCP servers in a single FastAPI application."
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from starlette.types import ASGIApp

from app.core import database
from app.core.auth import authenticate_bearer
from app.mcp.auth import get_request_token
from app.tools import catalog  # noqa: F401 — import-for-side-effect: populates TOOL_REGISTRY
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY, ToolSpec

log = structlog.get_logger("mcp.server")

_mcp: FastMCP | None = None


def _build_arg_model(spec: ToolSpec) -> type[ArgModelBase]:
    """Combine `spec.input_model`'s fields with `ArgModelBase`'s
    `model_dump_one_level()` (needed by FastMCP's internal arg validation)."""
    return type(f"{spec.name}_Args", (spec.input_model, ArgModelBase), {})


def _make_wrapper(spec: ToolSpec):
    """Build the per-tool callable FastMCP invokes for `spec`.

    Steps: extract + validate the bearer token, open a fresh session, run the
    tool through the same `invoke_tool` dispatch the agent graph will
    eventually use, commit for write-tagged tools (catalog wrappers are
    flush-only — the session owner commits), and return a JSON-safe dict.
    Any exception (auth failure, domain error, validation error) propagates:
    FastMCP's `Tool.run()` wraps it in a `ToolError`, and the lowlevel server's
    `call_tool` handler turns *that* into a proper `CallToolResult(isError=True)`
    — never a raw 500 or an unhandled crash.
    """

    async def wrapper(**kwargs: Any) -> dict[str, Any]:
        ctx: Context = kwargs.pop("ctx")

        token = get_request_token(ctx)
        if token is None:
            raise PermissionError("Missing or malformed 'Authorization: Bearer <token>' header.")

        async with database.async_session_factory() as session:
            try:
                usuario = await authenticate_bearer(token, session)
                tool_ctx = ToolContext(db=session, usuario=usuario)
                output = await invoke_tool(spec.name, tool_ctx, kwargs)
            except Exception:
                await session.rollback()
                raise

            if "write" in spec.tags:
                await session.commit()

            return output.model_dump(mode="json")

    wrapper.__name__ = spec.name
    return wrapper


def _register_tool(mcp: FastMCP, spec: ToolSpec) -> None:
    fn_metadata = FuncMetadata(
        arg_model=_build_arg_model(spec),
        output_schema=spec.output_model.model_json_schema(),
        output_model=spec.output_model,
        wrap_output=False,
    )
    tool = Tool(
        fn=_make_wrapper(spec),
        name=spec.name,
        title=None,
        description=spec.description,
        parameters=spec.input_model.model_json_schema(),
        fn_metadata=fn_metadata,
        is_async=True,
        context_kwarg="ctx",
    )
    mcp._tool_manager._tools[spec.name] = tool


def build_mcp_server() -> FastMCP:
    """Build (once, lazily) the curated `FastMCP` server from `TOOL_REGISTRY`."""
    global _mcp
    if _mcp is not None:
        return _mcp

    mcp = FastMCP(
        name="CashIn MCP",
        instructions=(
            "Curated CashIn backend tools: cuentas de cobro (invoices), document "
            "checklist, SECOP lookups, informes, and evidence — scoped to the "
            "authenticated user. Authentication, payments, and credit management "
            "are never exposed here."
        ),
        # Left at the SDK default ("/mcp") on purpose — see the mount comment in
        # app/main.py for why the mount is at path="" rather than "/mcp".
    )
    for spec in TOOL_REGISTRY.values():
        _register_tool(mcp, spec)

    _mcp = mcp
    return _mcp


def get_mcp_server() -> FastMCP:
    """Return the singleton curated `FastMCP` server, building it if needed."""
    return build_mcp_server()


def mcp_asgi_app() -> ASGIApp:
    """Return the streamable-http ASGI app for mounting at `/mcp`.

    See the module docstring for why the caller (`app.main`) must separately
    wire `get_mcp_server().session_manager.run()` into its own lifespan.
    """
    return get_mcp_server().streamable_http_app()
