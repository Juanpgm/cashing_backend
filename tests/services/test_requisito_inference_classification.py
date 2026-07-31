"""Tests for archive-member classification (formato-replicacion-inteligente,
slice 1, tasks 1.2-1.3): `requisito_inference_service.clasificar_miembros_archivo`.
"""

from __future__ import annotations

import json

import pytest
from app.schemas.agent import LLMResponse
from app.services import requisito_inference_service as svc


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, temperature=0.0, max_tokens=4096, response_format=None, **kwargs) -> LLMResponse:
        return LLMResponse(content=self._content, model="fake/test-model", total_tokens=10)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(content), raising=True)


async def test_confident_proposal_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    respuesta = json.dumps(
        {"miembros": [{"filename": "informe.docx", "tipo_documento": "informe_actividades", "confianza": 0.9}]}
    )
    _patch_llm(monkeypatch, respuesta)

    propuestas, avisos = await svc.clasificar_miembros_archivo([("informe.docx", b"contenido de informe")])

    assert avisos == []
    assert propuestas["informe.docx"].tipo_documento == "informe_actividades"
    assert propuestas["informe.docx"].confianza == 0.9


async def test_low_confidence_maps_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    respuesta = json.dumps(
        {"miembros": [{"filename": "raro.docx", "tipo_documento": "cuenta_cobro", "confianza": 0.2}]}
    )
    _patch_llm(monkeypatch, respuesta)

    propuestas, _avisos = await svc.clasificar_miembros_archivo([("raro.docx", b"contenido ambiguo")])

    assert propuestas["raro.docx"].tipo_documento is None


async def test_executable_member_yields_no_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threat matrix: executable-file classification — `.exe`/`.sh` members
    are never sent to the LLM and never proposed, regardless of its output."""
    _patch_llm(monkeypatch, json.dumps({"miembros": []}))

    propuestas, avisos = await svc.clasificar_miembros_archivo(
        [("virus.exe", b"MZ-fake-binary"), ("script.sh", b"#!/bin/sh\necho hi")]
    )

    assert propuestas == {}
    assert any("virus.exe" in a for a in avisos)
    assert any("script.sh" in a for a in avisos)


async def test_llm_failure_still_proposes_every_eligible_member_with_null_tipo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RES-001: a transient LLM failure must not strand the archive — every
    eligible member is still returned (for upload), with `tipo_documento=None`
    for manual pick, plus an aviso explaining why."""

    class _FailingLLM:
        async def complete(self, *_a, **_kw) -> LLMResponse:
            raise RuntimeError("llm down")

    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FailingLLM(), raising=True)

    propuestas, avisos = await svc.clasificar_miembros_archivo(
        [("informe.docx", b"contenido"), ("ds.xlsx", b"otro contenido")]
    )

    assert set(propuestas) == {"informe.docx", "ds.xlsx"}
    assert all(p.tipo_documento is None for p in propuestas.values())
    assert avisos
