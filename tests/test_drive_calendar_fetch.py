"""Tests for drive_fetch and calendar_fetch evidence nodes."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.calendar.port import CalendarAttendee, CalendarEvent
from app.adapters.drive.port import DriveFile
from app.models.integracion import IntegrationProvider

# ─────────────────────────────────────────────────────────────────────────────
# drive_fetch_node
# ─────────────────────────────────────────────────────────────────────────────


def _drive_file(fid: str, name: str) -> DriveFile:
    now = datetime(2024, 4, 12, tzinfo=UTC)
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
async def test_drive_fetch_truncation_keeps_keyword_queries_over_generic_terms(monkeypatch):
    """`drive_fetch_node`'s truncation only behaves correctly because keyword-derived
    queries are listed before generic-term ones in `build_drive_queries` — pin that order
    so a future reordering there breaks this test instead of silently degrading evidence.
    """
    from app.agent.nodes import drive_fetch as mod
    from app.agent.nodes.drive_fetch import _GENERIC_TERMS

    # Fewer slots than the 3 keyword + 5 generic queries build_drive_queries can produce
    # for a single obligación, so the slice at the drive_fetch_node call site is exercised.
    monkeypatch.setattr(mod.settings, "EVIDENCE_QUERIES_PER_OBLIGACION", 1)

    mock_adapter = MagicMock()
    mock_adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
    }

    with patch.object(mod, "DriveAdapter", return_value=mock_adapter):
        await mod.drive_fetch_node(state)

    called_keywords = [call.args[1].keywords[0] for call in mock_adapter.search_files.call_args_list]
    assert len(called_keywords) == 1
    assert called_keywords[0] not in _GENERIC_TERMS


@pytest.mark.asyncio
async def test_drive_fetch_microsoft_provider_uses_graph_adapter_and_appends(monkeypatch):
    """Calling drive_fetch_node once per connected provider (Slice C2's
    evidence_discovery_service loop) must APPEND results, not overwrite the
    other provider's — and tag each item with its own `provider`."""
    from app.agent.nodes import drive_fetch as mod

    google_adapter = MagicMock()
    google_adapter.search_files = AsyncMock(return_value=[_drive_file("g1", "Informe Google.pdf")])
    ms_adapter = MagicMock()
    ms_adapter.search_files = AsyncMock(return_value=[_drive_file("m1", "Informe OneDrive.pdf")])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
    }

    with (
        patch.object(mod, "DriveAdapter", return_value=google_adapter),
        patch.object(mod, "MicrosoftGraphAdapter", return_value=ms_adapter),
    ):
        state = await mod.drive_fetch_node(state, provider=IntegrationProvider.GOOGLE)
        state = await mod.drive_fetch_node(state, provider=IntegrationProvider.MICROSOFT)

    ev = state["drive_evidencias"]
    assert len(ev) == 2
    providers = {e["provider"] for e in ev}
    assert providers == {"google", "microsoft"}
    ms_adapter.search_files.assert_awaited()


@pytest.mark.asyncio
async def test_drive_fetch_preserves_existing_evidencias_on_provider_error():
    """One provider's failure must not wipe out evidence already gathered from
    another provider (evidence-discovery-gate spec: isolate per-provider failure)."""
    from app.agent.nodes import drive_fetch as mod

    google_adapter = MagicMock()
    google_adapter.search_files = AsyncMock(return_value=[_drive_file("g1", "Informe Google.pdf")])
    failing_ms_adapter = MagicMock()
    failing_ms_adapter.search_files = AsyncMock(side_effect=RuntimeError("Graph down"))

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
    }

    with patch.object(mod, "DriveAdapter", return_value=google_adapter):
        state = await mod.drive_fetch_node(state, provider=IntegrationProvider.GOOGLE)

    with patch.object(mod, "MicrosoftGraphAdapter", return_value=failing_ms_adapter):
        state = await mod.drive_fetch_node(state, provider=IntegrationProvider.MICROSOFT)

    # drive_query_failed is caught per-query inside the try, so this still returns
    # the Google evidence gathered in the previous call, unharmed.
    assert len(state["drive_evidencias"]) == 1
    assert state["drive_evidencias"][0]["provider"] == "google"


@pytest.mark.asyncio
async def test_drive_fetch_no_db_returns_empty():
    from app.agent.nodes.drive_fetch import drive_fetch_node

    result = await drive_fetch_node({"user_id": uuid.uuid4()})
    assert result["drive_evidencias"] == []


def test_build_drive_queries_includes_date_clause():
    from app.agent.nodes.drive_fetch import build_drive_queries

    queries = build_drive_queries("Entregar informe mensual", "2024-04-01", "2024-04-30")
    assert queries
    assert all(q.date_from is not None and q.date_to is not None for q in queries)
    assert any("informe" in kw for q in queries for kw in q.keywords)


def test_build_drive_queries_excludes_folders():
    from app.agent.nodes.drive_fetch import build_drive_queries

    queries = build_drive_queries("Entregar informe mensual", "2024-04-01", "2024-04-30")
    assert all(q.exclude_folders for q in queries)


def test_build_drive_queries_returns_drive_query_objects():
    from app.adapters.drive.port import DriveQuery
    from app.agent.nodes.drive_fetch import build_drive_queries

    queries = build_drive_queries("Entregar informe mensual", "2024-04-01", "2024-04-30")
    assert all(isinstance(q, DriveQuery) for q in queries)
    # One query per extracted keyword (up to 3) plus one per generic term — same
    # granularity as the pre-refactor per-string queries, so EVIDENCE_QUERIES_PER_OBLIGACION
    # truncation (which used to keep only the keyword-derived queries) still behaves the same.
    assert all(len(q.keywords) == 1 for q in queries)


# ─────────────────────────────────────────────────────────────────────────────
# calendar_fetch_node
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_fetch_normalizes_events():
    from app.agent.nodes import calendar_fetch as mod

    events = [
        CalendarEvent(
            id="ev1",
            summary="Reunión de seguimiento",
            description="Revisión de avances del contrato",
            html_link="https://calendar.google.com/event?eid=ev1",
            start=datetime.fromisoformat("2024-04-15T09:00:00-05:00"),
            attendees=[
                CalendarAttendee(is_self=True, response_status="accepted"),
                CalendarAttendee(email="supervisor@entidad.gov.co", is_self=False),
            ],
            event_type="default",
        ),
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
    # Metadatos de asistencia presentes para evidence_filter (evidence_filter.is_noise_calendar
    # lee metadata["attendees"][i]["self"]/["responseStatus"] — el shape debe preservarse
    # exactamente aunque ahora se construya desde CalendarAttendee, no desde el dict crudo.
    assert "metadata" in ev[0]
    assert "attendees" in ev[0]["metadata"]
    assert ev[0]["metadata"]["attendees"][0]["self"] is True
    assert ev[0]["metadata"]["attendees"][0]["responseStatus"] == "accepted"
    assert ev[0]["metadata"]["attendees"][1]["self"] is False
    assert ev[0]["metadata"]["is_all_day"] is False
    assert ev[0]["metadata"]["event_type"] == "default"


@pytest.mark.asyncio
async def test_calendar_fetch_marks_allday_events():
    from app.agent.nodes import calendar_fetch as mod

    events = [
        CalendarEvent(
            id="ev2",
            summary="Día festivo",
            start_date=date(2024, 4, 19),
            is_all_day=True,  # sin dateTime: el adapter ya marca all-day
            html_link="https://calendar.google.com/event?eid=ev2",
        ),
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


@pytest.mark.asyncio
async def test_calendar_fetch_microsoft_provider_uses_graph_adapter_and_appends():
    """Same append/tag contract as drive_fetch_node's provider param (Slice C2)."""
    from app.agent.nodes import calendar_fetch as mod

    google_event = CalendarEvent(id="g1", summary="Reunión Google", html_link="https://calendar.google.com/g1")
    ms_event = CalendarEvent(id="m1", summary="Reunión Outlook", html_link="https://outlook.office.com/m1")

    google_adapter = MagicMock()
    google_adapter.search_events = AsyncMock(return_value=[google_event])
    ms_adapter = MagicMock()
    ms_adapter.search_events = AsyncMock(return_value=[ms_event])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
    }

    with (
        patch.object(mod, "GoogleCalendarAdapter", return_value=google_adapter),
        patch.object(mod, "MicrosoftGraphAdapter", return_value=ms_adapter),
    ):
        state = await mod.calendar_fetch_node(state, provider=IntegrationProvider.GOOGLE)
        state = await mod.calendar_fetch_node(state, provider=IntegrationProvider.MICROSOFT)

    ev = state["calendar_evidencias"]
    assert len(ev) == 2
    providers = {e["provider"] for e in ev}
    assert providers == {"google", "microsoft"}


def test_declined_rsvp_is_noise_end_to_end():
    """Real adapter parse + real metadata extraction + real noise-filter call.

    A future rename/refactor of `_parse_event`, `_extract_event_metadata`, or
    `is_noise_calendar` breaks this test instead of silently breaking noise
    detection — the three halves are only checked together elsewhere via
    hand-built data.
    """
    from app.adapters.calendar.calendar_adapter import _parse_event
    from app.agent.nodes.calendar_fetch import _extract_event_metadata
    from app.agent.prompts.evidence_filter import is_noise_calendar

    raw_event = {
        "id": "ev-declined",
        "summary": "Reunión de seguimiento",
        "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        "attendees": [
            {"self": True, "responseStatus": "declined"},
            {"self": False, "email": "supervisor@entidad.gov.co", "responseStatus": "accepted"},
        ],
    }

    event = _parse_event(raw_event)
    metadata = _extract_event_metadata(event)

    assert is_noise_calendar(event.summary, metadata) is True


def test_accepted_rsvp_is_not_noise_end_to_end():
    """Same real pipeline, non-declined counterpart — must not be flagged as noise."""
    from app.adapters.calendar.calendar_adapter import _parse_event
    from app.agent.nodes.calendar_fetch import _extract_event_metadata
    from app.agent.prompts.evidence_filter import is_noise_calendar

    raw_event = {
        "id": "ev-accepted",
        "summary": "Reunión de seguimiento",
        "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        "attendees": [
            {"self": True, "responseStatus": "accepted"},
            {"self": False, "email": "supervisor@entidad.gov.co", "responseStatus": "accepted"},
        ],
    }

    event = _parse_event(raw_event)
    metadata = _extract_event_metadata(event)

    assert is_noise_calendar(event.summary, metadata) is False
