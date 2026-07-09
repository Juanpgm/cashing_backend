"""Bearer-token extraction for MCP tool invocations.

Isolated in its own module because FastMCP's request-context API is
version-sensitive (see the SDK investigation notes in `app.mcp.server`): if a
future `mcp` upgrade changes how the raw HTTP request is threaded through to a
tool call, only this function needs to change.

Current mechanism (mcp==1.28.0, `mcp.server.fastmcp`):
`Context.request_context.request` is the raw Starlette `Request` for the
in-flight streamable-http call — the SDK's streamable-http transport
(`mcp/server/streamable_http.py`) stashes it in `ServerMessageMetadata.request_context`,
which the lowlevel `Server` copies onto `RequestContext.request` before invoking
the tool handler. This is the same mechanism the SDK itself documents for
mounting multiple FastMCP servers behind one ASGI app.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context


def get_request_token(ctx: Context) -> str | None:
    """Extract the raw bearer token from the current call's Authorization header.

    Returns `None` when there is no active HTTP request, no Authorization
    header, or the header isn't a well-formed "Bearer <token>" value. Callers
    must treat `None` as "unauthenticated" and raise accordingly — this
    function never raises for a missing/malformed header, only for programmer
    error (e.g. calling it with something that isn't a `Context`).
    """
    try:
        request_context = ctx.request_context
    except ValueError:
        # "Context is not available outside of a request" — no in-flight MCP request.
        return None

    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return None

    auth_header = headers.get("authorization")
    if not auth_header:
        return None

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return token
