"""Runtime dispatch for registered tools.

`invoke_tool` is the single call site an MCP server (or the agent graph, later)
uses to run a capability by name: it looks the tool up in `TOOL_REGISTRY`,
validates the raw params against its `input_model`, and awaits the handler.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.exceptions import NotFoundError
from app.tools.context import ToolContext
from app.tools.registry import TOOL_REGISTRY, ToolSpec


def list_tools() -> list[ToolSpec]:
    """Return every registered `ToolSpec`, in registration order."""
    return list(TOOL_REGISTRY.values())


async def invoke_tool(name: str, ctx: ToolContext, params: dict[str, Any] | BaseModel) -> BaseModel:
    """Validate `params` against tool `name`'s input_model and run its handler.

    Raises `NotFoundError` if `name` isn't registered. Raises pydantic's
    `ValidationError` if `params` doesn't satisfy the tool's `input_model`.
    Any domain exception raised by the underlying service propagates unchanged.
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise NotFoundError("Tool", name)

    raw = params.model_dump() if isinstance(params, BaseModel) else params
    parsed = spec.input_model.model_validate(raw)

    return await spec.handler(ctx, parsed)
