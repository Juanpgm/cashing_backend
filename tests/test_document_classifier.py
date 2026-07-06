"""Unit tests for document_classifier — category inference from filename/description."""

from decimal import Decimal

import pytest

from app.models.categoria_documento import CategoriaDocumento
from app.services.document_classifier import (
    CATEGORIA_MIN_THRESHOLD,
    aplicar_clasificacion,
    clasificar,
)


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
