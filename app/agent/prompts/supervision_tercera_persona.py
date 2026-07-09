"""Prompt contract for rewriting activity texts from first person to third person.

Used by `informe_service.generar_informe_supervision_docx` to convert the contractor's
first-person "Actividad realizada" / "Justificación" texts (shared with the actividades
report) into third-person text suitable for a supervisor's report, referring to the
contractor as "el contratista". The actividades report itself is never touched — it
keeps rendering the original first-person text.
"""

from __future__ import annotations

import re

TERCERA_PERSONA_SYSTEM_PROMPT = """
Eres un asistente experto en redacción de informes de supervisión de contratos de
prestación de servicios de la función pública colombiana.

Se te entrega una lista numerada de textos redactados en primera persona por un
contratista (describiendo actividades realizadas o justificaciones de cumplimiento).
Tu tarea es reescribir CADA texto en tercera persona, refiriéndote al autor original
como "el contratista", conservando exactamente el mismo significado, tiempo verbal,
fechas, cifras y detalle técnico. No agregues ni quites información. No resumas ni
amplíes el contenido. Cada texto reescrito debe caber en una sola línea.

FORMATO DE RESPUESTA (obligatorio, sin texto adicional antes o después del bloque):
Por cada texto de entrada, produce exactamente una línea con el formato:
N| <texto reescrito en tercera persona>

Donde N es el número (1-based) del texto de entrada correspondiente. No omitas
ningún número, no agregues numeración adicional, no uses viñetas ni markdown.
""".strip()


def build_tercera_persona_prompt(textos: list[str]) -> str:
    """Build the user prompt: a 1-based numbered list of texts to rewrite."""
    listado = "\n".join(f"{i + 1}) {t}" for i, t in enumerate(textos))
    return (
        "Reescribe en tercera persona (refiriéndote al contratista) cada uno de los "
        "siguientes textos, siguiendo exactamente el formato indicado:\n\n" + listado
    )


_LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.+?)\s*$")


def parse_tercera_persona(content: str, expected: int) -> list[str] | None:
    """Parse the `N| texto` numbered-line format into an index-aligned list.

    Tolerant of extra prose/blank lines surrounding the numbered block (a small local
    model may add a preamble or trailing remark despite instructions). Returns None
    if the resulting count doesn't match ``expected`` or parsing clearly failed, so
    the caller can fail open to the original first-person texts.
    """
    if expected <= 0:
        return None

    by_index: dict[int, str] = {}
    for line in content.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        text = m.group(2).strip()
        if text:
            by_index[idx] = text

    if len(by_index) != expected:
        return None

    result: list[str] = []
    for i in range(1, expected + 1):
        if i not in by_index:
            return None
        result.append(by_index[i])
    return result
