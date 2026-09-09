"""Tests for `clasificar_evidencia` — single-evidence-to-obligación classification
used by `evidencia_service.subir_evidencias_cuenta` (cuenta-scoped evidence upload).
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from app.agent.nodes.evidence_matcher import _clasificar_via_llm, clasificar_evidencia

pytestmark = pytest.mark.asyncio


@dataclass
class _FakeObligacion:
    """Duck-typed stand-in for `app.models.obligacion.Obligacion` — the module under
    test only accesses `.descripcion`, so a real ORM instance isn't needed."""

    id: str
    descripcion: str


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


async def test_clasificar_evidencia_sin_obligaciones_retorna_none() -> None:
    llm = AsyncMock()
    result = await clasificar_evidencia("cualquier texto de evidencia", [], llm=llm)
    assert result is None
    llm.complete.assert_not_called()


async def test_clasificar_evidencia_un_candidato_sin_llamar_llm() -> None:
    ob = _FakeObligacion(id="ob1", descripcion="realizar informes tecnicos mensuales consultoria")
    llm = AsyncMock()
    result = await clasificar_evidencia("informe tecnico mensual de consultoria entregado", [ob], llm=llm)
    assert result is ob
    llm.complete.assert_not_called()


async def test_clasificar_evidencia_dos_candidatos_usa_llm() -> None:
    ob1 = _FakeObligacion(id="ob1", descripcion="realizar informes tecnicos mensuales consultoria")
    ob2 = _FakeObligacion(id="ob2", descripcion="realizar informes administrativos mensuales consultoria")
    llm = AsyncMock()
    llm.complete.return_value = _FakeResp("2")

    result = await clasificar_evidencia("informe mensual administrativo de consultoria entregado", [ob1, ob2], llm=llm)

    llm.complete.assert_called_once()
    assert result is ob2


async def test_clasificar_evidencia_llm_falla_usa_mejor_keyword_score() -> None:
    # ob1 has strictly higher keyword overlap with the evidence text than ob2.
    ob1 = _FakeObligacion(id="ob1", descripcion="informes tecnicos mensuales consultoria asesoria")
    ob2 = _FakeObligacion(id="ob2", descripcion="informes tecnicos generales consultoria")
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("llm down")

    result = await clasificar_evidencia(
        "informes tecnicos mensuales consultoria asesoria entregados", [ob1, ob2], llm=llm
    )

    assert result is ob1


async def test_clasificar_evidencia_cero_candidatos_llm_encuentra_match_semantico() -> None:
    # Source code has zero keyword overlap with the Spanish obligación wording,
    # but the LLM can still judge it semantically relevant.
    ob = _FakeObligacion(id="ob1", descripcion="desarrollar modulo de notificaciones por correo electronico")
    llm = AsyncMock()
    llm.complete.return_value = _FakeResp("1")

    result = await clasificar_evidencia("def send_email(to, subject, body): ...", [ob], llm=llm)

    llm.complete.assert_called_once()
    assert result is ob


async def test_clasificar_evidencia_cero_candidatos_llm_responde_ninguna() -> None:
    ob = _FakeObligacion(id="ob1", descripcion="desarrollar modulo de notificaciones por correo electronico")
    llm = AsyncMock()
    llm.complete.return_value = _FakeResp("0")

    result = await clasificar_evidencia("import os\nimport sys\n", [ob], llm=llm)

    assert result is None


async def test_clasificar_evidencia_cero_candidatos_llm_falla_retorna_none() -> None:
    # Unlike the >=1-keyword-candidate path, an LLM failure here has NO fallback
    # signal to fall back on — it must not fabricate a match.
    ob = _FakeObligacion(id="ob1", descripcion="desarrollar modulo de notificaciones por correo electronico")
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("llm down")

    result = await clasificar_evidencia("import os\nimport sys\n", [ob], llm=llm)

    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# groq/llama-3.1-8b-instant decommissioning fix: the new model
# (groq/openai/gpt-oss-20b) is a reasoning model — reasoning_effort must be
# forwarded and max_tokens must have real headroom above the bare minimum that
# empirically works, or the model returns EMPTY content (finish_reason=length)
# instead of raising, which the adapter's fallback-on-exception can't catch.
# ─────────────────────────────────────────────────────────────────────────────


async def test_clasificar_evidencia_sin_llm_inyectado_usa_modelo_groq_vigente() -> None:
    """When no llm is injected, clasificar_evidencia must build one against the
    live Groq model — groq/llama-3.1-8b-instant no longer exists in Groq's
    catalog (decommissioned)."""
    from app.agent.nodes import evidence_matcher as mod

    ob1 = _FakeObligacion(id="ob1", descripcion="realizar informes tecnicos mensuales consultoria")
    ob2 = _FakeObligacion(id="ob2", descripcion="realizar informes administrativos mensuales consultoria")
    fake_llm = AsyncMock()
    fake_llm.complete.return_value = _FakeResp("2")

    with patch.object(mod, "get_llm", return_value=fake_llm) as mock_get_llm:
        await clasificar_evidencia("informe mensual administrativo de consultoria entregado", [ob1, ob2])

    mock_get_llm.assert_called_once_with(model="groq/openai/gpt-oss-20b")


async def test_clasificar_via_llm_envia_reasoning_effort_y_max_tokens_con_margen() -> None:
    ob1 = _FakeObligacion(id="ob1", descripcion="realizar informes tecnicos mensuales consultoria")
    ob2 = _FakeObligacion(id="ob2", descripcion="realizar informes administrativos mensuales consultoria")
    llm = AsyncMock()
    llm.complete.return_value = _FakeResp("2")

    await _clasificar_via_llm("informe mensual administrativo", [ob1, ob2], llm, fallback=None)

    kwargs = llm.complete.call_args.kwargs
    assert kwargs["reasoning_effort"] == "low"
    # Old budget (8) empirically returned EMPTY content for this reasoning model —
    # real headroom above the bare-minimum-that-worked (~13 reasoning tokens + a
    # 1-digit answer in manual verification) is required, not just enough to pass.
    assert kwargs["max_tokens"] >= 40


async def test_clasificar_via_llm_contenido_vacio_degrada_a_fallback() -> None:
    """Reproduces the exact bug being fixed: at too-small a max_tokens budget the
    reasoning model returns HTTP 200 with EMPTY content (finish_reason=length) —
    no exception, so the adapter's fallback-on-exception never triggers. The
    caller must still degrade gracefully to `fallback` instead of crashing on
    the unparseable empty answer."""
    ob = _FakeObligacion(id="ob1", descripcion="realizar informes tecnicos mensuales consultoria")
    llm = AsyncMock()
    llm.complete.return_value = _FakeResp("")  # empty content — the reproduced bug

    result = await _clasificar_via_llm("informe mensual", [ob], llm, fallback=ob)

    assert result is ob
