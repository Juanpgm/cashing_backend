"""Robustness of the LLM-fallback obligation-extraction path (scanned/OCR tier).

Covers the three pieces added to lift the LLM path for noisy/OCR'd input:

1. Few-shot wiring — the entity example + the scanned/noisy example are actually
   included in the prompt sent to the model (they used to be defined but unused).
2. OCR-tolerant section detection — a flattened/mangled header is still found,
   or (when it truly cannot be matched) the whole cleaned text reaches the LLM
   as one bounded chunk instead of empty/garbage fragments.
3. Regression — a clean digital contract still short-circuits on the
   deterministic verbatim extractor; the LLM is never called for it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from app.agent.tools.contract_parser import (
    WHOLE_TEXT_FALLBACK_MAX_CHARS,
    extract_obligation_sections,
)
from app.schemas.agent import LLMResponse

# ── 1. End-to-end: noisy/polluted text escalates to the LLM, few-shots wired ──

_TEXTO_NOISY = (
    "CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "6. Ser designado como comité evaluador de los procesos de contratación que le sean "
    "asignados por el ordenador del gasto.\n"
    "7. Asesorar al ordenador del gasto en la evaluación de las propuestas presentadas "
    "dentro de los procesos de selección, en coordinación con los comités por con todo "
    "el corazón CLL. 60 CON CRA. Sª EDIF boqus capita musicd FLORESTATELEFONO: 2745888 "
    "FAX: 2746410 E-MAIL: infibague@infibague.gov.co WEB: www.infibague.gov.co 3 de 6 "
    "BanFUTURO INFIbagué INSTITUTO DE FINANCIAMIENTO PROMOCION Y DESARROLLO DE IBAGUE "
    "INFIBAGUE NIT: 890.700.755-5 estructuradores o evaluadores respectivos.\n"
    "8. Elaborar los informes técnicos requeridos por la supervisión.\n"
    "CLÁUSULA TERCERA — VALOR DEL CONTRATO: cien millones de pesos."
)


class _FakeStructuredLLM:
    """Records every call so the test can inspect the prompt actually sent."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[list] = []

    async def complete(self, messages, temperature=0.0, max_tokens=4096, response_format=None):
        self.calls.append(messages)
        return LLMResponse(
            content=self._content,
            model="fake/structured",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )


@pytest.mark.asyncio
async def test_polluted_text_escalates_to_llm_with_fewshots_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.adapters.llm as llm_module
    from app.services import document_service

    mocked_json = json.dumps(
        {
            "obligaciones": [
                {
                    "descripcion": "Ser designado como comité evaluador de los procesos de contratación",
                    "tipo": "especifica",
                    "etiqueta": "6",
                },
                {
                    "descripcion": (
                        "Asesorar al ordenador del gasto en la evaluación de las propuestas, en "
                        "coordinación con los comités estructuradores o evaluadores respectivos"
                    ),
                    "tipo": "especifica",
                    "etiqueta": "7",
                },
                {
                    "descripcion": "Elaborar los informes técnicos requeridos por la supervisión",
                    "tipo": "especifica",
                    "etiqueta": "8",
                },
            ]
        }
    )
    fake_llm = _FakeStructuredLLM(mocked_json)
    monkeypatch.setattr(llm_module, "get_llm", lambda model=None: fake_llm)

    db = AsyncMock()
    extraidas, avisos = await document_service.extraer_obligaciones_texto(_TEXTO_NOISY, None, db)

    # Verbatim did NOT short-circuit — the LLM was actually invoked.
    assert fake_llm.calls, "verbatim must reject polluted text and escalate to the LLM"

    system_msg = fake_llm.calls[0][0].content
    # Wiring is real: both the default entity few-shot and the scanned/noisy
    # few-shot ended up in the prompt actually sent to the model.
    assert "Realizar el diagnóstico territorial del municipio" in system_msg
    assert "boqus capita musicd" in system_msg
    assert "NIT: 890.700.755-5" in system_msg

    # The mocked structured result — not the polluted verbatim text — is what
    # gets returned/persisted.
    assert avisos == []
    assert [o.descripcion for o in extraidas] == [
        "Ser designado como comité evaluador de los procesos de contratación",
        "Asesorar al ordenador del gasto en la evaluación de las propuestas, en "
        "coordinación con los comités estructuradores o evaluadores respectivos",
        "Elaborar los informes técnicos requeridos por la supervisión",
    ]


# ── 2. Section detection: OCR-mangled header ─────────────────────────────────


def test_flattened_header_is_found_or_whole_text_fallback_reaches_llm() -> None:
    """A header with the whitespace flattened by OCR must not yield empty chunks."""
    texto = (
        "Consideraciones preliminares del contrato objeto del presente documento.\n"
        "OBLIGACIONESESPECIFICASDELCONTRATISTA\n"
        "1. Diseñar el plan de trabajo del proyecto asignado.\n"
        "2. Ejecutar las actividades de campo según el cronograma aprobado.\n"
        "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
        "CLAUSULA TERCERA VALOR DEL CONTRATO: cien millones de pesos."
    )
    chunks = extract_obligation_sections(texto)
    assert chunks, "must never return an empty list — the LLM needs something to read"
    joined = " ".join(chunks)
    assert "Diseñar el plan de trabajo" in joined


def test_unmatchable_header_falls_back_to_bounded_whole_text_chunk() -> None:
    """When even the OCR-tolerant regex cannot match (letters themselves mangled),
    the whole cleaned text is sent as ONE bounded chunk instead of blind slices."""
    texto = (
        "0BL1GAC10NES ESPEC1F1CAS DEL C0NTRAT1STA\n"
        "1. Diseñar el plan de trabajo del proyecto asignado.\n"
        "2. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
    )
    chunks = extract_obligation_sections(texto)
    assert len(chunks) == 1
    assert chunks[0] == texto[:WHOLE_TEXT_FALLBACK_MAX_CHARS]
    assert "Diseñar el plan de trabajo" in chunks[0]


# ── 3. Regression: clean digital contract stays on the verbatim path ────────


@pytest.mark.asyncio
async def test_clean_contract_still_uses_verbatim_path_llm_never_called(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.adapters.llm as llm_module
    from app.services import document_service

    texto = (
        "CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA: "
        "1. Diseñar e implementar los módulos del sistema de información. "
        "2. Realizar las pruebas unitarias y de integración de los componentes. "
        "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato. "
        "CLÁUSULA TERCERA — VALOR DEL CONTRATO: treinta millones de pesos."
    )

    def _fail_get_llm(model=None):
        raise AssertionError("LLM must not be called for a clean digital contract")

    monkeypatch.setattr(llm_module, "get_llm", _fail_get_llm)

    db = AsyncMock()
    extraidas, avisos = await document_service.extraer_obligaciones_texto(texto, None, db)

    assert avisos == []
    assert [o.descripcion for o in extraidas] == [
        "Diseñar e implementar los módulos del sistema de información",
        "Realizar las pruebas unitarias y de integración de los componentes",
        "Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato",
    ]
