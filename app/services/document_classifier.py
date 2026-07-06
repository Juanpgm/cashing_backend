"""Document classifier — infers CategoriaDocumento from filename and description.

Uses keyword scoring (accent-insensitive, case-insensitive) via app.core.text_match.
Respects categoria_override: re-classification never overwrites a manual override.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.text_match import keyword_score
from app.models.categoria_documento import CategoriaDocumento

# Minimum score to assign a non-OTROS category.
CATEGORIA_MIN_THRESHOLD = Decimal("0.001")

# Keywords per category (normalized at runtime via keyword_score).
# Order matters only for tie-breaking (first match wins if scores are equal).
CATEGORIA_KEYWORDS: dict[CategoriaDocumento, list[str]] = {
    CategoriaDocumento.CONTRATO: [
        "contrato", "cto", "clausulado", "minuta", "contract", "condiciones generales",
        "condiciones especiales", "acuerdo de prestacion", "prestacion de servicios",
    ],
    CategoriaDocumento.REGISTRO_PRESUPUESTAL: [
        "rpc", "rp ", "registro presupuestal", "compromiso presupuestal",
        "registro de compromiso",
    ],
    CategoriaDocumento.ACTA_INICIO: [
        "acta de inicio", "acta inicio", "inicio del contrato", "acta de arranque",
    ],
    CategoriaDocumento.RUT: [
        "rut", "registro tributario", "registro unico tributario",
        "registro único tributario",
    ],
    CategoriaDocumento.CEDULA: [
        "cedula", "cédula", "cc ", "cedula ciudadania", "documento de identidad",
        "tarjeta de identidad",
    ],
    CategoriaDocumento.SEGURIDAD_SOCIAL: [
        "seguridad social", "planilla", "pila", "aportes seguridad",
        "aportes parafiscales",
    ],
    CategoriaDocumento.EVIDENCIAS: [
        "evidencia", "soporte", "registro fotografico", "registro fotográfico",
        "entregable", "producto", "acta de entrega", "acta entrega",
    ],
    # OTROS has no keywords — it is the fallback
}

# Maps a category to the requisito_codigo it pre-fills in the checklist.
# OTROS → None means no pre-assignment.
CATEGORIA_A_REQUISITO: dict[CategoriaDocumento, str | None] = {
    CategoriaDocumento.CONTRATO: "CONTRATO",
    CategoriaDocumento.REGISTRO_PRESUPUESTAL: "RPC",
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
