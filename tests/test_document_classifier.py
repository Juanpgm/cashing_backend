"""Unit tests for document_classifier — category inference from filename/description."""

from decimal import Decimal
from pathlib import Path

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.services.document_classifier import (
    CATEGORIA_A_REQUISITO,
    CATEGORIA_KEYWORDS,
    CATEGORIA_MIN_THRESHOLD,
    CONTENIDO_SCORE_AMBIGUO,
    TIPO_A_REQUISITO,
    aplicar_clasificacion,
    clasificar,
    clasificar_contenido,
    extraer_texto_contenido,
)

# ── CDP first-class category (clasificacion-documentos-secop, D1) ───────────


def test_categoria_cdp_es_primera_clase():
    assert CategoriaDocumento.CDP.value == "cdp"
    assert "cdp" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]["primary"]
    assert "certificado de disponibilidad" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]["primary"]
    assert "disponibilidad presupuestal" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]["primary"]


def test_cdp_mapeos_a_requisito():
    assert CATEGORIA_A_REQUISITO[CategoriaDocumento.CDP] == "CDP"
    assert TIPO_A_REQUISITO["cdp"] == "CDP"


@pytest.mark.parametrize(
    "nombre",
    [
        "CDP_2024.pdf",
        "Certificado de Disponibilidad Presupuestal.pdf",
        "disponibilidad presupuestal 001.pdf",
    ],
)
def test_clasificar_cdp_por_nombre(nombre):
    cat, score = clasificar(nombre, None)
    assert cat == CategoriaDocumento.CDP
    assert score >= CATEGORIA_MIN_THRESHOLD


def test_clasificar_rpc_no_se_confunde_con_cdp():
    cat, _ = clasificar("RPC registro presupuestal compromiso.pdf", None)
    assert cat == CategoriaDocumento.REGISTRO_PRESUPUESTAL


# ── R2: primary/secondary scoring ────────────────────────────────────────────


@pytest.mark.parametrize(
    "nombre, expected_cat",
    [
        ("CDP.pdf", CategoriaDocumento.CDP),
        ("REGISTRO PRESUPUESTAL.pdf", CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("CONTRATO DE PRESTACION DE SERVICIOS.pdf", CategoriaDocumento.CONTRATO),
    ],
)
def test_clasificar_primary_filename_clears_auto_link_threshold(nombre, expected_cat):
    cat, score = clasificar(nombre, None)
    assert cat == expected_cat
    assert score >= Decimal("0.700")


def test_clasificar_secondary_only_stays_below_auto_link_threshold():
    """'condiciones generales' is a CONTRATO SECONDARY keyword only — no PRIMARY hit."""
    cat, score = clasificar("CONDICIONES GENERALES.pdf", None)
    assert cat == CategoriaDocumento.CONTRATO
    assert score < Decimal("0.700")


def test_clasificar_score_nunca_alcanza_el_cap_de_1000():
    # Every primary + secondary keyword of CONTRATO in one filename.
    nombre = (
        "contrato cto minuta clausulado contract condiciones generales condiciones especiales "
        "acuerdo de prestacion prestacion de servicios.pdf"
    )
    cat, score = clasificar(nombre, None)
    assert cat == CategoriaDocumento.CONTRATO
    assert score == Decimal("0.950")
    assert score < Decimal("1.000")


# ── R2/R3: realistic-filename sanity check (T2.5, design open question) ─────


@pytest.mark.parametrize(
    "nombre, expected_cat",
    [
        ("CONTRATO DE PRESTACION.pdf", CategoriaDocumento.CONTRATO),
        ("RPC 12345.pdf", CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("REGISTRO PRESUPUESTAL.pdf", CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("CDP-2025.pdf", CategoriaDocumento.CDP),
        ("9. CLAUSULADO.PDF.pdf", CategoriaDocumento.CONTRATO),
        ("Certificado de Disponibilidad.pdf", CategoriaDocumento.CDP),
    ],
)
def test_clasificar_nombres_reales_secop_clasifican_y_superan_umbral(nombre, expected_cat):
    """T2.5: real SECOP filename sample — every one clears AUTO_LINK_THRESHOLD
    and is correctly distinguished. No constant adjustment was needed for these
    (all land exactly on PRIMARY_BASE=0.750); see CATEGORIA_KEYWORDS docstring
    for the one adjustment this sanity check DID surface (ACTA_INICIO vs
    CONTRATO on "acta de inicio del contrato"-style names)."""
    cat, score = clasificar(nombre, None)
    assert cat == expected_cat
    assert score >= Decimal("0.700")


def test_clasificar_acta_inicio_del_contrato_no_se_confunde_con_contrato():
    """Real SECOP pattern: 'ACTA DE INICIO DEL CONTRATO No. XXX' must classify
    as ACTA_INICIO, not CONTRATO, despite legitimately mentioning 'contrato'."""
    cat, _ = clasificar("ACTA DE INICIO DEL CONTRATO No 123.pdf", None)
    assert cat == CategoriaDocumento.ACTA_INICIO


# ── correction (RELIABILITY-001): R3 dominance guard must not defeat R2 when
# the runner-up's only signal is CONTRATO's generic "contrato" keyword ───────


@pytest.mark.parametrize(
    "nombre, expected_cat",
    [
        ("ACTA DE INICIO DEL CONTRATO No 123.pdf", CategoriaDocumento.ACTA_INICIO),
        ("ACTA INICIO CONTRATO.pdf", CategoriaDocumento.ACTA_INICIO),
        ("REGISTRO PRESUPUESTAL DEL CONTRATO 123.pdf", CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("CDP DEL CONTRATO 2025.pdf", CategoriaDocumento.CDP),
    ],
)
def test_clasificar_no_se_demuestra_por_co_ocurrencia_generica_de_contrato(nombre, expected_cat):
    """A legitimate PRIMARY winner must clear AUTO_LINK_THRESHOLD even when the
    filename also contains CONTRATO's generic bare 'contrato' keyword — that
    keyword alone is not a genuine ambiguity threat (RELIABILITY-001)."""
    cat, score = clasificar(nombre, None)
    assert cat == expected_cat
    assert score >= Decimal("0.700")


def test_clasificar_rp_cdp_ambiguedad_genuina_sigue_demorada():
    """The genuine RP<->CDP confusion the R3 guard exists for must still demote."""
    cat, score = clasificar("REGISTRO PRESUPUESTAL Y CERTIFICADO DE DISPONIBILIDAD.pdf", None)
    assert cat in (CategoriaDocumento.REGISTRO_PRESUPUESTAL, CategoriaDocumento.CDP)
    assert score < Decimal("0.700")


# ── R3: import-time dominance guard ──────────────────────────────────────────


def test_clasificar_rp_cdp_ambiguo_sin_dominancia_da_0600():
    """Both RP and CDP PRIMARY keywords present, neither dominates -> ambiguous."""
    cat, score = clasificar("RPC anexo certificado de disponibilidad.pdf", None)
    assert cat in (CategoriaDocumento.REGISTRO_PRESUPUESTAL, CategoriaDocumento.CDP)
    assert score == CONTENIDO_SCORE_AMBIGUO


def test_clasificar_rp_dominante_sin_cdp_mantiene_confianza_alta():
    cat, score = clasificar("RPC 12345 registro presupuestal.pdf", None)
    assert cat == CategoriaDocumento.REGISTRO_PRESUPUESTAL
    assert score >= Decimal("0.750")


# ── R4: SECOP metadata priors ────────────────────────────────────────────────


def test_clasificar_tipo_origen_modificacion_reduce_score_contrato():
    _, score_mod = clasificar("contrato.pdf", None, tipo_origen="modificacion")
    _, score_base = clasificar("contrato.pdf", None, tipo_origen="contrato")
    assert score_mod < score_base
    assert score_base == Decimal("0.750")
    assert score_mod == Decimal("0.600")


def test_clasificar_tipo_hint_boosts_categoria_score():
    _, base = clasificar("condiciones generales.pdf", None)
    _, con_hint = clasificar("condiciones generales.pdf", None, tipo_hint="contrato")
    assert con_hint > base
    assert base == Decimal("0.400")
    assert con_hint == Decimal("0.500")


def test_clasificar_extension_imagen_nudges_evidencias():
    _, base = clasificar("soporte visita.jpg", None)
    _, con_ext = clasificar("soporte visita.jpg", None, extension="jpg")
    assert con_ext > base
    assert base == Decimal("0.750")
    assert con_ext == Decimal("0.800")


def test_clasificar_sin_metadata_secop_es_no_op():
    """Absent extension/tipo_origen/tipo_hint (non-SECOP upload) never changes the result."""
    assert clasificar("contrato.pdf", None) == clasificar(
        "contrato.pdf", None, extension=None, tipo_origen=None, tipo_hint=None
    )


# ── clasificar_contenido (borderline content sniff, D2) ─────────────────────

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "texto, expected_cat",
    [
        (
            "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES No. 123-2024\n"
            "Entre la entidad y el contratista se celebra el presente contrato, cuyo clausulado se detalla.",
            CategoriaDocumento.CONTRATO,
        ),
        (
            "CERTIFICADO DE DISPONIBILIDAD PRESUPUESTAL No. 2024-001\n"
            "La entidad certifica la disponibilidad presupuestal del rubro.",
            CategoriaDocumento.CDP,
        ),
        (
            "REGISTRO PRESUPUESTAL DEL COMPROMISO No. 555\n"
            "Registro de compromiso presupuestal a favor del contratista.",
            CategoriaDocumento.REGISTRO_PRESUPUESTAL,
        ),
    ],
)
def test_clasificar_contenido_hit_inequivoco_da_0850(texto, expected_cat):
    cat, score = clasificar_contenido(texto)
    assert cat == expected_cat
    assert score == Decimal("0.850")


def test_clasificar_contenido_ambiguo_multicategoria_da_0600():
    texto = (
        "El presente contrato se suscribe con cargo al registro presupuestal "
        "expedido por la entidad para amparar el compromiso."
    )
    cat, score = clasificar_contenido(texto)
    assert cat in (CategoriaDocumento.CONTRATO, CategoriaDocumento.REGISTRO_PRESUPUESTAL)
    assert score == Decimal("0.600")


def test_clasificar_contenido_rpc_dominante_con_mencion_de_cdp_da_0850():
    """Real SECOP RPC docs always cite their CDP once — that must not block 0.850.

    Modeled on live E2E evidence: an RPC document body legitimately repeats RPC
    signals (rpc/registro presupuestal/compromiso presupuestal/registro de
    compromiso) while mentioning CDP only once (certificado de disponibilidad).
    RPC hits (4) dominate the CDP runner-up (1) by >= CONTENIDO_DOMINANCIA_FACTOR,
    so the category must be unambiguous despite the sibling-category mention.
    """
    texto = (
        "MUNICIPIO SANTIAGO DE CALI\n"
        "Registro Presupuestal de Compromiso RPC No. 4500379639\n"
        "Fecha de Contabilizacion: 2024-05-10\n"
        "El presente registro de compromiso corresponde al compromiso presupuestal "
        "derivado del contrato, y se afecta el certificado de disponibilidad "
        "expedido previamente."
    )
    cat, score = clasificar_contenido(texto)
    assert cat == CategoriaDocumento.REGISTRO_PRESUPUESTAL
    assert score == Decimal("0.850")


def test_clasificar_contenido_empate_real_da_0600():
    """Genuinely tied signals (one RPC keyword, one CDP keyword) stay ambiguous."""
    texto = "El registro presupuestal fue expedido junto con el certificado de disponibilidad correspondiente."
    cat, score = clasificar_contenido(texto)
    assert cat in (CategoriaDocumento.REGISTRO_PRESUPUESTAL, CategoriaDocumento.CDP)
    assert score == Decimal("0.600")


def test_clasificar_contenido_rpc_real_no_pierde_confianza_ante_mencion_generica_de_contrato():
    """Regression (commit 8ba8ac9): winner selection moved from fractional/weighted
    keyword_score to raw keyword-hit COUNT. RPC's keyword list is short and specific
    (5 phrases); CONTRATO's is long and generic (9 phrases) — a real RPC document that
    also cites its underlying contract (routine, e.g. "de conformidad con las
    condiciones generales del contrato") racks up CONTRATO hits fast even though each
    one is weak/generic, while RPC's few hits are strong/specific. Under hit-COUNT
    dominance this real text scores only 0.600 (5 RPC hits vs 3 CONTRATO hits fails
    the >=2x-count bar) — below AUTO_LINK_THRESHOLD (0.700), blocking auto-link.
    Under the restored fractional score (RPC 5/5=1.000 vs CONTRATO 3/9=0.333, a 3x
    dominance) it correctly scores 0.850 and stays auto-linkable.
    """
    texto = (
        "MUNICIPIO SANTIAGO DE CALI SECRETARIA DE SEGURIDAD Y JUSTICIA Registro "
        "Presupuestal de Compromiso RPC No. 4500379639 Fecha de Contabilizacion: "
        "23.08.2025. En virtud del contrato No. 2025-01, y de conformidad con las "
        "condiciones generales del contrato, cuyo clausulado ampara la presente "
        "afectacion, se expide el registro de compromiso presupuestal."
    )
    cat, score = clasificar_contenido(texto)
    assert cat == CategoriaDocumento.REGISTRO_PRESUPUESTAL
    assert score == Decimal("0.850")


def test_clasificar_contenido_sin_hit():
    cat, score = clasificar_contenido("texto administrativo sin senales relevantes para el checklist")
    assert cat is None
    assert score == Decimal("0.000")


def test_clasificar_contenido_texto_vacio():
    assert clasificar_contenido(None) == (None, Decimal("0.000"))
    assert clasificar_contenido("") == (None, Decimal("0.000"))


def test_clasificar_contenido_solo_primeros_1500_chars():
    texto = ("x" * 2000) + " contrato de prestacion de servicios"
    cat, score = clasificar_contenido(texto)
    assert cat is None
    assert score == Decimal("0.000")


# ── extraer_texto_contenido (extraction helper, no OCR) ─────────────────────


def test_extraer_texto_contenido_docx():
    data = (FIXTURES / "contrato.docx").read_bytes()
    texto = extraer_texto_contenido(data, "contrato.docx")
    assert "CONTRATO DE PRESTACI" in texto
    cat, score = clasificar_contenido(texto)
    assert cat == CategoriaDocumento.CONTRATO
    assert score == Decimal("0.850")


def test_extraer_texto_contenido_pdf():
    data = (FIXTURES / "cdp.pdf").read_bytes()
    texto = extraer_texto_contenido(data, "cdp.pdf")
    assert "DISPONIBILIDAD PRESUPUESTAL" in texto
    cat, score = clasificar_contenido(texto)
    assert cat == CategoriaDocumento.CDP
    assert score == Decimal("0.850")


def test_extraer_texto_contenido_pdf_rpc():
    data = (FIXTURES / "rpc.pdf").read_bytes()
    cat, score = clasificar_contenido(extraer_texto_contenido(data, "rpc.pdf"))
    assert cat == CategoriaDocumento.REGISTRO_PRESUPUESTAL
    assert score == Decimal("0.850")


def test_extraer_texto_contenido_bytes_invalidos_devuelve_vacio():
    assert extraer_texto_contenido(b"\x00\x01\x02\x03\xff\xfe", "binario.exe") == ""


# ── clasificar ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "nombre, descripcion, expected_cat",
    [
        # CONTRATO
        ("contrato_2024.pdf", None, CategoriaDocumento.CONTRATO),
        ("CTO_CONSULTOR.pdf", None, CategoriaDocumento.CONTRATO),
        ("Clausulado del Servicio.pdf", None, CategoriaDocumento.CONTRATO),
        ("Minuta firmada.docx", None, CategoriaDocumento.CONTRATO),
        # REGISTRO_PRESUPUESTAL
        ("RPC_001.pdf", None, CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("Registro Presupuestal.pdf", None, CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        ("COMPROMISO PRESUPUESTAL 123.pdf", None, CategoriaDocumento.REGISTRO_PRESUPUESTAL),
        # ACTA_INICIO
        ("Acta de Inicio.pdf", None, CategoriaDocumento.ACTA_INICIO),
        ("ACTA INICIO CONTRATO.pdf", None, CategoriaDocumento.ACTA_INICIO),
        ("inicio del contrato.pdf", None, CategoriaDocumento.ACTA_INICIO),
        # RUT — acentos y variaciones
        ("RUT_contribuyente.pdf", None, CategoriaDocumento.RUT),
        ("Registro Unico Tributario.pdf", None, CategoriaDocumento.RUT),
        ("Registro Único Tributario.pdf", None, CategoriaDocumento.RUT),
        ("registro tributario actualizado.pdf", None, CategoriaDocumento.RUT),
        # CEDULA
        ("Cedula_ciudadania.pdf", None, CategoriaDocumento.CEDULA),
        ("Cédula de Ciudadanía.pdf", None, CategoriaDocumento.CEDULA),
        ("cedula contratista.pdf", None, CategoriaDocumento.CEDULA),
        # SEGURIDAD_SOCIAL
        ("Planilla Seguridad Social 202401.pdf", None, CategoriaDocumento.SEGURIDAD_SOCIAL),
        ("PILA enero.pdf", None, CategoriaDocumento.SEGURIDAD_SOCIAL),
        # EVIDENCIAS
        ("Evidencia soporte entrega.pdf", None, CategoriaDocumento.EVIDENCIAS),
        ("Registro fotografico.jpg", None, CategoriaDocumento.EVIDENCIAS),
        # OTROS — fallback
        ("anexo_varios.zip", None, CategoriaDocumento.OTROS),
        (None, None, CategoriaDocumento.OTROS),
        # Description-only match
        (None, "Acta de inicio del contrato", CategoriaDocumento.ACTA_INICIO),
    ],
)
def test_clasificar_categoria(nombre, descripcion, expected_cat):
    cat, score = clasificar(nombre, descripcion)
    assert cat == expected_cat, f"Expected {expected_cat} for '{nombre}' / '{descripcion}', got {cat}"
    if expected_cat != CategoriaDocumento.OTROS:
        assert score >= CATEGORIA_MIN_THRESHOLD


def test_clasificar_otros_returns_zero_score():
    cat, score = clasificar("anexo_generico.zip", None)
    assert cat == CategoriaDocumento.OTROS
    assert score == Decimal("0.000")


def test_clasificar_score_range():
    _, score = clasificar("contrato firmado.pdf", None)
    assert Decimal("0.000") <= score <= Decimal("1.000")


# ── aplicar_clasificacion ───────────────────────────────────────────────────


class _FakeDoc:
    def __init__(self, nombre_archivo=None, descripcion=None, categoria_override=False):
        self.nombre_archivo = nombre_archivo
        self.descripcion = descripcion
        self.categoria_override = categoria_override
        self.categoria = CategoriaDocumento.OTROS
        self.categoria_confianza = None


def test_aplicar_clasificacion_sets_categoria():
    doc = _FakeDoc(nombre_archivo="RUT_actualizado.pdf")
    aplicar_clasificacion(doc)
    assert doc.categoria == CategoriaDocumento.RUT
    assert doc.categoria_confianza is not None


def test_aplicar_clasificacion_respects_override():
    """Manual override must NOT be overwritten by auto-classification."""
    doc = _FakeDoc(nombre_archivo="RUT_actualizado.pdf", categoria_override=True)
    doc.categoria = CategoriaDocumento.CONTRATO  # manually set
    aplicar_clasificacion(doc)
    # Should remain CONTRATO — override is active
    assert doc.categoria == CategoriaDocumento.CONTRATO


def test_aplicar_clasificacion_forzar_overwrites_override():
    doc = _FakeDoc(nombre_archivo="RUT_actualizado.pdf", categoria_override=True)
    doc.categoria = CategoriaDocumento.CONTRATO
    aplicar_clasificacion(doc, forzar=True)
    # forzar=True ignores override
    assert doc.categoria == CategoriaDocumento.RUT


def test_aplicar_clasificacion_otros_when_unclassifiable():
    doc = _FakeDoc(nombre_archivo="random_file_xyz.zip")
    aplicar_clasificacion(doc)
    assert doc.categoria == CategoriaDocumento.OTROS
    assert doc.categoria_confianza is None


def test_aplicar_clasificacion_uses_nombre_attribute():
    """DocumentoFuente uses 'nombre' not 'nombre_archivo'."""

    class _FuteDoc:
        nombre = "Acta de Inicio firmada.pdf"
        descripcion = None
        categoria_override = False
        categoria = CategoriaDocumento.OTROS
        categoria_confianza = None

    doc = _FuteDoc()
    aplicar_clasificacion(doc)
    assert doc.categoria == CategoriaDocumento.ACTA_INICIO
