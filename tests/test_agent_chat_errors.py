"""Tests for `app.services.agent_chat_service._format_tool_error`.

Live bug this hardens against: a weak local model (llama3.1:8b) called
`crear_cuenta_cobro` with incomplete args, and the raw pydantic dump ("3
validation errors for CuentaCobroCreate ... contrato_id UUID input should be a
string ...") was shown DIRECTLY to the user. `_format_tool_error` must split
what the user sees (`user_resumen` — clean Spanish, no pydantic jargon) from
what goes back to the LLM (`llm_detail` — actionable enough to make the retry
succeed).
"""

from __future__ import annotations

import pytest
from app.core.exceptions import NotFoundError
from app.schemas.cuenta_cobro import CuentaCobroCreate
from app.services.agent_chat_service import _format_tool_error
from pydantic import ValidationError


def _build_validation_error(**kwargs: object) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        CuentaCobroCreate.model_validate(kwargs)
    return exc_info.value


class TestFormatToolErrorValidationError:
    def test_user_resumen_has_no_pydantic_jargon(self) -> None:
        exc = _build_validation_error(anio=2026)  # missing contrato_id AND mes
        user_resumen, _llm_detail = _format_tool_error(exc, "crear_cuenta_cobro")

        lowered = user_resumen.lower()
        assert "validation error" not in lowered
        assert "pydantic" not in lowered
        assert "field required" not in lowered

    def test_user_resumen_names_the_missing_fields(self) -> None:
        exc = _build_validation_error(anio=2026)
        user_resumen, _llm_detail = _format_tool_error(exc, "crear_cuenta_cobro")

        assert "contrato_id" in user_resumen
        assert "mes" in user_resumen
        assert "crear_cuenta_cobro" in user_resumen

    def test_llm_detail_points_at_listar_contratos_when_contrato_id_missing(self) -> None:
        exc = _build_validation_error(mes=2, anio=2026)  # missing contrato_id only
        _user_resumen, llm_detail = _format_tool_error(exc, "crear_cuenta_cobro")

        assert "listar_contratos" in llm_detail

    def test_llm_detail_lists_all_required_fields_not_just_one(self) -> None:
        exc = _build_validation_error(contrato_id="00000000-0000-0000-0000-000000000000")  # missing mes AND anio
        user_resumen, llm_detail = _format_tool_error(exc, "crear_cuenta_cobro")

        assert "mes" in user_resumen
        assert "anio" in user_resumen
        # No id-shaped field is missing here, so no discovery-tool hint is expected,
        # but the instruction to include ALL required args must still be present.
        assert "argumentos obligatorios" in llm_detail

    def test_invalid_uuid_type_error_also_recognized_as_contrato_id(self) -> None:
        exc = _build_validation_error(contrato_id="not-a-real-uuid", mes=2, anio=2026)
        user_resumen, llm_detail = _format_tool_error(exc, "crear_cuenta_cobro")

        assert "contrato_id" in user_resumen
        assert "listar_contratos" in llm_detail


class TestFormatToolErrorDomainError:
    def test_domain_error_user_resumen_is_its_own_message(self) -> None:
        exc = NotFoundError("Contrato", "abc123")
        user_resumen, _llm_detail = _format_tool_error(exc, "listar_contratos")

        assert user_resumen == exc.detail

    def test_domain_error_llm_detail_includes_the_message(self) -> None:
        exc = NotFoundError("Contrato", "abc123")
        _user_resumen, llm_detail = _format_tool_error(exc, "listar_contratos")

        assert exc.detail in llm_detail

    def test_domain_error_never_reads_like_pydantic(self) -> None:
        exc = NotFoundError("Contrato", "abc123")
        user_resumen, _llm_detail = _format_tool_error(exc, "listar_contratos")

        assert "validation error" not in user_resumen.lower()


class TestFormatToolErrorGenericException:
    def test_generic_exception_gets_a_generic_spanish_message(self) -> None:
        exc = RuntimeError("boom - some internal detail leaked here")
        user_resumen, llm_detail = _format_tool_error(exc, "generar_informe_actividades")

        assert user_resumen == "Ocurrió un error al ejecutar generar_informe_actividades."
        assert "boom" in llm_detail  # LLM still gets the raw detail to reason about

    def test_generic_exception_detail_is_truncated(self) -> None:
        exc = RuntimeError("x" * 1000)
        _user_resumen, llm_detail = _format_tool_error(exc, "some_tool")

        assert len(llm_detail) <= 300
