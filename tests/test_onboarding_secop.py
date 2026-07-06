"""Phase 1 tests — secop_discovery_node and POST /api/v1/onboarding/secop."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.nodes.secop_discovery import secop_discovery_node
from app.agent.state import AgentState
from app.schemas.agent import AgentMode


# ── Helpers ──────────────────────────────────────────────────────────────────

def _base_state(**kwargs) -> AgentState:
    return {
        "session_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "mode": AgentMode.SECOP_DISCOVERY,
        "messages": [],
        "user_input": "__secop__",
        "response": "",
        **kwargs,
    }


def _mock_db():
    return MagicMock()


_SAMPLE_CONTRATO = {
    "numero_contrato": "CO1.PCCNTR.0001",
    "entidad": "SENA",
    "objeto": "Prestación de servicios",
    "valor_contrato": 10_000_000,
}
_SAMPLE_DOC = {"tipo_documento": "acta_inicio", "numero_contrato": "CO1.PCCNTR.0001"}


# ── secop_discovery_node unit tests ─────────────────────────────────────────


class TestSecopDiscoveryNode:
    @pytest.mark.asyncio
    async def test_missing_cedula_returns_error(self):
        state = _base_state(_db=_mock_db())
        result = await secop_discovery_node(state)
        assert result["error"] == "cedula requerida para SECOP discovery"
        assert "cédula" in result["response"].lower()

    @pytest.mark.asyncio
    async def test_missing_db_returns_error(self):
        state = _base_state(cedula="1016019452")
        result = await secop_discovery_node(state)
        assert "db session" in result["error"]

    @pytest.mark.asyncio
    async def test_contracts_found_sets_state(self):
        state = _base_state(cedula="1016019452", _db=_mock_db())
        with patch(
            "app.agent.nodes.secop_discovery.secop_client.discover_contracts",
            AsyncMock(return_value=([_SAMPLE_CONTRATO], [_SAMPLE_DOC])),
        ):
            result = await secop_discovery_node(state)

        assert result["secop_contratos"] == [_SAMPLE_CONTRATO]
        assert result["secop_documentos"] == [_SAMPLE_DOC]
        assert result["onboarding_mode"] == "secop"
        assert result["current_phase"] == "secop_discovery"
        assert "1" in result["response"]  # mentions count

    @pytest.mark.asyncio
    async def test_no_contracts_sets_manual_mode(self):
        state = _base_state(cedula="9999999999", _db=_mock_db())
        with patch(
            "app.agent.nodes.secop_discovery.secop_client.discover_contracts",
            AsyncMock(return_value=([], [])),
        ):
            result = await secop_discovery_node(state)

        assert result["secop_contratos"] == []
        assert result["onboarding_mode"] == "manual"
        assert "9999999999" in result["response"]

    @pytest.mark.asyncio
    async def test_multiple_contracts_plural_message(self):
        contratos = [_SAMPLE_CONTRATO, {**_SAMPLE_CONTRATO, "numero_contrato": "CO1.PCCNTR.0002"}]
        state = _base_state(cedula="1016019452", _db=_mock_db())
        with patch(
            "app.agent.nodes.secop_discovery.secop_client.discover_contracts",
            AsyncMock(return_value=(contratos, [])),
        ):
            result = await secop_discovery_node(state)

        assert result["secop_contratos"] == contratos
        assert "2" in result["response"]


# ── POST /api/v1/onboarding/secop API tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_onboarding_secop_returns_contracts(client, test_user):
    """Happy path: SECOP returns 1 contract, endpoint returns it."""
    with patch(
        "app.agent.nodes.secop_discovery.secop_client.discover_contracts",
        AsyncMock(return_value=([_SAMPLE_CONTRATO], [_SAMPLE_DOC])),
    ):
        resp = await client.post(
            "/api/v1/onboarding/secop",
            json={"cedula": "1016019452"},
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_mode"] == "secop"
    assert len(data["contratos"]) == 1
    assert "session_id" in data
    assert data["message"]


@pytest.mark.asyncio
async def test_onboarding_secop_no_contracts(client, test_user):
    """When SECOP has no contracts, mode is 'manual'."""
    with patch(
        "app.agent.nodes.secop_discovery.secop_client.discover_contracts",
        AsyncMock(return_value=([], [])),
    ):
        resp = await client.post(
            "/api/v1/onboarding/secop",
            json={"cedula": "9999999999"},
            headers=test_user["headers"],
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["onboarding_mode"] == "manual"
    assert data["contratos"] == []


@pytest.mark.asyncio
async def test_onboarding_secop_invalid_cedula(client, test_user):
    """Invalid cédula format → 422."""
    resp = await client.post(
        "/api/v1/onboarding/secop",
        json={"cedula": "abc"},
        headers=test_user["headers"],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_onboarding_secop_requires_auth(client):
    """Unauthenticated request → 401."""
    resp = await client.post(
        "/api/v1/onboarding/secop",
        json={"cedula": "1016019452"},
    )
    assert resp.status_code == 401

