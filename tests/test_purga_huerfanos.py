"""Tests for `purgar_huerfanos_cuentas` — set-based cleanup of legacy soft-delete
leftovers (tombstoned CuentaCobro rows whose children were never removed) plus
any row anywhere whose FK chain is broken. Mirrors `eliminar_cuenta_cobro`'s
FK-safe order, applied set-wide across every orphan at once.

The critical assertion in this module is that a HEALTHY cuenta's full tree is
left completely untouched — the purge must never touch valid data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from app.models.actividad import Actividad
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
)
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.requisito_documento import RequisitoDocumento
from app.models.secop import SecopDocumento
from app.models.usuario import Usuario
from app.services import cuenta_cobro_service
from sqlalchemy import Delete, Update, select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CATALOGO_CODIGO = "PURGA_TEST_CAT"


async def _make_user(db: AsyncSession, *, email: str = "purga@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Test User",
        cedula=str(uuid.uuid4().int)[:9],
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID, numero: str) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato=numero,
        objeto="Prestación de servicios para pruebas de purga",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Pruebas",
        dependencia="Dirección de Innovación",
        supervisor_nombre="Juan Supervisor",
    )
    db.add(contrato)
    await db.flush()
    return contrato


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])
    return storage


async def _ensure_catalogo(db: AsyncSession) -> None:
    """The RequisitoDocumento catalog is a shared global table (codigo is the PK) —
    seed the test's catalog row once, idempotently, instead of per cuenta."""
    existing = (
        await db.execute(select(RequisitoDocumento).where(RequisitoDocumento.codigo == _CATALOGO_CODIGO))
    ).scalar_one_or_none()
    if existing is None:
        db.add(RequisitoDocumento(codigo=_CATALOGO_CODIGO, etiqueta="Catálogo de prueba"))
        await db.flush()


async def _seed_full_tree(
    db: AsyncSession, usuario: Usuario, contrato: Contrato, *, mes: int, tombstoned: bool
) -> dict[str, Any]:
    """One row in every child table, scoped to a fresh cuenta — healthy or tombstoned."""
    await _ensure_catalogo(db)

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.ENVIADA,
        pdf_storage_key=f"pdfs/fake/{mes}.pdf",
    )
    if tombstoned:
        cuenta.deleted_at = datetime.now(UTC)
    db.add(cuenta)
    await db.flush()

    obligacion = Obligacion(contrato_id=contrato.id, descripcion="Obligación de prueba", tipo=TipoObligacion.GENERAL)
    db.add(obligacion)
    await db.flush()

    actividad = Actividad(cuenta_cobro_id=cuenta.id, descripcion="Actividad de prueba", obligacion_id=obligacion.id)
    db.add(actividad)
    await db.flush()

    evidencia = Evidencia(
        actividad_id=actividad.id, storage_key=f"evidencias/fake/{mes}.pdf", nombre_archivo=f"{mes}.pdf"
    )
    db.add(evidencia)
    await db.flush()

    enlace = EvidenciaObligacion(
        evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="alta", status="confirmed"
    )
    db.add(enlace)

    job = ClasificacionEvidenciasJob(cuenta_cobro_id=cuenta.id, status="done", total=1, procesadas=1)
    db.add(job)

    borrador = BorradorCuentaCobro(cuenta_cobro_id=cuenta.id, version=1, contenido={"html": "<p>v1</p>"})
    db.add(borrador)

    requisito_cuenta = RequisitoCuenta(cuenta_cobro_id=cuenta.id, codigo="CUSTOM", etiqueta="Custom requisito")
    db.add(requisito_cuenta)
    await db.flush()

    documento_fuente = DocumentoFuente(
        usuario_id=usuario.id,
        cuenta_cobro_id=cuenta.id,
        storage_key=f"documentos/fake/{mes}.pdf",
        nombre=f"{mes}.pdf",
        tipo=TipoDocumentoFuente.RPC,
    )
    db.add(documento_fuente)
    await db.flush()

    documento_cuenta = DocumentoCuentaCobro(cuenta_cobro_id=cuenta.id, requisito_cuenta_id=requisito_cuenta.id)
    db.add(documento_cuenta)
    await db.flush()

    vinculo = DocumentoRequisitoVinculo(
        documento_cuenta_cobro_id=documento_cuenta.id, documento_fuente_id=documento_fuente.id
    )
    db.add(vinculo)

    conversacion = Conversacion(usuario_id=usuario.id, cuenta_cobro_id=cuenta.id, mensajes_json=[])
    db.add(conversacion)

    secop_documento = SecopDocumento(id_documento_secop=f"SECOP-PURGA-{mes}", datos_raw={})
    db.add(secop_documento)
    await db.flush()

    candidato = DocumentoChecklistCandidato(
        cuenta_cobro_id=cuenta.id,
        requisito_codigo=_CATALOGO_CODIGO,
        secop_documento_id=secop_documento.id,
        score=Decimal("0.900"),
    )
    db.add(candidato)

    await db.flush()

    return {
        "cuenta": cuenta,
        "obligacion": obligacion,
        "actividad": actividad,
        "evidencia": evidencia,
        "enlace": enlace,
        "job": job,
        "borrador": borrador,
        "requisito_cuenta": requisito_cuenta,
        "documento_cuenta": documento_cuenta,
        "vinculo": vinculo,
        "documento_fuente": documento_fuente,
        "conversacion": conversacion,
        "candidato": candidato,
    }


async def test_purgar_huerfanos_cuentas(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-001-2024")

    healthy = await _seed_full_tree(db, user, contrato, mes=1, tombstoned=False)
    tombstoned = await _seed_full_tree(db, user, contrato, mes=2, tombstoned=True)

    # The next two rows have intentionally broken FKs — the corrupt state the purge
    # exists to clean. Postgres enforces FKs on insert, so disable FK triggers for this
    # session while seeding them (standard pg technique); SQLite ignores FKs anyway.
    is_pg = db.bind.dialect.name == "postgresql"
    if is_pg:
        await db.execute(text("SET session_replication_role = 'replica'"))
    try:
        # A dangling EvidenciaObligacion — its evidencia was already physically deleted
        # by some prior partial cleanup, but this link row was left behind.
        dangling_enlace = EvidenciaObligacion(
            evidencia_id=uuid.uuid4(), obligacion_id=healthy["obligacion"].id, confianza="media", status="proposed"
        )
        db.add(dangling_enlace)

        # An orphan Actividad whose cuenta is entirely gone (not even a tombstone row).
        orphan_actividad = Actividad(cuenta_cobro_id=uuid.uuid4(), descripcion="Actividad huérfana")
        db.add(orphan_actividad)
        await db.flush()
    finally:
        # Always restore FK enforcement, even if the seeding flush raised, so it
        # can't stay disabled for the rest of this connection's statements.
        if is_pg:
            await db.execute(text("SET session_replication_role = 'origin'"))

    await db.commit()

    storage = _mock_storage()
    counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, storage)
    await db.commit()

    # --- Healthy cuenta's ENTIRE tree must be untouched ---
    for key, model in [
        ("cuenta", CuentaCobro),
        ("actividad", Actividad),
        ("evidencia", Evidencia),
        ("enlace", EvidenciaObligacion),
        ("job", ClasificacionEvidenciasJob),
        ("borrador", BorradorCuentaCobro),
        ("requisito_cuenta", RequisitoCuenta),
        ("documento_cuenta", DocumentoCuentaCobro),
        ("vinculo", DocumentoRequisitoVinculo),
        ("candidato", DocumentoChecklistCandidato),
    ]:
        row_id = healthy[key].id
        assert (
            await db.execute(select(model).where(model.id == row_id))
        ).scalar_one_or_none() is not None, f"healthy {key} was touched by the purge"
    healthy_fuente = (
        await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == healthy["documento_fuente"].id))
    ).scalar_one()
    assert healthy_fuente.cuenta_cobro_id == healthy["cuenta"].id
    healthy_conv = (
        await db.execute(select(Conversacion).where(Conversacion.id == healthy["conversacion"].id))
    ).scalar_one()
    assert healthy_conv.cuenta_cobro_id == healthy["cuenta"].id

    # --- Every row scoped to the tombstoned cuenta must be GONE ---
    for key, model in [
        ("cuenta", CuentaCobro),
        ("actividad", Actividad),
        ("evidencia", Evidencia),
        ("enlace", EvidenciaObligacion),
        ("job", ClasificacionEvidenciasJob),
        ("borrador", BorradorCuentaCobro),
        ("requisito_cuenta", RequisitoCuenta),
        ("documento_cuenta", DocumentoCuentaCobro),
        ("vinculo", DocumentoRequisitoVinculo),
        ("candidato", DocumentoChecklistCandidato),
    ]:
        row_id = tombstoned[key].id
        assert (
            await db.execute(select(model).where(model.id == row_id))
        ).scalar_one_or_none() is None, f"tombstoned {key} survived the purge"
    tomb_fuente = (
        await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == tombstoned["documento_fuente"].id))
    ).scalar_one()
    assert tomb_fuente.cuenta_cobro_id is None
    tomb_conv = (
        await db.execute(select(Conversacion).where(Conversacion.id == tombstoned["conversacion"].id))
    ).scalar_one()
    assert tomb_conv.cuenta_cobro_id is None

    # --- The dangling link and the orphan actividad are gone too ---
    assert (
        await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.id == dangling_enlace.id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(Actividad).where(Actividad.id == orphan_actividad.id))
    ).scalar_one_or_none() is None

    # --- Counts dict ---
    assert counts["cuenta_cobro"] == 1
    assert counts["actividad"] == 2  # tombstoned's + the fully-orphan one
    assert counts["evidencia"] == 1
    assert counts["evidencia_obligacion"] == 2  # tombstoned's + the dangling one
    assert counts["clasificacion_job"] == 1
    assert counts["borrador"] == 1
    assert counts["documento_cuenta_cobro"] == 1
    assert counts["documento_requisito_vinculo"] == 1
    assert counts["documento_checklist_candidato"] == 1
    assert counts["requisito_cuenta"] == 1
    assert counts["documento_fuente_actualizados"] == 1
    assert counts["conversacion_actualizadas"] == 1

    # --- Storage cleanup: the tombstoned evidencia's storage_key + its pdf_storage_key ---
    storage.delete.assert_any_call("evidencias/fake/2.pdf")
    storage.delete.assert_any_call("pdfs/fake/2.pdf")
    storage.list_objects.assert_any_call(f"paquetes/{user.id}/{tombstoned['cuenta'].id}/")


async def test_purgar_huerfanos_cuentas_sin_huerfanos_no_borra_nada(db: AsyncSession) -> None:
    """Safe to re-run: a clean DB (only healthy data) purges zero rows."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-002-2024")
    await _seed_full_tree(db, user, contrato, mes=3, tombstoned=False)
    await db.commit()

    counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, _mock_storage())
    await db.commit()

    assert all(v == 0 for k, v in counts.items() if k not in ("storage_keys_ok", "storage_keys_failed"))


async def test_purgar_huerfanos_cuentas_storage_failure_no_bloquea(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-003-2024")
    tombstoned = await _seed_full_tree(db, user, contrato, mes=4, tombstoned=True)
    await db.commit()

    storage = _mock_storage()
    storage.delete = AsyncMock(side_effect=Exception("storage is down"))

    counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, storage)
    await db.commit()

    assert (
        await db.execute(select(CuentaCobro).where(CuentaCobro.id == tombstoned["cuenta"].id))
    ).scalar_one_or_none() is None
    assert counts["storage_keys_failed"] >= 1


# ---------------------------------------------------------------------------
# RISK-001 (CRITICAL, reliability review): the validity checks must be
# correlated subqueries evaluated fresh by the DB at each statement's own
# execution — never a Python list/set of ids captured once at function start
# and reused across ~10 sequential DELETE/UPDATE statements. A cuenta
# committed by a live writer mid-run would silently fall outside a frozen
# snapshot taken at function start and get purged as a false orphan (real
# under Postgres READ COMMITTED with the API live).
#
# Pinning this with real concurrency would need a second DB connection
# interleaved mid-transaction, which the single aiosqlite test session here
# can't express. Instead this pins it at the SQL-shape level: a
# `.not_in(list_of_ids)` compiles to `NOT IN (?, ?, ...)` (bound parameters,
# no nested SELECT); a `.not_in(select(...))` compiles to
# `NOT IN (SELECT ...)` — a correlated subquery the DB evaluates fresh every
# time that specific statement runs. This test fails against the pre-fix
# code (frozen Python sets) and passes only once every cuenta-scoped
# DELETE/UPDATE embeds a real subquery.
# ---------------------------------------------------------------------------


async def test_purgar_huerfanos_usa_subqueries_no_listas_congeladas(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-SUBQ-2024")
    await _seed_full_tree(db, user, contrato, mes=5, tombstoned=False)
    await db.commit()

    captured: list[str] = []
    real_execute = AsyncSession.execute

    async def _spy(self: AsyncSession, statement: object, *args: object, **kwargs: object) -> object:
        if isinstance(statement, Delete | Update) and statement.table.name in (
            "actividades",
            "clasificacion_evidencias_job",
            "borradores_cuenta_cobro",
            "requisitos_cuenta",
            "documentos_cuenta_cobro",
            "documento_checklist_candidatos",
            "documentos_fuente",
            "conversaciones",
        ):
            captured.append(str(statement))
        return await real_execute(self, statement, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(AsyncSession, "execute", _spy):
        await cuenta_cobro_service.purgar_huerfanos_cuentas(db, _mock_storage())
    await db.commit()

    assert captured, "expected at least one DELETE/UPDATE against a cuenta-scoped table"
    for sql in captured:
        assert "SELECT" in sql, f"expected a correlated subquery (no frozen id list), got: {sql}"


# ---------------------------------------------------------------------------
# RISK-002 / RES-CRITICAL (script guardrails) — service-level dry_run
# ---------------------------------------------------------------------------


async def test_purgar_huerfanos_cuentas_dry_run_no_muta_nada(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-DRY-2024")
    tombstoned = await _seed_full_tree(db, user, contrato, mes=6, tombstoned=True)
    await db.commit()

    storage = _mock_storage()
    counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, storage, dry_run=True)

    # Same candidate counts as a real run would report...
    assert counts["cuenta_cobro"] == 1
    assert counts["actividad"] == 1
    assert counts["evidencia"] == 1
    assert counts["evidencia_obligacion"] == 1
    assert counts["clasificacion_job"] == 1
    assert counts["borrador"] == 1
    assert counts["documento_cuenta_cobro"] == 1
    assert counts["documento_requisito_vinculo"] == 1
    assert counts["documento_checklist_candidato"] == 1
    assert counts["requisito_cuenta"] == 1
    assert counts["documento_fuente_actualizados"] == 1
    assert counts["conversacion_actualizadas"] == 1
    assert counts["storage_keys_ok"] == 0
    assert counts["storage_keys_failed"] == 0

    # ...but NOTHING was actually deleted/updated, and storage was never touched.
    assert (
        await db.execute(select(CuentaCobro).where(CuentaCobro.id == tombstoned["cuenta"].id))
    ).scalar_one_or_none() is not None
    assert (
        await db.execute(select(Actividad).where(Actividad.id == tombstoned["actividad"].id))
    ).scalar_one_or_none() is not None
    fuente = (
        await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == tombstoned["documento_fuente"].id))
    ).scalar_one()
    assert fuente.cuenta_cobro_id == tombstoned["cuenta"].id
    storage.delete.assert_not_called()
    storage.list_objects.assert_not_called()


# ---------------------------------------------------------------------------
# RELIABILITY-002 — explicit NULL guard on the nullable-FK UPDATE predicates.
# `col.not_in(subquery)` with a subquery that returns ZERO rows evaluates to
# TRUE for every row of `col` under standard SQL semantics — INCLUDING NULL
# rows (`NULL NOT IN (empty set)` is TRUE, unlike `NULL NOT IN (non-empty
# set)` which is NULL/excluded). With zero healthy cuentas system-wide, a
# `DocumentoFuente`/`Conversacion` row that was NEVER linked to any cuenta
# (`cuenta_cobro_id IS NULL`) must still be excluded from the "rows updated"
# count — only rows that actually pointed at a (now-tombstoned) cuenta count.
# ---------------------------------------------------------------------------


async def test_purgar_huerfanos_cuentas_cero_cuentas_validas_no_cuenta_fuentes_sin_cuenta(
    db: AsyncSession,
) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id, "PURGA-ZERO-2024")
    tombstoned = await _seed_full_tree(db, user, contrato, mes=7, tombstoned=True)

    # A contract-level document, never linked to any cuenta — must NOT be
    # touched or counted, even though there are zero valid cuentas at all.
    sin_cuenta = DocumentoFuente(
        usuario_id=user.id,
        cuenta_cobro_id=None,
        storage_key="documentos/fake/sin-cuenta.pdf",
        nombre="sin-cuenta.pdf",
        tipo=TipoDocumentoFuente.RPC,
    )
    db.add(sin_cuenta)
    await db.commit()

    counts = await cuenta_cobro_service.purgar_huerfanos_cuentas(db, _mock_storage())
    await db.commit()

    # Only the ONE row that actually pointed at the tombstoned cuenta counts.
    assert counts["documento_fuente_actualizados"] == 1

    refreshed = (await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == sin_cuenta.id))).scalar_one()
    assert refreshed.cuenta_cobro_id is None
    tomb_fuente = (
        await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == tombstoned["documento_fuente"].id))
    ).scalar_one()
    assert tomb_fuente.cuenta_cobro_id is None
