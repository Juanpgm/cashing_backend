"""Tests for the user-monthly-context + evidence-content grounding additions.

Covers the three generation-input changes:
- `build_actividad_justificacion_prompt` gains an optional user-context section.
- `_evidence_links` only emits http(s) links (uploaded local_file evidence must
  never round-trip into EvidenceLink, whose validator rejects non-http links).
- `.rar` archives parse gracefully (no crash without an unrar backend) and
  `_extraer_texto_seguro` never raises on unreadable bytes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from app.agent.nodes.evidence_justify import _evidence_links
from app.agent.prompts.actividad_generation import build_actividad_justificacion_prompt
from app.agent.tools.document_parser import is_archive_filename, parse_document
from app.services.evidencia_service import _extraer_texto_seguro


def test_prompt_incluye_contexto_usuario():
    prompt = build_actividad_justificacion_prompt(
        "Presentar informes", "1. [email] Informe (2026-07-01)", contexto_usuario="Elaboré el informe X"
    )
    assert "Resumen del usuario" in prompt
    assert "Elaboré el informe X" in prompt


def test_prompt_omite_seccion_sin_contexto():
    prompt = build_actividad_justificacion_prompt("Presentar informes", "sin evidencias")
    assert "Resumen del usuario" not in prompt


def test_evidence_links_solo_http():
    links = _evidence_links(
        [
            {"source": "email", "title": "Correo", "link": "https://mail.google.com/x", "date": "2026-07-01"},
            {"source": "local_file", "filename": "acta.pdf", "link": "", "date": "2026-07-02"},
            {"source": "drive", "title": "Doc", "link": "javascript:alert(1)", "date": ""},
        ]
    )
    assert len(links) == 1
    assert links[0]["link"] == "https://mail.google.com/x"


def test_rar_es_archive_y_no_revienta_sin_backend():
    assert is_archive_filename("evidencias.rar")
    # Garbage rar bytes: must degrade to empty text, never raise.
    assert parse_document(b"Rar!\x1a\x07\x01\x00garbage", "evidencias.rar") == ""


async def test_extraer_texto_seguro_txt_y_binario():
    texto = await _extraer_texto_seguro("notas.txt", "Acta de reunión del 5 de julio".encode())
    assert texto is not None and "Acta de reuni" in texto
    # Undecodable binary garbage → "" (attempted, no text), never an exception.
    assert await _extraer_texto_seguro("foto.xyz", b"\x00\x01\x02\xff" * 100) == ""


async def test_extraer_texto_seguro_llama_con_relaxed_ocr_true():
    """`_extraer_texto_seguro` must opt into the relaxed OCR-acceptance gate —
    scanned evidence PDFs with concatenated OCR text are otherwise discarded
    (see `extraer_texto_documento`'s `relaxed_ocr` param)."""
    with patch(
        "app.services.document_service.extraer_texto_documento",
        AsyncMock(return_value=("texto ocr relajado", [])),
    ) as mock_extraer:
        texto = await _extraer_texto_seguro("escaneo.pdf", b"%PDF-fake")

    assert texto == "texto ocr relajado"
    mock_extraer.assert_awaited_once_with(b"%PDF-fake", "escaneo.pdf", relaxed_ocr=True)
