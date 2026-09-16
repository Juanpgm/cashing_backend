"""Unit tests for constancia_service.generar_constancia_pdf.

WeasyPrint requires native libs (GTK/cairo) that may not be present in CI,
so generate_pdf_from_template is mocked to return a minimal PDF stub.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.services import constancia_service

_FAKE_PDF = b"%PDF-1.4 fake\n%%EOF"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession, *, email: str = "u@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Ana Gómez",
        cedula="12345678",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=10,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="CC-001-2024",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Hacienda",
        supervisor_nombre="Carlos Supervisor",
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_obligacion(db: AsyncSession, contrato_id: uuid.UUID) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion="Elaborar informes técnicos mensuales",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constancia_genera_pdf_valido(db: AsyncSession) -> None:
    """Happy path: returns bytes and a filename ending in .pdf."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with patch(
        "app.services.constancia_service.generate_pdf_from_template",
        return_value=_FAKE_PDF,
    ):
        pdf_bytes, filename = await constancia_service.generar_constancia_pdf(db, user.id, cuenta.id)

    assert pdf_bytes == _FAKE_PDF
    assert filename.endswith(".pdf")
    assert "CC-001-2024" in filename
    assert "2024-03" in filename


@pytest.mark.asyncio
async def test_constancia_not_found(db: AsyncSession) -> None:
    """Unknown cuenta_id raises NotFoundError."""
    user = await _make_user(db)
    await db.commit()

    with pytest.raises(NotFoundError):
        await constancia_service.generar_constancia_pdf(db, user.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_constancia_ownership_error(db: AsyncSession) -> None:
    """User who does not own the contrato gets ForbiddenError."""
    owner = await _make_user(db, email="owner@test.com")
    other = await _make_user(db, email="other@test.com")
    contrato = await _make_contrato(db, owner.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with pytest.raises(ForbiddenError):
        await constancia_service.generar_constancia_pdf(db, other.id, cuenta.id)


@pytest.mark.asyncio
async def test_constancia_pdf_runs_off_event_loop(db: AsyncSession) -> None:
    """generate_pdf_from_template (WeasyPrint) must not block the event loop
    (slice 2.1, perf/phase2-async-doc-generators). A slow (mocked) sync render
    must not stall a concurrent coroutine."""
    import asyncio
    import time as time_module

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    def _slow_render(_template_html: str, _context: dict) -> bytes:
        time_module.sleep(0.2)
        return _FAKE_PDF

    tracker_ticks: list[float] = []

    async def _tracker() -> None:
        loop = asyncio.get_event_loop()
        start = loop.time()
        for _ in range(10):
            await asyncio.sleep(0.02)
            tracker_ticks.append(loop.time() - start)

    with patch("app.services.constancia_service.generate_pdf_from_template", side_effect=_slow_render):
        await asyncio.gather(
            constancia_service.generar_constancia_pdf(db, user.id, cuenta.id),
            _tracker(),
        )

    assert tracker_ticks[-1] < 0.35, (
        f"tracker was delayed past the offloaded render — got {tracker_ticks[-1]:.3f}s, expected ~0.2s"
    )


@pytest.mark.asyncio
async def test_constancia_pdf_propagates_generator_exception(db: AsyncSession) -> None:
    """An exception raised inside the threaded generate_pdf_from_template call
    must propagate to the async caller unchanged."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    class _WeasyPrintFailureError(RuntimeError):
        pass

    with (
        patch(
            "app.services.constancia_service.generate_pdf_from_template",
            side_effect=_WeasyPrintFailureError("weasyprint render failed"),
        ),
        pytest.raises(_WeasyPrintFailureError, match="weasyprint render failed"),
    ):
        await constancia_service.generar_constancia_pdf(db, user.id, cuenta.id)


@pytest.mark.asyncio
async def test_constancia_sin_actividades_genera_igual(db: AsyncSession) -> None:
    """A cuenta with no activities still produces a PDF (checklist-only constancia)."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    with patch(
        "app.services.constancia_service.generate_pdf_from_template",
        return_value=_FAKE_PDF,
    ) as mock_render:
        pdf_bytes, _ = await constancia_service.generar_constancia_pdf(db, user.id, cuenta.id)

    assert pdf_bytes == _FAKE_PDF
    # Template was called with an empty actividades list
    call_context = mock_render.call_args[0][1]
    assert call_context["actividades"] == []


@pytest.mark.asyncio
async def test_constancia_oculta_filas_legacy_primera_cuota_en_cuenta_recurrente(db: AsyncSession) -> None:
    """checklist/primera-cuota-2026-09-16, round 2, finding #4 (WARNING): the
    constancia must not print CEDULA/RUT/RPC/CDP rows the checklist itself has
    already decided to hide on a later cuota — those requisitos being
    permanently "Pendiente" on the certificate handed to the supervisor is a
    false signal for a contrato that is otherwise correctly radicado. Routes
    through `checklist_service.listar_filas_visibles`, the same seam
    `construir_checklist_completo` uses."""
    from app.models.cuenta_cobro import PosicionCuota
    from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
    from app.services import checklist_service

    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    primera = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
        numero_cuota=1,
        posicion=PosicionCuota.PRIMERA,
    )
    db.add(primera)
    await db.flush()
    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=2,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
        numero_cuota=2,
        posicion=PosicionCuota.RECURRENTE,
    )
    db.add(cuenta)
    await db.flush()

    # Legacy rows materialized before this rule shipped — empty, no linked doc.
    for codigo in ("CEDULA", "RUT", "RPC", "CDP"):
        db.add(
            DocumentoCuentaCobro(
                cuenta_cobro_id=cuenta.id,
                requisito_codigo=codigo,
                estado=EstadoRequisito.PENDIENTE,
            )
        )
    await db.commit()

    catalogo = await checklist_service.listar_catalogo(db)
    etiquetas_ocultas = {r.etiqueta for r in catalogo if r.codigo in {"CEDULA", "RUT", "RPC", "CDP"}}

    with patch(
        "app.services.constancia_service.generate_pdf_from_template",
        return_value=_FAKE_PDF,
    ) as mock_render:
        await constancia_service.generar_constancia_pdf(db, user.id, cuenta.id)

    call_context = mock_render.call_args[0][1]
    etiquetas = {item["etiqueta"] for item in call_context["checklist_items"]}
    assert not (etiquetas & etiquetas_ocultas)
