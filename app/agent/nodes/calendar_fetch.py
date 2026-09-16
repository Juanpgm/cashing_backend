"""Calendar fetch node — busca eventos (reuniones, entregas) como evidencia contractual.

Requiere scope calendar.readonly. Normaliza los eventos al mismo formato que las
otras fuentes de evidencia para que evidence_orchestrator los consolide.

Enriquece cada evento con metadatos de asistencia (attendees, is_all_day, event_type)
para que evidence_filter pueda descartar feriados, eventos rechazados y bloqueos
personales sin llamadas LLM adicionales.
"""

from __future__ import annotations

from itertools import zip_longest

import structlog

from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
from app.adapters.calendar.port import CalendarEvent
from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter
from app.agent.prompts.contract_terms import contract_query_variants
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.prompts.query_budget import obligacion_key, round_robin
from app.agent.state import AgentState
from app.core.config import settings
from app.models.integracion import IntegrationProvider

logger = structlog.get_logger("agent.nodes.calendar_fetch")

MAX_TERMS_TOTAL = 12


def _to_rfc3339(date_str: str, end_of_day: bool = False) -> str:
    """Convierte YYYY-MM-DD a RFC3339 UTC para la API de Calendar."""
    date_str = (date_str or "").strip().replace("/", "-")
    if not date_str:
        return ""
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{date_str}{suffix}"


def _event_start(event: CalendarEvent) -> str:
    if event.start is not None:
        return event.start.isoformat()
    if event.start_date is not None:
        return event.start_date.isoformat()
    return ""


def _build_calendar_query(obligaciones: list[dict]) -> str | None:
    """Extrae keywords de TODAS las obligaciones para sesgar la búsqueda de Calendar.

    Máximo esfuerzo: a diferencia de una búsqueda que solo mira las primeras
    obligaciones, esto combina keywords de cada obligación (no solo las primeras
    3) y solo capa el TOTAL de términos combinados (MAX_TERMS_TOTAL), tras
    deduplicar, para no producir una query desmesuradamente larga.

    Kept for backward compatibility (still exported/tested); superseded by
    `_calendar_terms` for the actual per-term fan-out `calendar_fetch_node`
    now does (root cause #5, evidencias/discovery-fix): joining every keyword
    into ONE query made Google AND them together, matching nothing once more
    than 1-2 terms combined.
    """
    keywords: list[str] = []
    for ob in obligaciones:
        desc = ob.get("descripcion") or ""
        keywords.extend(_extract_keywords(desc)[:2])
    unique = list(dict.fromkeys(keywords))[:MAX_TERMS_TOTAL]
    return " ".join(unique) if unique else None


def _calendar_terms(
    contrato: dict, obligaciones: list[dict], expanded_terms: dict[str, list[str]] | None = None
) -> list[str]:
    """One short term per Calendar API call, allocated with two SEPARATE budgets.

    Round-2 fix for the confirmed CRITICAL starvation finding: this used to
    concatenate every contract-number variant, then the entidad, then every
    obligación's keywords, and truncate the flat list at
    `EVIDENCE_MAX_CALENDAR_TERMS`. For a dotted DAGMA/Cali number the 7 variants
    plus the entidad filled all 8 slots, so NO obligación keyword and NO
    expanded phrase ever reached Calendar.

    Now: a small reserved contract-level block (only the matchable query
    variants — see `contract_query_variants` — plus the entidad), then the
    remaining budget dealt ROUND-ROBIN across obligaciones so each one is
    guaranteed its own keyword and its own expanded phrase.
    """
    contract_terms: list[str] = list(contract_query_variants(contrato.get("numero_contrato")))

    entidad = contrato.get("entidad")
    if entidad and len(str(entidad).strip()) > 3:
        contract_terms.append(str(entidad).strip())
    contract_terms = contract_terms[: min(settings.EVIDENCE_MAX_CONTRACT_QUERIES, settings.EVIDENCE_MAX_CALENDAR_TERMS)]

    expanded_terms = expanded_terms or {}
    groups: list[list[str]] = []
    for i, ob in enumerate(obligaciones):
        own = _extract_keywords(str(ob.get("descripcion") or ""))[:2]
        phrases = list(expanded_terms.get(obligacion_key(ob, i)) or [])
        group: list[str] = []
        for pair in zip_longest(own, phrases):
            group.extend(t for t in pair if t)
        if group:
            groups.append(group)

    budget = max(
        settings.EVIDENCE_MAX_CALENDAR_TERMS - len(contract_terms),
        settings.EVIDENCE_MIN_QUERIES_PER_OBLIGACION * len(groups),
    )
    terms = [*contract_terms, *round_robin(groups, budget)]
    return list(dict.fromkeys(t for t in terms if t))


def _extract_event_metadata(event: CalendarEvent) -> dict:
    """Extrae metadatos de asistencia del CalendarEvent normalizado.

    Preserva el shape crudo de Google (attendees[].self/responseStatus) que
    ``evidence_filter.is_noise_calendar`` espera, aunque ahora se construya
    desde ``CalendarAttendee`` en vez del dict crudo de la API.
    """
    return {
        "attendees": [
            {"self": a.is_self, "responseStatus": a.response_status, "email": a.email} for a in event.attendees
        ],
        "organizer": {"email": event.organizer_email} if event.organizer_email else {},
        "event_type": event.event_type,
        "is_all_day": event.is_all_day,
    }


def _event_content(ev: CalendarEvent) -> str:
    """Fold summary + description + location + attendee emails into one text
    blob for keyword/LLM scoring — location and attendees were previously
    discarded entirely (evidencias/discovery-fix root cause #5)."""
    parts = [ev.summary or "(evento sin título)", ev.description or ""]
    if ev.location:
        parts.append(f"Lugar: {ev.location}")
    attendee_emails = ", ".join(a.email for a in ev.attendees if a.email)
    if attendee_emails:
        parts.append(f"Asistentes: {attendee_emails}")
    return ". ".join(p for p in parts if p).strip()


def _event_link(ev: CalendarEvent) -> str:
    """Prefer the Meet URL when present — a Meet link is stronger contractual
    evidence (a real meeting happened) than the bare Calendar event page."""
    return ev.hangout_link or ev.html_link


async def calendar_fetch_node(
    state: AgentState, provider: IntegrationProvider = IntegrationProvider.GOOGLE
) -> AgentState:
    """Lista eventos del Calendar/Outlook del usuario en el período del contrato como evidencia.

    Requiere en state: user_id, _db, contrato_contexto (fecha_inicio/fecha_fin).
    Produce en state: calendar_evidencias (lista de dicts con title/link/date/event_id/metadata).

    `provider` selects the adapter (Google Calendar vs. Microsoft Graph). Results
    are APPENDED to any `calendar_evidencias` already in `state` — so calling this
    once per connected provider (evidence_discovery_service.descubrir_evidencias)
    merges every provider's events instead of the last call clobbering the rest.

    Fires ONE short query per term (contract-number variants, entidad,
    obligación keywords — see `_calendar_terms`) instead of ANDing every term
    into a single query, which matched nothing once more than 1-2 terms
    combined (root cause #5, evidencias/discovery-fix). Results are merged by
    event id across terms. A term with no results/an error is skipped —
    isolated per-term the same way drive_fetch_node isolates per-query
    failures — so one bad term never drops evidence another term found.
    """
    existing: list[dict] = state.get("calendar_evidencias") or []

    user_id = state.get("user_id")
    db = state.get("_db")
    if not user_id or not db:
        return {**state, "calendar_evidencias": existing}

    contrato = state.get("contrato_contexto") or {}
    time_min = _to_rfc3339(str(contrato.get("fecha_inicio", "")))
    time_max = _to_rfc3339(str(contrato.get("fecha_fin", "")), end_of_day=True)
    if not time_min or not time_max:
        return {**state, "calendar_evidencias": existing}

    obligaciones = state.get("obligaciones_contexto") or []
    terms = _calendar_terms(contrato, obligaciones, state.get("expanded_terms"))
    queries: list[str | None] = list(terms) if terms else [None]

    adapter = GoogleCalendarAdapter(db) if provider == IntegrationProvider.GOOGLE else MicrosoftGraphAdapter(db)

    events_by_id: dict[str, CalendarEvent] = {}
    any_call_succeeded = False
    last_error: Exception | None = None
    for q in queries:
        try:
            events = await adapter.search_events(
                user_id, time_min, time_max, max_results=settings.EVIDENCE_MAX_EVENTS, q=q
            )
        except Exception as exc:
            last_error = exc
            await logger.awarning(
                "calendar_query_failed", query=q, error=str(exc), user_id=str(user_id), provider=provider.value
            )
            continue
        any_call_succeeded = True
        for ev in events:
            events_by_id.setdefault(ev.id, ev)

    if not any_call_succeeded and last_error is not None:
        await logger.aerror(
            "calendar_fetch_error", error=str(last_error), user_id=str(user_id), provider=provider.value
        )
        return {
            **state,
            "calendar_evidencias": existing,
            "error": f"Error leyendo Calendar ({provider.value}): {last_error}. Verifica que tu cuenta esté conectada.",
        }

    calendar_evidencias = [
        {
            "source": "calendar",
            "title": ev.summary or "(evento sin título)",
            "content": _event_content(ev),
            "link": _event_link(ev),
            "date": _event_start(ev),
            "event_id": ev.id,
            "metadata": _extract_event_metadata(ev),
            "provider": provider.value,
        }
        # Round-2: Calendar was the ONLY source with no total cap. Email
        # truncates at EVIDENCE_MAX_EMAILS_TOTAL and Drive at
        # EVIDENCE_MAX_FILES_TOTAL, but the per-term fan-out this branch
        # introduced could merge terms x EVIDENCE_MAX_EVENTS items and push all
        # of them into evidence_filter's LLM batches and the single
        # `_embed_batch` call, where an oversized input degrades the WHOLE run
        # to keyword-only ranking. Applied to THIS call's contribution only, so
        # a second provider's events are not discarded.
        for ev in list(events_by_id.values())[: settings.EVIDENCE_MAX_EVENTS_TOTAL]
    ]

    await logger.ainfo(
        "calendar_fetch_complete",
        user_id=str(user_id),
        events=len(calendar_evidencias),
        terms=terms,
        provider=provider.value,
    )
    return {**state, "calendar_evidencias": existing + calendar_evidencias}
