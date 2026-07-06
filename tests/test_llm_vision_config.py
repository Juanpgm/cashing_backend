"""Vision model configuration: local (Ollama, rasterized) vs cloud (Gemini, native PDF).

Covers the content-part builder that adapts the payload to the target model, and
the LiteLLM adapter passing the provider API key from settings (LiteLLM otherwise
only reads os.environ) plus the no-fallback path used for vision calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from app.adapters.llm import litellm_adapter as mod
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.agent.tools.multimodal_parser import build_multimodal_content_parts, supports_native_pdf
from app.schemas.agent import LLMMessage


def _one_page_pdf(pages: int = 1) -> bytes:
    import fitz  # PyMuPDF

    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    data: bytes = doc.tobytes()
    doc.close()
    return data


# ── Content-part builder ────────────────────────────────────────────────────


def test_supports_native_pdf_by_provider() -> None:
    assert supports_native_pdf("gemini/gemini-2.5-flash") is True
    assert supports_native_pdf("gpt-4o") is True
    assert supports_native_pdf("ollama/llama3.2-vision") is False
    assert supports_native_pdf(None) is True  # cloud default


def test_image_becomes_single_image_part() -> None:
    parts = build_multimodal_content_parts(b"fake-png-bytes", "image/png", "ollama/llava")
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"


def test_pdf_for_cloud_model_is_single_file_part() -> None:
    parts = build_multimodal_content_parts(_one_page_pdf(), "application/pdf", "gemini/gemini-2.5-flash")
    assert len(parts) == 1
    assert parts[0]["type"] == "file"


def test_pdf_for_local_model_is_rasterized_to_image_parts() -> None:
    parts = build_multimodal_content_parts(
        _one_page_pdf(pages=2), "application/pdf", "ollama/llama3.2-vision", max_pdf_pages=8, dpi=72
    )
    assert len(parts) == 2
    assert all(p["type"] == "image_url" for p in parts)


# ── LiteLLM adapter: API key from settings + no-fallback path ───────────────


def test_api_key_resolved_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.settings, "GEMINI_API_KEY", "g-key")
    monkeypatch.setattr(mod.settings, "GROQ_API_KEY", "q-key")
    monkeypatch.setattr(mod.settings, "OPENAI_API_KEY", "o-key")
    assert LiteLLMAdapter._api_key_for("gemini/gemini-2.5-flash") == "g-key"
    assert LiteLLMAdapter._api_key_for("groq/llama3-8b-8192") == "q-key"
    assert LiteLLMAdapter._api_key_for("gpt-4o") == "o-key"
    assert LiteLLMAdapter._api_key_for("ollama/llama3.1:8b") is None


@pytest.mark.asyncio
async def test_complete_passes_api_key_and_skips_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    monkeypatch.setattr(mod.settings, "GEMINI_API_KEY", "secret-gemini")
    calls: list[dict[str, Any]] = []

    async def _fake_acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    adapter = LiteLLMAdapter()
    await adapter.complete(
        [LLMMessage(role="user", content="hi")],
        model="gemini/gemini-2.5-flash",
        fallback=False,
    )

    assert len(calls) == 1  # only the requested model, no fallback chain
    assert calls[0]["model"] == "gemini/gemini-2.5-flash"
    assert calls[0]["api_key"] == "secret-gemini"
