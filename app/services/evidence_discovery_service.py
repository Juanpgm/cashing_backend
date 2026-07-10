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
from datetime import date

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
from app.core.config import settings
from app.core.exceptions import GOOGLE_NOT_CONNECTED, ExternalServiceError, NotFoundError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.obligacion import Obligacion
from app.schemas.google_workspace import (
    EvidenceDiscoveryRequest,
    EvidenceDiscoveryResponse,
    ObligacionJustificada,
)
from app.services import google_workspace_service as gws

logger = structlog.get_logger("services.evidence_discovery")

MAX_EMAILS_PER_QUERY = 10
GMAIL_PERMALINK = "https://mail.google.com/mail/u/0/#all/{message_id}"
MAX_ACTIVIDADES_PREVIAS = 20


def _to_gmail_date(date_str: str) -> str:
    """YYYY-MM-DD → YYYY/MM/DD (formato de query de Gmail)."""
    return (date_str or "").strip().replace("-", "/")


async def _resolve_contrato_id(
    db: AsyncSession, usuario_id: uuid.UUID, req: EvidenceDiscoveryRequest
) -> uuid.UUID | None:
    """Resuelve el contrato_id explícito o el de la cuenta_id (si el frontend solo tiene esa).

    `cuenta_id` es un atajo: el frontend ya conoce la cuenta de cobro en curso, así que
    puede enviarla en vez de resolver el contrato_id por su cuenta. También habilita la
    carga de actividades previas del mismo contrato y el default de fechas.

    Security: both `contrato_id` and `cuenta_id` (via its own contrato) must belong to
    `usuario_id`. Without this check a malicious client could pass another user's
    contrato_id/cuenta_id and have their obligaciones/actividades exposed through the
    justificación the agent produces (mirrors `evidence_persist_service._verify_cuenta_owned`).
    """
    if req.contrato_id:
        result = await db.execute(
            select(Contrato.id).where(Contrato.id == req.contrato_id, Contrato.usuario_id == usuario_id)
        )
        if result.first() is None:
            raise NotFoundError("Contrato", str(req.contrato_id))
        return req.contrato_id
    if req.cuenta_id:
        result = await db.execute(
            select(CuentaCobro.contrato_id)
            .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
            .where(CuentaCobro.id == req.cuenta_id, Contrato.usuario_id == usuario_id)
        )
        row = result.first()
        if row is None:
            raise NotFoundError("CuentaCobro", str(req.cuenta_id))
        return row[0]
    return None


async def _resolve_obligaciones(
    db: AsyncSession, req: EvidenceDiscoveryRequest, contrato_id: uuid.UUID | None
) -> list[dict[str, str]]:
    """Normaliza las obligaciones desde el request o las carga por contrato_id/cuenta_id."""
    if req.obligaciones:
        return [{"id": ob.id or str(i), "descripcion": ob.descripcion} for i, ob in enumerate(req.obligaciones)]

    if contrato_id:
        result = await db.execute(
            select(Obligacion).where(Obligacion.contrato_id == contrato_id).order_by(Obligacion.orden)
        )
        rows = result.scalars().all()
        return [
            {"id": str(ob.id), "descripcion": ob.descripcion, "tipo": ob.tipo, "etiqueta": ob.etiqueta}
            for ob in rows
        ]

    raise ValidationError("Debes enviar 'obligaciones' o un 'contrato_id'/'cuenta_id' válido.")


async def _actividades_previas(
    db: AsyncSession, contrato_id: uuid.UUID | None, cuenta_id_actual: uuid.UUID | None
) -> list[str]:
    """Carga las actividades de cuentas anteriores del MISMO contrato (grounding temporal).

    Se usa para que el LLM de justificación no repita literalmente la redacción de
    meses anteriores. Excluye la cuenta actual (si se conoce) y limita a
    MAX_ACTIVIDADES_PREVIAS filas más recientes primero (mes/año descendente).
    """
    if not contrato_id:
        return []

    query = (
        select(Actividad.descripcion, CuentaCobro.mes, CuentaCobro.anio)
        .join(CuentaCobro, Actividad.cuenta_cobro_id == CuentaCobro.id)
        .where(CuentaCobro.contrato_id == contrato_id, Actividad.descripcion.is_not(None), Actividad.descripcion != "")
    )
    if cuenta_id_actual:
        query = query.where(CuentaCobro.id != cuenta_id_actual)
    query = query.order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc()).limit(MAX_ACTIVIDADES_PREVIAS)

    result = await db.execute(query)
    return [f"{mes:02d}/{anio}: {descripcion}" for descripcion, mes, anio in result.all()]


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

    max_obligaciones = settings.EVIDENCE_MAX_OBLIGACIONES_QUERIES
    obligaciones_para_query = obligaciones if max_obligaciones <= 0 else obligaciones[:max_obligaciones]

    queries: list[str] = []
    for ob in obligaciones_para_query:
        queries.extend(
            build_obligation_queries(ob["descripcion"], fi, ff, supervisor_email or None, entidad or None)[
                : settings.EVIDENCE_QUERIES_PER_OBLIGACION
            ]
        )
    seen_q: set[str] = set()
    unique_queries = [q for q in queries if not (q in seen_q or seen_q.add(q))]

    emails_by_id: dict[str, dict] = {}
    filtered_count = 0
    for query in unique_queries[: settings.EVIDENCE_MAX_QUERIES_TOTAL]:
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
    return list(emails_by_id.values())[: settings.EVIDENCE_MAX_EMAILS_TOTAL], filtered_count


async def descubrir_evidencias(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    req: EvidenceDiscoveryRequest,
) -> EvidenceDiscoveryResponse:
    """Punto de entrada: descubre evidencias y genera justificaciones por obligación."""
    contrato_id = await _resolve_contrato_id(db, usuario_id, req)
    obligaciones = await _resolve_obligaciones(db, req, contrato_id)

    # Default de fechas: solo cuando tenemos contexto de contrato (contrato_id o
    # cuenta_id) y el request no las trae. Sin ese contexto no hay una fecha de
    # inicio razonable que inferir, así que se deja tal cual venga del frontend.
    fecha_inicio = req.fecha_inicio
    fecha_fin = req.fecha_fin
    contrato: Contrato | None = None
    if contrato_id and (not fecha_inicio or not fecha_fin):
        contrato = await db.get(Contrato, contrato_id)
        if contrato is not None:
            fecha_inicio = fecha_inicio or contrato.fecha_inicio.isoformat()
            # Local "today" (not UTC): for a Colombia-time user, the default period
            # end must be their calendar today. Using UTC pushed fecha_fin to
            # "tomorrow" for anyone running after ~19:00 local (00:00 UTC), producing
            # a range that ends on a future date.
            fecha_fin = fecha_fin or date.today().isoformat()

    # Verificar conexión de Google antes de gastar llamadas.
    status = await gws.get_integration_status(db, usuario_id)
    if not status.connected:
        raise ExternalServiceError(
            "Google",
            "La cuenta de Google no está conectada. Usa /integraciones/google/connect.",
            code=GOOGLE_NOT_CONNECTED,
        )

    # 1. Reunir evidencia cruda de Gmail (filtra no-personal en origen).
    email_evidencias, email_filtered = await _gather_gmail_evidence(
        db, usuario_id, obligaciones, fecha_inicio, fecha_fin, req.supervisor_email, req.entidad
    )

    # Actividades de meses anteriores del mismo contrato (grounding para no repetir texto).
    actividades_previas = await _actividades_previas(db, contrato_id, req.cuenta_id)

    # Estado compartido por los nodos del agente.
    state: AgentState = {
        "user_id": usuario_id,
        "_db": db,
        "contrato_contexto": {"fecha_inicio": fecha_inicio, "fecha_fin": fecha_fin},
        "obligaciones_contexto": obligaciones,
        "obligaciones_extraidas": obligaciones,  # evidence_matcher lee esta key
        "email_evidencias": email_evidencias,
        "actividades_previas": actividades_previas,
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
        f"{fecha_inicio} a {fecha_fin}. Encontré {total} evidencia(s): "
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
