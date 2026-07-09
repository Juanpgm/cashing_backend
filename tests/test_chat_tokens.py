"""chat_node must surface real token usage into state so /chat reports it (was hardcoded 0)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.nodes.chat import chat_node


@pytest.mark.asyncio
async def test_chat_node_records_tokens() -> None:
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=MagicMock(content="hola", total_tokens=42))

    with patch("app.agent.nodes.chat.get_llm", return_value=fake):
        result = await chat_node({"messages": [], "user_input": "hi"})

    assert result["response"] == "hola"
    assert result["tokens_used"] == 42
