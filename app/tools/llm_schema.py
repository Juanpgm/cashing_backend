"""Export `app.tools.registry.TOOL_REGISTRY` as OpenAI-shaped function-calling tools.

Single call site for turning our declarative `ToolSpec` catalog into the
`tools=[...]` payload `LiteLLMAdapter.complete` forwards to the provider. Kept
separate from `app.mcp.server` (which builds MCP SDK `Tool` objects from the
same registry) since the two shapes are unrelated and neither should import
the other.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import TOOL_REGISTRY, ToolSpec


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop the top-level `title` key — it's Pydantic's model name, not useful to the LLM."""
    schema = dict(schema)
    schema.pop("title", None)
    return schema


def to_openai_tools(registry: dict[str, ToolSpec] | None = None) -> list[dict[str, Any]]:
    """Return every tool in `registry` (default: the global `TOOL_REGISTRY`) as an
    OpenAI-shaped `tools=[...]` entry: `{"type": "function", "function": {...}}`.
    """
    source = registry if registry is not None else TOOL_REGISTRY
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _clean_schema(spec.input_model.model_json_schema()),
            },
        }
        for spec in source.values()
    ]
