"""Tests for the `adiciones_contrato` model shape (billing-resilience-templates,
slice #4, task 4.2): `AdicionContrato` and `Obligacion.una_vez`.

Service-layer tests (`adicion_contrato_service.registrar_adicion`/`listar_adiciones`,
tasks 4.7-4.8) and tool-wrapper tests (task 4.13) live in
`tests/test_adicion_contrato_service.py`. R6-real-data and R7 (prórroga vs
`informe_final`) coverage lives in `tests/services/test_coherence_validator_service.py`
(tasks 4.10-4.11), alongside the existing R1-R6 catalog, since both exercise
`coherence_validator_service` directly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.models.adicion_contrato import AdicionContrato, TipoAdicion
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from sqlalchemy.ext.asyncio import AsyncSession


def test_tipo_adicion_enum_column_labels_match_lowercase_values() -> None:
    """Regression for the Postgres enum-label mismatch (same class of bug as
    slice #3's PosicionCuota C1): the ORM column MUST emit migration 026's
    lowercase `.value` labels, not the uppercase member names."""
    col_type = AdicionContrato.__table__.c.tipo.type
    assert set(col_type.enums) == {e.value for e in TipoAdicion}  # {"adicion","prorroga","otrosi"}
    assert set(col_type.enums) != {e.name for e in TipoAdicion}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ADICION-001",
        objeto="Servicios profesionales para pruebas de adiciones",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


# ── 4.2 — model shape ─────────────────────────────────────────────────────────


async def test_adicion_contrato_accepts_full_shape(db: AsyncSession, contrato: Contrato) -> None:
    evento = AdicionContrato(
        contrato_id=contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=1,
        rpc_nuevo="RPC-200",
        cdp_nuevo="CDP-200",
        valor_adicion=Decimal("1500000.00"),
        nueva_fecha_fin=date(2025, 3, 31),
        descripcion="Adición en valor y prórroga de tres meses.",
        fecha_evento=date(2024, 10, 1),
    )
    db.add(evento)
    await db.commit()
    await db.refresh(evento)

    assert evento.id is not None
    assert evento.tipo == TipoAdicion.ADICION
    assert evento.rpc_nuevo == "RPC-200"
    assert evento.cdp_nuevo == "CDP-200"
    assert evento.valor_adicion == Decimal("1500000.00")
    assert evento.nueva_fecha_fin == date(2025, 3, 31)
    assert evento.fecha_evento == date(2024, 10, 1)


async def test_adicion_contrato_accepts_null_valor_adicion(db: AsyncSession, contrato: Contrato) -> None:
    """Cross-change note (task 4.8): SECOP-sourced `valor_adicion` may be None
    (best-effort regex parse of free text) — the model must accept it as-is,
    never coerced to 0."""
    evento = AdicionContrato(
        contrato_id=contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=1,
        valor_adicion=None,
        descripcion="Adición sin monto parseable desde SECOP.",
        fecha_evento=date(2024, 10, 1),
    )
    db.add(evento)
    await db.commit()
    await db.refresh(evento)
    assert evento.valor_adicion is None


async def test_obligacion_una_vez_defaults_false(db: AsyncSession, contrato: Contrato) -> None:
    ob = Obligacion(contrato_id=contrato.id, descripcion="Entregar informe inicial", tipo=TipoObligacion.GENERAL)
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    assert ob.una_vez is False


async def test_obligacion_una_vez_accepts_true(db: AsyncSession, contrato: Contrato) -> None:
    ob = Obligacion(
        contrato_id=contrato.id, descripcion="Entregar dependientes", tipo=TipoObligacion.GENERAL, una_vez=True
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    assert ob.una_vez is True
