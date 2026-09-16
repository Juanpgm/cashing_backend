"""Provider-side caps, retries and date predicates.

Round-2 regression suite for three confirmed WARNING findings:

- Calendar was the ONLY source with no total cap. The branch turned one
  `events.list` call into one per term, so the merged pool could reach
  terms x EVIDENCE_MAX_EVENTS items flowing uncapped into the filter's LLM
  batches and the single `_embed_batch` call. Its adapter also has no
  429 backoff, unlike Gmail's.
- The Drive `(createdTime >= from or modifiedTime >= from)` lower bound is a
  no-op: for files Drive timestamps itself `modifiedTime >= createdTime`, so
  the OR adds no rows, while the unchanged `modifiedTime <= to` upper bound
  still excludes the case the comment claims to fix (created inside the
  period, edited after it).
- Gmail fetch amplification: one `users.messages.get` per returned id, at 25
  per query, with the result truncated to 60 — most of the work discarded.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _event(ev_id: str):
    ev = MagicMock()
    ev.id = ev_id
    ev.summary = f"Evento {ev_id}"
    ev.description = ""
    ev.location = ""
    ev.attendees = []
    ev.organizer_email = ""
    ev.event_type = "default"
    ev.is_all_day = False
    ev.start = None
    ev.start_date = None
    ev.hangout_link = ""
    ev.html_link = f"https://cal/{ev_id}"
    return ev


# ── Calendar total cap ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_merged_pool_is_capped_like_email_and_drive(monkeypatch):
    from app.agent.nodes import calendar_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_MAX_EVENTS_TOTAL", 25)

    counter = {"n": 0}

    async def _search(user_id, time_min, time_max, max_results=None, q=None):
        base = counter["n"] * 1000
        counter["n"] += 1
        return [_event(f"e{base + i}") for i in range(50)]

    adapter = MagicMock()
    adapter.search_events = AsyncMock(side_effect=_search)

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2025-09-01",
            "fecha_fin": "2025-09-30",
            "numero_contrato": "4161.010.26.1.027.2025",
            "entidad": "DAGMA",
        },
        "obligaciones_contexto": [
            {"id": f"ob{i}", "descripcion": f"Realizar actividad numero {i} de seguimiento"} for i in range(5)
        ],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=adapter):
        result = await mod.calendar_fetch_node(state)

    assert len(result["calendar_evidencias"]) == 25


@pytest.mark.asyncio
async def test_calendar_cap_is_applied_per_provider_not_to_the_accumulated_list(monkeypatch):
    """Results are APPENDED across providers; the cap must bound this call's
    contribution without discarding what another provider already found."""
    from app.agent.nodes import calendar_fetch as mod

    monkeypatch.setattr(mod.settings, "EVIDENCE_MAX_EVENTS_TOTAL", 5)

    adapter = MagicMock()
    adapter.search_events = AsyncMock(return_value=[_event(f"e{i}") for i in range(20)])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2025-09-01", "fecha_fin": "2025-09-30"},
        "calendar_evidencias": [{"source": "calendar", "event_id": "pre-existing"}],
    }

    with patch.object(mod, "GoogleCalendarAdapter", return_value=adapter):
        result = await mod.calendar_fetch_node(state)

    out = result["calendar_evidencias"]
    assert out[0]["event_id"] == "pre-existing"
    assert len(out) == 1 + 5


# ── Calendar retry ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_adapter_retries_on_rate_limit(monkeypatch):
    """8 sequential events.list calls per run with no 429 backoff, while Gmail
    has `_execute_with_retry` — a rate-limited term just lost its results."""
    from app.adapters.calendar import calendar_adapter as mod

    assert hasattr(mod.GoogleCalendarAdapter, "_execute_with_retry"), (
        "Calendar has no retry helper; a 429 on one term silently drops its events"
    )

    adapter = mod.GoogleCalendarAdapter(MagicMock())
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise mod.GoogleHttpError(MagicMock(status=429, reason="Rate Limit Exceeded"), b"rate limit")
        return {"ok": True}

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    assert await adapter._execute_with_retry(_flaky) == {"ok": True}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_calendar_adapter_does_not_retry_a_non_rate_limit_error(monkeypatch):
    from app.adapters.calendar import calendar_adapter as mod

    adapter = mod.GoogleCalendarAdapter(MagicMock())
    attempts = {"n": 0}

    def _boom():
        attempts["n"] += 1
        raise mod.GoogleHttpError(MagicMock(status=404, reason="Not Found"), b"nope")

    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    with pytest.raises(mod.GoogleHttpError):
        await adapter._execute_with_retry(_boom)
    assert attempts["n"] == 1


# ── Drive date predicate ──────────────────────────────────────────────────────


def test_drive_date_predicate_is_symmetric():
    """`(created >= from or modified >= from) and modified <= to` adds no rows
    and still excludes a file created inside the period and edited after it."""
    from app.adapters.drive.drive_adapter import DriveAdapter
    from app.adapters.drive.port import DriveQuery

    q = DriveQuery(
        keywords=["informe"],
        date_from=datetime(2025, 9, 1, 0, 0, 0),
        date_to=datetime(2025, 9, 30, 23, 59, 59),
        exclude_folders=True,
    )
    built = DriveAdapter(MagicMock())._translate_query(q)

    assert "(createdTime >= '2025-09-01T00:00:00' and createdTime <= '2025-09-30T23:59:59')" in built
    assert "(modifiedTime >= '2025-09-01T00:00:00' and modifiedTime <= '2025-09-30T23:59:59')" in built
    # The two windows are ORed: either timestamp landing in the period qualifies.
    assert " or " in built


def test_drive_date_predicate_with_only_a_lower_bound():
    from app.adapters.drive.drive_adapter import DriveAdapter
    from app.adapters.drive.port import DriveQuery

    q = DriveQuery(keywords=["informe"], date_from=datetime(2025, 9, 1), date_to=None)
    built = DriveAdapter(MagicMock())._translate_query(q)
    assert "createdTime >= '2025-09-01T00:00:00'" in built
    assert "modifiedTime >= '2025-09-01T00:00:00'" in built
    assert "<=" not in built


def test_drive_date_predicate_with_no_bounds_at_all():
    from app.adapters.drive.drive_adapter import DriveAdapter
    from app.adapters.drive.port import DriveQuery

    built = DriveAdapter(MagicMock())._translate_query(DriveQuery(keywords=["informe"]))
    assert "createdTime" not in built
    assert "modifiedTime" not in built


# ── Gmail fetch amplification ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gmail_per_query_fetch_shrinks_once_the_total_pool_is_full(monkeypatch):
    """25 per query x 24 queries = up to 600 full messages.get calls, truncated
    to 60. The later queries must still RUN (every obligación needs its own
    search) but fetch far fewer messages once the pool is already full."""
    from app.services import evidence_discovery_service as eds

    monkeypatch.setattr(eds.settings, "EVIDENCE_MAX_EMAILS_TOTAL", 10)

    requested: list[int] = []
    counter = {"n": 0}

    async def _fake_search(usuario_id, query, max_results):
        requested.append(max_results)
        base = counter["n"] * 1000
        counter["n"] += 1
        out = []
        for i in range(max_results):
            m = MagicMock()
            m.id = f"m{base + i}"
            m.subject = "asunto"
            m.body_plain = "cuerpo"
            m.snippet = ""
            m.sender = "supervisor@cali.gov.co"
            m.labels = []
            m.headers = {}
            m.attachments = []
            m.date = None
            out.append(m)
        return out

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    obligaciones = [{"id": f"ob{i}", "descripcion": f"Realizar actividad numero {i} mensual"} for i in range(5)]

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_email_evidence(
            MagicMock(),
            uuid.uuid4(),
            obligaciones,
            "2025-09-01",
            "2025-09-30",
            None,
            "DAGMA",
            numero_contrato="4161.010.26.1.027.2025",
        )

    assert len(requested) > 5, "later queries were skipped entirely — obligaciones would go unsearched"
    assert requested[-1] < requested[0], (
        f"per-query fetch never shrank once the pool was full: {requested}"
    )
    assert sum(requested) < 25 * len(requested), "total fetch volume was not bounded"


# ── Entidad truncation ────────────────────────────────────────────────────────


def test_long_entidad_is_truncated_on_a_word_boundary():
    """`entidad.strip()[:60]` cut long official Colombian names MID-TOKEN and
    then quoted the fragment as an exact-phrase Gmail query, which tokenizes —
    so `"...Domiciliarios de Barrancaberme"` matched nothing while still
    consuming a query slot."""
    from app.agent.prompts.email_evidence import build_contract_queries

    entidad = "Empresa de Servicios Publicos Domiciliarios de Barrancabermeja"
    queries = build_contract_queries([], "2025/09/01", "2025/10/01", None, entidad)
    entity_query = next(q for q in queries if "Servicios" in q)
    phrase = entity_query.split('"')[1]
    assert not entidad.startswith(phrase) or entidad[len(phrase) : len(phrase) + 1] in ("", " "), (
        f"entidad cut mid-token: {phrase!r}"
    )


def test_short_entidad_is_not_truncated_at_all():
    from app.agent.prompts.email_evidence import build_contract_queries

    queries = build_contract_queries([], "2025/09/01", "2025/10/01", None, "DAGMA Cali")
    assert '"DAGMA Cali"' in queries[0]


def test_obligation_queries_entidad_is_also_word_boundary_truncated():
    """The same defect exists at `entidad[:40]` in build_obligation_queries, and
    fires once per obligación — higher frequency than the contract-level one."""
    from app.agent.prompts.email_evidence import build_obligation_queries

    entidad = "Instituto Colombiano de Bienestar Familiar Cecilia de la Fuente de Lleras"
    queries = build_obligation_queries("Entregar informes", "2025/09/01", "2025/10/01", None, entidad)
    entity_query = next(q for q in queries if "Instituto" in q)
    phrase = entity_query.split('"')[1]
    assert entidad[len(phrase) : len(phrase) + 1] in ("", " "), f"entidad cut mid-token: {phrase!r}"
