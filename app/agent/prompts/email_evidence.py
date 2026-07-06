"""Prompts para búsqueda y análisis de evidencias en correo electrónico."""

from __future__ import annotations

EMAIL_OBLIGATION_MATCHING_PROMPT = """
Eres un asistente experto en contratos de la función pública colombiana.
Tu tarea es determinar si los siguientes correos electrónicos son evidencia
relevante para las obligaciones contractuales del contratista.

## Obligaciones contractuales
{obligaciones}

## Correos encontrados
{emails_context}

Para cada correo, determina:
1. ¿Es evidencia de alguna obligación? (sí/no)
2. ¿Para cuál obligación específica (usa el id)?
3. ¿Qué actividad demuestra? (1-2 oraciones concretas)
4. Nivel de relevancia: alta (prueba directa) / media (mención indirecta) / baja (contexto general)

Responde ÚNICAMENTE con un array JSON válido con este formato exacto:
[
  {{
    "email_id": "id_del_correo",
    "es_evidencia": true,
    "obligacion_id": "uuid-o-null",
    "actividad_sugerida": "Participación en reunión de seguimiento del contrato del día 15 de enero",
    "relevancia": "alta",
    "justificacion": "El correo contiene el acta firmada de la reunión de seguimiento"
  }}
]
""".strip()


EMAIL_SUMMARY_SYSTEM_PROMPT = """
Eres un asistente especializado en contratos de prestación de servicios de la
función pública colombiana. Ayudas a contratistas a recopilar evidencias de sus
actividades para elaborar cuentas de cobro.

Cuando el usuario pide buscar correos o evidencias, explica qué encontraste de forma
clara y concisa: cuántos correos revisaste, cuáles son relevantes para cada
obligación, y qué actividades demuestran.
""".strip()


# Exclusiones de ruido aplicadas a todas las queries de Gmail (gratis, servidor-side).
GMAIL_NOISE_EXCLUSIONS = (
    "-category:promotions -category:social -category:forums "
    "-category:updates -in:spam -in:trash"
)


def build_obligation_queries(
    descripcion: str,
    fecha_inicio: str,
    fecha_fin: str,
    supervisor_email: str | None = None,
    entidad: str | None = None,
) -> list[str]:
    """Construye queries de Gmail para buscar evidencia de una obligación contractual.

    Args:
        descripcion: Texto de la obligación (ej. "Elaborar informes de gestión mensual")
        fecha_inicio: Formato YYYY/MM/DD
        fecha_fin: Formato YYYY/MM/DD
        supervisor_email: Correo del supervisor del contrato
        entidad: Nombre de la entidad contratante

    Returns:
        Lista de queries ordenadas de más a menos específica.
    """
    keywords = _extract_keywords(descripcion)
    noise = GMAIL_NOISE_EXCLUSIONS
    queries: list[str] = []

    # 1. Palabras clave de la obligación en asunto
    if keywords:
        kw_str = " OR ".join(keywords[:4])
        queries.append(
            f"subject:({kw_str}) after:{fecha_inicio} before:{fecha_fin} {noise}"
        )

    # 2. Desde el supervisor
    if supervisor_email:
        queries.append(
            f"from:{supervisor_email} after:{fecha_inicio} before:{fecha_fin} {noise}"
        )

    # 3. Patrones comunes de evidencia en función pública
    queries.append(
        f"subject:(informe OR entrega OR acta OR reporte OR reunión OR meeting) "
        f"after:{fecha_inicio} before:{fecha_fin} {noise}"
    )

    # 4. Aprobaciones y visto bueno
    queries.append(
        f"subject:(aprobado OR aprobación OR \"visto bueno\" OR recibido OR aprobó) "
        f"after:{fecha_inicio} before:{fecha_fin} {noise}"
    )

    # 5. Entidad en el cuerpo (si disponible)
    if entidad and len(entidad) > 5:
        safe_entity = entidad[:40].replace('"', "")
        queries.append(
            f'"{safe_entity}" after:{fecha_inicio} before:{fecha_fin} {noise}'
        )

    return queries


def format_emails_for_llm(emails: list[dict[str, str]]) -> str:
    """Formatea correos para el prompt de análisis del LLM."""
    if not emails:
        return "No se encontraron correos en el período indicado."

    parts = []
    for i, email in enumerate(emails, 1):
        parts.append(
            f"--- Correo {i} ---\n"
            f"ID: {email.get('id', 'N/A')}\n"
            f"De: {email.get('sender', 'N/A')}\n"
            f"Asunto: {email.get('subject', 'N/A')}\n"
            f"Fecha: {email.get('date', 'N/A')}\n"
            f"Resumen: {email.get('snippet', '')}\n"
            f"Cuerpo: {email.get('body_plain', '')[:500]}"
        )
    return "\n\n".join(parts)


def format_obligaciones_for_llm(obligaciones: list[dict[str, str | int | None]]) -> str:
    """Formatea obligaciones para el prompt de análisis."""
    if not obligaciones:
        return "Sin obligaciones definidas."
    parts = []
    for o in obligaciones:
        parts.append(
            f"- ID: {o.get('id')} | Tipo: {o.get('tipo')} | "
            f"Descripción: {o.get('descripcion')}"
        )
    return "\n".join(parts)


# ── Internal helpers ──────────────────────────────────────────────────────────

_STOPWORDS = {
    "de", "la", "el", "en", "y", "a", "los", "las", "con", "por", "para",
    "del", "al", "se", "que", "un", "una", "su", "sus", "es", "son",
    "este", "esta", "esto", "como", "más", "si", "no", "le", "lo",
}


def _extract_keywords(text: str) -> list[str]:
    """Extrae 4-5 palabras clave relevantes de la descripción de la obligación."""
    words = text.replace(",", " ").replace(".", " ").split()
    candidates = [
        w.lower().strip("():;-")
        for w in words
        if len(w) > 4 and w.lower() not in _STOPWORDS
    ]
    # Deduplicate preserving order
    seen: set[str] = set()
    unique = []
    for w in candidates:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:5]
