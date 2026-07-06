"""Gmail MCP Server — exposes Gmail tools to AI agents.

Tools:
- search_emails: Search user's inbox with a Gmail query string
- send_email: Send an email on behalf of the user

Proxies to the real CashIn endpoints: POST /integraciones/email/search and /email/send.

This server proxies requests to the CashIn FastAPI backend and requires
a valid Bearer token to authenticate.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("CASHIN_API_URL", "http://localhost:8000/api/v1")
BEARER_TOKEN = os.getenv("CASHIN_BEARER_TOKEN", "")

mcp = FastMCP("cashin-gmail")

_HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}", "Content-Type": "application/json"}


@mcp.tool()
async def search_emails(
    query: str,
    max_results: int = 10,
) -> list[dict]:  # type: ignore[type-arg]
    """Search the authenticated user's Gmail inbox.

    Args:
        query: Gmail search query (e.g. "from:cliente@empresa.com subject:informe")
        max_results: Maximum number of messages to return (1-50).

    Returns:
        List of email summary dicts with id, subject, from, date, snippet.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/email/search",
            headers=_HEADERS,
            json={"query": query, "max_results": min(max_results, 100)},
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def send_email(
    to: str,
    subject: str,
    body_html: str,
) -> dict:  # type: ignore[type-arg]
    """Send an email via the authenticated user's Gmail account.

    Args:
        to: Recipient email address (comma-separated for multiple).
        subject: Email subject line.
        body_html: HTML email body.

    Returns:
        Dict with message_id of the sent message.
    """
    payload = {
        "to": [addr.strip() for addr in to.split(",") if addr.strip()],
        "subject": subject,
        "body_html": body_html,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/email/send",
            headers=_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
