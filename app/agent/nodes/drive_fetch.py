"""Drive fetch node — explora Google Drive buscando documentos que sirvan de evidencia.

Análogo a email_fetch pero sobre el Drive del usuario (requiere scope drive.readonly).
Construye queries de Drive desde las obligaciones del contrato y devuelve los archivos
encontrados con su link (webViewLink) para soportar la cuenta de cobro.
"""

from __future__ import annotations

import structlog

from app.adapters.drive.drive_adapter import DriveAdapter
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.drive_fetch")

# Términos genéricos de evidencia documental en la función pública.
_GENERIC_TERMS = ("informe", "acta", "entrega", "soporte", "reporte")
MAX_FILES_PER_QUERY = 10
MAX_FILES_TOTAL = 25


def _to_drive_datetime(date_str: str, end_of_day: bool = False) -> str:
    """Convierte YYYY-MM-DD a RFC3339 que entiende la query de Drive (modifiedTime)."""
    date_str = (date_str or "").strip().replace("/", "-")
    if not date_str:
        return ""
    suffix = "T23:59:59" if end_of_day else "T00:00:00"
    return f"{date_str}{suffix}"


def build_drive_queries(
    descripcion: str,
    fecha_inicio: str,
    fecha_fin: str,
) -> list[str]:
    """Construye fragmentos de query de Drive para buscar evidencia de una obligación.

    Args:
        descripcion: Texto de la obligación.
        fecha_inicio / fecha_fin: YYYY-MM-DD del período a cubrir.

    Returns:
        Lista de fragmentos de query Drive (sin el ``trashed=false``, que agrega el adapter).
    """
    inicio = _to_drive_datetime(fecha_inicio)
    fin = _to_drive_datetime(fecha_fin, end_of_day=True)
    date_clause = ""
    if inicio and fin:
        date_clause = f" and modifiedTime >= '{inicio}' and modifiedTime <= '{fin}'"

    no_folders = " and mimeType != 'application/vnd.google-apps.folder'"
    queries: list[str] = []
    for kw in _extract_keywords(descripcion)[:3]:
        safe = kw.replace("'", "")
        queries.append(f"(name contains '{safe}' or fullText contains '{safe}'){date_clause}{no_folders}")

    for term in _GENERIC_TERMS:
        queries.append(f"name contains '{term}'{date_clause}{no_folders}")

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
    queries: list[str] = []
    if obligaciones:
        for oblig in obligaciones[:3]:
            queries.extend(build_drive_queries(str(oblig.get("descripcion", "")), fecha_inicio, fecha_fin)[:2])
    else:
        queries = build_drive_queries(state.get("user_input", ""), fecha_inicio, fecha_fin)

    # Deduplicar queries preservando orden.
    seen_q: set[str] = set()
    unique_queries = [q for q in queries if not (q in seen_q or seen_q.add(q))]

    adapter = DriveAdapter(db)
    files_by_id: dict[str, dict] = {}
    try:
        for query in unique_queries[:5]:
            try:
                files = await adapter.search_files(user_id, query, MAX_FILES_PER_QUERY)
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

    drive_evidencias = list(files_by_id.values())[:MAX_FILES_TOTAL]
    await logger.ainfo("drive_fetch_complete", user_id=str(user_id), files=len(drive_evidencias))
    return {**state, "drive_evidencias": drive_evidencias}
