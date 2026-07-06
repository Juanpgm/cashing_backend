"""Google Calendar MCP Server — exposes Calendar tools to AI agents.

Tools:
- list_events: List calendar events in a time range
- get_event: Get a specific calendar event by ID

Proxies requests to the CashIn FastAPI backend with Bearer auth.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("CASHIN_API_URL", "http://localhost:8000/api/v1")
BEARER_TOKEN = os.getenv("CASHIN_BEARER_TOKEN", "")

mcp = FastMCP("cashin-calendar")

_HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}


@mcp.tool()
async def list_events(
    time_min: str,
    time_max: str,
    calendar_id: str = "primary",
    max_results: int = 50,
) -> list[dict]:  # type: ignore[type-arg]
    """List Google Calendar events in a time range.

    Args:
        time_min: Start of time range in RFC3339 format (e.g. "2024-01-01T00:00:00Z").
        time_max: End of time range in RFC3339 format (e.g. "2024-01-31T23:59:59Z").
        calendar_id: Calendar ID (default: "primary").
        max_results: Maximum number of events to return (1-100).

    Returns:
        List of event dicts with id, summary, start, end, description, location.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{BASE_URL}/integraciones/calendar/eventos",
            headers=_HEADERS,
            params={
                "time_min": time_min,
                "time_max": time_max,
                "calendar_id": calendar_id,
                "max_results": min(max_results, 100),
            },
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_event(
    event_id: str,
    calendar_id: str = "primary",
) -> dict:  # type: ignore[type-arg]
    """Get a specific Google Calendar event by ID.

    Args:
        event_id: The Google Calendar event ID.
        calendar_id: Calendar ID (default: "primary").

    Returns:
        Event dict with id, summary, start, end, description, attendees, location.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BASE_URL}/integraciones/calendar/eventos/{event_id}",
            headers=_HEADERS,
            params={"calendar_id": calendar_id},
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
