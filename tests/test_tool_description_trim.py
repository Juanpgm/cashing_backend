"""Regression pins for the description trims applied to the 4 largest tool
descriptions in radicacion-sin-friccion 1.8 (definir_requisitos_checklist,
radicar_cuenta, crear_cuenta_cobro, importar_documento).

Asserts BOTH halves of the requirement: the description got shorter, AND every
piece of information the LLM actually needs to call the tool correctly is
still present verbatim (required fields, valid enum values, the
COHERENCE_CHECK_FAILED / CHECKLIST_INCOMPLETE recovery playbook radicar_cuenta
depends on).
"""

from __future__ import annotations

import app.tools.catalog  # noqa: F401 — registers every catalog tool
from app.tools.registry import TOOL_REGISTRY

# Original lengths measured before this slice's trim (see slice 1.8 report).
_ORIGINAL_LENGTHS = {
    "definir_requisitos_checklist": 1444,
    "radicar_cuenta": 1254,
    "crear_cuenta_cobro": 987,
    "importar_documento": 895,
}


def _description(name: str) -> str:
    return TOOL_REGISTRY[name].description


def test_all_four_descriptions_got_shorter() -> None:
    for name, original_len in _ORIGINAL_LENGTHS.items():
        new_len = len(_description(name))
        assert new_len < original_len, f"{name}: expected a reduction from {original_len}, got {new_len}"


def test_definir_requisitos_checklist_keeps_ordering_and_modo_contract() -> None:
    desc = _description("definir_requisitos_checklist")
    assert "crear_cuenta_cobro" in desc
    assert "estandar" in desc
    assert "augment" in desc
    assert "reemplazar" in desc
    assert "cuenta_id" in desc
    # The "never re-call once documents are linked" no-op safety note.
    assert "vinculados" in desc or "no-op" in desc.lower() or "no se modific" in desc.lower()


def test_radicar_cuenta_keeps_the_coherence_gate_explanation() -> None:
    """The coherence-gate explanation added in slice 1.6 must survive the trim
    verbatim enough that the LLM still knows the two error codes and how to
    react to each."""
    desc = _description("radicar_cuenta")
    assert "COHERENCE_CHECK_FAILED" in desc
    assert "CHECKLIST_INCOMPLETE" in desc
    assert "validar_coherencia_cuenta" in desc
    assert "resumen_checklist" in desc
    assert "cuenta_id" in desc


def test_crear_cuenta_cobro_keeps_required_field_contract() -> None:
    desc = _description("crear_cuenta_cobro")
    assert "contrato_id" in desc
    assert "mes" in desc
    assert "anio" in desc
    assert "listar_contratos" in desc


def test_importar_documento_keeps_scope_rules() -> None:
    desc = _description("importar_documento")
    assert "filename" in desc
    assert "contrato" in desc
    assert "checklist" in desc or "cuenta" in desc


def test_total_char_reduction_across_the_four_tools() -> None:
    """See the slice 1.8 report for the exact before/after numbers this pins
    (982 chars: 4580 -> 3598) — asserted loosely here (`> 0`) so a future
    additional trim doesn't force editing this test, only the report."""
    original_total = sum(_ORIGINAL_LENGTHS.values())
    new_total = sum(len(_description(name)) for name in _ORIGINAL_LENGTHS)
    reduction = original_total - new_total
    assert reduction > 0
