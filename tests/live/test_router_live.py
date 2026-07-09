"""Live-LLM tests for the router node — real Ollama classifies user intent.

Phrasings are picked directly from the vocabulary used in ROUTER_PROMPT
(app/agent/prompts/system.py) so a small local model has the best chance of
matching the intended mode.
"""

from __future__ import annotations

import pytest

from app.agent.nodes.router import router_node
from app.schemas.agent import AgentMode

pytestmark = pytest.mark.live_llm


@pytest.mark.asyncio
async def test_router_classifies_greeting_as_chat() -> None:
    state = {"user_input": "Hola, ¿qué puedes hacer por mí?"}
    result = await router_node(state)
    assert result["mode"] == AgentMode.CHAT


@pytest.mark.asyncio
async def test_router_classifies_obligation_extraction() -> None:
    state = {
        "user_input": (
            "Necesito extraer las obligaciones específicas de este contrato en "
            "PDF que acabo de subir."
        )
    }
    result = await router_node(state)
    assert result["mode"] == AgentMode.EXTRACT_OBLIGATIONS


@pytest.mark.asyncio
async def test_router_classifies_generate_activities() -> None:
    state = {
        "user_input": (
            "Genera las actividades y justificaciones de mi cuenta de cobro "
            "para este mes."
        )
    }
    result = await router_node(state)
    assert result["mode"] == AgentMode.GENERATE_ACTIVITIES
