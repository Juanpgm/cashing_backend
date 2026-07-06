"""Resilient vision model chain for multimodal contract extraction.

Regression cover for the bug where a single hardcoded vision model
(``fallback=False``) silently broke the whole vision path the moment the
provider decommissioned it — the user saw "no data extracted" with no recovery.
The chain must skip decommissioned and key-less models and fall through to a
working one.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.schemas.agent import ContratoExtractionResult, LLMResponse, ObligacionItemLLM
from app.services import document_service


def _set_keys(monkeypatch: pytest.MonkeyPatch, *, gemini: str = "", groq: str = "", openai: str = "") -> None:
    monkeypatch.setattr(document_service.settings, "GEMINI_API_KEY", gemini)
    monkeypatch.setattr(document_service.settings, "GROQ_API_KEY", groq)
    monkeypatch.setattr(document_service.settings, "OPENAI_API_KEY", openai)


def test_chain_skips_decommissioned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured-but-decommissioned model is dropped; fallbacks remain."""
    monkeypatch.setattr(
        document_service.settings, "LLM_MULTIMODAL_MODEL", "groq/llama-3.2-11b-vision-preview"
    )
    _set_keys(monkeypatch, gemini="g", groq="k")

    chain = document_service._vision_model_chain()

    assert "groq/llama-3.2-11b-vision-preview" not in chain
    assert "gemini/gemini-2.5-flash-lite" in chain
    assert "groq/meta-llama/llama-4-scout-17b-16e-instruct" in chain


def test_chain_filters_models_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Models whose provider key is missing are never tried."""
    monkeypatch.setattr(
        document_service.settings, "LLM_MULTIMODAL_MODEL", "groq/meta-llama/llama-4-scout-17b-16e-instruct"
    )
    _set_keys(monkeypatch, gemini="", groq="k")  # no Gemini key

    chain = document_service._vision_model_chain()

    assert chain == ["groq/meta-llama/llama-4-scout-17b-16e-instruct"]
    assert all(not m.startswith("gemini/") for m in chain)


def test_chain_empty_when_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """No usable provider → empty chain (caller surfaces an actionable aviso)."""
    monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-2.5-flash-lite")
    _set_keys(monkeypatch, gemini="", groq="")

    assert document_service._vision_model_chain() == []


def test_chain_dedupes_configured_equal_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuring a model that is also a fallback must not duplicate it."""
    monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-2.5-flash-lite")
    _set_keys(monkeypatch, gemini="g", groq="k")

    chain = document_service._vision_model_chain()

    assert chain.count("gemini/gemini-2.5-flash-lite") == 1


class _FakeLLM:
    """LLM stub: fails for the first model, succeeds for the rest."""

    def __init__(self, model: str, fail_models: set[str], payload: ContratoExtractionResult) -> None:
        self._model = model
        self._fail_models = fail_models
        self._payload = payload

    async def complete(self, messages: list[Any], **kwargs: Any) -> LLMResponse:
        if self._model in self._fail_models:
            raise RuntimeError(f"model {self._model} decommissioned")
        return LLMResponse(content=self._payload.model_dump_json(), model=self._model)


@pytest.mark.asyncio
async def test_multimodal_recovers_when_first_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """First model raises → loop falls through to the next and returns its result."""
    monkeypatch.setattr(
        document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-2.5-flash-lite"
    )
    _set_keys(monkeypatch, gemini="g", groq="k")

    payload = ContratoExtractionResult(
        obligaciones=[ObligacionItemLLM(descripcion="Elaborar informes mensuales", tipo="especifica", etiqueta="1")],
        transcripcion="TEXTO DEL CONTRATO",
    )
    failing = {"gemini/gemini-2.5-flash-lite"}  # simulate Gemini quota depleted

    import app.adapters.llm as llm_module

    monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _FakeLLM(model, failing, payload))

    # Minimal valid PNG so is_multimodal_supported passes and parts build.
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )

    result = await document_service._extraer_contrato_multimodal(png, "image/png")

    assert result is not None
    assert len(result.obligaciones) == 1
    assert result.obligaciones[0].descripcion == "Elaborar informes mensuales"


@pytest.mark.asyncio
async def test_multimodal_returns_none_when_all_models_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every model failing returns None so the caller can add an aviso."""
    monkeypatch.setattr(
        document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-2.5-flash-lite"
    )
    _set_keys(monkeypatch, gemini="g", groq="k")

    payload = ContratoExtractionResult()
    all_models = {
        "gemini/gemini-2.5-flash-lite",
        "groq/meta-llama/llama-4-scout-17b-16e-instruct",
    }

    import app.adapters.llm as llm_module

    monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _FakeLLM(model, all_models, payload))

    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )

    assert await document_service._extraer_contrato_multimodal(png, "image/png") is None
