"""LLM port (interface) for language model interactions."""

from typing import Any, Protocol

from pydantic import BaseModel

from app.schemas.agent import LLMMessage, LLMResponse


class LLMPort(Protocol):
    """Abstract LLM interface — implemented by LiteLLM or test stubs."""

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        response_format: type[BaseModel] | dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send messages and return a completion.

        ``tools`` (OpenAI function-calling schema, see
        ``app.tools.llm_schema.to_openai_tools``) and ``tool_choice`` are
        forwarded to the underlying provider when set; the returned
        ``LLMResponse.tool_calls`` is populated when the model requests one
        or more tool invocations instead of (or alongside) plain content.
        """
        ...

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> "AsyncIterator[str]":  # noqa: F821
        """Stream completion tokens one by one."""
        ...
