"""Evidence discovery service — agente 'explorer' de evidencias en Google Workspace.

Orquesta el descubrimiento de evidencias en Gmail + Drive + Calendar para un conjunto de
obligaciones contractuales y genera el texto de justificación por obligación, con los links
de soporte, listo para montar la Cuenta de Cobro / Radicación.

Reusa los nodos del grafo del agente como funciones encadenadas (más testeable que la
ejecución completa del grafo): gather Gmail → drive_fetch → calendar_fetch →
evidence_orchestrator → evidence_matcher → evidence_justify.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.email.gmail_adapter import GmailAdapter
from app.agent.nodes.calendar_fetch import calendar_fetch_node
from app.agent.nodes.drive_fetch import drive_fetch_node
from app.agent.nodes.evidence_filter import evidence_filter_node
from app.agent.nodes.evidence_justify import evidence_justify_node
from app.agent.nodes.evidence_matcher import evidence_matcher_node
from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node
from app.agent.prompts.email_evidence import build_obligation_queries
from app.agent.prompts.evidence_filter import score_non_personal_email
from app.agent.state import AgentState
from app.core.exceptions import ExternalServiceError, ValidationError
from app.models.obligacion import Obligacion
from app.schemas.google_workspace import (
    EvidenceDiscoveryRequest,
    EvidenceDiscoveryResponse,
    ObligacionJustificada,
)
from app.services import google_workspace_service as gws

logger = structlog.get_logger("services.evidence_discovery")

MAX_EMAILS_PER_QUERY = 10
MAX_EMAILS_TOTAL = 25
GMAIL_PERMALINK = "https://mail.google.com/mail/u/0/#all/{message_id}"


def _to_gmail_date(date_str: str) -> str:
    """YYYY-MM-DD → YYYY/MM/DD (formato de query de Gmail)."""
    return (date_str or "").strip().replace("-", "/")


async def _resolve_obligaciones(db: AsyncSession, req: EvidenceDiscoveryRequest) -> list[dict[str, str]]:
    """Normaliza las obligaciones desde el request o las carga por contrato_id."""
    if req.obligaciones:
        return [{"id": ob.id or str(i), "descripcion": ob.descripcion} for i, ob in enumerate(req.obligaciones)]

    if req.contrato_id:
        result = await db.execute(
            select(Obligacion).where(Obligacion.contrato_id == req.contrato_id).order_by(Obligacion.orden)
        )
        rows = result.scalars().all()
        return [
            {"id": str(ob.id), "descripcion": ob.descripcion, "tipo": ob.tipo, "etiqueta": ob.etiqueta}
            for ob in rows
        ]

    raise ValidationError("Debes enviar 'obligaciones' o un 'contrato_id' válido.")


async def _gather_gmail_evidence(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    obligaciones: list[dict[str, str]],
    fecha_inicio: str,
    fecha_fin: str,
    supervisor_email: str | None,
    entidad: str | None,
) -> tuple[list[dict], int]:
    """Busca correos crudos como evidencia y los normaliza al formato común.

    Returns (emails, filtered_count) — filtered_count is how many non-personal
    emails were dropped before they could contaminate the evidence pipeline.
    """
    adapter = GmailAdapter(db)
    fi, ff = _to_gmail_date(fecha_inicio), _to_gmail_date(fecha_fin)

    queries: list[str] = []
    for ob in obligaciones[:3]:
        queries.extend(
            build_obligation_queries(ob["descripcion"], fi, ff, supervisor_email or None, entidad or None)[:2]
        )
    seen_q: set[str] = set()
    unique_queries = [q for q in queries if not (q in seen_q or seen_q.add(q))]

    emails_by_id: dict[str, dict] = {}
    filtered_count = 0
    for query in unique_queries[:5]:
        try:
            messages = await adapter.search_messages(usuario_id, query, MAX_EMAILS_PER_QUERY)
        except Exception as exc:
            await logger.awarning("gmail_query_failed", query=query, error=str(exc))
            continue
        for m in messages:
            if m.id not in emails_by_id:
                score, reason = score_non_personal_email(
                    sender=m.sender,
                    subject=m.subject,
                    labels=list(m.labels or []),
                    headers=dict(m.headers or {}),
                )
                if score >= 3:
                    filtered_count += 1
                    await logger.adebug(
                        "gmail_evidence_filtered_non_personal",
                        subject=m.subject[:80],
                        sender=m.sender[:80],
                        score=score,
                        reason=reason,
                    )
                    continue
                emails_by_id[m.id] = {
                    "source": "email",
                    "content": (m.body_plain or m.snippet or "")[:800],
                    "title": m.subject,
                    "subject": m.subject,
                    "link": GMAIL_PERMALINK.format(message_id=m.id),
                    "date": m.date.isoformat() if m.date else "",
                    "message_id": m.id,
                    "sender": m.sender,
                    "labels": list(m.labels or []),
                    "headers": dict(m.headers or {}),
                }
    return list(emails_by_id.values())[:MAX_EMAILS_TOTAL], filtered_count


async def descubrir_evidencias(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    req: EvidenceDiscoveryRequest,
) -> EvidenceDiscoveryResponse:
    """Punto de entrada: descubre evidencias y genera justificaciones por obligación."""
    obligaciones = await _resolve_obligaciones(db, req)

    # Verificar conexión de Google antes de gastar llamadas.
    status = await gws.get_integration_status(db, usuario_id)
    if not status.connected:
        raise ExternalServiceError(
            "Google", "La cuenta de Google no está conectada. Usa /integraciones/google/connect."
        )

    # 1. Reunir evidencia cruda de Gmail (filtra no-personal en origen).
    email_evidencias, email_filtered = await _gather_gmail_evidence(
        db, usuario_id, obligaciones, req.fecha_inicio, req.fecha_fin, req.supervisor_email, req.entidad
    )

    # Estado compartido por los nodos del agente.
    state: AgentState = {
        "user_id": usuario_id,
        "_db": db,
        "contrato_contexto": {"fecha_inicio": req.fecha_inicio, "fecha_fin": req.fecha_fin},
        "obligaciones_contexto": obligaciones,
        "obligaciones_extraidas": obligaciones,  # evidence_matcher lee esta key
        "email_evidencias": email_evidencias,
    }

    # 2-3. Explorar Drive y Calendar.
    state = await drive_fetch_node(state)
    state = await calendar_fetch_node(state)

    # 4. Consolidar → filtrar ruido → emparejar → justificar.
    state = await evidence_orchestrator_node(state)
    state = await evidence_filter_node(state)
    state = await evidence_matcher_node(state)
    state = await evidence_justify_node(state)

    justificaciones = state.get("justificaciones") or []
    obligaciones_out = [ObligacionJustificada.model_validate(j) for j in justificaciones]

    fuentes = {
        "email": len(state.get("email_evidencias") or []),
        "drive": len(state.get("drive_evidencias") or []),
        "calendar": len(state.get("calendar_evidencias") or []),
    }
    total = sum(fuentes.values())
    descartadas = (state.get("evidencias_descartadas") or 0) + email_filtered
    resumen = (
        f"Exploré Gmail, Drive y Calendar para {len(obligaciones)} obligación(es) en el período "
        f"{req.fecha_inicio} a {req.fecha_fin}. Encontré {total} evidencia(s): "
        f"{fuentes['email']} correos, {fuentes['drive']} documentos de Drive, "
        f"{fuentes['calendar']} eventos de Calendar."
        + (f" Se descartaron {descartadas} item(s) como ruido." if descartadas else "")
    )

    await logger.ainfo(
        "evidence_discovery_done",
        user_id=str(usuario_id),
        obligaciones=len(obligaciones),
        total_evidencias=total,
        descartadas=descartadas,
    )

    return EvidenceDiscoveryResponse(
        obligaciones=obligaciones_out,
        resumen=resumen,
        total_evidencias=total,
        fuentes=fuentes,
    )
