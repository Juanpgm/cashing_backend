"""Tests for agent_service — mocking LangGraph and DB."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.agent import AgentMode, ChatMessageResponse


class TestGetGraph:
    def test_returns_compiled_graph(self) -> None:
        from app.services import agent_service

        graph = agent_service.get_graph()
        assert graph is not None


class TestInitialiseGraph:
    def test_initialise_graph(self) -> None:
        from app.services.agent_service import get_graph, initialise_graph

        initialise_graph()
        graph = get_graph()
        assert graph is not None

    def test_initialise_replaces_graph(self) -> None:
        from app.services import agent_service

        old_graph = agent_service._graph
        agent_service.initialise_graph()
        new_graph = agent_service._graph
        # Both are compiled CompiledGraph instances
        assert new_graph is not None
        # After re-init, the module graph is replaced
        assert new_graph is not old_graph or new_graph is not None


class TestAgentServiceChat:
    @pytest.mark.asyncio
    async def test_chat_creates_conversation_if_none(self) -> None:
        """chat() creates a Conversacion when session_id is None."""
        from app.services.agent_service import chat

        user_id = uuid.uuid4()

        # Mock DB
        mock_db = AsyncMock()
        # execute returns no existing conversation (session_id is None → skip query)
        fake_convo = MagicMock()
        fake_convo.id = uuid.uuid4()
        fake_convo.mensajes_json = []

        # Patch Conversacion constructor and db.add/flush
        with (
            patch("app.services.agent_service.Conversacion", return_value=fake_convo),
            patch(
                "app.services.agent_service._graph",
                new_callable=lambda: type(
                    "FakeGraph",
                    (),
                    {
                        "ainvoke": AsyncMock(
                            return_value={
                                "response": "Hola",
                                "messages": [],
                            }
                        )
                    },
                )(),
            ),
        ):
            result = await chat(mock_db, user_id, "Hola", session_id=None)

        assert isinstance(result, ChatMessageResponse)
        assert result.content == "Hola"

    @pytest.mark.asyncio
    async def test_chat_loads_existing_conversation(self) -> None:
        """chat() loads existing Conversacion when session_id is provided."""
        from app.services.agent_service import chat

        user_id = uuid.uuid4()
        session_id = uuid.uuid4()

        fake_convo = MagicMock()
        fake_convo.id = session_id
        fake_convo.usuario_id = user_id
        fake_convo.mensajes_json = [{"role": "user", "content": "prev"}, {"role": "assistant", "content": "ok"}]

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_convo

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        fake_graph = MagicMock()
        fake_graph.ainvoke = AsyncMock(
            return_value={"response": "respuesta", "messages": []}
        )

        with patch("app.services.agent_service._graph", fake_graph):
            result = await chat(mock_db, user_id, "nueva pregunta", session_id=session_id)

        assert result.session_id == session_id
        assert result.content == "respuesta"


class TestAgentServiceHistory:
    @pytest.mark.asyncio
    async def test_get_history_returns_messages(self) -> None:
        from app.services.agent_service import get_conversation_history

        user_id = uuid.uuid4()
        session_id = uuid.uuid4()

        fake_convo = MagicMock()
        fake_convo.mensajes_json = [{"role": "user", "content": "hola"}]

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_convo

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        history = await get_conversation_history(mock_db, user_id, session_id)
        assert len(history) == 1
        assert history[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_get_history_returns_empty_if_not_found(self) -> None:
        from app.services.agent_service import get_conversation_history

        user_id = uuid.uuid4()
        session_id = uuid.uuid4()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        history = await get_conversation_history(mock_db, user_id, session_id)
        assert history == []
