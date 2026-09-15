"""Network-failure-injection tests for LiteLLMAdapter — phase 4.3 of
radicacion-sin-friccion (openspec/changes/radicacion-sin-friccion/tasks.md ~L222).

Unlike the Gmail/Drive/Calendar adapters, LiteLLMAdapter's fallback semantics
are an intentional design choice (`complete()`/`embed()` catch broad
`Exception` and advance the model chain) — see the module's own docstrings.
This file does NOT change that behavior; it proves real `litellm.exceptions`
types (not just bare `RuntimeError`, as the existing test_litellm_adapter.py
suite uses) are actually caught by the existing fallback machinery, single-
model and fallback-chain, plus malformed-response edge cases (empty
`choices`, missing `usage`, malformed embedding items) and the terminal
all-models-failed `RuntimeError`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import litellm.exceptions as litellm_exc
import pytest
from app.adapters.llm.litellm_adapter import LiteLLMAdapter
from app.core.config import settings
from app.schemas.agent import LLMMessage


def _fake_completion_response(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    return SimpleNamespace(choices=[choice], usage=usage)


def _timeout(model: str) -> litellm_exc.Timeout:
    return litellm_exc.Timeout(message="Request timed out", model=model, llm_provider="gemini")


def _rate_limit(model: str) -> litellm_exc.RateLimitError:
    return litellm_exc.RateLimitError(message="Rate limit exceeded", llm_provider="groq", model=model)


def _auth_error(model: str) -> litellm_exc.AuthenticationError:
    return litellm_exc.AuthenticationError(message="Invalid API key", llm_provider="gemini", model=model)


def _bad_request(model: str) -> litellm_exc.BadRequestError:
    return litellm_exc.BadRequestError(message="Invalid request", model=model, llm_provider="gemini")


def _internal_server_error(model: str) -> litellm_exc.InternalServerError:
    return litellm_exc.InternalServerError(message="Internal server error", llm_provider="gemini", model=model)


def _api_connection_error(model: str) -> litellm_exc.APIConnectionError:
    return litellm_exc.APIConnectionError(message="Connection failed", llm_provider="gemini", model=model)


_REAL_LITELLM_ERRORS = {
    "Timeout": _timeout,
    "RateLimitError": _rate_limit,
    "AuthenticationError": _auth_error,
    "BadRequestError": _bad_request,
    "InternalServerError": _internal_server_error,
    "APIConnectionError": _api_connection_error,
}


class TestSingleModelRealLiteLLMExceptions:
    """`fallback=False` — `complete()`'s outer fallback loop still runs (with a
    1-model chain), so a real litellm exception type is caught by the same
    broad `except Exception` as any other model error and re-raised as the
    terminal `RuntimeError("All LLM models failed...")`, carrying the real
    exception's message. This is existing, documented behavior (see
    `test_embed_raises_when_all_models_fail` in test_litellm_adapter.py for
    the same pattern on the embedding side) — not something this slice
    changes; these tests prove it holds for REAL litellm exception types,
    not just the bare `RuntimeError` the pre-existing suite used."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_REAL_LITELLM_ERRORS))
    async def test_real_exception_surfaces_in_terminal_runtime_error(self, name: str) -> None:
        adapter = LiteLLMAdapter(default_model="gemini/gemini-2.5-flash")
        exc = _REAL_LITELLM_ERRORS[name]("gemini/gemini-2.5-flash")

        with (
            patch("litellm.acompletion", new=AsyncMock(side_effect=exc)),
            pytest.raises(RuntimeError, match="All LLM models failed") as exc_info,
        ):
            await adapter.complete([LLMMessage(role="user", content="hola")], fallback=False)

        assert str(exc) in str(exc_info.value)


class TestFallbackChainRealLiteLLMExceptions:
    """Fallback chain — the primary model fails TWICE (tenacity's internal
    retry) with a real litellm exception type, and the next model in the
    chain succeeds. Mirrors test_litellm_adapter.py's existing
    `test_fallback_to_non_groq_model_...` pattern, but with real exception
    types instead of bare RuntimeError."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_REAL_LITELLM_ERRORS))
    async def test_real_exception_falls_back_to_next_model(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash-lite")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        exc = _REAL_LITELLM_ERRORS[name]("groq/openai/gpt-oss-20b")
        fake_response = _fake_completion_response()

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[exc, exc, fake_response]),
        ) as mock_call:
            result = await adapter.complete([LLMMessage(role="user", content="hola")])

        assert result.content == "ok"
        assert result.fallback_depth == 1
        assert mock_call.call_count == 3


class TestMalformedResponseShapes:
    @pytest.mark.asyncio
    async def test_empty_choices_falls_back_to_next_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`response.choices[0]` on an empty list raises IndexError inside
        `_call_model`, caught by the broad `except Exception` in `complete()`'s
        fallback loop — same fail-open behavior as any other model error."""
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash-lite")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        empty_response = SimpleNamespace(choices=[], usage=None)
        fake_response = _fake_completion_response()

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[empty_response, empty_response, fake_response]),
        ) as mock_call:
            result = await adapter.complete([LLMMessage(role="user", content="hola")])

        assert result.content == "ok"
        assert mock_call.call_count == 3

    @pytest.mark.asyncio
    async def test_missing_usage_attribute_falls_back_to_next_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response object with no `usage` attribute at all raises
        AttributeError inside `_call_model` — also caught by the broad
        fallback `except Exception`, not a crash."""
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash-lite")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        message = SimpleNamespace(content="partial", tool_calls=None)
        choice = SimpleNamespace(message=message)
        no_usage_response = SimpleNamespace(choices=[choice])  # no .usage attribute
        fake_response = _fake_completion_response()

        with patch(
            "litellm.acompletion",
            new=AsyncMock(side_effect=[no_usage_response, no_usage_response, fake_response]),
        ) as mock_call:
            result = await adapter.complete([LLMMessage(role="user", content="hola")])

        assert result.content == "ok"
        assert mock_call.call_count == 3

    @pytest.mark.asyncio
    async def test_usage_none_is_handled_gracefully_zero_tokens(self) -> None:
        """Not a bug: `usage.prompt_tokens if usage else 0` already tolerates a
        present-but-None `usage` field without any fallback needed."""
        adapter = LiteLLMAdapter(default_model="gemini/gemini-2.5-flash")
        message = SimpleNamespace(content="ok", tool_calls=None)
        choice = SimpleNamespace(message=message)
        response = SimpleNamespace(choices=[choice], usage=None)

        with patch("litellm.acompletion", new=AsyncMock(return_value=response)):
            result = await adapter.complete([LLMMessage(role="user", content="hola")], fallback=False)

        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.total_tokens == 0


class TestAllModelsFailedTerminalError:
    @pytest.mark.asyncio
    async def test_all_models_fail_raises_runtime_error_with_last_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash-lite")
        monkeypatch.setattr(settings, "LLM_LOCAL_MODEL", "")
        adapter = LiteLLMAdapter(default_model="groq/openai/gpt-oss-20b")
        exc = _internal_server_error("groq/openai/gpt-oss-20b")

        with (
            patch("litellm.acompletion", new=AsyncMock(side_effect=exc)),
            pytest.raises(RuntimeError, match="All LLM models failed"),
        ):
            await adapter.complete([LLMMessage(role="user", content="hola")])

    @pytest.mark.asyncio
    async def test_all_embedding_models_fail_raises_runtime_error(self) -> None:
        adapter = LiteLLMAdapter()
        exc = _api_connection_error(settings.LLM_EMBEDDING_MODEL)

        with (
            patch("litellm.aembedding", new=AsyncMock(side_effect=exc)),
            pytest.raises(RuntimeError, match="All embedding models failed"),
        ):
            await adapter.embed(["texto"])


class TestMalformedEmbedData:
    @pytest.mark.asyncio
    async def test_embed_item_missing_embedding_field_falls_back(self) -> None:
        """A malformed embedding item (neither dict['embedding'] nor
        .embedding present) raises KeyError/AttributeError inside the list
        comprehension, caught by embed()'s broad `except Exception`."""
        adapter = LiteLLMAdapter()
        malformed_response = SimpleNamespace(data=[{"not_embedding": [0.1]}])
        good_response = SimpleNamespace(data=[{"embedding": [0.9, 0.8]}])

        with patch(
            "litellm.aembedding",
            new=AsyncMock(side_effect=[malformed_response, good_response]),
        ) as mock_call:
            result = await adapter.embed(["texto"])

        assert result == [[0.9, 0.8]]
        assert mock_call.call_count == 2

    @pytest.mark.asyncio
    async def test_embed_falls_back_when_primary_raises(self) -> None:
        adapter = LiteLLMAdapter()
        good_response = SimpleNamespace(data=[{"embedding": [0.1]}])

        with patch(
            "litellm.aembedding",
            new=AsyncMock(side_effect=[RuntimeError("empty upstream"), good_response]),
        ):
            result = await adapter.embed(["texto"])

        assert result == [[0.1]]

    @pytest.mark.asyncio
    async def test_embed_returns_empty_list_for_empty_data_without_raising(self) -> None:
        """Malformed-but-not-erroring shape: an empty `response.data` list
        (0 embeddings for N inputs) is not itself an exception — it silently
        returns an empty list. Documented here as current behavior, not
        something this slice is scoped to change."""
        adapter = LiteLLMAdapter()
        empty_response = SimpleNamespace(data=[])

        with patch("litellm.aembedding", new=AsyncMock(return_value=empty_response)):
            result = await adapter.embed(["texto uno", "texto dos"])

        assert result == []


class TestGenerarActividadesAgente422DoesNotLeakRawException:
    """`cuenta_cobro_service.generar_actividades_agente` maps any LLM failure
    to a 422 ValidationError — this must not interpolate the raw exception
    text into the user-facing message (it used to: `f"... {exc}"`, leaking
    whatever the LLM SDK exception's __str__ happens to contain).

    Exercises the REAL service function end-to-end (DB-backed contrato +
    cuenta, mirrors tests/test_generar_actividades_agente.py's fixtures) so
    this is a genuine regression test, not a hand-rolled reproduction of the
    except/raise block.
    """

    @pytest.mark.asyncio
    async def test_raw_exception_text_not_in_validation_error_message(
        self,
        db,  # type: ignore[no-untyped-def]
        test_user,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from datetime import date

        from app.core.exceptions import ValidationError
        from app.models.contrato import Contrato
        from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
        from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
        from app.services import cuenta_cobro_service

        user = test_user["user"]
        contrato = Contrato(
            usuario_id=user.id,
            numero_contrato="CTR-LLM-FAIL-001",
            objeto="Servicios profesionales",
            valor_total=12_000_000,
            valor_mensual=1_000_000,
            fecha_inicio=date(2024, 1, 1),
            fecha_fin=date(2024, 12, 31),
            entidad="Alcaldía",
        )
        db.add(contrato)
        await db.commit()
        await db.refresh(contrato)

        db.add(
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=contrato.id,
                storage_key=f"users/{user.id}/contrato.pdf",
                nombre="contrato.pdf",
                tipo=TipoDocumentoFuente.CONTRATO,
                texto_extraido="OBJETO: Prestación de servicios. ACTIVIDADES: elaborar informes.",
            )
        )
        await db.commit()

        cuenta = CuentaCobro(
            contrato_id=contrato.id,
            mes=5,
            anio=2024,
            estado=EstadoCuentaCobro.BORRADOR,
            valor=contrato.valor_mensual,
        )
        db.add(cuenta)
        await db.commit()
        await db.refresh(cuenta)

        secret_looking_text = "SUPER_SECRET_INTERNAL_DEBUG_DETAIL_XYZ123"
        exc_with_secret = _internal_server_error("gemini/gemini-2.5-flash")
        exc_with_secret.message = secret_looking_text  # type: ignore[attr-defined]

        class _FailingLLM:
            async def complete(self, *_args: object, **_kwargs: object) -> object:
                raise exc_with_secret

        import app.adapters.llm as llm_pkg

        monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FailingLLM(), raising=True)

        with pytest.raises(ValidationError) as exc_info:
            await cuenta_cobro_service.generar_actividades_agente(db, user.id, cuenta.id)

        assert secret_looking_text not in exc_info.value.detail
        assert secret_looking_text not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validation_error_still_maps_to_422(
        self,
        db,  # type: ignore[no-untyped-def]
        test_user,  # type: ignore[no-untyped-def]
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression guard: the fix must not change the HTTP status contract
        (422 via `EXCEPTION_STATUS_MAP[ValidationError]`), only the message
        content."""
        from datetime import date

        from app.core.exceptions import ValidationError, domain_to_http
        from app.models.contrato import Contrato
        from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
        from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
        from app.services import cuenta_cobro_service

        user = test_user["user"]
        contrato = Contrato(
            usuario_id=user.id,
            numero_contrato="CTR-LLM-FAIL-002",
            objeto="Servicios profesionales",
            valor_total=12_000_000,
            valor_mensual=1_000_000,
            fecha_inicio=date(2024, 1, 1),
            fecha_fin=date(2024, 12, 31),
            entidad="Alcaldía",
        )
        db.add(contrato)
        await db.commit()
        await db.refresh(contrato)

        db.add(
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=contrato.id,
                storage_key=f"users/{user.id}/contrato.pdf",
                nombre="contrato.pdf",
                tipo=TipoDocumentoFuente.CONTRATO,
                texto_extraido="OBJETO: Prestación de servicios. ACTIVIDADES: elaborar informes.",
            )
        )
        await db.commit()

        cuenta = CuentaCobro(
            contrato_id=contrato.id,
            mes=5,
            anio=2024,
            estado=EstadoCuentaCobro.BORRADOR,
            valor=contrato.valor_mensual,
        )
        db.add(cuenta)
        await db.commit()
        await db.refresh(cuenta)

        class _FailingLLM:
            async def complete(self, *_args: object, **_kwargs: object) -> object:
                raise _api_connection_error("gemini/gemini-2.5-flash")

        import app.adapters.llm as llm_pkg

        monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FailingLLM(), raising=True)

        with pytest.raises(ValidationError) as exc_info:
            await cuenta_cobro_service.generar_actividades_agente(db, user.id, cuenta.id)

        http_exc = domain_to_http(exc_info.value)
        assert http_exc.status_code == 422
