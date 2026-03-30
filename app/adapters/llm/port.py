"""LLM port (interface) for language model interactions."""

from typing import Protocol

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
    ) -> LLMResponse:
        """Send messages and return a completion."""
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
