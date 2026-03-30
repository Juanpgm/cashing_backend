"""Health endpoint tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "environment" in data


@pytest.mark.asyncio
async def test_docs_available_in_dev(client: AsyncClient) -> None:
    response = await client.get("/docs")
    # In development mode docs should be available
    assert response.status_code in (200, 307)
