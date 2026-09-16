"""Prompt for semantic query-term expansion (evidencias/discovery-fix WU7).

The Gmail/Drive/Calendar search must stay SEMANTIC: evidence that satisfies an
obligación without ever mentioning the contract number (a supervisor's reply
titled "Re: avance de actividades", a Drive file named "Informe abril.docx")
must still be found. This asks one LLM call for a handful of search phrases
per obligación instead of relying solely on the obligación's own wording.
"""

from __future__ import annotations

QUERY_EXPANSION_SYSTEM_PROMPT = """\
Eres un asistente experto en contratos de prestación de servicios de la función \
pública colombiana. Para cada obligación contractual, genera entre 4 y 8 frases \
de búsqueda cortas que ayuden a encontrar evidencia real de su cumplimiento en \
Gmail, Google Drive y Calendar:

- Sinónimos y formas alternativas de describir la obligación.
- Nombres de entregables esperados (ej. "informe de avance", "acta de reunión", \
"listado de asistencia", "planilla seguridad social", "acta de entrega").
- Posibles nombres de la contraparte (supervisor, área o dependencia de la entidad).
- 2-3 variantes en español/inglés útiles como nombre de archivo (ej. "monthly \
report", "meeting minutes").

Responde ÚNICAMENTE con un objeto JSON donde cada clave es el id de la \
obligación y el valor es un array de frases:
{"<id_obligacion>": ["frase 1", "frase 2", "frase 3", "frase 4"], ...}
"""


def build_query_expansion_prompt(header: str, obligaciones: list[dict]) -> str:
    """Build the user prompt: contract header (if any) + one line per obligación."""
    parts: list[str] = []
    if header:
        parts.append(header)
    parts.append("Obligaciones (usa el id EXACTO como clave en tu respuesta):")
    for i, ob in enumerate(obligaciones):
        ob_id = str(ob.get("id") or i)
        descripcion = str(ob.get("descripcion") or "")
        parts.append(f'- id "{ob_id}": {descripcion}')
    return "\n".join(parts)
