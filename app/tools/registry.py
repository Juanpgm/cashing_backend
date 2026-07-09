"""Single source of truth for agent/MCP-exposed tool capabilities.

Register a capability with the `@tool` decorator on the handler function; the
concrete wrappers live under `app.tools.catalog` (see
`app/tools/catalog/__init__.py` for the registration entrypoint). Nothing here
knows about FastAPI, MCP, or the agent graph — this module is just the
declarative registry + decorator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.tools.context import ToolContext

ToolHandler = Callable[[ToolContext, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    """Declarative description of one callable tool capability."""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    tags: tuple[str, ...] = ()
    consumes_credits: int = 0


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    tags: tuple[str, ...] = (),
    consumes_credits: int = 0,
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a handler function as a tool named `name`.

    Raises `ValueError` if `name` is already registered — tool names are
    globally unique across the whole catalog, since they double as the
    identifier an MCP client/agent uses to invoke the capability.
    """

    def decorator(handler: ToolHandler) -> ToolHandler:
        if name in TOOL_REGISTRY:
            raise ValueError(f"Tool '{name}' is already registered.")
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            handler=handler,
            tags=tags,
            consumes_credits=consumes_credits,
        )
        return handler

    return decorator
