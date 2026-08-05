"""Tests for page-furniture stripping in scanned SECOP contract text.

Bug: OCR transcriptions of scanned contracts interleave running header/footer
boilerplate (entity address, NIT, teléfono/fax/e-mail/web, page markers, entity
banners) into the extracted text. Left uncleaned this (a) leaks into an
obligation's description and (b) can hide the next item's enumeration marker
from the line-based splitter, merging two consecutive obligations into one.

Covers:
1. `strip_page_furniture` — removes labeled contact/NIT fields and page markers.
2. `extract_obligaciones_verbatim` rejects a polluted verbatim result (falls
   back to the LLM extractor) instead of returning footer-contaminated text.
3. The merge case: a footer run interleaved between item "6." and item "7."
   (simulated page break) no longer swallows item 7 into item 6 once the
   furniture is stripped first.
"""

from __future__ import annotations

from app.agent.tools.contract_parser import (
    extract_obligaciones_verbatim,
    strip_page_furniture,
)

# ── User-reported example (verbatim, used as the oracle) ───────────────────

POLLUTED_OBLIGACION = (
    "Apoyar a la Gerencia de Proyectos Especiales Grupo de Alumbrado Público en la "
    "proyección de las respuestas a requerimientos o peticiones efectuadas por los "
    "entes de control, autoridades judiciales o administrativas, así como las "
    "realizadas por particulares, en el marco de procesos de contratación pública "
    "adelantados para la modernización del alumbrado público, en coordinación con "
    "los comités por con todo el corazón CLL. 60 CON CRA. Sª EDIF, CAMI NORTE B/ LA "
    "FLORESTATELEFONO: 2745888-2786888-2747444 FAX: 2746410 E-MAIL: "
    "infibague@infibague.gov.co WEB: www.infibague.gov.coJBAGUE - TOLIMA boqus "
    "capita musicd 3 de 6 BanFUTURO INFIbagué INSTITUTO DE FINANCIAMIENTO, "
    "PROMOCION Y DESARROLLO DE IBAGUÉ INFIBAGUE NIT: 890.700.755-5 estructuradores "
    "o evaluadores respectivos"
)


# ── Fix A: strip_page_furniture ─────────────────────────────────────────────


def test_strip_page_furniture_removes_contact_and_nit_signals() -> None:
    """Labeled contact fields and the NIT value are removed from the polluted example.

    Deliberately conservative (see contract_parser.strip_page_furniture): unlabeled
    prose/banners (address, entity slogan) are NOT stripped here — that residual
    noise is left to the hardened LLM prompt. This test checks what Fix A actually
    guarantees: the labeled signals are gone.
    """
    cleaned = strip_page_furniture(POLLUTED_OBLIGACION)
    for signal in ("TELEFONO", "FAX", "E-MAIL", "NIT", "890.700.755-5", "www.infibague"):
        assert signal not in cleaned, f"{signal!r} should have been stripped, got: {cleaned!r}"
    # Real obligation wording on both sides of the excised labels is preserved.
    assert "en coordinación con los comités" in cleaned
    assert "estructuradores o evaluadores respectivos" in cleaned


def test_strip_page_furniture_collapses_whitespace_left_by_removal() -> None:
    """Removing a labeled field must not leave doubled spaces behind."""
    texto = "con los comités  TELEFONO: 2745888-2786888-2747444 estructuradores"
    assert strip_page_furniture(texto) == "con los comités estructuradores"


def test_strip_page_furniture_handles_glued_ocr_join() -> None:
    """Mangled OCR joins (label glued to a preceding/following word) still match."""
    texto = "LA FLORESTATELEFONO: 2745888 FAX: 2746410 siguiente"
    cleaned = strip_page_furniture(texto)
    assert "TELEFONO" not in cleaned
    assert "FAX" not in cleaned
    assert "siguiente" in cleaned


def test_strip_page_furniture_leaves_bare_page_marker_alone_without_footer_run() -> None:
    """A bare 'N de M' with no other footer signal is NOT a detected footer run."""
    texto = "El comité revisó 3 de 6 expedientes pendientes de firma."
    assert strip_page_furniture(texto) == texto


def test_strip_page_furniture_strips_page_marker_within_detected_footer_run() -> None:
    texto = "Fin del anexo. TELEFONO: 2745888 3 de 6 Continúa en la siguiente hoja."
    cleaned = strip_page_furniture(texto)
    assert "3 de 6" not in cleaned
    assert "TELEFONO" not in cleaned


def test_strip_page_furniture_does_not_strip_real_prose_with_de_los() -> None:
    """'3 de los 6 comités' must never be mistaken for a page marker."""
    texto = "El contratista participó en 3 de los 6 comités técnicos programados."
    assert strip_page_furniture(texto) == texto


def test_strip_page_furniture_returns_input_unchanged_when_nothing_matches() -> None:
    texto = "Diseñar el plan de intervención del alumbrado público en el sector asignado."
    assert strip_page_furniture(texto) == texto


def test_strip_page_furniture_empty_input() -> None:
    assert strip_page_furniture("") == ""


# ── Fix B: reject polluted verbatim, escalate to LLM ────────────────────────


def test_extract_obligaciones_verbatim_rejects_footer_polluted_item() -> None:
    """A verbatim item still carrying a NIT/e-mail/phone signature must be rejected.

    ``extract_obligaciones_verbatim`` returns [] (not the polluted text) so the
    caller (document_service) falls through to the LLM extractor instead.
    """
    texto = (
        "OBLIGACIONES ESPECÍFICAS:\n"
        "1. Diseñar el sistema de información según los requerimientos técnicos.\n"
        "2. Apoyar la proyección de respuestas TELEFONO: 2745888-2786888 FAX: 2746410 "
        "E-MAIL: infibague@infibague.gov.co NIT: 890.700.755-5 a los entes de control.\n"
        "3. Las demás actividades que le asigne la supervisión y se relacionen con el "
        "objeto del contrato.\n"
        "\nVALOR DEL CONTRATO: diez millones."
    )
    assert extract_obligaciones_verbatim(texto) == []


def test_extract_obligaciones_verbatim_clean_block_unaffected() -> None:
    """Regression: a CLEAN enumerated block still returns its items unchanged."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS:\n"
        "1. Diseñar el sistema de información según los requerimientos técnicos.\n"
        "2. Ejecutar las pruebas de integración de cada componente asignado.\n"
        "3. Las demás actividades que le asigne la supervisión y se relacionen con el "
        "objeto del contrato.\n"
        "\nVALOR DEL CONTRATO: diez millones."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert result[0].descripcion == "Diseñar el sistema de información según los requerimientos técnicos"


# ── Merge case: footer run interleaved between item 6 and item 7 ───────────

MERGE_FOOTER_TEXTO = (
    "OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Diseñar el plan de intervención del alumbrado público en el sector asignado.\n"
    "2. Ejecutar el mantenimiento preventivo de las luminarias del municipio.\n"
    "3. Elaborar los informes técnicos mensuales de avance de obra.\n"
    "4. Coordinar con la interventoría las visitas técnicas programadas.\n"
    "5. Actualizar el inventario de luminarias intervenidas en el sistema.\n"
    "6. Realizar seguimiento a los contratos derivados de la gestión del alumbrado "
    "público en coordinación con los comités técnicos. "
    "TELEFONO: 2745888-2786888-2747444 FAX: 2746410 "
    "E-MAIL: infibague@infibague.gov.co WEB: www.infibague.gov.co "
    "3 de 6 "
    "NIT: 890.700.755-5 "
    "7. Asesorar al ordenador del gasto en la suscripción de los contratos "
    "relacionados con el objeto contractual.\n"
    "8. Las demás actividades que le asigne la supervisión y se relacionen con el "
    "objeto del contrato.\n"
    "\nVALOR DEL CONTRATO: cien millones."
)


def test_merge_case_items_stay_separate_after_stripping_page_furniture() -> None:
    """A page-break footer interleaved between '6.' and '7.' must not merge them.

    The page marker inside the footer run breaks the flattened line back into
    two lines once ``strip_page_furniture`` runs first, letting the line-based
    splitter recover item 7's marker instead of absorbing it into item 6.
    """
    cleaned = strip_page_furniture(MERGE_FOOTER_TEXTO)
    result = extract_obligaciones_verbatim(cleaned)

    assert len(result) == 8
    item_6, item_7, item_8 = result[5], result[6], result[7]

    assert "Asesorar" not in item_6.descripcion
    assert item_6.etiqueta == "6"

    assert item_7.descripcion.startswith("Asesorar al ordenador del gasto")
    assert item_7.etiqueta == "7"
    for noise in ("TELEFONO", "FAX", "E-MAIL", "NIT", "890.700.755-5", "3 de 6"):
        assert noise not in item_7.descripcion

    assert "Las demás actividades" in item_8.descripcion
