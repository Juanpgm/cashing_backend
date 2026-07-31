"""Tests for `clasificar_evidencia` — single-evidence-to-obligación classification
used by `evidencia_service.subir_evidencias_cuenta` (cuenta-scoped evidence upload).
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest
from app.agent.nodes.evidence_matcher import clasificar_evidencia

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
