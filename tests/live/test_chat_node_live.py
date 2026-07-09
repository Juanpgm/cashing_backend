"""Live-LLM test for the chat node — real Ollama generates a conversational reply."""

from __future__ import annotations

import pytest

from app.agent.nodes.chat import chat_node

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_chat_node_responds_to_greeting() -> None:
    state = {
        "user_input": "Hola, buenos días. ¿Qué puedes ayudarme a hacer con mis cuentas de cobro?",
        "messages": [],
    }
    result = await chat_node(state)

    assert "error" not in result or not result.get("error")
    assert isinstance(result["response"], str)
    assert len(result["response"]) > 20
    # History was extended with the user turn + the assistant reply.
    assert len(result["messages"]) >= 2
    assert result["messages"][-1].role == "assistant"
