"""Calendar fetch node — busca eventos (reuniones, entregas) como evidencia contractual.

Requiere scope calendar.readonly. Normaliza los eventos al mismo formato que las
otras fuentes de evidencia para que evidence_orchestrator los consolide.

Enriquece cada evento con metadatos de asistencia (attendees, is_all_day, event_type)
para que evidence_filter pueda descartar feriados, eventos rechazados y bloqueos
personales sin llamadas LLM adicionales.
"""

from __future__ import annotations

import structlog

from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.state import AgentState
from app.core.config import settings

logger = structlog.get_logger("agent.nodes.calendar_fetch")

MAX_TERMS_TOTAL = 12


def _to_rfc3339(date_str: str, end_of_day: bool = False) -> str:
    """Convierte YYYY-MM-DD a RFC3339 UTC para la API de Calendar."""
    date_str = (date_str or "").strip().replace("/", "-")
    if not date_str:
        return ""
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{date_str}{suffix}"


def _event_start(event: dict) -> str:
    start = event.get("start") or {}
    return start.get("dateTime") or start.get("date") or ""


def _build_calendar_query(obligaciones: list[dict]) -> str | None:
    """Extrae keywords de TODAS las obligaciones para sesgar la búsqueda de Calendar.

    Máximo esfuerzo: a diferencia de una búsqueda que solo mira las primeras
    obligaciones, esto combina keywords de cada obligación (no solo las primeras
    3) y solo capa el TOTAL de términos combinados (MAX_TERMS_TOTAL), tras
    deduplicar, para no producir una query desmesuradamente larga.
    """
    keywords: list[str] = []
    for ob in obligaciones:
        desc = ob.get("descripcion") or ""
        keywords.extend(_extract_keywords(desc)[:2])
    unique = list(dict.fromkeys(keywords))[:MAX_TERMS_TOTAL]
    return " ".join(unique) if unique else None


def _extract_event_metadata(ev: dict) -> dict:
    """Extrae metadatos de asistencia del evento raw de Google Calendar."""
    start = ev.get("start") or {}
    is_all_day = "date" in start and "dateTime" not in start
    return {
        "attendees": ev.get("attendees") or [],
        "organizer": ev.get("organizer") or {},
        "event_type": ev.get("eventType") or "default",
        "is_all_day": is_all_day,
    }


async def calendar_fetch_node(state: AgentState) -> AgentState:
    """Lista eventos del Calendar del usuario en el período del contrato como evidencia.

    Requiere en state: user_id, _db, contrato_contexto (fecha_inicio/fecha_fin).
    Produce en state: calendar_evidencias (lista de dicts con title/link/date/event_id/metadata).
    """
    user_id = state.get("user_id")
    db = state.get("_db")
    if not user_id or not db:
        return {**state, "calendar_evidencias": []}

    contrato = state.get("contrato_contexto") or {}
    time_min = _to_rfc3339(str(contrato.get("fecha_inicio", "")))
    time_max = _to_rfc3339(str(contrato.get("fecha_fin", "")), end_of_day=True)
    if not time_min or not time_max:
        return {**state, "calendar_evidencias": []}

    obligaciones = state.get("obligaciones_contexto") or []
    q = _build_calendar_query(obligaciones)

    adapter = GoogleCalendarAdapter(db)
    try:
        events = await adapter.search_events(
            user_id, time_min, time_max, max_results=settings.EVIDENCE_MAX_EVENTS, q=q
        )
    except Exception as exc:
        await logger.aerror("calendar_fetch_error", error=str(exc), user_id=str(user_id))
        return {
            **state,
            "calendar_evidencias": [],
            "error": f"Error leyendo Calendar: {exc}. Verifica que tu cuenta de Google esté conectada.",
        }

    calendar_evidencias = []
    for ev in events:
        summary = ev.get("summary", "(evento sin título)")
        description = ev.get("description", "") or ""
        calendar_evidencias.append(
            {
                "source": "calendar",
                "title": summary,
                "content": f"{summary}. {description}".strip(),
                "link": ev.get("htmlLink", ""),
                "date": _event_start(ev),
                "event_id": ev.get("id", ""),
                "metadata": _extract_event_metadata(ev),
            }
        )

    await logger.ainfo("calendar_fetch_complete", user_id=str(user_id), events=len(calendar_evidencias), q=q)
    return {**state, "calendar_evidencias": calendar_evidencias}
