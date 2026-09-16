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
async def test_drive_fetch_generic_terms_always_included_even_when_budget_is_tight(monkeypatch):
    """Generic evidence terms (informe/acta/entrega/soporte/reporte) must ALWAYS
    run, even when EVIDENCE_QUERIES_PER_OBLIGACION only allows one keyword-derived
    query — they used to be sliced away entirely once an obligación yielded >= N
    keywords (evidencias/discovery-fix root cause #4)."""
    from app.agent.nodes import drive_fetch as mod
    from app.agent.nodes.drive_fetch import _GENERIC_TERMS

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

    called_terms = [call.args[1].keywords[0] for call in mock_adapter.search_files.call_args_list]
    non_generic = [t for t in called_terms if t not in _GENERIC_TERMS]
    assert len(non_generic) == 1  # capped by the setting
    for term in _GENERIC_TERMS:
        assert term in called_terms  # never sliced away


@pytest.mark.asyncio
async def test_drive_fetch_queries_contract_number_variants_first(monkeypatch):
    """When contrato_contexto carries numero_contrato, its variants must be
    queried — and ordered ahead of the obligación's own keywords."""
    from app.agent.nodes import drive_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_QUERIES_PER_OBLIGACION", 1)

    mock_adapter = MagicMock()
    mock_adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2024-04-01",
            "fecha_fin": "2024-04-30",
            "numero_contrato": "4161.010.26.1.027.2025",
        },
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
    }

    with patch.object(mod, "DriveAdapter", return_value=mock_adapter):
        await mod.drive_fetch_node(state)

    called_terms = [call.args[1].keywords[0] for call in mock_adapter.search_files.call_args_list]
    # The single keyword-budget slot went to the contract number, not the obligación keyword.
    assert called_terms[0] == "4161.010.26.1.027.2025"


@pytest.mark.asyncio
async def test_drive_fetch_includes_expanded_phrase_queries(monkeypatch):
    """evidencias/discovery-fix WU7: LLM-generated search phrases (e.g. a
    deliverable name) must be queried too, not just the obligación's own
    keywords and the contract number."""
    from app.agent.nodes import drive_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_QUERIES_PER_OBLIGACION", 10)

    mock_adapter = MagicMock()
    mock_adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}],
        "expanded_terms": {"ob1": ["planilla de seguridad social"]},
    }

    with patch.object(mod, "DriveAdapter", return_value=mock_adapter):
        await mod.drive_fetch_node(state)

    called_terms = [call.args[1].keywords[0] for call in mock_adapter.search_files.call_args_list]
    assert "planilla de seguridad social" in called_terms


@pytest.mark.asyncio
async def test_drive_fetch_uses_settings_page_size(monkeypatch):
    from app.agent.nodes import drive_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_DRIVE_PAGE_SIZE", 33)

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

    max_results = [call.args[1].max_results for call in mock_adapter.search_files.call_args_list]
    assert all(n == 33 for n in max_results)


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
    """El node dispara una query por término contra el adapter."""
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

    assert mock_adapter.search_events.await_count >= 1
    call_kwargs = mock_adapter.search_events.call_args.kwargs
    assert "q" in call_kwargs
    assert call_kwargs["q"] is not None  # se construyó una query de keywords


@pytest.mark.asyncio
async def test_calendar_fetch_fires_one_query_per_term_and_merges_by_id():
    """Root cause #5 (evidencias/discovery-fix): joining ALL keywords into ONE
    AND-of-everything query matched nothing once more than 1-2 terms combined.
    Now one short query per term is fired, merging results by event id."""
    from app.agent.nodes import calendar_fetch as mod

    call_queries: list[str | None] = []

    async def _fake_search(usuario_id, time_min, time_max, calendar_id="primary", max_results=50, q=None):
        call_queries.append(q)
        # Same event returned for every term — must be merged, not duplicated.
        return [CalendarEvent(id="ev1", summary="Reunión de seguimiento", html_link="https://calendar.google.com/ev1")]

    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(side_effect=_fake_search)

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2024-04-01",
            "fecha_fin": "2024-04-30",
            "numero_contrato": "4161.010.26.1.027.2025",
            "entidad": "DAGMA",
        },
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento del proyecto"}],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        result = await mod.calendar_fetch_node(state)

    assert mock_adapter.search_events.await_count > 1  # more than one term-scoped call
    assert len(result["calendar_evidencias"]) == 1  # merged by event id, not duplicated
    assert any(q and "4161" in q for q in call_queries)  # contract number was one of the terms


@pytest.mark.asyncio
async def test_calendar_fetch_includes_expanded_phrase_terms():
    """evidencias/discovery-fix WU7: LLM-generated search phrases must also be
    queried against Calendar (e.g. a Meet-titled term the obligación text
    itself never mentions)."""
    from app.agent.nodes import calendar_fetch as mod

    call_queries: list[str | None] = []

    async def _fake_search(usuario_id, time_min, time_max, calendar_id="primary", max_results=50, q=None):
        call_queries.append(q)
        return []

    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(side_effect=_fake_search)

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2024-04-01", "fecha_fin": "2024-04-30"},
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento"}],
        "expanded_terms": {"ob1": ["mesa de trabajo mensual"]},
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        await mod.calendar_fetch_node(state)

    assert "mesa de trabajo mensual" in call_queries


@pytest.mark.asyncio
async def test_calendar_fetch_bounds_term_count_by_setting(monkeypatch):
    """`EVIDENCE_MAX_CALENDAR_TERMS` bounds the CONTRACT-level block and the
    discretionary pool — it never truncates the per-obligación floor.

    Round 2: this used to assert a flat `await_count == 2`, which is what let
    the contract-number variants swallow every Calendar slot and leave the
    obligación unsearched (confirmed CRITICAL finding).
    """
    from app.agent.nodes import calendar_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_MAX_CALENDAR_TERMS", 2)

    mock_adapter = MagicMock()
    mock_adapter.search_events = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2024-04-01",
            "fecha_fin": "2024-04-30",
            "numero_contrato": "4161.010.26.1.027.2025",
            "entidad": "DAGMA",
        },
        "obligaciones_contexto": [{"id": "ob1", "descripcion": "Asistir a reuniones de seguimiento del proyecto"}],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=mock_adapter):
        await mod.calendar_fetch_node(state)

    terms = [call.kwargs["q"] for call in mock_adapter.search_events.call_args_list]
    # Contract block capped at the setting (2), floor of 2 for the one obligación.
    assert sum(1 for t in terms if "4161" in t or t == "DAGMA") == 2
    assert mock_adapter.search_events.await_count == 2 + mod.settings.EVIDENCE_MIN_QUERIES_PER_OBLIGACION
    assert any("asistir" in t.lower() or "reuniones" in t.lower() for t in terms), (
        f"obligación keyword starved out of Calendar — terms: {terms}"
    )


@pytest.mark.asyncio
async def test_calendar_fetch_no_dates_returns_empty():
    from app.agent.nodes.calendar_fetch import calendar_fetch_node

    result = await calendar_fetch_node({"user_id": uuid.uuid4(), "_db": MagicMock(), "contrato_contexto": {}})
    assert result["calendar_evidencias"] == []


@pytest.mark.asyncio
async def test_calendar_fetch_prefers_hangout_link_and_includes_location_attendees():
    """Meet links never surfaced before (only html_link was captured) — root
    cause #5. `link` must prefer the Meet URL when present, and location +
    attendee emails must be folded into `content` for scoring."""
    from app.agent.nodes import calendar_fetch as mod

    events = [
        CalendarEvent(
            id="ev1",
            summary="Reunión de seguimiento",
            description="Revisión de avances",
            html_link="https://calendar.google.com/event?eid=ev1",
            location="Sala virtual",
            hangout_link="https://meet.google.com/abc-defg-hij",
            start=datetime.fromisoformat("2024-04-15T09:00:00-05:00"),
            attendees=[CalendarAttendee(email="supervisor@entidad.gov.co", is_self=False)],
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

    ev = result["calendar_evidencias"][0]
    assert ev["link"] == "https://meet.google.com/abc-defg-hij"
    assert "supervisor@entidad.gov.co" in ev["content"]
    assert "Sala virtual" in ev["content"]


@pytest.mark.asyncio
async def test_calendar_fetch_falls_back_to_html_link_without_meet():
    from app.agent.nodes import calendar_fetch as mod

    events = [
        CalendarEvent(id="ev1", summary="Reunión presencial", html_link="https://calendar.google.com/event?eid=ev1"),
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

    assert result["calendar_evidencias"][0]["link"] == "https://calendar.google.com/event?eid=ev1"


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


def test_parse_event_extracts_hangout_link():
    from app.adapters.calendar.calendar_adapter import _parse_event

    raw_event = {
        "id": "ev-meet",
        "summary": "Reunión de seguimiento",
        "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
    }

    event = _parse_event(raw_event)
    assert event.hangout_link == "https://meet.google.com/abc-defg-hij"


def test_parse_event_extracts_meet_link_from_conference_data_when_no_hangout_link():
    from app.adapters.calendar.calendar_adapter import _parse_event

    raw_event = {
        "id": "ev-conf",
        "summary": "Reunión de seguimiento",
        "start": {"dateTime": "2024-04-15T09:00:00-05:00"},
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "phone", "uri": "tel:+1234"},
                {"entryPointType": "video", "uri": "https://meet.google.com/xyz-uvwq-rst"},
            ]
        },
    }

    event = _parse_event(raw_event)
    assert event.hangout_link == "https://meet.google.com/xyz-uvwq-rst"


def test_parse_event_no_meet_link_defaults_empty():
    from app.adapters.calendar.calendar_adapter import _parse_event

    event = _parse_event({"id": "ev-plain", "summary": "Reunión presencial"})
    assert event.hangout_link == ""


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
