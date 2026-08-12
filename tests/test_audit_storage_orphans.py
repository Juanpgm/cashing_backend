"""Tests for scripts/audit_storage_orphans.py (Req 10c).

Covers: (a) listed object with no DB row → reported as orphan, (b) object WITH
a matching DB row → not an orphan, (c) --delete only removes orphans older
than --min-age-hours, (d) without --delete, storage.delete is never called.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from app.adapters.storage.port import StorageObjectInfo
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.usuario import Usuario
from scripts.audit_storage_orphans import Orphan, _run_scope, delete_orphans, find_orphans
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


def _mock_storage(objects: list[StorageObjectInfo]) -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=objects)
    storage.delete = AsyncMock()
    return storage


async def _make_evidencia_scenario(db: AsyncSession, *, storage_key: str | None) -> Evidencia:
    user = Usuario(
        email=f"{uuid.uuid4()}@test.com",
        nombre="Orphan Test User",
        cedula="998877665",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ORPHAN-001",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=datetime(2024, 1, 1, tzinfo=UTC).date(),
        fecha_fin=datetime(2024, 12, 31, tzinfo=UTC).date(),
    )
    db.add(contrato)
    await db.flush()

    cuenta = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, valor=3_000_000, estado=EstadoCuentaCobro.BORRADOR)
    db.add(cuenta)
    await db.flush()

    actividad = Actividad(cuenta_cobro_id=cuenta.id, descripcion="Actividad de prueba")
    db.add(actividad)
    await db.flush()

    evidencia = Evidencia(
        actividad_id=actividad.id,
        storage_key=storage_key,
        nombre_archivo="informe.pdf",
    )
    db.add(evidencia)
    await db.commit()
    return evidencia


async def test_object_without_db_row_is_orphan(db: AsyncSession) -> None:
    await _make_evidencia_scenario(db, storage_key="evidencias/u1/a1/keep-me.pdf")
    storage = _mock_storage(
        [
            StorageObjectInfo(key="evidencias/u1/a1/keep-me.pdf", size_bytes=100),
            StorageObjectInfo(key="evidencias/u1/a1/orphan.pdf", size_bytes=200),
        ]
    )

    orphans = await find_orphans(db, storage, "evidencias")

    assert [o.key for o in orphans] == ["evidencias/u1/a1/orphan.pdf"]


async def test_object_with_matching_db_row_is_not_orphan(db: AsyncSession) -> None:
    await _make_evidencia_scenario(db, storage_key="evidencias/u1/a1/keep-me.pdf")
    storage = _mock_storage([StorageObjectInfo(key="evidencias/u1/a1/keep-me.pdf", size_bytes=100)])

    orphans = await find_orphans(db, storage, "evidencias")

    assert orphans == []


async def test_delete_only_removes_orphans_older_than_min_age(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    old_orphan = Orphan(
        scope="evidencias", key="evidencias/u1/a1/old.pdf", size_bytes=10, last_modified=now - timedelta(hours=48)
    )
    young_orphan = Orphan(
        scope="evidencias", key="evidencias/u1/a1/young.pdf", size_bytes=10, last_modified=now - timedelta(hours=1)
    )
    storage = _mock_storage([])

    deleted, skipped = await delete_orphans(storage, [old_orphan, young_orphan], min_age_hours=24)

    assert deleted == 1
    assert skipped == 1
    storage.delete.assert_awaited_once_with("evidencias/u1/a1/old.pdf")


async def test_without_delete_flag_never_calls_storage_delete(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _make_evidencia_scenario(db, storage_key=None)
    now = datetime.now(UTC)
    old_object = StorageObjectInfo(
        key="evidencias/u1/a1/orphan.pdf", size_bytes=10, last_modified=now - timedelta(hours=48)
    )
    storage = _mock_storage([old_object])
    monkeypatch.setattr("scripts.audit_storage_orphans.get_storage", lambda bucket: storage)

    await _run_scope(db, "evidencias", do_delete=False, min_age_hours=24, limit=None)

    storage.delete.assert_not_called()
