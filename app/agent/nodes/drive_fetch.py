"""Drive fetch node — explora Google Drive buscando documentos que sirvan de evidencia.

Análogo a email_fetch pero sobre el Drive del usuario (requiere scope drive.readonly).
Construye queries de Drive desde las obligaciones del contrato y devuelve los archivos
encontrados con su link (webViewLink) para soportar la cuenta de cobro.
"""

from __future__ import annotations

from datetime import datetime
from itertools import zip_longest

import structlog

from app.adapters.drive.drive_adapter import DriveAdapter
from app.adapters.drive.port import DriveQuery
from app.adapters.microsoft.graph_adapter import MicrosoftGraphAdapter
from app.agent.prompts.contract_terms import contract_query_variants
from app.agent.prompts.email_evidence import _extract_keywords, _safe_entity_phrase
from app.agent.prompts.query_budget import obligacion_key, round_robin
from app.agent.state import AgentState
from app.core.config import settings
from app.models.integracion import IntegrationProvider

logger = structlog.get_logger("agent.nodes.drive_fetch")

# Términos genéricos de evidencia documental en la función pública.
_GENERIC_TERMS = ("informe", "acta", "entrega", "soporte", "reporte")


def _to_drive_datetime(date_str: str, end_of_day: bool = False) -> datetime | None:
    """Convierte YYYY-MM-DD a datetime naive (modifiedTime) para DriveQuery."""
    date_str = (date_str or "").strip().replace("/", "-")
    if not date_str:
        return None
    suffix = "T23:59:59" if end_of_day else "T00:00:00"
    return datetime.fromisoformat(f"{date_str}{suffix}")


def build_drive_queries(
    descripcion: str,
    fecha_inicio: str,
    fecha_fin: str,
    extra_terms: list[str] | None = None,
) -> list[DriveQuery]:
    """Construye `DriveQuery` para buscar evidencia de una obligación en Drive.

    Args:
        descripcion: Texto de la obligación.
        fecha_inicio / fecha_fin: YYYY-MM-DD del período a cubrir.
        extra_terms: términos de mayor prioridad que las keywords de la
            obligación (p.ej. variantes del número de contrato) — se listan
            PRIMERO, antes de las keywords propias de la obligación.

    Returns:
        `extra_terms` + una `DriveQuery` por keyword extraída (hasta 3) + una
        por término genérico — en ese orden. `drive_fetch_node` es quien
        decide qué parte del resultado cae bajo `EVIDENCE_QUERIES_PER_OBLIGACION`
        (los términos genéricos SIEMPRE se incluyen, nunca se truncan — ver su
        docstring).
    """
    date_from = _to_drive_datetime(fecha_inicio)
    date_to = _to_drive_datetime(fecha_fin, end_of_day=True)

    def _query(term: str) -> DriveQuery:
        return DriveQuery(
            keywords=[term],
            date_from=date_from,
            date_to=date_to,
            exclude_folders=True,
            max_results=settings.EVIDENCE_DRIVE_PAGE_SIZE,
        )

    priority_terms = list(extra_terms or [])
    seen: set[str] = set(priority_terms)
    for kw in _extract_keywords(descripcion)[:3]:
        term = kw.replace("'", "")
        if term not in seen:
            seen.add(term)
            priority_terms.append(term)

    queries = [_query(term) for term in priority_terms]
    queries.extend(_query(term) for term in _GENERIC_TERMS)
    return queries


async def drive_fetch_node(state: AgentState, provider: IntegrationProvider = IntegrationProvider.GOOGLE) -> AgentState:
    """Busca documentos de evidencia en el Drive/OneDrive del usuario.

    Requiere en state: user_id, _db, contrato_contexto (fecha_inicio/fecha_fin),
    obligaciones_contexto (opcional).

    Produce en state: drive_evidencias (lista de dicts con title/link/date/file_id).

    `provider` selects the adapter (Google Drive vs. Microsoft Graph/OneDrive).
    Results are APPENDED to any `drive_evidencias` already in `state` — so calling
    this once per connected provider (evidence_discovery_service.descubrir_evidencias)
    merges every provider's files instead of the last call clobbering the rest.
    """
    existing: list[dict] = state.get("drive_evidencias") or []

    user_id = state.get("user_id")
    db = state.get("_db")
    if not user_id or not db:
        return {**state, "drive_evidencias": existing}

    contrato = state.get("contrato_contexto") or {}
    obligaciones = state.get("obligaciones_contexto") or []
    fecha_inicio = str(contrato.get("fecha_inicio", ""))
    fecha_fin = str(contrato.get("fecha_fin", ""))
    numero_query_variants = contract_query_variants(contrato.get("numero_contrato"))
    # LLM-generated search phrases (evidencias/discovery-fix WU7) — deliverable
    # names, counterpart names, filename variants — keyed by obligación id.
    expanded_terms: dict[str, list[str]] = state.get("expanded_terms") or {}

    # Construir queries: por obligación si existen, si no genéricas.
    max_obligaciones = settings.EVIDENCE_MAX_OBLIGACIONES_QUERIES
    obligaciones_para_query = obligaciones if max_obligaciones <= 0 else obligaciones[:max_obligaciones]

    date_from = _to_drive_datetime(fecha_inicio)
    date_to = _to_drive_datetime(fecha_fin, end_of_day=True)

    def _query(term: str) -> DriveQuery:
        return DriveQuery(
            keywords=[term],
            date_from=date_from,
            date_to=date_to,
            exclude_folders=True,
            max_results=settings.EVIDENCE_DRIVE_PAGE_SIZE,
        )

    # Contract-level terms are a per-RUN signal, emitted ONCE — repeating them
    # inside every obligación's slice is what consumed the whole budget before
    # a single obligación keyword was reached (round-2 CRITICAL fix). Only the
    # matchable query variants are used; the weaker forms stay available for
    # scoring via `contract_number_variants`.
    contract_terms: list[str] = list(numero_query_variants)
    entidad = str(contrato.get("entidad") or "").strip()
    if len(entidad) > 3:
        # Round-3 fix (confirmed WARNING): the RAW untruncated entidad was used
        # here, so a 65+ char formal SECOP name never matched the acronym real
        # filenames actually contain ("Informe DAGMA.pdf"). Shares
        # `_safe_entity_phrase` with Gmail's contract-level query instead of a
        # third, independent derivation.
        contract_terms.append(_safe_entity_phrase(entidad, 60))
    contract_terms = contract_terms[: settings.EVIDENCE_MAX_CONTRACT_QUERIES]

    # One group per obligación — its own keywords interleaved with its expanded
    # semantic phrases, so the round-robin floor covers both signals.
    def _obligacion_group(descripcion: str, ob_id: str = "") -> list[str]:
        own = [kw.replace("'", "") for kw in _extract_keywords(descripcion)[: settings.EVIDENCE_QUERIES_PER_OBLIGACION]]
        phrases = list(expanded_terms.get(ob_id) or [])
        group: list[str] = []
        for pair in zip_longest(own, phrases):
            group.extend(t for t in pair if t)
        return group

    if obligaciones:
        groups = [
            _obligacion_group(str(ob.get("descripcion", "")), obligacion_key(ob, i))
            for i, ob in enumerate(obligaciones_para_query)
        ]
    else:
        groups = [_obligacion_group(state.get("user_input", ""))]
    groups = [g for g in groups if g]

    # Round-3 fix (confirmed WARNING, same shape as the Gmail ceiling
    # regression): `obligacion_budget = max(remaining, MIN_PER_OB * n_groups)`
    # let the per-obligación FLOOR REQUIREMENT alone decide the total once
    # obligaciones outnumbered the budget. The floor is still honoured ON TOP
    # of the nominal budget for a contract with few obligaciones (unchanged,
    # intentional round-2 behavior), but the floor REQUIREMENT itself is now
    # capped at the nominal (pre-contract) budget, bounding the worst-case
    # overrun to `len(contract_terms)` no matter how many obligaciones exist.
    nominal_budget = max(settings.EVIDENCE_MAX_QUERIES_TOTAL - len(_GENERIC_TERMS), 0)
    remaining_after_contract = max(nominal_budget - len(contract_terms), 0)
    if groups:
        floor_needed = settings.EVIDENCE_MIN_QUERIES_PER_OBLIGACION * len(groups)
        capped_floor = min(floor_needed, nominal_budget)
        if capped_floor < floor_needed:
            logger.info(
                "drive_query_budget_floor_capped",
                requested=floor_needed,
                capped_to=capped_floor,
                n_obligaciones=len(groups),
            )
        obligacion_budget = max(remaining_after_contract, capped_floor)
    else:
        obligacion_budget = 0

    # Generic evidence terms ALWAYS run and are never truncated (evidencias/
    # discovery-fix root cause #4) — they are the contract-agnostic recall net.
    terms = [*contract_terms, *round_robin(groups, obligacion_budget), *_GENERIC_TERMS]
    queries: list[DriveQuery] = [_query(t) for t in terms]

    # Deduplicar queries preservando orden (DriveQuery no es hasheable: se usa
    # una tupla normalizada de sus campos como clave).
    seen_keys: set[tuple] = set()  # type: ignore[type-arg]
    unique_queries: list[DriveQuery] = []
    for query in queries:
        key = (
            tuple(query.keywords),
            query.date_from,
            query.date_to,
            query.exclude_folders,
            tuple(query.mime_types or []),
            query.max_results,
        )
        if key not in seen_keys:
            seen_keys.add(key)
            unique_queries.append(query)

    adapter = DriveAdapter(db) if provider == IntegrationProvider.GOOGLE else MicrosoftGraphAdapter(db)
    files_by_id: dict[str, dict] = {}
    try:
        for query in unique_queries:
            try:
                files = await adapter.search_files(user_id, query)
            except Exception as exc:
                await logger.awarning("drive_query_failed", query=query, error=str(exc), provider=provider.value)
                continue
            for f in files:
                if f.id not in files_by_id:
                    files_by_id[f.id] = {
                        "source": "drive",
                        "title": f.name,
                        "content": f.name,
                        "link": f.web_view_link,
                        "date": f.modified_at.isoformat() if f.modified_at else "",
                        "file_id": f.id,
                        "mime_type": f.mime_type,
                        # Carried for cross-provider dedup: the same document in
                        # Drive and OneDrive has different file ids, so identity
                        # is (name, size, mime) — see evidence_dedup.
                        "size": f.size_bytes,
                        "provider": provider.value,
                    }
    except Exception as exc:
        await logger.aerror("drive_fetch_error", error=str(exc), user_id=str(user_id), provider=provider.value)
        return {
            **state,
            "drive_evidencias": existing,
            "error": f"Error explorando Drive ({provider.value}): {exc}. Verifica que tu cuenta esté conectada.",
        }

    drive_evidencias = list(files_by_id.values())[: settings.EVIDENCE_MAX_FILES_TOTAL]
    await logger.ainfo(
        "drive_fetch_complete", user_id=str(user_id), files=len(drive_evidencias), provider=provider.value
    )
    return {**state, "drive_evidencias": existing + drive_evidencias}
