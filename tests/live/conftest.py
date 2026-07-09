"""Fixtures for the LIVE-LLM test suite — hits a real local Ollama server.

These tests are opt-in (marked ``live_llm``) and deselected by default via the
project-wide ``addopts = -m "not live_llm"`` in ``pyproject.toml``. To run them:

    uv run python -m pytest tests/live -m live_llm -q

(``-m live_llm`` overrides the default ``addopts`` deselection for this invocation.)

Requirements to actually exercise the LLM calls:
    - Ollama running locally at http://localhost:11434 (OLLAMA_BASE_URL)
    - The `llama3.1:8b` model pulled (`ollama pull llama3.1:8b`)

If Ollama is unreachable or the model is missing, the whole directory is
skipped gracefully (see ``ollama_available`` below) instead of failing.

Every test in this directory automatically gets:
    - ``ollama_available`` (session, autouse): skips the whole run if the
      local Ollama server or the target model isn't there.
    - ``live_llm_settings`` (function, autouse): forces every model-bearing
      Settings field (LLM_DEFAULT_MODEL, LLM_FALLBACK_MODEL, LLM_LOCAL_MODEL,
      LLM_EXTRACTION_MODEL, LLM_MULTIMODAL_MODEL) to "ollama/llama3.1:8b" and
      blanks cloud provider API keys, so these tests are 100% local — no
      Gemini/Groq/Mistral/OpenAI call is ever attempted, even by nodes (like
      quality_gate_node) that hardcode a cloud model string.

Runtime expectations: llama3.1:8b on a dev machine can take 10-60s per
completion. Keep max_tokens small in new tests to bound latency.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest

OLLAMA_BASE_URL = "http://localhost:11434"
TARGET_MODEL = "llama3.1:8b"
FORCED_MODEL = "ollama/llama3.1:8b"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the `live_llm` marker to every test collected under tests/live."""
    for item in items:
        if "tests/live" in str(item.fspath).replace("\\", "/") or "tests\\live" in str(item.fspath):
            item.add_marker(pytest.mark.live_llm)


@pytest.fixture(scope="session", autouse=True)
def ollama_available() -> None:
    """Skip the entire live-LLM suite if Ollama or the target model isn't available."""
    try:
        resp = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any connectivity issue means "not reachable"
        pytest.skip(f"Ollama not reachable at {OLLAMA_BASE_URL}: {exc}")
        return

    data = resp.json()
    model_names = {m.get("name", "") for m in data.get("models", [])}
    # Ollama tags include the ":tag" suffix (e.g. "llama3.1:8b") — match exactly or by prefix.
    if not any(name == TARGET_MODEL or name.startswith(f"{TARGET_MODEL}") for name in model_names):
        pytest.skip(f"Model '{TARGET_MODEL}' not found in Ollama tags: {sorted(model_names)}")


@pytest.fixture(autouse=True)
def live_llm_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Force every LLM model field to the local Ollama model; blank cloud API keys.

    This guarantees the live suite NEVER reaches a cloud provider, even for nodes
    that hardcode a cloud model string (e.g. quality_gate_node uses
    "gemini/gemini-2.5-flash" literally, not from settings) — for that node we
    monkeypatch its imported `get_llm` reference directly so the hardcoded model
    argument is ignored.
    """
    from app.adapters.llm import get_llm as real_get_llm
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_DEFAULT_MODEL", FORCED_MODEL)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", FORCED_MODEL)
    monkeypatch.setattr(settings, "LLM_LOCAL_MODEL", FORCED_MODEL)
    monkeypatch.setattr(settings, "LLM_EXTRACTION_MODEL", FORCED_MODEL)
    monkeypatch.setattr(settings, "LLM_MULTIMODAL_MODEL", FORCED_MODEL)
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", OLLAMA_BASE_URL)

    # Defense in depth: even if a model string slips through, no cloud key is
    # available so litellm fails fast on auth instead of silently succeeding.
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "MISTRAL_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    # quality_gate_node hardcodes get_llm(model="gemini/gemini-2.5-flash") — settings
    # patching above can't touch that literal, so force its imported `get_llm` name.
    import app.agent.nodes.quality_gate as quality_gate_module

    def _forced_ollama_get_llm(model: str | None = None) -> object:  # noqa: ARG001
        return real_get_llm(FORCED_MODEL)

    monkeypatch.setattr(quality_gate_module, "get_llm", _forced_ollama_get_llm)

    yield
