"""Email fetch node — busca correos en Gmail como evidencia de obligaciones contractuales."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.email.gmail_adapter import GmailAdapter
from app.adapters.llm import get_llm
from app.agent.prompts.email_evidence import (
    EMAIL_OBLIGATION_MATCHING_PROMPT,
    EMAIL_SUMMARY_SYSTEM_PROMPT,
    build_obligation_queries,
    format_emails_for_llm,
    format_obligaciones_for_llm,
)
from app.agent.prompts.evidence_filter import score_non_personal_email
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.email_fetch")

# Máximo de correos a analizar por query (controla tokens del LLM)
MAX_EMAILS_PER_QUERY = 10
# Máximo de correos a pasar al LLM para matching
MAX_EMAILS_FOR_LLM = 20


async def email_fetch_node(state: AgentState) -> AgentState:
    """Busca correos en Gmail y usa LLM para emparejarlos con obligaciones contractuales.

    Requiere en state:
    - user_id: UUID del usuario
    - contrato_contexto: dict con fecha_inicio, fecha_fin, entidad, supervisor_email
    - obligaciones_contexto: lista de dicts con id, descripcion, tipo

    Produce en state:
    - email_evidence: texto estructurado con los matches para el nodo chat
    - email_message_ids: IDs de los correos encontrados
    - email_query: última query usada
    - response: resumen en lenguaje natural para el usuario
    """
    user_id = state.get("user_id")
    if not user_id:
        return {**state, "error": "user_id requerido para buscar evidencias"}

    contrato = state.get("contrato_contexto") or {}
    obligaciones = state.get("obligaciones_contexto") or []

    fecha_inicio = str(contrato.get("fecha_inicio", ""))
    fecha_fin = str(contrato.get("fecha_fin", ""))
    supervisor_email = str(contrato.get("supervisor_email") or "")
    entidad = str(contrato.get("entidad") or "")

    if not fecha_inicio or not fecha_fin:
        return {
            **state,
            "error": "Se necesitan fecha_inicio y fecha_fin del contrato para buscar correos",
        }

    # Necesitamos acceso a la DB para el adapter — viene en state vía contexto del grafo
    # El adapter se obtiene desde el state._db inyectado por el service
    db = state.get("_db")
    if not db:
        return {
            **state,
            "response": "La búsqueda de correos requiere contexto de base de datos. "
            "Usa el endpoint POST /api/v1/integraciones/evidencias para esta función.",
        }

    try:
        adapter = GmailAdapter(db)

        # Construir queries para cada obligación o query genérica
        all_queries: list[str] = []
        if obligaciones:
            for oblig in obligaciones[:3]:  # máximo 3 obligaciones para no saturar la API
                queries = build_obligation_queries(
                    descripcion=str(oblig.get("descripcion", "")),
                    fecha_inicio=_format_date_for_gmail(fecha_inicio),
                    fecha_fin=_format_date_for_gmail(fecha_fin),
                    supervisor_email=supervisor_email or None,
                    entidad=entidad or None,
                )
                all_queries.extend(queries[:2])  # 2 queries por obligación
        else:
            all_queries = build_obligation_queries(
                descripcion=state.get("user_input", "actividades del contrato"),
                fecha_inicio=_format_date_for_gmail(fecha_inicio),
                fecha_fin=_format_date_for_gmail(fecha_fin),
                supervisor_email=supervisor_email or None,
                entidad=entidad or None,
            )

        # Deduplicar queries
        seen_queries: set[str] = set()
        unique_queries = []
        for q in all_queries:
            if q not in seen_queries:
                seen_queries.add(q)
                unique_queries.append(q)

        # Buscar correos (deduplicados por ID)
        all_emails_map: dict[str, dict[str, str]] = {}
        last_query = ""
        for query in unique_queries[:5]:  # máximo 5 queries
            try:
                messages = await adapter.search_messages(user_id, query, MAX_EMAILS_PER_QUERY)
                last_query = query
                for msg in messages:
                    if msg.id not in all_emails_map:
                        score, reason = score_non_personal_email(
                            sender=msg.sender,
                            subject=msg.subject,
                            labels=list(msg.labels or []),
                            headers=dict(msg.headers or {}),
                        )
                        if score >= 3:
                            await logger.adebug(
                                "email_fetch_filtered_non_personal",
                                subject=msg.subject[:80],
                                score=score,
                                reason=reason,
                            )
                            continue
                        all_emails_map[msg.id] = {
                            "id": msg.id,
                            "sender": msg.sender,
                            "subject": msg.subject,
                            "date": msg.date.isoformat(),
                            "snippet": msg.snippet,
                            "body_plain": msg.body_plain[:800],
                        }
            except Exception as exc:
                await logger.awarning("email_query_failed", query=query, error=str(exc))
                continue

        all_emails = list(all_emails_map.values())[:MAX_EMAILS_FOR_LLM]
        message_ids = [e["id"] for e in all_emails]

        if not all_emails:
            response = (
                f"No encontré correos en el período {fecha_inicio} - {fecha_fin} "
                f"con las queries utilizadas. Intenta buscar con términos más específicos."
            )
            return {
                **state,
                "email_evidence": [],
                "email_message_ids": [],
                "email_query": last_query,
                "response": response,
            }

        # Usar LLM para emparejar correos con obligaciones
        evidence_list = await _match_emails_to_obligations(
            emails=all_emails,
            obligaciones=obligaciones,
        )

        # Generar respuesta en lenguaje natural
        response = await _summarize_findings(
            emails_found=len(all_emails),
            evidence_list=evidence_list,
            queries_used=unique_queries,
        )

        await logger.ainfo(
            "email_fetch_complete",
            user_id=str(user_id),
            emails_found=len(all_emails),
            queries_used=len(unique_queries),
        )

        return {
            **state,
            "email_evidence": evidence_list,
            "email_message_ids": message_ids,
            "email_query": last_query,
            "response": response,
        }

    except Exception as exc:
        await logger.aerror("email_fetch_error", error=str(exc), user_id=str(user_id))
        return {
            **state,
            "error": str(exc),
            "response": f"Error buscando correos: {exc}. Verifica que tu cuenta de Google esté conectada.",
        }


async def _match_emails_to_obligations(
    emails: list[dict[str, str]],
    obligaciones: list[dict[str, str | int | None]],
) -> list[dict[str, str]]:
    """Usa LLM para determinar qué correos son evidencia de qué obligaciones.

    Retorna lista de dicts con estructura: {email_id, subject, obligacion, relevancia}.
    """
    llm = get_llm()

    emails_context = format_emails_for_llm(emails)
    obligaciones_context = format_obligaciones_for_llm(obligaciones)

    prompt = EMAIL_OBLIGATION_MATCHING_PROMPT.format(
        obligaciones=obligaciones_context,
        emails_context=emails_context,
    )

    try:
        resp = await llm.complete(
            [LLMMessage(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=2048,
        )
        raw = resp.content.strip()
        # Strip markdown fences
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw.strip())
        try:
            parsed: list[dict[str, str]] = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("not a list")
            return parsed
        except (json.JSONDecodeError, ValueError):
            await logger.awarning("email_evidence_json_parse_failed", preview=raw[:200])
            return []
    except Exception as exc:
        await logger.awarning("llm_matching_failed", error=str(exc))
        return []


async def _summarize_findings(
    emails_found: int,
    evidence_list: list[dict[str, str]],
    queries_used: list[str],
) -> str:
    """Genera un resumen conversacional de los hallazgos."""
    llm = get_llm()

    evidence_preview = json.dumps(evidence_list[:5], ensure_ascii=False)[:1500]
    summary_prompt = (
        f"Encontré {emails_found} correos relevantes.\n\n"
        f"Matches encontrados:\n{evidence_preview}\n\n"
        f"Resume en 3-5 oraciones qué correos son útiles como evidencia contractual, "
        f"mencionando para qué obligaciones sirven. Sé específico y conciso. "
        f"Habla en primera persona como asistente del contratista."
    )

    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=EMAIL_SUMMARY_SYSTEM_PROMPT),
                LLMMessage(role="user", content=summary_prompt),
            ],
            temperature=0.3,
            max_tokens=512,
        )
        return resp.content
    except Exception:
        return (
            f"Encontré {emails_found} correos en el período especificado. "
            f"Revisé {len(queries_used)} búsquedas diferentes en tu Gmail."
        )


def _format_date_for_gmail(date_str: str) -> str:
    """Convierte YYYY-MM-DD a YYYY/MM/DD para queries de Gmail."""
    return date_str.replace("-", "/")
