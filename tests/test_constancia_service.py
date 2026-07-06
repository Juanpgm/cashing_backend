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
        pdf_bytes, filename = await constancia_service.generar_constancia_pdf(
            db, user.id, cuenta.id
        )

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
        pdf_bytes, _ = await constancia_service.generar_constancia_pdf(
            db, user.id, cuenta.id
        )

    assert pdf_bytes == _FAKE_PDF
    # Template was called with an empty actividades list
    call_context = mock_render.call_args[0][1]
    assert call_context["actividades"] == []
