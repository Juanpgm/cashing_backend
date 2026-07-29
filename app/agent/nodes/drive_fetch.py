"""Drive fetch node — explora Google Drive buscando documentos que sirvan de evidencia.

Análogo a email_fetch pero sobre el Drive del usuario (requiere scope drive.readonly).
Construye queries de Drive desde las obligaciones del contrato y devuelve los archivos
encontrados con su link (webViewLink) para soportar la cuenta de cobro.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from app.adapters.drive.drive_adapter import DriveAdapter
from app.adapters.drive.port import DriveQuery
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.state import AgentState
from app.core.config import settings

logger = structlog.get_logger("agent.nodes.drive_fetch")

# Términos genéricos de evidencia documental en la función pública.
_GENERIC_TERMS = ("informe", "acta", "entrega", "soporte", "reporte")
MAX_FILES_PER_QUERY = 10


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
) -> list[DriveQuery]:
    """Construye `DriveQuery` para buscar evidencia de una obligación en Drive.

    Args:
        descripcion: Texto de la obligación.
        fecha_inicio / fecha_fin: YYYY-MM-DD del período a cubrir.

    Returns:
        Una `DriveQuery` por keyword extraída (hasta 3) más una por término
        genérico — misma granularidad que las queries crudas pre-refactor, para
        preservar el truncado `EVIDENCE_QUERIES_PER_OBLIGACION` sin cambios.
    """
    date_from = _to_drive_datetime(fecha_inicio)
    date_to = _to_drive_datetime(fecha_fin, end_of_day=True)

    def _query(term: str) -> DriveQuery:
        return DriveQuery(
            keywords=[term],
            date_from=date_from,
            date_to=date_to,
            exclude_folders=True,
            max_results=MAX_FILES_PER_QUERY,
        )

    queries = [_query(kw.replace("'", "")) for kw in _extract_keywords(descripcion)[:3]]
    queries.extend(_query(term) for term in _GENERIC_TERMS)
    return queries


async def drive_fetch_node(state: AgentState) -> AgentState:
    """Busca documentos de evidencia en el Drive del usuario.

    Requiere en state: user_id, _db, contrato_contexto (fecha_inicio/fecha_fin),
    obligaciones_contexto (opcional).

    Produce en state: drive_evidencias (lista de dicts con title/link/date/file_id).
    """
    user_id = state.get("user_id")
    db = state.get("_db")
    if not user_id or not db:
        return {**state, "drive_evidencias": []}

    contrato = state.get("contrato_contexto") or {}
    obligaciones = state.get("obligaciones_contexto") or []
    fecha_inicio = str(contrato.get("fecha_inicio", ""))
    fecha_fin = str(contrato.get("fecha_fin", ""))

    # Construir queries: por obligación si existen, si no genéricas.
    max_obligaciones = settings.EVIDENCE_MAX_OBLIGACIONES_QUERIES
    obligaciones_para_query = obligaciones if max_obligaciones <= 0 else obligaciones[:max_obligaciones]

    queries: list[DriveQuery] = []
    if obligaciones:
        for oblig in obligaciones_para_query:
            queries.extend(
                build_drive_queries(str(oblig.get("descripcion", "")), fecha_inicio, fecha_fin)[
                    : settings.EVIDENCE_QUERIES_PER_OBLIGACION
                ]
            )
    else:
        queries = build_drive_queries(state.get("user_input", ""), fecha_inicio, fecha_fin)

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

    adapter = DriveAdapter(db)
    files_by_id: dict[str, dict] = {}
    try:
        for query in unique_queries[: settings.EVIDENCE_MAX_QUERIES_TOTAL]:
            try:
                files = await adapter.search_files(user_id, query)
            except Exception as exc:
                await logger.awarning("drive_query_failed", query=query, error=str(exc))
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
                    }
    except Exception as exc:
        await logger.aerror("drive_fetch_error", error=str(exc), user_id=str(user_id))
        return {
            **state,
            "drive_evidencias": [],
            "error": f"Error explorando Drive: {exc}. Verifica que tu cuenta de Google esté conectada.",
        }

    drive_evidencias = list(files_by_id.values())[: settings.EVIDENCE_MAX_FILES_TOTAL]
    await logger.ainfo("drive_fetch_complete", user_id=str(user_id), files=len(drive_evidencias))
    return {**state, "drive_evidencias": drive_evidencias}
