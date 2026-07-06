"""Quality gate node — LLM judge validates extracted obligations (Phase 3)."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.quality_gate import QUALITY_GATE_SYSTEM, QUALITY_GATE_USER
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.quality_gate")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_gate_result(content: str) -> dict:
    match = _JSON_RE.search(content)
    if not match:
        return {"aprobado": False, "puntuacion": 0, "problemas": ["No se pudo parsear respuesta del LLM"], "sugerencias": []}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return {"aprobado": False, "puntuacion": 0, "problemas": ["JSON inválido del LLM"], "sugerencias": []}


async def quality_gate_node(state: AgentState) -> AgentState:
    """Validate quality of extracted obligations using LLM judge.

    Reads: obligaciones_extraidas, contrato_extraido
    Writes: quality_gate_passed, quality_issues, current_phase
    """
    obligaciones: list = state.get("obligaciones_extraidas") or []
    contrato: dict = state.get("contrato_extraido") or {}

    if not obligaciones:
        return {
            **state,
            "quality_gate_passed": False,
            "quality_issues": ["No hay obligaciones extraídas para evaluar"],
            "current_phase": "quality_gate",
        }

    objeto = contrato.get("objeto") or contrato.get("objeto_contrato") or "No especificado"
    n_obs = len(obligaciones)

    # Summarize obligations for the LLM (cap at 20 to stay within token budget)
    sample = obligaciones[:20]
    obs_json = json.dumps(sample, ensure_ascii=False, indent=2)[:6000]

    llm = get_llm(model="gemini/gemini-2.5-flash")
    messages = [
        LLMMessage(role="system", content=QUALITY_GATE_SYSTEM),
        LLMMessage(
            role="user",
            content=QUALITY_GATE_USER.replace("{n_obligaciones}", str(n_obs))
            .replace("{obligaciones_json}", obs_json)
            .replace("{objeto_contrato}", objeto[:500]),
        ),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=1024)
    except Exception as exc:
        await logger.awarning("quality_gate_llm_failed", error=str(exc))
        # Fail open: don't block the pipeline on a model error
        return {
            **state,
            "quality_gate_passed": True,
            "quality_issues": [f"Quality gate omitido: error de LLM ({exc})"],
            "current_phase": "quality_gate",
        }

    result = _parse_gate_result(resp.content)
    passed = bool(result.get("aprobado", False))
    issues: list[str] = result.get("problemas", [])
    score: int = result.get("puntuacion", 0)

    await logger.ainfo(
        "quality_gate_done",
        passed=passed,
        score=score,
        n_issues=len(issues),
        tokens=resp.total_tokens,
    )

    return {
        **state,
        "quality_gate_passed": passed,
        "quality_issues": issues,
        "current_phase": "quality_gate",
    }
