"""Golden regression sobre los contratos REALES del vault (carpeta EJEMPLOS).

Estos PDFs son los casos que el usuario reportó como problemáticos. Sirven como
red de seguridad de extremo a extremo: PDF real → parse_pdf (pdfplumber) →
extract_obligaciones_verbatim. Si el vault no está presente (CI u otra máquina),
los tests se saltan en vez de fallar.

Conteos esperados (verificados contra los .md de cada subcarpeta):
- EJEMPLO #1: 8 obligaciones (A–H), marcadores en mayúscula aplanados.
- EJEMPLO #2: 4 obligaciones (1–4).
- EJEMPLO #3: 6 obligaciones (1–6).
- EJEMPLO #4: PDF escaneado (sin capa de texto) → debe ir a la ruta de visión,
  por lo que el extractor verbatim devuelve [] y el texto es insuficiente.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agent.tools.contract_parser import extract_obligaciones_verbatim
from app.agent.tools.document_parser import parse_pdf

# tests/ → cashing-backend → cashing (workspace) → cashing_vault/...
_WORKSPACE = Path(__file__).resolve().parents[2]
_EJEMPLOS = (
    _WORKSPACE
    / "cashing_vault"
    / "chashing_vault"
    / "CASHING_LONGTERM_CONTEXT"
    / "PROCESOS"
    / "EXTRACCION_TEXTO_DOCS"
    / "AGENTES"
    / "EXTRACCION_OBLIGACIONES_CONTRATOS"
    / "EJEMPLOS"
)

# Mínimo de caracteres para considerar el texto "suficiente" (mismo umbral que
# is_text_sufficient por defecto); por debajo, el documento va a la ruta de visión.
_MIN_TEXT_CHARS = 200


def _pdf_in(folder: str) -> Path | None:
    """Return the single PDF inside an EJEMPLO folder, or None if unavailable."""
    base = _EJEMPLOS / folder
    if not base.is_dir():
        return None
    return next(iter(base.glob("*.pdf")), None)


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("EJEMPLO #1", 8),
        ("EJEMPLO #2", 4),
        ("EJEMPLO #3", 6),
    ],
)
def test_golden_text_pdf_obligation_count(folder: str, expected: int) -> None:
    """Los PDFs con capa de texto extraen exactamente las obligaciones esperadas."""
    pdf = _pdf_in(folder)
    if pdf is None:
        pytest.skip(f"Vault no disponible: {folder}")

    texto = parse_pdf(pdf.read_bytes())
    if len(texto.strip()) < _MIN_TEXT_CHARS:
        pytest.skip(f"{folder}: PDF sin capa de texto (escaneado)")

    result = extract_obligaciones_verbatim(texto)
    etiquetas = [o.etiqueta for o in result]
    assert len(result) == expected, f"{folder}: esperado {expected}, obtenido {len(result)} ({etiquetas})"

    # El último ítem es siempre el cierre "Las demás…".
    catch_all = result[-1].descripcion
    assert "las dem" in catch_all.lower()

    # El cierre NO debe quedar truncado (el bug original cortaba en "…por la"
    # porque "supervisión" disparaba el fin de sección). La cláusula completa
    # siempre termina en "…del contrato" o "…del servicio".
    assert catch_all.lower().rstrip(" .").endswith(("contrato", "servicio")), (
        f"{folder}: catch-all truncado → {catch_all!r}"
    )

    # Verbatim: cada descripción aparece literalmente en el texto del contrato
    # (modulo colapso de espacios en blanco).
    import re

    texto_norm = re.sub(r"\s+", " ", texto)
    for ob in result:
        assert ob.descripcion in texto_norm, f"{folder}: no es verbatim → {ob.descripcion!r}"


def test_golden_scanned_pdf_goes_to_vision() -> None:
    """EJEMPLO #4 es escaneado: sin texto, el verbatim devuelve [] (→ visión)."""
    pdf = _pdf_in("EJEMPLO #4")
    if pdf is None:
        pytest.skip("Vault no disponible: EJEMPLO #4")

    texto = parse_pdf(pdf.read_bytes())
    assert len(texto.strip()) < _MIN_TEXT_CHARS, "EJEMPLO #4 debería ser escaneado (sin texto)"
    assert extract_obligaciones_verbatim(texto) == []
