"""Tests for the agent service, graph, and chat/document APIs."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

# ── Agent schema tests ──────────────────────────────────────────────


class TestAgentSchemas:
    def test_llm_message_creation(self) -> None:
        from app.schemas.agent import LLMMessage

        msg = LLMMessage(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"

    def test_llm_response_creation(self) -> None:
        from app.schemas.agent import LLMResponse

        resp = LLMResponse(content="Hi", model="test", prompt_tokens=10, completion_tokens=5, total_tokens=15)
        assert resp.total_tokens == 15

    def test_chat_message_request_validation(self) -> None:
        from app.schemas.agent import ChatMessageRequest
        from pydantic import ValidationError

        # Empty message fails
        with pytest.raises(ValidationError):
            ChatMessageRequest(message="")

        # Valid
        req = ChatMessageRequest(message="Hello agent")
        assert req.session_id is None

    def test_agent_mode_enum(self) -> None:
        from app.schemas.agent import AgentMode

        assert AgentMode.CHAT == "chat"
        assert AgentMode.PIPELINE == "pipeline"
        assert AgentMode.CONFIG == "config"


# ── Document parser tests ───────────────────────────────────────────


class TestDocumentParser:
    def test_parse_document_unsupported_format(self) -> None:
        from app.agent.tools.document_parser import parse_document

        with pytest.raises(ValueError, match="Unsupported file format"):
            parse_document(b"data", "file.txt")

    @patch("app.agent.tools.document_parser.parse_pdf", return_value="PDF text")
    def test_parse_document_pdf(self, mock_pdf: MagicMock) -> None:
        from app.agent.tools.document_parser import parse_document

        result = parse_document(b"data", "contract.pdf")
        assert result == "PDF text"
        mock_pdf.assert_called_once()

    @patch("app.agent.tools.document_parser.parse_docx", return_value="DOCX text")
    def test_parse_document_docx(self, mock_docx: MagicMock) -> None:
        from app.agent.tools.document_parser import parse_document

        result = parse_document(b"data", "contract.docx")
        assert result == "DOCX text"
        mock_docx.assert_called_once()


# ── Template filler tests ───────────────────────────────────────────


class TestTemplateFiller:
    def test_fill_text_template(self) -> None:
        from app.agent.tools.template_filler import fill_template

        result = fill_template("Hello {{ name }}", {"name": "World"})
        assert "World" in result

    def test_fill_template_missing_var(self) -> None:
        from app.agent.tools.template_filler import fill_template

        result = fill_template("Hello {{ name }}", {})
        assert "Hello" in result


# ── Agent state tests ───────────────────────────────────────────────


class TestAgentState:
    def test_state_creation(self) -> None:
        import uuid

        from app.agent.state import AgentState
        from app.schemas.agent import AgentMode

        state: AgentState = {
            "session_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "mode": AgentMode.CHAT,
            "messages": [],
            "user_input": "test",
            "response": "",
        }
        assert state["mode"] == AgentMode.CHAT


# ── LLM adapter tests ──────────────────────────────────────────────


class TestLiteLLMAdapter:
    def test_get_model_chain_default(self) -> None:
        from app.adapters.llm.litellm_adapter import LiteLLMAdapter

        adapter = LiteLLMAdapter()
        chain = adapter._get_model_chain(None)
        assert len(chain) >= 1

    def test_get_model_chain_custom(self) -> None:
        from app.adapters.llm.litellm_adapter import LiteLLMAdapter

        adapter = LiteLLMAdapter(default_model="test/model")
        chain = adapter._get_model_chain("custom/model")
        assert chain[0] == "custom/model"

    @pytest.mark.asyncio
    async def test_complete_fallback_all_fail(self) -> None:
        from app.adapters.llm.litellm_adapter import LiteLLMAdapter
        from app.schemas.agent import LLMMessage

        adapter = LiteLLMAdapter()
        with (
            patch("app.adapters.llm.litellm_adapter.LiteLLMAdapter._call_model", side_effect=RuntimeError("fail")),
            pytest.raises(RuntimeError, match="All LLM models failed"),
        ):
            await adapter.complete([LLMMessage(role="user", content="hi")])

    @pytest.mark.asyncio
    async def test_complete_success(self) -> None:
        from app.adapters.llm.litellm_adapter import LiteLLMAdapter
        from app.schemas.agent import LLMMessage, LLMResponse

        adapter = LiteLLMAdapter()
        mock_response = LLMResponse(content="ok", model="test", total_tokens=10)
        with patch.object(adapter, "_call_model", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.complete([LLMMessage(role="user", content="hi")])
            assert result.content == "ok"


# ── Chat API tests ──────────────────────────────────────────────────


class TestChatAPI:
    @pytest.mark.asyncio
    async def test_chat_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/chat/", json={"message": "hello"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_chat_success(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
    ) -> None:
        with patch("app.services.agent_service.chat", new_callable=AsyncMock) as mock_chat:
            import uuid

            from app.schemas.agent import ChatMessageResponse

            mock_chat.return_value = ChatMessageResponse(
                session_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                content="Hello!",
                tokens_used=0,
            )
            resp = await client.post(
                "/api/v1/chat/",
                json={"message": "hello"},
                headers=test_user["headers"],
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["content"] == "Hello!"
            assert "session_id" in data

    @pytest.mark.asyncio
    async def test_chat_empty_message(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
    ) -> None:
        resp = await client.post(
            "/api/v1/chat/",
            json={"message": ""},
            headers=test_user["headers"],
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_history_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/chat/00000000-0000-0000-0000-000000000001")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_stream_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/chat/stream", json={"message": "hello"})
        assert resp.status_code == 401


# ── Document API tests ──────────────────────────────────────────────


class TestDocumentAPI:
    @pytest.mark.asyncio
    async def test_upload_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/documentos/upload")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_upload_no_file(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
    ) -> None:
        resp = await client.post(
            "/api/v1/documentos/upload",
            headers=test_user["headers"],
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_process_unauthorized(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/documentos/process",
            json={"document_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert resp.status_code == 401


# ── Graph build test ────────────────────────────────────────────────


class TestGraph:
    def test_build_graph(self) -> None:
        from app.agent.graph import build_graph

        graph = build_graph()
        assert graph is not None
