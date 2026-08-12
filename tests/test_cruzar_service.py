"""Unit tests for cruzar_service.cruzar_documentos.

Uses in-memory aiosqlite (same setup as test_cobertura_service.py).
LLM calls are mocked with unittest.mock so no real API keys are needed.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.services import cruzar_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Shared test helpers (mirrors test_cobertura_service.py conventions)
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, *, email: str = "u@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Test User",
        cedula="123456789",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="001-2024",
        objeto="Prestación de servicios profesionales de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_obligacion(
    db: AsyncSession, contrato_id: uuid.UUID, orden: int, descripcion: str | None = None
) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=descripcion or f"Realizar informes técnicos mensuales de consultoría {orden}",
        tipo=TipoObligacion.ESPECIFICA,
        orden=orden,
    )
    db.add(ob)
    await db.flush()
    return ob


async def _make_cuenta(db: AsyncSession, contrato_id: uuid.UUID) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=3,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cuenta)
    await db.flush()
    return cuenta


async def _make_documento(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    texto_extraido: str | None = None,
    nombre: str = "informe_marzo.pdf",
) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        storage_key=f"docs/{uuid.uuid4()}.pdf",
        nombre=nombre,
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
        texto_extraido=texto_extraido,
    )
    db.add(doc)
    await db.flush()
    return doc


async def _make_actividad(db: AsyncSession, cuenta_id: uuid.UUID, obligacion_id: uuid.UUID) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta_id,
        obligacion_id=obligacion_id,
        descripcion="Actividad preexistente",
        justificacion="Justificación preexistente",
        fecha_realizacion=date(2024, 3, 31),
    )
    db.add(act)
    await db.flush()
    return act


# ---------------------------------------------------------------------------
# Mock LLM response helper
# ---------------------------------------------------------------------------


def _make_llm_response(content: str) -> MagicMock:
    """Build a mock LLMResponse-like object."""
    resp = MagicMock()
    resp.content = content
    resp.total_tokens = 10
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_relevance_batch_partial_selection() -> None:
    """A single batch call classifies all candidates; only the indices returned are relevant."""
    candidates = [
        {"content": "irrelevante uno", "source": "a.pdf"},
        {"content": "evidencia relevante dos", "source": "b.pdf"},
        {"content": "irrelevante tres", "source": "c.pdf"},
    ]
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=_make_llm_response("[2]"))

    flags = await cruzar_service._llm_relevance_batch("obligación X", candidates, mock_llm)

    assert flags == [False, True, False]
    # Exactly ONE LLM call for all three candidates (the whole point of batching)
    assert mock_llm.complete.await_count == 1


@pytest.mark.asyncio
async def test_llm_relevance_batch_fails_closed_on_garbage() -> None:
    """Unparseable / error responses mark every candidate as not-relevant (fail closed)."""
    candidates = [{"content": "x", "source": "a.pdf"}, {"content": "y", "source": "b.pdf"}]
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=_make_llm_response("no soy un array"))

    flags = await cruzar_service._llm_relevance_batch("obligación X", candidates, mock_llm)

    assert flags == [False, False]


@pytest.mark.asyncio
async def test_cruzar_raises_not_found_for_unknown_cuenta(db: AsyncSession) -> None:
    """Non-existent cuenta_id must raise NotFoundError, not crash."""
    user = await _make_user(db)
    await db.commit()

    with pytest.raises(NotFoundError):
        await cruzar_service.cruzar_documentos(db, user.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_cruzar_raises_forbidden_for_wrong_user(db: AsyncSession) -> None:
    """A user who doesn't own the contrato must get ForbiddenError."""
    owner = await _make_user(db, email="owner@test.com")
    other = await _make_user(db, email="other@test.com")
    contrato = await _make_contrato(db, owner.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with pytest.raises(ForbiddenError):
        await cruzar_service.cruzar_documentos(db, other.id, cuenta.id)


@pytest.mark.asyncio
async def test_cruzar_returns_cobertura_response_when_no_docs(db: AsyncSession) -> None:
    """When no DocumentoFuente with texto_extraido exists, returns current cobertura without crashing."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    # Document exists but has no texto_extraido
    await _make_documento(db, user.id, contrato.id, texto_extraido=None)
    await db.commit()

    result = await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    # Must return a valid CoberturaResponse (all obligations without evidence → rojo)
    assert result.resumen.total == 1
    assert result.resumen.sin_evidencia == 1
    assert result.listo_para_generar is False


@pytest.mark.asyncio
async def test_cruzar_creates_actividades_for_relevant_docs(db: AsyncSession) -> None:
    """When LLM returns RELEVANTE, an Actividad is created for the matching obligation."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(
        db,
        contrato.id,
        1,
        descripcion="Elaborar informes técnicos mensuales de consultoría y asesoría",
    )
    cuenta = await _make_cuenta(db, contrato.id)
    # Document with texto_extraido that shares keywords with the obligation
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido=(
            "Informe técnico mensual de consultoría y asesoría para el período de marzo 2024. "
            "Se elaboraron los documentos requeridos según las obligaciones contractuales."
        ),
    )
    await db.commit()

    # Mock the THREE LLM calls in order: relevance batch, actividad, justification
    mock_relevance_resp = _make_llm_response("[1]")
    mock_actividad_resp = _make_llm_response(
        "Elaboré el informe técnico mensual de consultoría y asesoría del período de marzo."
    )
    mock_justification_resp = _make_llm_response(
        "El informe técnico mensual de consultoría fue elaborado según se evidencia en informe_marzo.pdf."
    )
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=[mock_relevance_resp, mock_actividad_resp, mock_justification_resp])

    with patch("app.services.cruzar_service.get_llm", return_value=mock_llm):
        with patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            result = await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    # Should have created at least one Actividad
    acts_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividades = list(acts_result.scalars().all())
    assert len(actividades) >= 1
    assert actividades[0].obligacion_id == ob.id
    assert actividades[0].justificacion is not None
    assert len(actividades[0].justificacion) > 0
    # descripcion (actividad realizada) must be the grounded LLM text, never the
    # generic "Evidencia documental: {source}" placeholder nor the justificación.
    assert actividades[0].descripcion == mock_actividad_resp.content
    assert not actividades[0].descripcion.startswith("Evidencia documental:")
    assert actividades[0].descripcion != actividades[0].justificacion
    assert actividades[0].justificacion_origen == "llm"

    # CoberturaResponse must reflect the new actividades (DEBIL because no Evidencia files attached)
    assert result.resumen.total == 1


@pytest.mark.asyncio
async def test_cruzar_actividad_falls_back_deterministically_on_llm_error(db: AsyncSession) -> None:
    """When the actividad-generation LLM call fails, descripcion must be a
    deterministic sentence naming the source document — never the obligación text,
    never the raw "Evidencia documental: {source}" placeholder."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(
        db,
        contrato.id,
        1,
        descripcion="Elaborar informes técnicos mensuales de consultoría y asesoría",
    )
    cuenta = await _make_cuenta(db, contrato.id)
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido=(
            "Informe técnico mensual de consultoría y asesoría para el período de marzo 2024. "
            "Se elaboraron los documentos requeridos según las obligaciones contractuales."
        ),
    )
    await db.commit()

    mock_relevance_resp = _make_llm_response("[1]")
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=[mock_relevance_resp, RuntimeError("llm down"), RuntimeError("llm down")])

    with patch("app.services.cruzar_service.get_llm", return_value=mock_llm):
        with patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    acts_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividades = list(acts_result.scalars().all())
    assert len(actividades) == 1
    assert actividades[0].descripcion == "Elaboración y entrega de informe_marzo.pdf."
    assert actividades[0].descripcion != ob.descripcion
    assert not actividades[0].descripcion.startswith("Evidencia documental:")
    assert actividades[0].justificacion_origen == "seed"


@pytest.mark.asyncio
async def test_cruzar_skips_obligacion_with_no_keyword_match(db: AsyncSession) -> None:
    """When document text has zero keyword overlap with obligation, no Actividad is created."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(
        db,
        contrato.id,
        1,
        descripcion="Supervisar cronograma presupuestal financiero trimestral",
    )
    cuenta = await _make_cuenta(db, contrato.id)
    # Document with completely unrelated text (no overlapping 4-char words)
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido="Recibo de pago servicios públicos agua luz gas domicilio residencial.",
    )
    await db.commit()

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock()  # Should never be called

    with patch("app.services.cruzar_service.get_llm", return_value=mock_llm):
        with patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            result = await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    # LLM must NOT have been called (keyword filter handled it)
    mock_llm.complete.assert_not_called()

    # No Actividades created
    acts_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividades = list(acts_result.scalars().all())
    assert len(actividades) == 0

    # Obligation shows up as SIN_EVIDENCIA
    assert result.resumen.sin_evidencia == 1


@pytest.mark.asyncio
async def test_cruzar_clears_existing_actividades_before_run(db: AsyncSession) -> None:
    """Pre-existing Actividades for the cuenta are deleted before re-running the matcher."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(db, contrato.id, 1)
    cuenta = await _make_cuenta(db, contrato.id)
    # Pre-existing activity that should be wiped
    await _make_actividad(db, cuenta.id, ob.id)
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido="Documento sin palabras relevantes para la obligación contractual técnica.",
    )
    await db.commit()

    # Verify pre-existing actividad is there
    before_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    assert len(list(before_result.scalars().all())) == 1

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=_make_llm_response("[]"))

    with patch("app.services.cruzar_service.get_llm", return_value=mock_llm):
        with patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    # After running, old activity must be gone (even if no new ones were created)
    after_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividades_after = list(after_result.scalars().all())
    assert len(actividades_after) == 0


# ---------------------------------------------------------------------------
# Contract-level context block (clasificacion-documentos-secop, design D4)
# ---------------------------------------------------------------------------


def _ctx_item(nombre: str, snippet: str | None, categoria: str = "otros") -> dict:
    return {
        "id": uuid.uuid4(),
        "fuente": "secop",
        "nombre": nombre,
        "descripcion": None,
        "categoria": categoria,
        "snippet": snippet,
        "url_descarga": None,
        "storage_key": None,
    }


def test_contexto_contrato_bloque_vacio_es_cadena_vacia() -> None:
    """Zero context docs → empty string, so prompts stay byte-identical."""
    assert cruzar_service._contexto_contrato_bloque([]) == ""


def test_contexto_contrato_bloque_acotado_5_docs_y_400_chars() -> None:
    docs = [_ctx_item(f"doc-{i}.pdf", "s" * 1000) for i in range(8)]
    bloque = cruzar_service._contexto_contrato_bloque(docs)
    # Only the first 5 docs make it in, snippets truncated to 400 chars.
    assert bloque.count("doc-") <= 5
    assert "doc-0.pdf" in bloque
    assert "s" * 401 not in bloque
    assert len(bloque) <= 2500


def test_contexto_contrato_bloque_linea_solo_metadata_sin_texto() -> None:
    bloque = cruzar_service._contexto_contrato_bloque([_ctx_item("anexo_sin_texto.pdf", None)])
    assert "anexo_sin_texto.pdf" in bloque
    assert "sin texto extra" in bloque  # metadata-only line, never downloads


def test_contexto_contrato_bloque_total_menor_a_2500() -> None:
    docs = [_ctx_item("n" * 300 + f"{i}.pdf", "s" * 400) for i in range(5)]
    bloque = cruzar_service._contexto_contrato_bloque(docs)
    assert 0 < len(bloque) <= 2500


@pytest.mark.asyncio
async def test_cruzar_incluye_bloque_de_contexto_en_prompts(db: AsyncSession) -> None:
    """Context docs reach BOTH writer prompts (actividad + justificación)."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id, 1, descripcion="Elaborar informes técnicos mensuales de consultoría")
    cuenta = await _make_cuenta(db, contrato.id)
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido="Informe técnico mensual de consultoría del período con actividades realizadas.",
    )
    await db.commit()

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(
        side_effect=[
            _make_llm_response("[1]"),
            _make_llm_response("Actividad generada."),
            _make_llm_response("Justificación generada."),
        ]
    )
    contexto = [_ctx_item("acta_secop_interna.pdf", "Detalle operativo del acta de contexto")]

    with (
        patch("app.services.cruzar_service.get_llm", return_value=mock_llm),
        patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate,
        patch.object(
            cruzar_service.checklist_service,
            "listar_documentos_contexto",
            new=AsyncMock(return_value=contexto),
        ),
    ):
        mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
        await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    # Calls: [0] relevance batch (no context), [1] actividad, [2] justificación.
    prompts_escritores = [call.args[0][1].content for call in mock_llm.complete.await_args_list[1:]]
    assert len(prompts_escritores) == 2
    for prompt in prompts_escritores:
        assert "acta_secop_interna.pdf" in prompt
        assert "Detalle operativo del acta de contexto" in prompt
    # Relevance batch prompt stays untouched.
    assert "acta_secop_interna.pdf" not in mock_llm.complete.await_args_list[0].args[0][1].content


@pytest.mark.asyncio
async def test_cruzar_sin_contexto_prompts_identicos_al_actual(db: AsyncSession) -> None:
    """Zero context docs → writer prompts do NOT gain any context section.

    The evidence doc gets a MAPPED categoria (EVIDENCIAS) so it does not qualify
    as OTROS/unmapped context — the contract genuinely has zero context docs.
    """
    from app.models.categoria_documento import CategoriaDocumento

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id, 1, descripcion="Elaborar informes técnicos mensuales de consultoría")
    cuenta = await _make_cuenta(db, contrato.id)
    doc = await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido="Informe técnico mensual de consultoría del período con actividades realizadas.",
    )
    doc.categoria = CategoriaDocumento.EVIDENCIAS
    await db.commit()

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(
        side_effect=[
            _make_llm_response("[1]"),
            _make_llm_response("Actividad generada."),
            _make_llm_response("Justificación generada."),
        ]
    )

    with (
        patch("app.services.cruzar_service.get_llm", return_value=mock_llm),
        patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate,
    ):
        mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
        await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    for call in mock_llm.complete.await_args_list[1:]:
        assert "Documentos de contexto" not in call.args[0][1].content


@pytest.mark.asyncio
async def test_cruzar_reuses_actividad_stub_with_evidencia_instead_of_deleting_it(db: AsyncSession) -> None:
    """A stub Actividad created by the upload-first evidence flow (Evidencia
    already attached) must survive the refresh-from-docs delete and be filled
    with the generated text in place — deleting it would either orphan the
    Evidencia row (SQLite, no FK enforcement) or raise a FK violation
    (Postgres, no ON DELETE clause on Evidencia.actividad_id)."""
    from app.models.evidencia import Evidencia

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    ob = await _make_obligacion(db, contrato.id, 1, descripcion="Elaborar informes técnicos mensuales de avance")
    cuenta = await _make_cuenta(db, contrato.id)
    stub = await _make_actividad(db, cuenta.id, ob.id)
    stub_id = stub.id
    db.add(Evidencia(actividad_id=stub.id, storage_key="evidencias/x/y.pdf", nombre_archivo="y.pdf"))
    await _make_documento(
        db,
        user.id,
        contrato.id,
        texto_extraido="Informe técnico de avance mensual con detalle de actividades realizadas.",
    )
    await db.commit()

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(
        side_effect=[
            _make_llm_response("[1]"),
            _make_llm_response("Elaboración del informe técnico mensual."),
            _make_llm_response("Cumple la obligación de reportar avances."),
        ]
    )

    with patch("app.services.cruzar_service.get_llm", return_value=mock_llm):
        with patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            await cruzar_service.cruzar_documentos(db, user.id, cuenta.id)

    result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividades = list(result.scalars().all())
    assert len(actividades) == 1
    assert actividades[0].id == stub_id  # same row reused, not a duplicate
    assert actividades[0].descripcion == "Elaboración del informe técnico mensual."

    ev_result = await db.execute(select(Evidencia).where(Evidencia.actividad_id == stub_id))
    assert ev_result.scalar_one_or_none() is not None  # evidence link intact
