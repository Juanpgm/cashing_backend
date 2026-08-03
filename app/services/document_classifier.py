"""Document classifier — infers CategoriaDocumento from filename and description.

Uses keyword scoring (accent-insensitive, case-insensitive) via app.core.text_match.
Respects categoria_override: re-classification never overwrites a manual override.
"""

from __future__ import annotations

from decimal import Decimal

from app.agent.tools.document_parser import parse_document
from app.core.text_match import keyword_hits, keyword_score, normalize
from app.models.categoria_documento import CategoriaDocumento

# Minimum score to assign a non-OTROS category.
CATEGORIA_MIN_THRESHOLD = Decimal("0.001")

# Content-sniff scores (clasificacion-documentos-secop, design D2).
# Unambiguous keyword hit → auto-linkable (below manual 1.000); multi-category
# ambiguous hit → candidate only (below AUTO_LINK_THRESHOLD 0.700).
CONTENIDO_SCORE_UNICO = Decimal("0.850")
CONTENIDO_SCORE_AMBIGUO = Decimal("0.600")
# Real SECOP documents legitimately mention sibling categories (an RPC always
# cites its CDP, a contrato cites its condiciones) — demanding exclusivity made
# CONTENIDO_SCORE_UNICO unreachable in practice. A category dominates instead
# of needing to be the only one hit: top keyword_score must be at least this
# many times the runner-up's (runner-up score 0 is automatically dominant).
CONTENIDO_DOMINANCIA_FACTOR = 2
# Only the head of the document decides — titles/headers carry the signal.
_CONTENIDO_MAX_CHARS = 1500
# Persisted extraction cap for memoization on secop_documentos.texto_extraido.
TEXTO_EXTRAIDO_MAX_CHARS = 20_000

# clasificacion-documentos-refuerzo (R2): PRIMARY/SECONDARY keyword scoring.
# A PRIMARY hit clears AUTO_LINK_THRESHOLD (0.700) by itself; SECONDARY hits
# only add small increments and never reach it alone — a secondary-only match
# stays a manual-Vincular candidate. p,s = PRIMARY/SECONDARY hit counts:
#   p == 0 and s == 0 -> 0.000
#   p == 0             -> min(0.400 + STEP*(s-1), SECONDARY_ONLY_CAP)
#   p >= 1             -> min(PRIMARY_BASE + STEP*(p-1) + STEP*s, CAP)
PRIMARY_BASE = Decimal("0.750")
STEP = Decimal("0.050")
CAP = Decimal("0.950")  # < 1.000: manual categoria_override reserves the ceiling.
SECONDARY_ONLY_CAP = Decimal("0.600")
_SECONDARY_ONLY_BASE = Decimal("0.400")

# Correction (RELIABILITY-001): CONTRATO's bare "contrato" PRIMARY keyword is
# generic — it legitimately co-occurs in most real SECOP filenames regardless
# of the document's true category (an ACTA_INICIO, RPC or CDP filename
# routinely also reads "... DEL CONTRATO ..."). A runner-up category whose
# ONLY matched PRIMARY keyword is this generic term is not a genuine
# ambiguity threat: it must neither win a ranking tie against a category that
# matched a specific keyword, nor trigger the R3 dominance-guard demotion
# below. That guard exists for genuinely-confusable same-tier pairs (e.g.
# REGISTRO_PRESUPUESTAL vs CDP, both matching their own specific phrases) —
# not to punish another category for co-occurring with the word "contrato".
_GENERIC_PRIMARY_KEYWORDS = frozenset({"contrato"})

# R4: bounded, clamped adjustments from SECOP metadata (never an override).
ADJ_MODIFICACION_CONTRATO = Decimal("-0.150")  # a modificatorio is not the base contrato
ADJ_TIPO_HINT = Decimal("0.100")  # dataset-declared type is a strong prior
ADJ_EXT_IMG_EVIDENCIA = Decimal("0.050")  # jpg/png/gif nudges EVIDENCIAS
_EXT_IMAGENES = frozenset({"jpg", "jpeg", "png", "gif"})

# Keywords per category, partitioned PRIMARY (clears auto-link alone) /
# SECONDARY (small increment only). Grounded in real SECOP filenames
# (clasificacion-documentos-refuerzo design-technical.md R2 table).
#
# Deviation from the design table, found via the T2.5 realistic-filename
# sanity check: "inicio del contrato" was originally proposed as ACTA_INICIO
# SECONDARY, but real SECOP filenames routinely read "ACTA DE INICIO DEL
# CONTRATO No. XXX" — CONTRATO's bare "contrato" PRIMARY hit then outscored
# ACTA_INICIO's SECONDARY-only score outright (0.750 vs 0.400), misclassifying
# the doc as CONTRATO. Promoted to ACTA_INICIO PRIMARY (still ties CONTRATO at
# 0.750) and paired with a generic-vs-specific tie-break in `_score_categoria`/
# `clasificar` (a match on any keyword other than CONTRATO's bare "contrato"
# wins ties, see `_GENERIC_PRIMARY_KEYWORDS`) so ACTA_INICIO's specific phrase
# beats CONTRATO's generic single word — and the same signal stops CONTRATO's
# generic co-occurrence from demoting a legitimately-won winner via R3.
CATEGORIA_KEYWORDS: dict[CategoriaDocumento, dict[str, list[str]]] = {
    CategoriaDocumento.CONTRATO: {
        "primary": ["contrato", "cto", "minuta", "clausulado"],
        "secondary": [
            "contract",
            "condiciones generales",
            "condiciones especiales",
            "acuerdo de prestacion",
            "prestacion de servicios",
        ],
    },
    CategoriaDocumento.REGISTRO_PRESUPUESTAL: {
        "primary": ["rpc", "rp", "registro presupuestal", "registro de compromiso"],
        "secondary": ["compromiso presupuestal"],
    },
    CategoriaDocumento.CDP: {
        # "cdp-" dropped as a secondary keyword (present in the design table):
        # R1's word-boundary matching already lets "cdp" match "cdp-045" (a
        # digit/hyphen neighbor isn't a letter, so it doesn't block the
        # keyword), making it a pure no-op that only diluted clasificar_
        # contenido's fractional denominator. T2.5 outcome, see apply-progress.
        "primary": ["cdp", "certificado de disponibilidad", "disponibilidad presupuestal"],
        "secondary": [],
    },
    CategoriaDocumento.ACTA_INICIO: {
        "primary": ["acta de inicio", "acta inicio", "acta de arranque", "inicio del contrato"],
        "secondary": [],
    },
    CategoriaDocumento.RUT: {
        "primary": ["rut", "registro unico tributario", "registro único tributario"],
        "secondary": ["registro tributario"],
    },
    CategoriaDocumento.CEDULA: {
        "primary": ["cedula", "cédula"],
        "secondary": ["cc", "cedula ciudadania", "documento de identidad", "tarjeta de identidad"],
    },
    CategoriaDocumento.SEGURIDAD_SOCIAL: {
        "primary": ["seguridad social", "planilla", "pila"],
        "secondary": ["aportes seguridad", "aportes parafiscales"],
    },
    CategoriaDocumento.EVIDENCIAS: {
        "primary": [
            "evidencia",
            "soporte",
            "registro fotografico",
            "registro fotográfico",
            "entregable",
            "acta de entrega",
            "acta entrega",
        ],
        "secondary": ["producto"],
    },
    # OTROS has no keywords — it is the fallback
}

# Maps a category to the requisito_codigo it pre-fills in the checklist.
# OTROS → None means no pre-assignment.
CATEGORIA_A_REQUISITO: dict[CategoriaDocumento, str | None] = {
    CategoriaDocumento.CONTRATO: "CONTRATO",
    CategoriaDocumento.REGISTRO_PRESUPUESTAL: "RPC",
    CategoriaDocumento.CDP: "CDP",
    CategoriaDocumento.ACTA_INICIO: "ACTA_INICIO",
    CategoriaDocumento.RUT: "RUT",
    CategoriaDocumento.CEDULA: "CEDULA",
    CategoriaDocumento.SEGURIDAD_SOCIAL: "SEGURIDAD_SOCIAL",
    CategoriaDocumento.EVIDENCIAS: "EVIDENCIAS",
    CategoriaDocumento.OTROS: None,
}

# Maps the user-declared TipoDocumentoFuente value (string) to the checklist
# requisito_codigo. Secondary matching signal: covers requisitos that have no
# CategoriaDocumento equivalent (INFORME_*, COMPROBANTE_PAGO_SS, DS_CONSECUTIVO,
# FICHA_TECNICA, DEPENDIENTES) and acts as a reliable fallback for the rest.
# "instrucciones" and "plantilla" intentionally excluded — utility docs, not checklist items.
TIPO_A_REQUISITO: dict[str, str] = {
    "contrato": "CONTRATO",
    "rpc": "RPC",
    "cdp": "CDP",
    "seguridad_social": "SEGURIDAD_SOCIAL",
    "comprobante_pago_ss": "COMPROBANTE_PAGO_SS",
    "informe_actividades": "INFORME_ACTIVIDADES",
    "informe_supervision": "INFORME_SUPERVISION",
    "ds_consecutivo": "DS_CONSECUTIVO",
    "cedula": "CEDULA",
    "rut": "RUT",
    "ficha_tecnica": "FICHA_TECNICA",
    "acta_inicio": "ACTA_INICIO",
    "dependientes": "DEPENDIENTES",
}

# Reverse of CATEGORIA_A_REQUISITO — resolves an R4 datos_raw tipo_hint (via
# TIPO_A_REQUISITO) back to the CategoriaDocumento it should nudge.
_REQUISITO_A_CATEGORIA: dict[str, CategoriaDocumento] = {
    v: k for k, v in CATEGORIA_A_REQUISITO.items() if v is not None
}


def _tipo_hint_categoria(tipo_hint: str | None) -> CategoriaDocumento | None:
    """Resolve a SECOP datos_raw tipo_documento/tipo_de_documento hint to a categoria.

    Best-effort: normalizes the hint (accents/case/whitespace) and looks it up
    in the existing TIPO_A_REQUISITO map. Unresolvable hints (unknown value,
    None) are a no-op — never raises.
    """
    if not tipo_hint:
        return None
    req_codigo = TIPO_A_REQUISITO.get(normalize(tipo_hint).replace(" ", "_"))
    if req_codigo is None:
        return None
    return _REQUISITO_A_CATEGORIA.get(req_codigo)


def _score_categoria(
    haystacks: list[str | None], primary: list[str], secondary: list[str]
) -> tuple[Decimal, int, bool]:
    """Absolute PRIMARY/SECONDARY score for one category (R2).

    Returns ``(score, primary_hit_count, has_specific_primary)``:
    - ``primary_hit_count`` feeds the R3 dominance guard (only a runner-up
      with >=1 PRIMARY hit is a candidate ambiguity threat).
    - ``has_specific_primary`` is True when at least one matched PRIMARY
      keyword is NOT in ``_GENERIC_PRIMARY_KEYWORDS`` — used both to break
      EXACT score ties (a specific match outranks a generic-only one) and, in
      `clasificar`, to decide whether a runner-up is a genuine ambiguity
      threat. See ``_GENERIC_PRIMARY_KEYWORDS`` for why.
    """
    p_hits = keyword_hits(haystacks, primary)
    s_hits = keyword_hits(haystacks, secondary)
    p, s = len(p_hits), len(s_hits)
    if p == 0 and s == 0:
        return Decimal("0.000"), 0, False
    if p == 0:
        return min(_SECONDARY_ONLY_BASE + STEP * (s - 1), SECONDARY_ONLY_CAP), 0, False
    has_specific = any(k not in _GENERIC_PRIMARY_KEYWORDS for k in p_hits)
    return min(PRIMARY_BASE + STEP * (p - 1) + STEP * s, CAP), p, has_specific


def _es_dominante(best: Decimal, runner_up: Decimal) -> bool:
    """True if `best` dominates `runner_up` by CONTENIDO_DOMINANCIA_FACTOR.

    A runner-up of 0 is automatically dominant. Shared by `clasificar` (R3)
    and `clasificar_contenido` (content-sniff, unchanged) — one dominance
    rule, two call sites.
    """
    return runner_up == 0 or best >= CONTENIDO_DOMINANCIA_FACTOR * runner_up


def _aplicar_priors(
    scored: list[tuple[CategoriaDocumento, Decimal, int, bool]],
    extension: str | None,
    tipo_origen: str | None,
    tipo_hint: str | None,
) -> list[tuple[CategoriaDocumento, Decimal, int, bool]]:
    """SECOP metadata as bounded, clamped bias — never an override (R4).

    Each adjustment is clamped to [0.000, CAP] per category: it can never
    reach the manual-override-reserved 1.000, and it can never flip a primary
    match to OTROS (the single largest penalty, ADJ_MODIFICACION_CONTRATO,
    still leaves a PRIMARY_BASE hit well above zero). Absent metadata (a
    non-SECOP DocumentoFuente upload has no datos_raw) means every input here
    is None, so this function is effectively an identity no-op.
    """
    ext_norm = (extension or "").strip().lstrip(".").lower()
    hint_cat = _tipo_hint_categoria(tipo_hint)
    out: list[tuple[CategoriaDocumento, Decimal, int, bool]] = []
    for cat, score, p, has_specific in scored:
        adj = Decimal("0.000")
        if cat is CategoriaDocumento.CONTRATO and tipo_origen == "modificacion":
            adj += ADJ_MODIFICACION_CONTRATO
        if hint_cat is not None and cat is hint_cat:
            adj += ADJ_TIPO_HINT
        if cat is CategoriaDocumento.EVIDENCIAS and ext_norm in _EXT_IMAGENES:
            adj += ADJ_EXT_IMG_EVIDENCIA
        nuevo = max(Decimal("0.000"), min(score + adj, CAP))
        out.append((cat, nuevo, p, has_specific))
    return out


def clasificar(
    nombre: str | None,
    descripcion: str | None,
    extension: str | None = None,
    tipo_origen: str | None = None,
    tipo_hint: str | None = None,
) -> tuple[CategoriaDocumento, Decimal]:
    """Return the best-matching category and its confidence score.

    R2: PRIMARY hits clear AUTO_LINK_THRESHOLD (0.700) by themselves; SECONDARY
    hits only nudge. R3: if a runner-up category also has a PRIMARY hit from a
    keyword that isn't CONTRATO's generic bare "contrato" (see
    `_GENERIC_PRIMARY_KEYWORDS`) and the winner doesn't dominate it by
    CONTENIDO_DOMINANCIA_FACTOR, confidence is demoted to
    CONTENIDO_SCORE_AMBIGUO (0.600) — a manual-Vincular candidate, never
    silently auto-linked to the wrong requisito. A runner-up whose only
    PRIMARY signal is that generic keyword is not a genuine ambiguity threat
    and never triggers this demotion. R4: `extension`/`tipo_origen`/
    `tipo_hint` (SECOP metadata, all optional) bias — never override — the
    per-category score before ranking.

    If no category scores above CATEGORIA_MIN_THRESHOLD, returns (OTROS, 0.000).
    """
    haystacks = [nombre, descripcion]

    scored: list[tuple[CategoriaDocumento, Decimal, int, bool]] = []
    for cat, kws in CATEGORIA_KEYWORDS.items():
        score, p, has_specific = _score_categoria(haystacks, kws["primary"], kws["secondary"])
        if score > Decimal("0.000"):
            scored.append((cat, score, p, has_specific))

    if not scored:
        return CategoriaDocumento.OTROS, Decimal("0.000")

    scored = _aplicar_priors(scored, extension, tipo_origen, tipo_hint)

    # Score first; a specific (non-generic) PRIMARY match only breaks EXACT
    # ties over a generic-only one — see _GENERIC_PRIMARY_KEYWORDS.
    ranked = sorted(scored, key=lambda kv: (kv[1], kv[3]), reverse=True)
    best_cat, best_score, _, _ = ranked[0]

    if len(ranked) > 1:
        _, runner_up_score, runner_up_p, runner_up_specific = ranked[1]
        if runner_up_p >= 1 and runner_up_specific and not _es_dominante(best_score, runner_up_score):
            best_score = CONTENIDO_SCORE_AMBIGUO

    if best_score < CATEGORIA_MIN_THRESHOLD:
        return CategoriaDocumento.OTROS, Decimal("0.000")

    return best_cat, best_score


def clasificar_contenido(texto: str | None) -> tuple[CategoriaDocumento | None, Decimal]:
    """Content-based re-score for the borderline sniff (no LLM, no embeddings).

    Scores the first ~1500 chars of extracted text against CATEGORIA_KEYWORDS
    (PRIMARY + SECONDARY combined into one flat list) using the same
    fractional/weighted keyword_score as before R2 — hits divided by that
    category's OWN combined keyword-list length, not a raw hit count. This is
    what makes a specific phrase beat a frequent generic word: RPC's list is
    short, so each hit counts for more; CONTRATO's is long, so the same hit
    count means less. A prior version ranked by raw hit count instead, which
    let CONTRATO's long, generic keyword list out-count RPC's short, specific
    one on real documents that legitimately cite their contract (regression,
    commit 8ba8ac9).
    The top category is unambiguous (0.850) when its score dominates the
    runner-up's by CONTENIDO_DOMINANCIA_FACTOR (`_es_dominante`, shared with
    the R3 dominance guard in `clasificar`); otherwise it's ambiguous,
    candidate only (0.600). No hit at all → (None, 0.000).
    Real documents routinely mention a sibling category once or twice (an RPC
    always cites its CDP) — dominance, not exclusivity, is what should decide.
    """
    if not texto:
        return None, Decimal("0.000")
    fragmento = texto[:_CONTENIDO_MAX_CHARS]

    scored: list[tuple[CategoriaDocumento, Decimal]] = []
    for cat, kws in CATEGORIA_KEYWORDS.items():
        score = keyword_score([fragmento], kws["primary"] + kws["secondary"])
        if score > Decimal("0.000"):
            scored.append((cat, score))

    if not scored:
        return None, Decimal("0.000")

    ranked = sorted(scored, key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else Decimal("0.000")
    if _es_dominante(best_score, runner_up_score):
        return best_cat, CONTENIDO_SCORE_UNICO
    return best_cat, CONTENIDO_SCORE_AMBIGUO


def extraer_texto_contenido(data: bytes, filename: str) -> str:
    """Best-effort text extraction for the content sniff (PDF/DOCX/text; NO OCR).

    Reuses the shared document parser (pdfplumber/python-docx). Returns "" when
    the bytes are genuinely non-textual — ``parse_document``'s documented
    ``ValueError`` contract, a legitimate and permanent "no text" signal. Any
    OTHER exception (an unexpected parser failure) propagates instead of being
    collapsed into "", so the caller can treat it as a retryable error rather
    than the terminal "sin_texto" state. Output is truncated to
    TEXTO_EXTRAIDO_MAX_CHARS for persistence on secop_documentos.texto_extraido.
    """
    try:
        texto = parse_document(data, filename)
    except ValueError:
        return ""
    return texto[:TEXTO_EXTRAIDO_MAX_CHARS]


def aplicar_clasificacion(doc: object, *, forzar: bool = False) -> None:
    """Set doc.categoria and doc.categoria_confianza in place.

    Skips silently if doc.categoria_override is True and forzar is False,
    so that manual overrides are never overwritten by automatic re-classification.

    R4: when `doc.datos_raw` is present (SECOP-imported docs, populated by
    `secop_service._upsert_documento` before this call), its `_tipo_origen`
    (proceso/contrato/modificacion) and `tipo_documento`/`tipo_de_documento`
    fields are read as bounded priors. Non-SECOP `DocumentoFuente` uploads
    have no `datos_raw` attribute/value → priors no-op (getattr default + `or {}`).
    """
    if getattr(doc, "categoria_override", False) and not forzar:
        return

    nombre = getattr(doc, "nombre_archivo", None) or getattr(doc, "nombre", None)
    descripcion = getattr(doc, "descripcion", None)
    extension = getattr(doc, "extension", None)
    raw = getattr(doc, "datos_raw", None) or {}
    tipo_origen = raw.get("_tipo_origen")
    tipo_hint = raw.get("tipo_documento") or raw.get("tipo_de_documento")

    cat, confianza = clasificar(nombre, descripcion, extension, tipo_origen, tipo_hint)
    doc.categoria = cat  # type: ignore[attr-defined]
    doc.categoria_confianza = float(confianza) if confianza else None  # type: ignore[attr-defined]
