"""Live-LLM smoke test for the LiteLLM adapter — direct get_llm().complete() call."""

from __future__ import annotations

import pytest

from app.adapters.llm import get_llm
from app.schemas.agent import LLMMessage

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_adapter_follows_simple_instruction() -> None:
    llm = get_llm()
    messages = [LLMMessage(role="user", content="Responde únicamente con la palabra: LISTO")]

    resp = await llm.complete(messages, temperature=0.0, max_tokens=10)

    assert "listo" in resp.content.strip().lower()
    assert resp.model.startswith("ollama")
    assert resp.total_tokens >= 0
    assert resp.prompt_tokens >= 0
