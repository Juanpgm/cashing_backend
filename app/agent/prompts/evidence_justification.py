"""Prompt para generar el texto de justificación de una obligación a partir de evidencias."""

from __future__ import annotations

EVIDENCE_JUSTIFICATION_SYSTEM_PROMPT = """
Eres un asistente experto en cuentas de cobro de contratos de prestación de servicios
de la función pública colombiana. Redactas la justificación del cumplimiento de una
obligación contractual a partir de las evidencias recolectadas (correos, documentos de
Drive, eventos de calendario).

Reglas:
- Redacta en español formal, en primera persona del contratista ("realicé", "asistí",
  "elaboré"), en 2-4 oraciones.
- Describe QUÉ actividades demuestran el cumplimiento, apoyándote SOLO en las evidencias
  dadas. No inventes hechos ni fechas que no estén en las evidencias.
- Si no hay evidencias suficientes, dilo explícitamente y sugiere qué soporte falta.
- No incluyas los links en el texto (se adjuntan aparte). No uses markdown.
""".strip()


def build_justification_prompt(obligacion: str, evidencias_texto: str) -> str:
    """Construye el prompt de usuario para justificar una obligación."""
    return (
        f"## Obligación contractual\n{obligacion}\n\n"
        f"## Evidencias recolectadas\n{evidencias_texto}\n\n"
        "Redacta la justificación del cumplimiento de esta obligación."
    )


def format_evidencias_for_prompt(evidencias: list[dict]) -> str:
    """Formatea las evidencias matcheadas para el prompt de justificación."""
    if not evidencias:
        return "No se encontraron evidencias para esta obligación."
    parts = []
    for i, ev in enumerate(evidencias, 1):
        source = ev.get("source", "?")
        title = ev.get("title") or ev.get("subject") or ev.get("filename") or "(sin título)"
        date = ev.get("date", "")
        content = (ev.get("content") or "")[:300]
        parts.append(f"{i}. [{source}] {title} ({date})\n   {content}")
    return "\n".join(parts)
