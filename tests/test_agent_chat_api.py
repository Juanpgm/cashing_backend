"""Tests for POST /api/v1/agent/chat — the free-form tool-calling chat endpoint.

The LLM is patched at `app.services.agent_chat_service.get_llm` with a scripted
fake (no network). Exercises the frozen response contract, file-count/type limits,
and auth enforcement through the real `client` fixture (httpx ASGITransport).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import app.api.v1.agent_chat as agent_chat_module
import pytest
from app.schemas.agent import LLMResponse
from httpx import AsyncClient

_PDF_MAGIC = b"%PDF-1.4 sample pdf content for agent chat tests"


class _ScriptedLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_chat_with_file_matches_frozen_contract(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fake_llm = _ScriptedLLM([LLMResponse(content="Hola, recibí tu contrato.", model="fake", total_tokens=15)])

    with patch("app.services.agent_chat_service.get_llm", return_value=fake_llm):
        response = await client.post(
            "/api/v1/agent/chat",
            headers=test_user["headers"],
            data={"message": "Aquí está mi contrato"},
            files=[("files", ("contrato.pdf", _PDF_MAGIC, "application/pdf"))],
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"session_id", "content", "tool_events", "documentos", "tokens_used"}
    assert isinstance(body["session_id"], str)
    assert body["content"] == "Hola, recibí tu contrato."
    assert body["tool_events"] == []
    assert isinstance(body["documentos"], list)
    assert body["documentos"][0]["filename"] == "contrato.pdf"
    assert isinstance(body["documentos"][0]["caracteres_extraidos"], int)
    assert body["tokens_used"] == 15


@pytest.mark.asyncio
async def test_chat_without_files_matches_contract(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fake_llm = _ScriptedLLM([LLMResponse(content="Hola, ¿en qué te ayudo?", model="fake", total_tokens=5)])

    with patch("app.services.agent_chat_service.get_llm", return_value=fake_llm):
        response = await client.post(
            "/api/v1/agent/chat",
            headers=test_user["headers"],
            data={"message": "Hola"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Hola, ¿en qué te ayudo?"
    assert body["documentos"] == []


@pytest.mark.asyncio
async def test_more_than_five_files_rejected(client: AsyncClient, test_user: dict[str, Any]) -> None:
    files = [("files", (f"doc_{i}.pdf", _PDF_MAGIC, "application/pdf")) for i in range(6)]

    response = await client.post(
        "/api/v1/agent/chat",
        headers=test_user["headers"],
        data={"message": "Aquí van varios archivos"},
        files=files,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_blocked_file_extension_rejected(client: AsyncClient, test_user: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/agent/chat",
        headers=test_user["headers"],
        data={"message": "Ejecuta esto"},
        files=[("files", ("virus.exe", b"MZ-fake-binary", "application/octet-stream"))],
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_aggregate_attachment_size_over_cap_rejected(client: AsyncClient, test_user: dict[str, Any]) -> None:
    # Each file is under the per-file evidence cap (25MB) but the two together exceed
    # the endpoint's aggregate cap (40MB) — must be rejected before any tool runs.
    chunk = _PDF_MAGIC + b"\x00" * (21 * 1024 * 1024 - len(_PDF_MAGIC))
    files = [
        ("files", ("grande1.pdf", chunk, "application/pdf")),
        ("files", ("grande2.pdf", chunk, "application/pdf")),
    ]

    response = await client.post(
        "/api/v1/agent/chat",
        headers=test_user["headers"],
        data={"message": "Aquí van dos archivos grandes"},
        files=files,
    )

    assert response.status_code in (400, 422)


def test_chat_endpoint_has_rate_limit_decorator() -> None:
    """Confirm `@limiter.limit(...)` guards POST /agent/chat, mirroring documentos.py."""
    source = Path(agent_chat_module.__file__).read_text(encoding="utf-8")
    assert '@limiter.limit("5/minute")' in source


@pytest.mark.asyncio
async def test_auth_required(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/agent/chat",
        data={"message": "Hola"},
    )

    assert response.status_code == 401
