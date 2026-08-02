"""Document classifier — infers CategoriaDocumento from filename and description.

Uses keyword scoring (accent-insensitive, case-insensitive) via app.core.text_match.
Respects categoria_override: re-classification never overwrites a manual override.
"""

from __future__ import annotations

from decimal import Decimal

from app.agent.tools.document_parser import parse_document
from app.core.text_match import keyword_score
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

# Keywords per category (normalized at runtime via keyword_score).
# Order matters only for tie-breaking (first match wins if scores are equal).
CATEGORIA_KEYWORDS: dict[CategoriaDocumento, list[str]] = {
    CategoriaDocumento.CONTRATO: [
        "contrato",
        "cto",
        "clausulado",
        "minuta",
        "contract",
        "condiciones generales",
        "condiciones especiales",
        "acuerdo de prestacion",
        "prestacion de servicios",
    ],
    CategoriaDocumento.REGISTRO_PRESUPUESTAL: [
        "rpc",
        "rp ",
        "registro presupuestal",
        "compromiso presupuestal",
        "registro de compromiso",
    ],
    CategoriaDocumento.CDP: [
        "cdp",
        "certificado de disponibilidad",
        "disponibilidad presupuestal",
    ],
    CategoriaDocumento.ACTA_INICIO: [
        "acta de inicio",
        "acta inicio",
        "inicio del contrato",
        "acta de arranque",
    ],
    CategoriaDocumento.RUT: [
        "rut",
        "registro tributario",
        "registro unico tributario",
        "registro único tributario",
    ],
    CategoriaDocumento.CEDULA: [
        "cedula",
        "cédula",
        "cc ",
        "cedula ciudadania",
        "documento de identidad",
        "tarjeta de identidad",
    ],
    CategoriaDocumento.SEGURIDAD_SOCIAL: [
        "seguridad social",
        "planilla",
        "pila",
        "aportes seguridad",
        "aportes parafiscales",
    ],
    CategoriaDocumento.EVIDENCIAS: [
        "evidencia",
        "soporte",
        "registro fotografico",
        "registro fotográfico",
        "entregable",
        "producto",
        "acta de entrega",
        "acta entrega",
    ],
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


def clasificar(
    nombre: str | None,
    descripcion: str | None,
    extension: str | None = None,
) -> tuple[CategoriaDocumento, Decimal]:
    """Return the best-matching category and its confidence score.

    If no category scores above CATEGORIA_MIN_THRESHOLD, returns (OTROS, 0.000).
    """
    haystacks = [nombre, descripcion]

    best_cat = CategoriaDocumento.OTROS
    best_score = Decimal("0.000")

    for cat, keywords in CATEGORIA_KEYWORDS.items():
        score = keyword_score(haystacks, keywords)
        if score > best_score:
            best_score = score
            best_cat = cat

    if best_score < CATEGORIA_MIN_THRESHOLD:
        return CategoriaDocumento.OTROS, Decimal("0.000")

    return best_cat, best_score


def clasificar_contenido(texto: str | None) -> tuple[CategoriaDocumento | None, Decimal]:
    """Content-based re-score for the borderline sniff (no LLM, no embeddings).

    Scores the first ~1500 chars of extracted text against CATEGORIA_KEYWORDS using
    the same fractional/weighted keyword_score as clasificar() — hits divided by
    that category's OWN keyword-list length, not a raw hit count. This is what makes
    a specific phrase beat a frequent generic word: RPC's list is short (5 phrases),
    so each hit counts for more; CONTRATO's is long (9), so the same hit count means
    less. A prior version ranked by raw hit count instead, which let CONTRATO's long,
    generic keyword list out-count RPC's short, specific one on real documents that
    legitimately cite their contract (regression, commit 8ba8ac9).
    The top category is unambiguous (0.850) when its score dominates the runner-up's
    by CONTENIDO_DOMINANCIA_FACTOR (a runner-up of 0 is automatically dominant);
    otherwise it's ambiguous, candidate only (0.600). No hit at all → (None, 0.000).
    Real documents routinely mention a sibling category once or twice (an RPC always
    cites its CDP) — dominance, not exclusivity, is what should decide.
    """
    if not texto:
        return None, Decimal("0.000")
    fragmento = texto[:_CONTENIDO_MAX_CHARS]

    scored: list[tuple[CategoriaDocumento, Decimal]] = []
    for cat, keywords in CATEGORIA_KEYWORDS.items():
        score = keyword_score([fragmento], keywords)
        if score > Decimal("0.000"):
            scored.append((cat, score))

    if not scored:
        return None, Decimal("0.000")

    ranked = sorted(scored, key=lambda kv: kv[1], reverse=True)
    best_cat, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else Decimal("0.000")
    if runner_up_score == 0 or best_score >= CONTENIDO_DOMINANCIA_FACTOR * runner_up_score:
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
    """
    if getattr(doc, "categoria_override", False) and not forzar:
        return

    nombre = getattr(doc, "nombre_archivo", None) or getattr(doc, "nombre", None)
    descripcion = getattr(doc, "descripcion", None)
    extension = getattr(doc, "extension", None)

    cat, confianza = clasificar(nombre, descripcion, extension)
    doc.categoria = cat  # type: ignore[attr-defined]
    doc.categoria_confianza = float(confianza) if confianza else None  # type: ignore[attr-defined]
