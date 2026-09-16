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
#
# `-category:updates` fue removido (evidencias/discovery-fix): esa categoría
# también atrapa notificaciones legítimas de la entidad (SECOP, Drive shares,
# invitaciones de Calendar) — descartaba evidencia real junto con el ruido.
GMAIL_NOISE_EXCLUSIONS = "-category:promotions -category:social -category:forums -in:spam -in:trash"


def _safe_entity_phrase(entidad: str, limit: int) -> str:
    """Quote-safe entidad, truncated on a WORD boundary.

    Round-2 fix (confirmed WARNING): a bare `entidad[:limit]` cut long official
    Colombian names mid-token and the fragment was then emitted as a QUOTED
    exact-phrase Gmail query. Gmail tokenizes phrase searches, so
    `"...Domiciliarios de Barrancaberme"` can never match a document while
    still consuming a slot in the query budget. Cutting on whitespace keeps the
    phrase searchable; if the first word alone exceeds the limit we fall back
    to the hard cut rather than emitting nothing.

    Round-3 fix (confirmed WARNING): Colombian official entity names put the
    distinctive acronym LAST ("... del Medio Ambiente DAGMA", "... Social
    ESE", "... Empresas Municipales de Cali EICE ESP - EMCALI") — a plain
    word-boundary cut from the head keeps the generic part and drops exactly
    the token that appears in real correspondence subject lines, and that
    SECOP imports write into `Contrato.entidad` verbatim (only a hard
    `[:255]`, no acronym-aware shortening). When the name ends in a
    2-8-character all-uppercase token, that acronym is preserved and the head
    is trimmed to make room for it instead.
    """
    from app.agent.prompts.contract_terms import entidad_search_term

    # Strip the legal-entity suffix first: "S.A.S"/"E.S.E"/"Ltda" is boilerplate
    # shared by thousands of entities and only dilutes the phrase.
    cleaned = entidad_search_term(entidad).replace('"', "")
    if len(cleaned) <= limit:
        return cleaned

    tokens = cleaned.split()
    last = tokens[-1] if tokens else ""
    if 2 <= len(last) <= 8 and last.isupper() and last.isalpha():
        budget = limit - len(last) - 1  # room for a separating space
        if budget > 0:
            head = cleaned[:budget]
            cut = head.rfind(" ")
            prefix = (head[:cut] if cut > 0 else "").rstrip(" -,.")
            candidate = f"{prefix} {last}".strip() if prefix else last
            if candidate and len(candidate) <= limit:
                return candidate
        return last

    head = cleaned[:limit]
    cut = head.rfind(" ")
    return (head[:cut].rstrip(" -,.") if cut > 0 else head).strip()


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

    # 1. Palabras clave de la obligación en asunto + variante sin scope (cuerpo
    #    del correo) — antes SOLO se buscaba en el asunto, así que una entidad
    #    que responde citando la obligación en el cuerpo (no en el asunto)
    #    nunca aparecía.
    if keywords:
        kw_str = " OR ".join(keywords[:4])
        queries.append(f"subject:({kw_str}) after:{fecha_inicio} before:{fecha_fin} {noise}")
        queries.append(f"({kw_str}) after:{fecha_inicio} before:{fecha_fin} {noise}")

    # 2. Desde el supervisor
    if supervisor_email:
        queries.append(f"from:{supervisor_email} after:{fecha_inicio} before:{fecha_fin} {noise}")

    # 3. Patrones comunes de evidencia en función pública
    queries.append(
        f"subject:(informe OR entrega OR acta OR reporte OR reunión OR meeting) "
        f"after:{fecha_inicio} before:{fecha_fin} {noise}"
    )

    # 4. Aprobaciones y visto bueno
    queries.append(
        f'subject:(aprobado OR aprobación OR "visto bueno" OR recibido OR aprobó) '
        f"after:{fecha_inicio} before:{fecha_fin} {noise}"
    )

    # 5. Entidad en el cuerpo (si disponible)
    if entidad and len(entidad) > 5:
        safe_entity = _safe_entity_phrase(entidad, 40)
        queries.append(f'"{safe_entity}" after:{fecha_inicio} before:{fecha_fin} {noise}')

    return queries


def build_contract_queries(
    numero_variants: list[str],
    fecha_inicio: str,
    fecha_fin: str,
    supervisor_email: str | None = None,
    entidad: str | None = None,
) -> list[str]:
    """Contract-level Gmail queries — NOT scoped to any single obligación.

    Built once per discovery run (not per-obligación) and given priority in
    the query budget: a contract number or entidad mention is a much stronger
    signal than any single obligación's keywords, and the old design never
    searched for either as free text (root cause #2/#3, evidencias/discovery-fix).

    Args:
        numero_variants: contract-number variants (see `contract_terms.
            contract_number_variants`), most specific first.
        fecha_inicio / fecha_fin: YYYY/MM/DD (Gmail query date format).
        supervisor_email: correo del supervisor del contrato.
        entidad: nombre de la entidad contratante.

    Returns:
        Queries ordered with contract-number variants first, then entidad,
        then supervisor — so truncation to a query budget never drops the
        number before the weaker signals.
    """
    noise = GMAIL_NOISE_EXCLUSIONS
    queries: list[str] = []

    for variant in numero_variants:
        safe = variant.replace('"', "")
        queries.append(f'"{safe}" after:{fecha_inicio} before:{fecha_fin} {noise}')

    if numero_variants:
        exact = numero_variants[0].replace('"', "")
        queries.append(f'"{exact}" has:attachment after:{fecha_inicio} before:{fecha_fin} {noise}')

    if entidad and len(entidad.strip()) > 3:
        safe_entity = _safe_entity_phrase(entidad, 60)
        queries.append(f'"{safe_entity}" after:{fecha_inicio} before:{fecha_fin} {noise}')

    if supervisor_email:
        queries.append(f"from:{supervisor_email} after:{fecha_inicio} before:{fecha_fin} {noise}")

    return queries


def build_expanded_phrase_queries(phrases: list[str], fecha_inicio: str, fecha_fin: str) -> list[str]:
    """Unscoped full-text Gmail queries, one per semantic search phrase
    (evidencias/discovery-fix WU7) — e.g. a deliverable name or counterpart
    name the LLM generated (`app.agent.nodes.query_expansion.expand_search_terms`),
    so evidence that never mentions the contract number or the obligación's own
    wording can still be found.

    Round-3 fix (confirmed WARNING, escalated from SUGGESTION): a single-word
    phrase quoted as an exact-phrase query (`"elaborar" after:... before:...`)
    is an unscoped full-text search over the whole widened window — it
    returns essentially the mailbox, then pulls a full page of messages for
    almost no discriminating power. The round-robin floor interleaves the
    deterministic fallback's single keywords as phrase[0], so every
    obligación on that path spent one of its two guaranteed query slots on
    this near-useless query. Only genuinely multi-word phrases are emitted.
    """
    noise = GMAIL_NOISE_EXCLUSIONS
    queries: list[str] = []
    for phrase in phrases:
        safe = (phrase or "").replace('"', "").strip()
        if not safe or " " not in safe:
            continue
        queries.append(f'"{safe}" after:{fecha_inicio} before:{fecha_fin} {noise}')
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
        parts.append(f"- ID: {o.get('id')} | Tipo: {o.get('tipo')} | Descripción: {o.get('descripcion')}")
    return "\n".join(parts)


# ── Internal helpers ──────────────────────────────────────────────────────────

_STOPWORDS = {
    "de",
    "la",
    "el",
    "en",
    "y",
    "a",
    "los",
    "las",
    "con",
    "por",
    "para",
    "del",
    "al",
    "se",
    "que",
    "un",
    "una",
    "su",
    "sus",
    "es",
    "son",
    "este",
    "esta",
    "esto",
    "como",
    "más",
    "si",
    "no",
    "le",
    "lo",
    # Round-3 fix (confirmed SUGGESTION): `evidence_matcher._PROSE_TOKEN_RE`
    # only ever emits tokens of 4+ letters, so 25 of the original 30 entries
    # above (`de`, `la`, `los`, `del`, `por`, `que`, `con`, `un`, `su`, `es`...)
    # can NEVER be produced by that regex — the stopword half of the round-2
    # tokenizer fix was effectively a 5-word no-op. These are the 4+-char
    # Spanish function words the regex actually emits, plus contract
    # boilerplate that appears in essentially every Colombian obligación
    # (weakening cross-obligación discrimination without them).
    "sobre",
    "desde",
    "entre",
    "cuando",
    "donde",
    "todos",
    "todas",
    "cada",
    "debe",
    "según",
    "mismo",
    "misma",
    "dicha",
    "dicho",
    "cual",
    "cuales",
    "sean",
    "demás",
    "acuerdo",
    "contractual",
    "contractuales",
    "cumplimiento",
    "conforme",
    "requeridos",
    "requeridas",
}


def _extract_keywords(text: str) -> list[str]:
    """Extrae 4-5 palabras clave relevantes de la descripción de la obligación.

    Round-3 fix (confirmed WARNING, escalated from SUGGESTION): the strip set
    didn't include quote characters, so an obligación quoting a deliverable
    title ('Elaborar el informe "Estado del arte" ...') produced tokens like
    `'"estado'`/`'arte"'`. Fed into `subject:(a OR b OR "estado OR arte")`,
    Gmail reads the unstripped pair as one literal phrase — silently
    collapsing the OR list into a phrase search that matches almost nothing,
    and when the closing quote is itself truncated off by the `[:4]`/`[:5]`
    slicing downstream, the resulting UNBALANCED quote absorbs `after:`/
    `before:` and the noise exclusions into the phrase text too.
    """
    words = text.replace(",", " ").replace(".", " ").split()
    candidates = [
        w.lower().strip("():;-\"'‘’“”«»")
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
