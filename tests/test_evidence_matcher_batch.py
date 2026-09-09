"""evidence_matcher must classify all candidates for an obligation in ONE LLM call
(batched), not one call per candidate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.nodes import evidence_matcher


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _CountingLLM:
    """Records how many times complete() is called and returns a fixed batch answer."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=64, **kwargs) -> _FakeResp:
        self.calls += 1
        self.last_kwargs = {"max_tokens": max_tokens, **kwargs}
        return _FakeResp(self.content)


@pytest.mark.asyncio
async def test_matcher_batches_one_call_per_obligation() -> None:
    fake = _CountingLLM("[1]")  # only the first (highest-score) candidate is relevant
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "realizar informes tecnicos mensuales consultoria"}],
        "evidence_raw": [
            {"id": "a", "content": "informes tecnicos mensuales realizados consultoria"},
            {"id": "b", "content": "informes administrativos presupuesto reunion"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # One batched call for the single obligation, NOT one per candidate.
    assert fake.calls == 1
    matched = result["matched_evidence"]["ob1"]
    assert [e["id"] for e in matched] == ["a"]  # only candidate 1 kept


@pytest.mark.asyncio
async def test_matcher_empty_when_no_candidates() -> None:
    fake = _CountingLLM("[]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "algo muy especifico xyz"}],
        "evidence_raw": [{"id": "a", "content": "contenido totalmente distinto sin relacion"}],
    }
    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # No candidate passes the keyword filter → no LLM call at all.
    assert fake.calls == 0
    assert result["matched_evidence"]["ob1"] == []


@pytest.mark.asyncio
async def test_matcher_max_effort_fallback_when_zero_candidates_above_threshold() -> None:
    """An obligación with ZERO candidates clearing the 0.15 keyword threshold must
    not stay silently empty if there's at least some weak (score > 0) overlap:
    the top-3 weakly-overlapping candidates are still sent to the LLM."""
    fake = _CountingLLM("[1]")  # LLM confirms the single weak candidate is relevant
    state = {
        "obligaciones_extraidas": [
            {"id": "ob1", "descripcion": "supervisar cronograma presupuestal financiero trimestral"}
        ],
        "evidence_raw": [
            # Only ONE overlapping word ("cronograma") → below the 0.15 filter, but > 0.
            {"id": "a", "content": "reunión de cronograma general del equipo administrativo"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # The weak candidate WAS sent to the LLM (max-effort), not silently dropped.
    assert fake.calls == 1
    assert [e["id"] for e in result["matched_evidence"]["ob1"]] == ["a"]


@pytest.mark.asyncio
async def test_matcher_llm_error_falls_back_to_keyword_threshold_not_all_false() -> None:
    """On LLM error, evidence_matcher must NOT fail fully closed (drop everything) —
    it falls back to accepting only candidates whose keyword overlap already clears
    a stricter deterministic bar (>= 0.30)."""

    class _RaisingLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("llm down")

    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            # High overlap (>= 0.30) — should be accepted by the fallback.
            {"id": "strong", "content": "informes tecnicos mensuales consultoria asesoria elaborados"},
            # Low overlap (< 0.30, but >= 0.15 so it still passes the initial filter) — must be rejected.
            {"id": "weak", "content": "informes generales sin relacion directa con nada mas del contrato"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=_RaisingLLM()):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert "strong" in matched_ids
    assert "weak" not in matched_ids


@pytest.mark.asyncio
async def test_matcher_garbage_llm_output_does_not_accept_low_score_candidates() -> None:
    """Unparseable LLM output must fall back the same way as an exception — garbage
    output must NOT accept low-score candidates just because the LLM "answered"."""
    fake = _CountingLLM("no soy un array JSON")

    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            {"id": "strong", "content": "informes tecnicos mensuales consultoria asesoria elaborados"},
            {"id": "weak", "content": "informes generales sin relacion directa con nada mas del contrato"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert "strong" in matched_ids
    assert "weak" not in matched_ids


# ─────────────────────────────────────────────────────────────────────────────
# groq/llama-3.1-8b-instant decommissioning fix: new model + reasoning_effort +
# larger max_tokens headroom (see evidence_matcher.py module changes).
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evidence_matcher_node_constructs_llm_against_live_groq_model() -> None:
    """groq/llama-3.1-8b-instant was decommissioned by Groq — evidence_matcher_node
    must build its LLM client against the current model."""
    fake = _CountingLLM("[1]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "realizar informes tecnicos mensuales consultoria"}],
        "evidence_raw": [{"id": "a", "content": "informes tecnicos mensuales realizados consultoria"}],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake) as mock_get_llm:
        await evidence_matcher.evidence_matcher_node(state)

    mock_get_llm.assert_called_once_with(model="groq/openai/gpt-oss-20b")


@pytest.mark.asyncio
async def test_llm_relevance_batch_sends_reasoning_effort_and_safe_max_tokens() -> None:
    """The new model (groq/openai/gpt-oss-20b) is a reasoning model — reasoning_effort
    must be forwarded and max_tokens must have real headroom above the verified-
    minimum-working budget (64), not just the bare minimum."""
    fake = _CountingLLM("[1]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "realizar informes tecnicos mensuales consultoria"}],
        "evidence_raw": [{"id": "a", "content": "informes tecnicos mensuales realizados consultoria"}],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        await evidence_matcher.evidence_matcher_node(state)

    assert fake.last_kwargs["reasoning_effort"] == "low"
    assert fake.last_kwargs["max_tokens"] >= 120


@pytest.mark.asyncio
async def test_llm_relevance_batch_empty_content_falls_back_not_crashes() -> None:
    """Reproduces the too-small-max_tokens failure mode at the batch-relevance call
    site: litellm can return 200 with EMPTY content (finish_reason=length) instead
    of raising — no exception for the adapter's fallback-on-exception to catch.
    Must degrade to the deterministic keyword-score fallback, not crash."""

    class _EmptyContentLLM:
        async def complete(self, *args, **kwargs) -> _FakeResp:
            return _FakeResp("")

    # High overlap (>= 0.30) — accepted by the deterministic fallback bar.
    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            {"id": "strong", "content": "informes tecnicos mensuales consultoria asesoria elaborados"},
            {"id": "weak", "content": "informes generales sin relacion directa con nada mas del contrato"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=_EmptyContentLLM()):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert "strong" in matched_ids
    assert "weak" not in matched_ids


# ─────────────────────────────────────────────────────────────────────────────
# Local-upload wiring (evidencia_service._clasificar_y_enlazar_lote) reuses the
# SAME batched matcher — many files, few obligaciones must still cost at most
# one LLM call per obligación (evidence-classification-pipeline: Batched-per-
# obligación matching).
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subir_evidencias_cuenta_local_upload_batches_one_llm_call_per_obligacion(db) -> None:
    """5 uploaded files against 1 obligación must trigger AT MOST one matcher
    LLM call, not one per file — proving the new evidencia_service wiring
    reuses evidence_matcher_node's existing batching property instead of
    calling the LLM per evidencia (evidence-classification-pipeline: Batched-
    per-obligación matching)."""
    from datetime import date
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.models.evidencia_obligacion import EvidenciaObligacion
    from app.models.obligacion import Obligacion, TipoObligacion
    from app.models.usuario import Usuario
    from app.services import evidencia_service
    from sqlalchemy import select

    user = Usuario(
        email=f"batch-{uuid4()}@test.com",
        nombre="Batch User",
        cedula="555000111",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-BATCH-001",
        objeto="Consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales de consultoria y asesoria",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    db.add(ob)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1)
    db.add(cuenta)
    await db.commit()
    await db.refresh(ob)
    await db.refresh(cuenta)
    await db.refresh(contrato, attribute_names=["obligaciones"])

    storage = AsyncMock()
    storage.upload.return_value = "key"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"

    fake = AsyncMock()
    fake.embed = AsyncMock(side_effect=RuntimeError("no network in tests"))
    fake.complete = AsyncMock(return_value=MagicMock(content="[1,2,3,4,5]"))

    archivos = [
        (f"informe{i}.txt", "text/plain", f"Informe tecnico mensual de consultoria y asesoria {i}.".encode())
        for i in range(5)
    ]

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=fake):
        await evidencia_service.subir_evidencias_cuenta(
            db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta.id, archivos=archivos
        )

    # 5 files, 1 obligación → exactly 1 batched relevance call (not 0, not 5).
    assert fake.complete.await_count == 1

    links_result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == ob.id))
    links = links_result.scalars().all()
    assert len(links) == 5  # every uploaded evidencia resolved to a link for this obligación


@pytest.mark.asyncio
async def test_subir_evidencias_cuenta_constructs_llm_against_live_groq_model(db) -> None:
    """evidencia_service.subir_evidencias_cuenta's `clasificar_evidencia` client must
    also target the live (non-decommissioned) Groq model — groq/llama-3.1-8b-instant
    no longer exists in Groq's catalog."""
    from datetime import date
    from unittest.mock import AsyncMock, MagicMock, patch
    from uuid import uuid4

    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
    from app.models.obligacion import Obligacion, TipoObligacion
    from app.models.usuario import Usuario
    from app.services import evidencia_service

    user = Usuario(
        email=f"model-{uuid4()}@test.com",
        nombre="Model User",
        cedula="555000222",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-MODEL-001",
        objeto="Consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales de consultoria y asesoria",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    db.add(ob)
    cuenta = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1)
    db.add(cuenta)
    await db.commit()
    await db.refresh(ob)
    await db.refresh(cuenta)
    await db.refresh(contrato, attribute_names=["obligaciones"])

    storage = AsyncMock()
    storage.upload.return_value = "key"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"

    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=MagicMock(content="1"))

    archivos = [("informe.txt", "text/plain", b"Informe tecnico mensual de consultoria y asesoria.")]

    with (
        patch.object(evidencia_service, "get_llm", return_value=fake) as mock_get_llm,
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=fake),
    ):
        await evidencia_service.subir_evidencias_cuenta(
            db=db, storage=storage, usuario_id=user.id, cuenta_id=cuenta.id, archivos=archivos
        )

    mock_get_llm.assert_called_once_with(model="groq/openai/gpt-oss-20b")
