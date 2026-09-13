"""Integration tests for phase-gating wired into `agent_chat_service.chat_with_tools`
(radicacion-sin-friccion 1.8).

Verifies the REAL per-iteration `tools=[...]` payload sent to `llm.complete` —
not just the pure `app.tools.phase_gating` unit behavior — including the
within-turn unlock (a tool becoming visible the iteration right after its
precondition tool succeeds) and the real measured prompt token-count drop.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import litellm
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.agent import LLMResponse, LLMToolCall
from app.services import agent_chat_service
from app.tools.llm_schema import to_openai_tools
from sqlalchemy.ext.asyncio import AsyncSession


class ToolCapturingLLM:
    """Like `ScriptedLLM` (test_agent_chat_service_iterations.py) but also
    records the `tools=[...]` kwarg offered on each call, as a set of names."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.tool_names_seen: list[set[str]] = []

    async def complete(
        self, messages: list[Any], *, tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> LLMResponse:
        self.call_count += 1
        offered = {t["function"]["name"] for t in (tools or [])}
        self.tool_names_seen.append(offered)
        if not self._responses:
            raise AssertionError("ToolCapturingLLM ran out of scripted responses")
        return self._responses.pop(0)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, scripted: ToolCapturingLLM) -> None:
    monkeypatch.setattr(agent_chat_service, "get_llm", lambda *args, **kwargs: scripted)


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"gating_{suffix}@example.com",
        nombre=f"Gating User {suffix}",
        cedula=f"8181{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"GATE-{suffix}",
        objeto="Objeto de prueba para gating",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"8181{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_fresh_turn_hides_cuenta_scoped_and_orthogonal_tools_on_first_iteration(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _contrato = await _make_user_with_contrato(db, "01")
    scripted = ToolCapturingLLM([LLMResponse(content="Hola, ¿en qué te ayudo?", model="fake")])
    _patch_llm(monkeypatch, scripted)

    await agent_chat_service.chat_with_tools(db, user, "Hola, quiero radicar", None, {})

    assert scripted.call_count == 1
    offered = scripted.tool_names_seen[0]
    assert "listar_contratos" in offered
    assert "crear_cuenta_cobro" in offered
    assert "radicar_cuenta" not in offered
    assert "resumen_checklist" not in offered
    assert "ingerir_plantilla_organismo" not in offered
    assert "listar_adiciones_contrato" not in offered


@pytest.mark.asyncio
async def test_cuenta_scoped_tools_unlock_the_iteration_right_after_crear_cuenta_cobro_succeeds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "02")
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 5, "anio": 2026}
    )
    scripted = ToolCapturingLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Cuenta creada, ¿seguimos?", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted)

    await agent_chat_service.chat_with_tools(db, user, "Creame la cuenta de mayo", None, {})

    assert scripted.call_count == 2
    # Iteration 0: no cuenta yet — checklist tool hidden.
    assert "definir_requisitos_checklist" not in scripted.tool_names_seen[0]
    # Iteration 1 (right after crear_cuenta_cobro succeeded): now visible.
    assert "definir_requisitos_checklist" in scripted.tool_names_seen[1]
    assert "radicar_cuenta" in scripted.tool_names_seen[1]


@pytest.mark.asyncio
async def test_continuation_turn_with_cuenta_recap_shows_cuenta_scoped_tools_from_iteration_zero(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, "03")

    # Turn 1: create the cuenta so a recap gets persisted.
    tool_call = LLMToolCall(
        id="call_1", name="crear_cuenta_cobro", arguments={"contrato_id": str(contrato.id), "mes": 6, "anio": 2026}
    )
    scripted_1 = ToolCapturingLLM(
        [
            LLMResponse(content="", model="fake", tool_calls=[tool_call]),
            LLMResponse(content="Cuenta creada.", model="fake"),
        ]
    )
    _patch_llm(monkeypatch, scripted_1)
    result_1 = await agent_chat_service.chat_with_tools(db, user, "Creame la cuenta de junio", None, {})

    # Turn 2 (continuation, same session_id): the recap alone must be enough.
    scripted_2 = ToolCapturingLLM([LLMResponse(content="Dale.", model="fake")])
    _patch_llm(monkeypatch, scripted_2)
    await agent_chat_service.chat_with_tools(db, user, "Segui con el checklist", result_1.session_id, {})

    assert "definir_requisitos_checklist" in scripted_2.tool_names_seen[0]
    assert "resumen_checklist" in scripted_2.tool_names_seen[0]


@pytest.mark.asyncio
async def test_keyword_in_message_reveals_orthogonal_tool_on_first_iteration(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, _contrato = await _make_user_with_contrato(db, "04")
    scripted = ToolCapturingLLM([LLMResponse(content="Contame más sobre la plantilla.", model="fake")])
    _patch_llm(monkeypatch, scripted)

    await agent_chat_service.chat_with_tools(db, user, "Quiero cambiar mi plantilla de PDF", None, {})

    assert "ingerir_plantilla_organismo" in scripted.tool_names_seen[0]
    assert "obtener_plantilla_organismo" in scripted.tool_names_seen[0]
    # Unrelated orthogonal group stays hidden.
    assert "listar_adiciones_contrato" not in scripted.tool_names_seen[0]


def test_representative_fresh_turn_prompt_tokens_drop_below_6000() -> None:
    """Real measured token count (litellm.token_counter, no mocks) for the
    representative conversation state chosen for this slice's audit: the
    FIRST message of a brand-new radicación conversation — no prior recap, no
    tool calls yet. This is the highest-leverage point for phase-gating (see
    slice 1.8 report) and the state the plan's explicit "<6000/iteration"
    target is asserted against.
    """
    from app.tools import phase_gating

    all_tools = to_openai_tools()
    message = "Ya cargué el RUT y la cédula, ¿qué me falta para radicar la cuenta de cobro de este mes?"
    hidden = phase_gating.hidden_tool_names(message=message, cuenta_known=False, called_tool_names=frozenset())
    gated_tools = phase_gating.filter_openai_tools(all_tools, hidden)

    system_prompt = agent_chat_service._build_system_prompt([], None, None)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    baseline_tokens = litellm.token_counter(model="gpt-3.5-turbo", messages=messages, tools=all_tools)
    gated_tokens = litellm.token_counter(model="gpt-3.5-turbo", messages=messages, tools=gated_tools)

    assert baseline_tokens > 6000, "sanity check: baseline (ungated) should be well above the target"
    assert gated_tokens < 6000, f"expected <6000 tokens for the fresh-turn representative state, got {gated_tokens}"
