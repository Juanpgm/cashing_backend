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
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Send messages and return a completion.

        ``tools`` (OpenAI function-calling schema, see
        ``app.tools.llm_schema.to_openai_tools``) and ``tool_choice`` are
        forwarded to the underlying provider when set; the returned
        ``LLMResponse.tool_calls`` is populated when the model requests one
        or more tool invocations instead of (or alongside) plain content.

        ``reasoning_effort`` (``"low"``/``"medium"``/``"high"``) is a Groq-specific
        extra param for reasoning models — implementations forward it only when
        the model actually called is a Groq model; other providers never receive
        it. An unrecognized value should raise ``ValueError`` before any network
        call.
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

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Return one embedding vector per input text.

        Used for in-memory cosine similarity ranking in the evidence matcher —
        embeddings are never persisted (evidence-embeddings capability).
        """
        ...
