"""Tests for T6 (system prompt playbook) and T7 (ui_action for
`definir_requisitos_checklist`) — the model had no way to know
`definir_requisitos_checklist` must run right after `crear_cuenta_cobro`."""

from __future__ import annotations

import uuid

from app.schemas.checklist import ChecklistResumen, RequisitoCatalogoOut, RequisitoChecklistItem
from app.services import agent_chat_service
from app.tools.catalog.requisitos import DefinirRequisitosChecklistOutput


def test_system_prompt_mentions_definir_requisitos_checklist_playbook() -> None:
    prompt = agent_chat_service.SYSTEM_PROMPT_TEMPLATE
    assert "definir_requisitos_checklist" in prompt
    assert "crear_cuenta_cobro" in prompt


def test_system_prompt_requires_explicit_confirmation_before_radicar() -> None:
    """F5: at MAX_TOOL_ITERATIONS=20 the agent can now reach the last playbook
    step cold — `radicar_cuenta` must never fire without the user explicitly
    confirming first."""
    prompt = agent_chat_service.SYSTEM_PROMPT_TEMPLATE
    assert "radicar_cuenta" in prompt
    lowered = prompt.lower()
    assert "confirmación" in lowered or "confirmacion" in lowered
    # The rule must actually be attached to radicar_cuenta, not just exist
    # somewhere unrelated in the prompt.
    idx = lowered.find("radicar_cuenta")
    window = lowered[max(0, idx - 400) : idx + 400]
    assert "confirmación" in window or "confirmacion" in window


def test_system_prompt_keeps_existing_rules_intact() -> None:
    """T6 must be additive — no pre-existing rule text should be removed."""
    prompt = agent_chat_service.SYSTEM_PROMPT_TEMPLATE
    assert "NUNCA inventes, adivines ni escribas un placeholder" in prompt
    assert "El informe de supervisión SÍ se puede generar" in prompt


def test_definir_requisitos_checklist_has_ui_action_builder() -> None:
    assert "definir_requisitos_checklist" in agent_chat_service._UI_ACTION_BUILDERS


def test_definir_requisitos_checklist_ui_action_reuses_checklist_resumen_shape() -> None:
    cuenta_id = uuid.uuid4()
    requisito = RequisitoCatalogoOut(
        codigo="RUT",
        etiqueta="RUT vigente",
        obligatorio=True,
        solo_primera_cuenta=False,
        permite_autogen=False,
        orden=1,
    )
    item = RequisitoChecklistItem(requisito=requisito, estado="cargado")
    resumen = ChecklistResumen(total=1, cumplidos=1, pendientes=0, lista_pendientes=[], radicacion_lista=False)
    output = DefinirRequisitosChecklistOutput(
        cuenta_cobro_id=cuenta_id,
        modo="estandar",
        requisitos_custom=0,
        items=[item],
        resumen=resumen,
    )

    action = agent_chat_service._run_ui_action_builder("definir_requisitos_checklist", output)

    assert action is not None
    assert action.type == "checklist_resumen"
    assert action.payload["cuenta_id"] == str(cuenta_id)
    assert action.payload["cumplidos"] == 1
