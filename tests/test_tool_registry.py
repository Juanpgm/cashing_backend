"""Unit tests for app.tools.registry / app.tools.invoke — the tool registration
and dispatch mechanism the (future) MCP server uses to call capabilities by name.

These tests register throwaway dummy tools directly against TOOL_REGISTRY. An
autouse fixture snapshots/restores the registry around every test so dummy
registrations never leak into other test modules (notably test_tool_catalog.py,
which asserts on the exact set of real catalog tool names).
"""

from __future__ import annotations

import pytest
from app.core.exceptions import NotFoundError
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool, list_tools
from app.tools.registry import TOOL_REGISTRY, tool
from pydantic import BaseModel, ValidationError


@pytest.fixture(autouse=True)
def _isolated_registry():
    snapshot = dict(TOOL_REGISTRY)
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(snapshot)


class _DummyInput(BaseModel):
    value: int


class _DummyOutput(BaseModel):
    doubled: int


def _dummy_ctx() -> ToolContext:
    # Registry/dispatch mechanics never touch db/usuario — a bare context is enough.
    return ToolContext(db=None, usuario=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tool_decorator_registers_spec() -> None:
    @tool(
        name="test_dummy_double",
        description="Doubles a number.",
        input_model=_DummyInput,
        output_model=_DummyOutput,
        tags=("read",),
        consumes_credits=2,
    )
    async def _handler(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value * 2)

    assert "test_dummy_double" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["test_dummy_double"]
    assert spec.description == "Doubles a number."
    assert spec.tags == ("read",)
    assert spec.consumes_credits == 2
    assert spec.input_model is _DummyInput
    assert spec.output_model is _DummyOutput


def test_tool_decorator_duplicate_name_raises() -> None:
    @tool(name="test_dummy_dup", description="d", input_model=_DummyInput, output_model=_DummyOutput)
    async def _handler_a(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value)

    with pytest.raises(ValueError, match="already registered"):

        @tool(name="test_dummy_dup", description="d2", input_model=_DummyInput, output_model=_DummyOutput)
        async def _handler_b(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
            return _DummyOutput(doubled=params.value)


@pytest.mark.asyncio
async def test_invoke_tool_dict_params() -> None:
    @tool(name="test_dummy_invoke_dict", description="d", input_model=_DummyInput, output_model=_DummyOutput)
    async def _handler(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value * 2)

    result = await invoke_tool("test_dummy_invoke_dict", _dummy_ctx(), {"value": 3})
    assert isinstance(result, _DummyOutput)
    assert result.doubled == 6


@pytest.mark.asyncio
async def test_invoke_tool_basemodel_params() -> None:
    @tool(name="test_dummy_invoke_model", description="d", input_model=_DummyInput, output_model=_DummyOutput)
    async def _handler(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value * 2)

    result = await invoke_tool("test_dummy_invoke_model", _dummy_ctx(), _DummyInput(value=5))
    assert result.doubled == 10


@pytest.mark.asyncio
async def test_invoke_unknown_tool_raises_not_found() -> None:
    with pytest.raises(NotFoundError):
        await invoke_tool("test_tool_that_does_not_exist", _dummy_ctx(), {})


@pytest.mark.asyncio
async def test_invoke_tool_bad_params_raises_validation_error() -> None:
    @tool(name="test_dummy_invoke_bad", description="d", input_model=_DummyInput, output_model=_DummyOutput)
    async def _handler(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value * 2)

    with pytest.raises(ValidationError):
        await invoke_tool("test_dummy_invoke_bad", _dummy_ctx(), {"value": "not-an-int"})


def test_list_tools_includes_registered_spec() -> None:
    @tool(name="test_dummy_list", description="d", input_model=_DummyInput, output_model=_DummyOutput)
    async def _handler(ctx: ToolContext, params: _DummyInput) -> _DummyOutput:
        return _DummyOutput(doubled=params.value)

    names = {spec.name for spec in list_tools()}
    assert "test_dummy_list" in names
