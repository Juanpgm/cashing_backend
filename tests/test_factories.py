"""Tests for the reusable factory-boy builders in `tests/factories.py`.

Covers slice 4.1 of radicacion-sin-friccion: real, importable `factory.Factory`
subclasses for the 5 core domain models (Usuario, Contrato, CuentaCobro,
RequisitoCuenta, Evidencia) instead of every test hand-rolling its own fixture
data. See `tests/factories.py` for the pattern rationale.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.evidencia import Evidencia
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.usuario import Usuario
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import (
    ActividadFactory,
    ContratoFactory,
    CuentaCobroFactory,
    EvidenciaFactory,
    RequisitoCuentaFactory,
    UsuarioFactory,
)

# Note: `asyncio_mode = "auto"` (pyproject.toml) — async tests need no marker. This
# module deliberately mixes sync (`.build()`, no DB) and async (`create_async()`,
# DB-backed) tests, so no module-level `pytestmark` is set (it would misfire
# warnings on the sync ones) — mirrors `test_cuota_position.py`.


# ── build() — unsaved instances need no DB ─────────────────────────────────


def test_usuario_factory_build_produces_valid_unsaved_instance() -> None:
    user = UsuarioFactory.build()

    assert isinstance(user, Usuario)
    assert user.email
    assert user.nombre
    assert user.password_hash


def test_contrato_factory_build_produces_valid_unsaved_instance() -> None:
    contrato = ContratoFactory.build()

    assert isinstance(contrato, Contrato)
    assert contrato.numero_contrato
    assert contrato.usuario is not None
    assert isinstance(contrato.usuario, Usuario)


# ── Uniqueness across two calls in the same test ────────────────────────────


def test_usuario_factory_email_is_unique_across_two_builds() -> None:
    first = UsuarioFactory.build()
    second = UsuarioFactory.build()

    assert first.email != second.email


def test_contrato_factory_numero_contrato_is_unique_across_two_builds() -> None:
    first = ContratoFactory.build()
    second = ContratoFactory.build()

    assert first.numero_contrato != second.numero_contrato


# ── Overriding a field is honored, not silently ignored ─────────────────────


def test_contrato_factory_override_valor_mensual_is_honored() -> None:
    contrato = ContratoFactory.build(valor_mensual=Decimal("5000000"))

    assert contrato.valor_mensual == Decimal("5000000")


# ── SubFactory chains: creating a child persists a valid parent chain ───────


async def test_cuenta_cobro_factory_create_async_creates_parent_contrato_and_usuario(
    db: AsyncSession,
) -> None:
    cuenta = await CuentaCobroFactory.create_async(db)
    await db.commit()

    assert cuenta.id is not None
    assert cuenta.contrato_id is not None

    contrato = (await db.execute(select(Contrato).where(Contrato.id == cuenta.contrato_id))).scalar_one()
    assert contrato.usuario_id is not None

    usuario = (await db.execute(select(Usuario).where(Usuario.id == contrato.usuario_id))).scalar_one()
    assert usuario.email


async def test_cuenta_cobro_factory_create_async_accepts_explicit_contrato(db: AsyncSession) -> None:
    contrato = await ContratoFactory.create_async(db, numero_contrato="CTR-EXPLICIT-001")
    await db.commit()

    cuenta = await CuentaCobroFactory.create_async(db, contrato=contrato)
    await db.commit()

    assert cuenta.contrato_id == contrato.id


async def test_requisito_cuenta_factory_create_async_creates_parent_cuenta_when_not_given(
    db: AsyncSession,
) -> None:
    requisito = await RequisitoCuentaFactory.create_async(db)
    await db.commit()

    assert requisito.cuenta_cobro_id is not None
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.id == requisito.cuenta_cobro_id))).scalar_one()
    assert cuenta.id == requisito.cuenta_cobro_id


async def test_evidencia_factory_create_async_creates_full_chain(db: AsyncSession) -> None:
    evidencia = await EvidenciaFactory.create_async(db)
    await db.commit()

    assert isinstance(evidencia, Evidencia)
    assert evidencia.id is not None
    actividad = (await db.execute(select(Actividad).where(Actividad.id == evidencia.actividad_id))).scalar_one()
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.id == actividad.cuenta_cobro_id))).scalar_one()
    assert cuenta.id is not None


async def test_actividad_factory_create_async_creates_parent_cuenta(db: AsyncSession) -> None:
    actividad = await ActividadFactory.create_async(db)
    await db.commit()

    assert actividad.cuenta_cobro_id is not None
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.id == actividad.cuenta_cobro_id))).scalar_one()
    assert cuenta.id == actividad.cuenta_cobro_id


# ── Round-trip through a real service call ───────────────────────────────────


async def test_cuenta_cobro_factory_round_trips_through_checklist_service(db: AsyncSession) -> None:
    """A factory-built CuentaCobro must not be missing a field some downstream
    service silently requires — proves the produced instance is genuinely usable,
    not just importable."""
    cuenta = await CuentaCobroFactory.create_async(db)
    await db.commit()

    resultado = await checklist_service.construir_checklist_completo(db, cuenta)

    assert resultado["cuenta_cobro_id"] == cuenta.id
    assert len(resultado["items"]) > 0


# ── RequisitoCuentaFactory: explicit parent is honored, not overridden ──────


async def test_requisito_cuenta_factory_create_async_honors_explicit_cuenta_cobro(
    db: AsyncSession,
) -> None:
    cuenta = await CuentaCobroFactory.create_async(db)
    await db.commit()

    requisito = await RequisitoCuentaFactory.create_async(db, cuenta_cobro=cuenta, codigo="CUSTOM_EXPLICIT")
    await db.commit()

    assert requisito.cuenta_cobro_id == cuenta.id
    assert isinstance(requisito, RequisitoCuenta)
