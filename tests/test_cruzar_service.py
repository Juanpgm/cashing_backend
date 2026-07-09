"""Unit tests for cruzar_service.cruzar_documentos.

Uses in-memory aiosqlite (same setup as test_cobertura_service.py).
LLM calls are mocked with unittest.mock so no real API keys are needed.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.services import cruzar_service


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
