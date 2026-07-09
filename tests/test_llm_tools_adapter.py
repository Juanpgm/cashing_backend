"""Tests for function-calling support in LiteLLMAdapter (app/adapters/llm/litellm_adapter.py).

All litellm calls are mocked — no network. Ollama IS running locally during
development for manual probing (see agent_chat_service docs), but tests must
never depend on it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.schemas.agent import LLMMessage


def _fake_tool_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _fake_litellm_response(
    content: str = "",
    tool_calls: list[SimpleNamespace] | None = None,
    prompt_tokens: int = 5,
    completion_tokens: int = 5,
    total_tokens: int = 10,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


class TestToolsPassthrough:
    @pytest.mark.asyncio
    async def test_tools_and_tool_choice_forwarded_to_litellm(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(content="hi")

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            result = await adapter.complete(
                [LLMMessage(role="user", content="hola")],
                tools=TOOLS,
                tool_choice="auto",
                fallback=False,
            )

        assert result.content == "hi"
        kwargs = mock_call.call_args.kwargs
        assert kwargs["tools"] == TOOLS
        assert kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_no_tools_means_no_tools_kwarg(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(content="hi")

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete([LLMMessage(role="user", content="hola")], fallback=False)

        kwargs = mock_call.call_args.kwargs
        assert "tools" not in kwargs
        assert "tool_choice" not in kwargs


class TestToolCallParsing:
    @pytest.mark.asyncio
    async def test_valid_tool_calls_are_parsed(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(
            content="",
            tool_calls=[_fake_tool_call("call_1", "get_weather", '{"city": "Bogota"}')],
        )

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete(
                [LLMMessage(role="user", content="clima en Bogota")], tools=TOOLS, fallback=False
            )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        call = result.tool_calls[0]
        assert call.id == "call_1"
        assert call.name == "get_weather"
        assert call.arguments == {"city": "Bogota"}

    @pytest.mark.asyncio
    async def test_malformed_arguments_json_degrades_to_empty_dict(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(
            content="",
            tool_calls=[_fake_tool_call("call_2", "get_weather", "{not valid json")],
        )

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete(
                [LLMMessage(role="user", content="clima")], tools=TOOLS, fallback=False
            )

        assert result.tool_calls is not None
        assert result.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_arguments_as_json_array_string_degrades_to_empty_dict(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(
            content="",
            tool_calls=[_fake_tool_call("call_3", "get_weather", "[1, 2, 3]")],
        )

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete(
                [LLMMessage(role="user", content="clima")], tools=TOOLS, fallback=False
            )

        assert result.tool_calls is not None
        assert result.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_arguments_as_json_scalar_string_degrades_to_empty_dict(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(
            content="",
            tool_calls=[_fake_tool_call("call_4", "get_weather", '"foo"')],
        )

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete(
                [LLMMessage(role="user", content="clima")], tools=TOOLS, fallback=False
            )

        assert result.tool_calls is not None
        assert result.tool_calls[0].arguments == {}

    @pytest.mark.asyncio
    async def test_arguments_as_native_dict_are_passed_through(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(
            content="",
            tool_calls=[_fake_tool_call("call_5", "get_weather", {"city": "Bogota"})],
        )

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete(
                [LLMMessage(role="user", content="clima")], tools=TOOLS, fallback=False
            )

        assert result.tool_calls is not None
        assert result.tool_calls[0].arguments == {"city": "Bogota"}

    @pytest.mark.asyncio
    async def test_no_tool_calls_returns_none(self) -> None:
        adapter = LiteLLMAdapter(default_model="groq/llama-3.1-8b")
        fake_response = _fake_litellm_response(content="just text", tool_calls=None)

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)):
            result = await adapter.complete([LLMMessage(role="user", content="hola")], fallback=False)

        assert result.tool_calls is None


class TestOllamaChatRewrite:
    @pytest.mark.asyncio
    async def test_ollama_rewritten_to_ollama_chat_when_tools_present(self) -> None:
        adapter = LiteLLMAdapter(default_model="ollama/llama3.1:8b")
        fake_response = _fake_litellm_response(content="ok")

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete(
                [LLMMessage(role="user", content="hola")], tools=TOOLS, model="ollama/llama3.1:8b", fallback=False
            )

        assert mock_call.call_args.kwargs["model"] == "ollama_chat/llama3.1:8b"

    @pytest.mark.asyncio
    async def test_ollama_not_rewritten_without_tools(self) -> None:
        adapter = LiteLLMAdapter(default_model="ollama/llama3.1:8b")
        fake_response = _fake_litellm_response(content="ok")

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete(
                [LLMMessage(role="user", content="hola")], model="ollama/llama3.1:8b", fallback=False
            )

        assert mock_call.call_args.kwargs["model"] == "ollama/llama3.1:8b"

    @pytest.mark.asyncio
    async def test_ollama_rewrite_keeps_api_base(self) -> None:
        adapter = LiteLLMAdapter(default_model="ollama/llama3.1:8b")
        fake_response = _fake_litellm_response(content="ok")

        with patch("litellm.acompletion", new=AsyncMock(return_value=fake_response)) as mock_call:
            await adapter.complete(
                [LLMMessage(role="user", content="hola")], tools=TOOLS, model="ollama/llama3.1:8b", fallback=False
            )

        assert "api_base" in mock_call.call_args.kwargs

    def test_rewrite_helper_is_idempotent(self) -> None:
        assert LiteLLMAdapter._rewrite_model_for_tools("ollama/llama3.1:8b", TOOLS) == "ollama_chat/llama3.1:8b"
        assert LiteLLMAdapter._rewrite_model_for_tools("ollama_chat/llama3.1:8b", TOOLS) == "ollama_chat/llama3.1:8b"
        assert LiteLLMAdapter._rewrite_model_for_tools("groq/llama-3.1-8b", TOOLS) == "groq/llama-3.1-8b"
        assert LiteLLMAdapter._rewrite_model_for_tools("ollama/llama3.1:8b", None) == "ollama/llama3.1:8b"


class TestMessageSerialization:
    def test_plain_messages_serialize_without_extra_keys(self) -> None:
        messages = [LLMMessage(role="user", content="hola")]
        out = LiteLLMAdapter._to_litellm_messages(messages)
        assert out == [{"role": "user", "content": "hola"}]

    def test_tool_message_includes_tool_call_id(self) -> None:
        messages = [LLMMessage(role="tool", content='{"ok": true}', tool_call_id="call_1")]
        out = LiteLLMAdapter._to_litellm_messages(messages)
        assert out[0]["tool_call_id"] == "call_1"
        assert out[0]["role"] == "tool"

    def test_assistant_message_includes_tool_calls(self) -> None:
        tool_calls = [{"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}]
        messages = [LLMMessage(role="assistant", content="", tool_calls=tool_calls)]
        out = LiteLLMAdapter._to_litellm_messages(messages)
        assert out[0]["tool_calls"] == tool_calls

    def test_message_without_tool_fields_omits_them(self) -> None:
        messages = [LLMMessage(role="assistant", content="hi")]
        out = LiteLLMAdapter._to_litellm_messages(messages)
        assert "tool_call_id" not in out[0]
        assert "tool_calls" not in out[0]
