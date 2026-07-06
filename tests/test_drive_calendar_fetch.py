"""Tests for drive_fetch and calendar_fetch evidence nodes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.drive.port import DriveFile


# ─────────────────────────────────────────────────────────────────────────────
# drive_fetch_node
# ─────────────────────────────────────────────────────────────────────────────


def _drive_file(fid: str, name: str) -> DriveFile:
    now = datetime(2024, 4, 12, tzinfo=timezone.utc)
    return DriveFile(
        id=fid,
        name=name,
        mime_type="application/pdf",
        size_bytes=1024,
        created_at=now,
        modified_at=now,
        web_view_link=f"https://drive.google.com/file/d/{fid}/view",
    )


@pytest.mark.asyncio
async def test_drive_fetch_returns_evidence_with_links():
    from app.agent.nodes import drive_fetch as mod

    mock_adapter = MagicMock()
    mock_adapter.search_files = AsyncMock(return_value=[_drive_file("f1", "Informe actividades abril.pdf")])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
    }

    with patch.object(mod, "DriveAdapter", return_value=mock_adapter):
        result = await mod.drive_fetch_node(state)

    ev = result["drive_evidencias"]
    assert len(ev) == 1
    assert ev[0]["source"] == "drive"
    assert ev[0]["link"].endswith("/view")
    assert ev[0]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_drive_fetch_dedupes_files_across_queries():
    from app.agent.nodes import drive_fetch as mod

    # Same file returned by every query → must appear once.
    mock_adapter = MagicMock()
    mock_adapter.search_files = AsyncMock(return_value=[_drive_file("dup", "acta.pdf")])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento"}],
    }

    with patch.object(mod, "DriveAdapter", return_value=mock_adapter):
        result = await mod.drive_fetch_node(state)

    assert len(result["drive_evidencias"]) == 1


@pytest.mark.asyncio
async def test_drive_fetch_no_db_returns_empty():
    from app.agent.nodes.drive_fetch import drive_fetch_node

    result = await drive_fetch_node({"user_id": uuid.uuid4()})
    assert result["drive_evidencias"] == []


def test_build_drive_queries_includes_date_clause():
    from app.agent.nodes.drive_fetch import build_drive_queries

    queries = build_drive_queries("Entregar informe mensual", "2024-04-01", "2024-04-30")
    assert queries
    assert all("modifiedTime >=" in q for q in queries)
    assert any("informe" in q for q in queries)


def test_build_drive_queries_excludes_folders():
    from app.agent.nodes.drive_fetch import build_drive_queries

    queries = build_drive_queries("Entregar informe mensual", "2024-04-01", "2024-04-30")
    assert all("mimeType != 'application/vnd.google-apps.folder'" in q for q in queries)


# ─────────────────────────────────────────────────────────────────────────────
# calendar_fetch_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_fetch_normalizes_events():
    from app.agent.nodes import calendar_fetch as mod

    events = [
        {
            "id": "ev1",
            "summary": "Reunión de seguimiento",
            "description": "Revisión de avances del contrato",
            "htmlLink": "https://calendar.google.com/event?eid=ev1",
            "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
            "attendees": [{"self": True, "responseStatus": "accepted"}, {"self": False, "email": "supervisor@entidad.gov.co"}],
            "eventType": "default",
        },
    ]
    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(return_value=events)

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento"}],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        result = await mod.calendar_fetch_node(state)

    ev = result["calendar_evidencias"]
    assert len(ev) == 1
    assert ev[0]["source"] == "calendar"
    assert ev[0]["link"].startswith("https://calendar.google.com")
    assert ev[0]["date"] == "2024-04-15T09:00:00-05:00"
    # Metadatos de asistencia presentes para evidence_filter
    assert "metadata" in ev[0]
    assert "attendees" in ev[0]["metadata"]
    assert ev[0]["metadata"]["is_all_day"] is False
    assert ev[0]["metadata"]["event_type"] == "default"


@pytest.mark.asyncio
async def test_calendar_fetch_marks_allday_events():
    from app.agent.nodes import calendar_fetch as mod

    events = [
        {
            "id": "ev2",
            "summary": "Día festivo",
            "start": {"date": "2024-04-19"},  # all-day: sin dateTime
            "htmlLink": "https://calendar.google.com/event?eid=ev2",
        },
    ]
    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(return_value=events)

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        result = await mod.calendar_fetch_node(state)

    ev = result["calendar_evidencias"]
    assert ev[0]["metadata"]["is_all_day"] is True


@pytest.mark.asyncio
async def test_calendar_fetch_passes_keyword_query():
    """El node construye una query q desde las obligaciones y la pasa al adapter."""
    from app.agent.nodes import calendar_fetch as mod

    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento del proyecto"}],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        await mod.calendar_fetch_node(state)

    call_kwargs = mock_adapter.search_events.call_args.kwargs
    assert "q" in call_kwargs
    assert call_kwargs["q"] is not None  # se construyó una query de keywords


@pytest.mark.asyncio
async def test_calendar_fetch_no_dates_returns_empty():
    from app.agent.nodes.calendar_fetch import calendar_fetch_node

    result = await calendar_fetch_node({"user_id": uuid.uuid4(), "_db": MagicMock(), "contrato_contexto": {}})
    assert result["calendar_evidencias"] == []
