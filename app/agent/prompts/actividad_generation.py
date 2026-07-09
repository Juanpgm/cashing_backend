"""Shared contract for generating DISTINCT actividad + justificación text per obligación.

Single source of truth reused by `evidence_justify_node` (Gmail/Drive/Calendar explorer
agent) and `cruzar_service` (document-crossing). Both flows previously conflated
"actividad realizada" with "justificación" (or with the obligación's own text) — this
module fixes that at the prompt-contract level:

- ACTIVIDAD: qué se hizo concretamente en el período (specific, grounded on evidence).
- JUSTIFICACION: cómo lo realizado da cumplimiento a la obligación (cites evidence).

Both texts must never echo the obligación's literal wording, and must never be the
same text (or one a paraphrase of the other).
"""

from __future__ import annotations

import re
import unicodedata

ACTIVIDAD_JUSTIFICACION_SYSTEM_PROMPT = """
Eres un asistente experto en cuentas de cobro de contratos de prestación de servicios
de la función pública colombiana. A partir de una obligación contractual, las evidencias
recolectadas (correos, documentos de Drive, eventos de calendario) y el contexto del
contrato, redactas DOS textos distintos y complementarios:

1. ACTIVIDAD: qué se hizo concretamente en el período. Primera persona del contratista
   en pasado o forma impersonal, específico, mencionando entregables, reuniones o
   documentos reales que aparecen en las evidencias (con su título y/o fecha).
2. JUSTIFICACION: cómo lo realizado da cumplimiento a la obligación contractual,
   citando las evidencias por título y fecha.

REGLAS OBLIGATORIAS (nunca las rompas):
- NUNCA repitas ni parafrasees el texto literal de la obligación en ninguno de los dos campos.
- ACTIVIDAD y JUSTIFICACION deben ser textos DISTINTOS entre sí — nunca el mismo texto,
  ni uno siendo una copia o paráfrasis del otro.
- Prohibido usar frases genéricas vacías como "se cumplió la obligación" o "se realizó
  la actividad conforme a lo solicitado" sin contenido concreto.
- Basa ambos textos SOLO en las evidencias y el contexto proporcionados. No inventes
  hechos, cifras ni fechas que no estén presentes.
- Si dispones de actividades de meses anteriores del mismo contrato, NO repitas su
  redacción literal; describe el período actual con su propio contenido.
- Español formal, sin markdown, sin viñetas, sin listas.

FORMATO DE RESPUESTA (exactamente estas dos líneas, sin texto adicional antes o después):
ACTIVIDAD: <texto de la actividad realizada>
JUSTIFICACION: <texto de la justificación de cumplimiento>
""".strip()


def build_actividad_justificacion_prompt(
    obligacion: str,
    evidencias_texto: str,
    contrato_contexto: str = "",
    actividades_previas_texto: str = "",
) -> str:
    """Construye el prompt de usuario para generar actividad + justificación."""
    parts = [f"## Obligación contractual\n{obligacion}"]
    if contrato_contexto:
        parts.append(f"## Contexto del contrato\n{contrato_contexto}")
    parts.append(f"## Evidencias recolectadas\n{evidencias_texto}")
    if actividades_previas_texto:
        parts.append(f"## Actividades de meses anteriores (NO las repitas literalmente)\n{actividades_previas_texto}")
    parts.append(
        "Redacta la ACTIVIDAD realizada y la JUSTIFICACION del cumplimiento de esta "
        "obligación, siguiendo exactamente el formato de dos líneas indicado."
    )
    return "\n\n".join(parts)


def format_actividades_previas(actividades_previas: list[str], max_items: int = 20) -> str:
    """Formatea actividades previas (strings 'MM/YYYY: descripcion') para el prompt."""
    if not actividades_previas:
        return ""
    return "\n".join(f"- {a}" for a in actividades_previas[:max_items])


_ACTIVIDAD_RE = re.compile(r"ACTIVIDAD\s*:\s*(.+?)(?:\n\s*JUSTIFICACION\s*:|\Z)", re.IGNORECASE | re.DOTALL)
_JUSTIFICACION_RE = re.compile(r"JUSTIFICACION\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)


def parse_actividad_justificacion(content: str) -> tuple[str, str] | None:
    """Parse the strict `ACTIVIDAD: ...` / `JUSTIFICACION: ...` format.

    Returns (actividad, justificacion) or None if the response doesn't follow the
    contract (e.g. a small/local model ignored the format instruction) — callers
    should fall back to a deterministic, non-echoing text in that case.
    """
    m_act = _ACTIVIDAD_RE.search(content)
    m_just = _JUSTIFICACION_RE.search(content)
    if not m_act or not m_just:
        return None
    actividad = m_act.group(1).strip()
    justificacion = m_just.group(1).strip()
    if not actividad or not justificacion:
        return None
    return actividad, justificacion


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, and fold accents (NFD + strip combining marks)
    so accent-only differences (e.g. "REALIZACIÓN" vs "realizacion") don't defeat the
    near-identical comparison."""
    folded = unicodedata.normalize("NFD", text.strip().lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", folded)


def is_near_identical(a: str, b: str) -> bool:
    """True if two texts are near-identical: equal once normalized, or one contains
    the other and the shorter is more than 80% of the longer's length.

    Used to guard against a model producing `descripcion == justificacion` (or a
    trivial paraphrase of it) despite the prompt's explicit instruction not to.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return bool(shorter) and shorter in longer and len(shorter) / len(longer) > 0.8
