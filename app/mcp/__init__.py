"""MCP (Model Context Protocol) server package — curated tool surface.

`app.mcp.server` builds the FastMCP ASGI app from `app.tools.registry.TOOL_REGISTRY`
(the single source of truth for agent/MCP-exposed capabilities). `app.mcp.auth`
isolates the one place that reaches into FastMCP's request-context machinery to
recover the raw `Authorization` header for a tool call.
"""
