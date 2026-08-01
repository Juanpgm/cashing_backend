"""Unit tests for document_classifier — category inference from filename/description."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.models.categoria_documento import CategoriaDocumento
from app.services.document_classifier import (
    CATEGORIA_A_REQUISITO,
    CATEGORIA_KEYWORDS,
    CATEGORIA_MIN_THRESHOLD,
    TIPO_A_REQUISITO,
    aplicar_clasificacion,
    clasificar,
    clasificar_contenido,
    extraer_texto_contenido,
)


# ── CDP first-class category (clasificacion-documentos-secop, D1) ───────────

def test_categoria_cdp_es_primera_clase():
    assert CategoriaDocumento.CDP.value == "cdp"
    assert "cdp" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]
    assert "certificado de disponibilidad" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]
    assert "disponibilidad presupuestal" in CATEGORIA_KEYWORDS[CategoriaDocumento.CDP]


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
