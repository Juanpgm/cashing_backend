"""Unit tests for `app.tools.phase_gating` (radicacion-sin-friccion 1.8).

Pure-function tests — no DB, no LLM. Edge cases covered explicitly per the
project's testing standard: boundary/empty inputs, malformed recap text, and
"already used" persistence across a keyword no longer being repeated.
"""

from __future__ import annotations

import app.tools.catalog  # noqa: F401 — registers every catalog tool
from app.tools import phase_gating
from app.tools.registry import TOOL_REGISTRY


def test_cuenta_scoped_tool_names_includes_known_cuenta_scoped_tools() -> None:
    scoped = phase_gating.cuenta_scoped_tool_names()
    # A representative sample of tools whose input model requires cuenta_id or
    # cuenta_cobro_id — real assertions on names, not just a count.
    for name in (
        "resumen_checklist",
        "definir_requisitos_checklist",
        "radicar_cuenta",
        "preparar_radicacion",
        "generar_informe_actividades",
        "generar_informe_supervision",
        "marcar_requisito",
        "subir_evidencias_desde_chat",
    ):
        assert name in scoped, f"expected {name} to be cuenta-scoped"


def test_cuenta_scoped_tool_names_excludes_discovery_and_contract_level_tools() -> None:
    scoped = phase_gating.cuenta_scoped_tool_names()
    for name in (
        "listar_contratos",
        "listar_cuentas_cobro",
        "crear_cuenta_cobro",
        "importar_contrato_secop",
        "importar_documento",
        "inferir_requisitos_estructurados",
        "buscar_secop_por_cedula",
        "explorar_documentos_secop_agentico",
    ):
        assert name not in scoped, f"expected {name} to NOT be cuenta-scoped"


def test_cuenta_scoped_tool_names_is_self_maintaining_against_the_live_registry() -> None:
    """Every tool actually registered whose schema requires cuenta_id/cuenta_cobro_id
    must appear — this is a drift guard, not a hand-maintained list."""
    scoped = phase_gating.cuenta_scoped_tool_names()
    for name, spec in TOOL_REGISTRY.items():
        fields = spec.input_model.model_fields
        requires_cuenta = any(f in fields and fields[f].is_required() for f in ("cuenta_id", "cuenta_cobro_id"))
        assert (name in scoped) == requires_cuenta, f"drift detected for {name}"


def test_cuenta_known_from_recap_none_is_false() -> None:
    assert phase_gating.cuenta_known_from_recap(None) is False


def test_cuenta_known_from_recap_empty_string_is_false() -> None:
    assert phase_gating.cuenta_known_from_recap("") is False


def test_cuenta_known_from_recap_true_on_crear_cuenta_cobro_marker() -> None:
    recap = "[agent-recap] crear_cuenta_cobro:ok id=11111111-1111-1111-1111-111111111111"
    assert phase_gating.cuenta_known_from_recap(recap) is True


def test_cuenta_known_from_recap_true_on_a_later_cuenta_scoped_marker_even_without_crear_cuenta_cobro() -> None:
    """Simulates a long conversation where the recap budget dropped the original
    crear_cuenta_cobro entry but kept a more recent cuenta-scoped call."""
    recap = "[agent-recap] generar_informe_supervision:ok cuenta_id=22222222-2222-2222-2222-222222222222"
    assert phase_gating.cuenta_known_from_recap(recap) is True


def test_cuenta_known_from_recap_false_when_only_discovery_tools_ran() -> None:
    recap = "[agent-recap] listar_contratos:ok | importar_contrato_secop:ok contrato_id=1"
    assert phase_gating.cuenta_known_from_recap(recap) is False


def test_cuenta_known_from_recap_false_on_failed_call_only() -> None:
    recap = "[agent-recap] crear_cuenta_cobro:error"
    assert phase_gating.cuenta_known_from_recap(recap) is False


def test_cuenta_known_from_recap_true_on_cuenta_id_evidence_from_a_non_scoped_tool_name() -> None:
    """BLOCKER 1 regression: `listar_cuentas_cobro` is neither `crear_cuenta_cobro`
    nor a cuenta-SCOPED tool (its input never requires a cuenta id), but its
    output can still reveal a real one — the marker/tool-name allowlist alone
    must not be the only signal."""
    recap = "[agent-recap] listar_cuentas_cobro:ok cuenta_id=33333333-3333-3333-3333-333333333333"
    assert phase_gating.cuenta_known_from_recap(recap) is True


def test_cuenta_known_from_recap_true_on_cuenta_cobro_id_field_name_variant() -> None:
    recap = "[agent-recap] some_future_tool:ok cuenta_cobro_id=44444444-4444-4444-4444-444444444444"
    assert phase_gating.cuenta_known_from_recap(recap) is True


def test_find_cuenta_ids_none_dumped_returns_empty() -> None:
    assert phase_gating.find_cuenta_ids(None) == []
    assert phase_gating.find_cuenta_ids({}) == []


def test_find_cuenta_ids_finds_top_level_cuenta_record_shape() -> None:
    """Mirrors `crear_cuenta_cobro`'s own dumped output shape — a top-level
    CuentaCobroResponse-like record with `id` + the CuentaCobro field signature."""
    dumped = {
        "id": "11111111-1111-1111-1111-111111111111",
        "contrato_id": "22222222-2222-2222-2222-222222222222",
        "mes": 5,
        "anio": 2026,
        "estado": "borrador",
        "valor": "1000000.00",
    }
    assert phase_gating.find_cuenta_ids(dumped) == ["11111111-1111-1111-1111-111111111111"]


def test_find_cuenta_ids_finds_nested_list_cuenta_record_shape() -> None:
    """Mirrors `listar_cuentas_cobro`'s dumped output — a top-level `cuentas` list
    of CuentaCobroResumen-shaped items, the exact BLOCKER 1 reproduction shape."""
    dumped = {
        "cuentas": [
            {
                "id": "55555555-5555-5555-5555-555555555555",
                "contrato_id": "66666666-6666-6666-6666-666666666666",
                "mes": 8,
                "anio": 2026,
                "estado": "radicada",
                "valor": "2000000.00",
            }
        ]
    }
    assert phase_gating.find_cuenta_ids(dumped) == ["55555555-5555-5555-5555-555555555555"]


def test_find_cuenta_ids_empty_list_yields_no_evidence() -> None:
    """A `listar_cuentas_cobro` call that legitimately found nothing yet must
    NOT unlock cuenta-scoped tools — an empty list is not evidence."""
    assert phase_gating.find_cuenta_ids({"cuentas": []}) == []


def test_find_cuenta_ids_ignores_unrelated_id_shaped_records() -> None:
    """`listar_contratos` also returns `id`-bearing dict items, but they are
    NOT cuenta records (no contrato_id/mes/anio/estado/valor signature) — must
    never be mistaken for cuenta evidence."""
    dumped = {
        "contratos": [
            {"id": "77777777-7777-7777-7777-777777777777", "numero_contrato": "C-1", "objeto": "x"},
        ]
    }
    assert phase_gating.find_cuenta_ids(dumped) == []


def test_called_tool_names_from_recap_includes_failed_calls_for_retry_visibility() -> None:
    """CRITICAL 3: a FAILED orthogonal-gated call is even more reason to keep the
    tool visible for a retry on the very next iteration."""
    recap = "[agent-recap] registrar_adicion_contrato:error"
    assert phase_gating.called_tool_names_from_recap(recap) == frozenset({"registrar_adicion_contrato"})


def test_called_tool_names_from_recap_empty_when_no_recap() -> None:
    assert phase_gating.called_tool_names_from_recap(None) == frozenset()
    assert phase_gating.called_tool_names_from_recap("") == frozenset()


def test_called_tool_names_from_recap_finds_orthogonal_tool_marker() -> None:
    recap = "[agent-recap] ingerir_plantilla_organismo:ok"
    assert phase_gating.called_tool_names_from_recap(recap) == frozenset({"ingerir_plantilla_organismo"})


def test_hidden_tool_names_hides_cuenta_scoped_when_cuenta_unknown() -> None:
    hidden = phase_gating.hidden_tool_names(message="hola", cuenta_known=False, called_tool_names=frozenset())
    assert "radicar_cuenta" in hidden
    assert "resumen_checklist" in hidden
    assert "listar_contratos" not in hidden


def test_hidden_tool_names_reveals_cuenta_scoped_when_cuenta_known() -> None:
    hidden = phase_gating.hidden_tool_names(message="hola", cuenta_known=True, called_tool_names=frozenset())
    assert "radicar_cuenta" not in hidden
    assert "resumen_checklist" not in hidden


def test_hidden_tool_names_hides_orthogonal_tools_by_default() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="quiero radicar mi cuenta", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "ingerir_plantilla_organismo" in hidden
    assert "obtener_plantilla_organismo" in hidden
    assert "listar_adiciones_contrato" in hidden
    assert "registrar_adicion_contrato" in hidden


def test_hidden_tool_names_reveals_orthogonal_tool_on_keyword_match() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="quiero subir una plantilla nueva para mis PDFs", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "ingerir_plantilla_organismo" not in hidden
    assert "obtener_plantilla_organismo" not in hidden
    # Unrelated orthogonal tools stay hidden — a "plantilla" mention doesn't
    # blanket-reveal "adiciones" tools too.
    assert "listar_adiciones_contrato" in hidden


def test_hidden_tool_names_keyword_match_is_case_insensitive() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="Necesito registrar una ADICIÓN al contrato", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "registrar_adicion_contrato" not in hidden
    assert "listar_adiciones_contrato" not in hidden


def test_hidden_tool_names_reveals_adicion_tools_on_otrosi_synonym() -> None:
    """CRITICAL 3: 'otrosí' never contains the substring 'adici' — the old
    keyword set missed this real-world synonym entirely."""
    hidden = phase_gating.hidden_tool_names(
        message="Firmamos un otrosí que cambia el plazo del contrato", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "registrar_adicion_contrato" not in hidden
    assert "listar_adiciones_contrato" not in hidden


def test_hidden_tool_names_reveals_adicion_tools_on_prorroga_synonym_without_accent() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="Necesito una prorroga del contrato por 3 meses", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "registrar_adicion_contrato" not in hidden
    assert "listar_adiciones_contrato" not in hidden


def test_hidden_tool_names_reveals_plantilla_tools_on_machote_synonym() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="¿Tenés el machote que usa la entidad para el PDF?", cuenta_known=True, called_tool_names=frozenset()
    )
    assert "ingerir_plantilla_organismo" not in hidden
    assert "obtener_plantilla_organismo" not in hidden


def test_hidden_tool_names_matches_keyword_from_recent_messages_not_only_current() -> None:
    """CRITICAL 3(b): the trigger keyword can be a MESSAGE OR TWO ago — the
    current message alone ('dale, seguí') must not be the only window checked."""
    hidden = phase_gating.hidden_tool_names(
        message="dale, seguí",
        cuenta_known=True,
        called_tool_names=frozenset(),
        recent_messages=("Quiero registrar un otrosí de prórroga",),
    )
    assert "registrar_adicion_contrato" not in hidden
    assert "listar_adiciones_contrato" not in hidden


def test_hidden_tool_names_recent_messages_default_does_not_leak_unrelated_keywords() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="dale, seguí",
        cuenta_known=True,
        called_tool_names=frozenset(),
        recent_messages=("Quiero cargar el RUT",),
    )
    assert "registrar_adicion_contrato" in hidden
    assert "listar_adiciones_contrato" in hidden


def test_hidden_tool_names_stays_revealed_once_already_called_even_without_keyword() -> None:
    hidden = phase_gating.hidden_tool_names(
        message="¿cómo va todo?",
        cuenta_known=True,
        called_tool_names=frozenset({"ingerir_plantilla_organismo"}),
    )
    assert "ingerir_plantilla_organismo" not in hidden
    # A DIFFERENT orthogonal tool that was never called stays hidden.
    assert "obtener_plantilla_organismo" in hidden


def test_hidden_tool_names_empty_message_hides_all_orthogonal_tools() -> None:
    hidden = phase_gating.hidden_tool_names(message="", cuenta_known=True, called_tool_names=frozenset())
    assert {
        "ingerir_plantilla_organismo",
        "obtener_plantilla_organismo",
        "listar_adiciones_contrato",
        "registrar_adicion_contrato",
    } <= hidden


def test_hidden_tool_names_never_hides_a_pre_cuenta_happy_path_sequence_tool() -> None:
    """`FakeLLMPort.HAPPY_PATH_SEQUENCE` tools that do NOT require a cuenta id
    (listar_contratos, crear_cuenta_cobro, importar_documento) must NEVER be
    hidden, in either cuenta_known state — the agent must always be able to
    start and to create the cuenta in the first place."""
    from app.adapters.llm.fake_adapter import HAPPY_PATH_SEQUENCE

    happy_path_tools = {name for name in HAPPY_PATH_SEQUENCE if name is not None}
    happy_path_tools |= {v for v in HAPPY_PATH_SEQUENCE.values() if v is not None}
    pre_cuenta_tools = happy_path_tools - phase_gating.cuenta_scoped_tool_names()
    assert pre_cuenta_tools == {"listar_contratos", "crear_cuenta_cobro", "importar_documento"}

    for cuenta_known in (True, False):
        hidden = phase_gating.hidden_tool_names(message="", cuenta_known=cuenta_known, called_tool_names=frozenset())
        assert not (hidden & pre_cuenta_tools), (
            f"cuenta_known={cuenta_known} hid a pre-cuenta happy-path tool: {hidden & pre_cuenta_tools}"
        )


def test_hidden_tool_names_reveals_cuenta_scoped_happy_path_tools_once_cuenta_known() -> None:
    """The REST of `HAPPY_PATH_SEQUENCE` (radicacion-sin-friccion 1.9: extended
    past the old 6-tool stub to the full documented playbook) IS cuenta-scoped
    and correctly hidden while `cuenta_known=False` — the agent_chat_service
    integration is what guarantees `cuenta_known` flips True the instant
    `crear_cuenta_cobro` succeeds, before the NEXT llm.complete() call offers
    tools again."""
    from app.adapters.llm.fake_adapter import HAPPY_PATH_SEQUENCE

    happy_path_tools = {name for name in HAPPY_PATH_SEQUENCE if name is not None}
    happy_path_tools |= {v for v in HAPPY_PATH_SEQUENCE.values() if v is not None}
    cuenta_scoped_happy_path = happy_path_tools & phase_gating.cuenta_scoped_tool_names()
    assert cuenta_scoped_happy_path == {
        "definir_requisitos_checklist",
        "auto_vincular_documentos",
        "crear_actividades_desde_obligaciones",
        "subir_evidencias_desde_chat",
        "generar_informe_actividades",
        "generar_informe_supervision",
        "resumen_checklist",
        "preparar_radicacion",
        "radicar_cuenta",
    }

    hidden = phase_gating.hidden_tool_names(message="", cuenta_known=True, called_tool_names=frozenset())
    assert not (hidden & cuenta_scoped_happy_path)


def test_filter_openai_tools_no_hidden_returns_same_list_object() -> None:
    tools = [{"type": "function", "function": {"name": "a"}}]
    assert phase_gating.filter_openai_tools(tools, frozenset()) is tools


def test_filter_openai_tools_drops_hidden_names() -> None:
    tools = [
        {"type": "function", "function": {"name": "a"}},
        {"type": "function", "function": {"name": "b"}},
    ]
    result = phase_gating.filter_openai_tools(tools, frozenset({"b"}))
    assert [t["function"]["name"] for t in result] == ["a"]


def test_filter_openai_tools_handles_malformed_entry_without_raising() -> None:
    tools = [{"type": "function"}]  # missing "function" key entirely
    result = phase_gating.filter_openai_tools(tools, frozenset({"a"}))
    assert result == tools
