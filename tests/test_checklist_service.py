"""Tests for app.services.checklist_service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    EstadoRequisito,
)
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CHK-001",
        objeto="Servicios de checklist",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── asegurar_checklist ─────────────────────────────────────────────────────


async def test_asegurar_checklist_creates_rows_first_cuenta(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)

    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    # First cuenta → recurring + first-only requisitos
    assert "CONTRATO" in codigos
    assert "RPC" in codigos
    assert "SEGURIDAD_SOCIAL" in codigos
    assert "CEDULA" in codigos  # first-only
    assert "RUT" in codigos  # first-only
    assert "ACTA_INICIO" in codigos


async def test_asegurar_checklist_contract_level_appears_every_cuenta(db: AsyncSession, contrato: Contrato) -> None:
    """Contract-level requisitos (CONTRATO, RUT, CEDULA, ACTA_INICIO) appear on EVERY
    cuenta — they are auto-fulfilled by the shared contract-level document, so
    solo_primera_cuenta no longer hides them on later cuentas."""
    # Earlier cuenta
    await _make_cuenta(db, contrato, mes=1)
    # Later cuenta
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" in codigos
    assert "CEDULA" in codigos  # contract-level → shared, appears on every cuenta
    assert "RUT" in codigos
    assert "ACTA_INICIO" in codigos


async def test_asegurar_checklist_is_idempotent(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)

    filas1 = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    filas2 = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    assert len(filas1) == len(filas2)
    # No duplicate rows in DB
    from sqlalchemy import select

    res = await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id))
    rows = list(res.scalars().all())
    codigos = [r.requisito_codigo for r in rows]
    assert len(codigos) == len(set(codigos))


async def test_new_cuenta_does_not_inherit_old_cuenta_links(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Two-tier model: a new cuenta never copies stale LINKS, but contract-level
    documents (shared) re-derive via auto_vincular while cuenta-level documents
    (scoped to another cuenta) never leak in.
    """
    from sqlalchemy import select

    user = test_user["user"]

    async def _fila(cuenta_id, codigo):
        r = await db.execute(
            select(DocumentoCuentaCobro).where(
                DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
                DocumentoCuentaCobro.requisito_codigo == codigo,
            )
        )
        return r.scalar_one()

    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta1)

    # Contract-level document (CONTRATO): shared, cuenta_cobro_id NULL.
    df_contrato = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key="k/contrato",
        nombre="contrato.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
    )
    # Cuenta-level document (SEGURIDAD_SOCIAL): strictly scoped to cuenta1.
    df_cuenta = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta1.id,
        storage_key="k/ss",
        nombre="planilla.pdf",
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
    )
    db.add_all([df_contrato, df_cuenta])
    await db.commit()

    # Second cuenta: rows start PENDIENTE — no link copied over.
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()
    assert (await _fila(cuenta2.id, "CONTRATO")).estado == EstadoRequisito.PENDIENTE

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta2)
    await db.commit()

    # Contract-level CONTRATO re-derives from the shared document → CARGADO.
    assert (await _fila(cuenta2.id, "CONTRATO")).estado == EstadoRequisito.CARGADO
    # Cuenta-level SEGURIDAD_SOCIAL (scoped to cuenta1) never leaks into cuenta2.
    assert (await _fila(cuenta2.id, "SEGURIDAD_SOCIAL")).estado == EstadoRequisito.PENDIENTE


# ── detectar_desde_secop ───────────────────────────────────────────────────


async def test_detectar_desde_secop_scores_and_autolinks(db: AsyncSession, contrato: Contrato) -> None:
    # Seed SECOP docs with names that match keywords for distinct requisitos
    doc_contrato = SecopDocumento(
        id_documento_secop="DOC-1",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="Contrato firmado minuta clausulado.pdf",
        descripcion="Contrato",
        datos_raw={},
    )
    doc_rpc = SecopDocumento(
        id_documento_secop="DOC-2",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="RPC registro presupuestal compromiso presupuestal.pdf",
        descripcion="RP",
        datos_raw={},
    )
    doc_unrelated = SecopDocumento(
        id_documento_secop="DOC-3",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="Anexo Z.pdf",
        descripcion="",
        datos_raw={},
    )
    db.add_all([doc_contrato, doc_rpc, doc_unrelated])
    await db.commit()

    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    result = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert "CONTRATO" in result
    assert "RPC" in result
    # Top score for CONTRATO should be the contrato doc
    top_contrato_doc, top_score_contrato = result["CONTRATO"][0]
    assert top_contrato_doc.id == doc_contrato.id
    assert top_score_contrato >= Decimal("0.700")

    # Check row was auto-linked
    from sqlalchemy import select

    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == "CONTRATO",
        )
    )
    fila = res.scalar_one()
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc_contrato.id

    # Candidate rows persisted
    cand_res = await db.execute(
        select(DocumentoChecklistCandidato).where(
            DocumentoChecklistCandidato.cuenta_cobro_id == cuenta.id,
            DocumentoChecklistCandidato.requisito_codigo == "CONTRATO",
        )
    )
    candidatos = list(cand_res.scalars().all())
    assert len(candidatos) >= 1


# ── manual transitions ─────────────────────────────────────────────────────


async def test_marcar_no_aplica_and_cumplido_manual(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    fila = await checklist_service.marcar_no_aplica(db, cuenta.id, "DS_CONSECUTIVO")
    await db.commit()
    assert fila.estado == EstadoRequisito.NO_APLICA

    fila2 = await checklist_service.marcar_cumplido_manual(db, cuenta.id, "COMPROBANTE_PAGO_SS")
    await db.commit()
    assert fila2.estado == EstadoRequisito.CUMPLIDO_MANUAL


# ── resumen ────────────────────────────────────────────────────────────────


async def test_computar_resumen_marks_radicacion_lista_when_complete(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    catalogo = await checklist_service.listar_catalogo(db)

    from sqlalchemy import select

    res = await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id))
    filas = list(res.scalars().all())

    # Mark all obligatorios as cumplido_manual or no_aplica
    for fila in filas:
        req = next(c for c in catalogo if c.codigo == fila.requisito_codigo)
        if req.obligatorio:
            fila.estado = EstadoRequisito.CUMPLIDO_MANUAL
        else:
            fila.estado = EstadoRequisito.NO_APLICA
    await db.commit()

    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["pendientes"] == 0
    assert resumen["radicacion_lista"] is True
    assert resumen["cumplidos"] == resumen["total"]


async def test_computar_resumen_radicacion_no_lista_si_falta(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["pendientes"] > 0
    assert resumen["radicacion_lista"] is False


# ── 1:N document links per requisito ────────────────────────────────────────


async def _make_documento_fuente(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    cuenta: CuentaCobro,
    nombre: str,
    tipo: TipoDocumentoFuente = TipoDocumentoFuente.RPC,
) -> DocumentoFuente:
    df = DocumentoFuente(
        usuario_id=test_user["user"].id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key=f"k/{nombre}",
        nombre=nombre,
        tipo=tipo,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)
    return df


async def test_vincular_documento_fuente_es_idempotente(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")

    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()
    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    assert len(item["documentos_fuente"]) == 1
    assert item["documentos_fuente"][0]["id"] == df.id
    assert item["estado"] == EstadoRequisito.CARGADO


async def test_vincular_documento_fuente_multiples_agrega_sin_sobreescribir(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Linking 3 different documents to the same requisito must keep ALL of them
    (the previous behaviour overwrote the singular FK on every new link — data loss)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-original.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-adicion-1.pdf")
    df3 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-adicion-2.pdf")

    for df in (df1, df2, df3):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
        await db.commit()

    from sqlalchemy import select

    fila_res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == "RPC",
        )
    )
    fila = fila_res.scalar_one()
    assert fila.estado == EstadoRequisito.CARGADO
    # Primary slot must be the FIRST one linked — never overwritten by later links.
    assert fila.documento_fuente_id == df1.id

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    ids = [d["id"] for d in item["documentos_fuente"]]
    assert ids == [df1.id, df2.id, df3.id]
    assert item["documento_fuente"]["id"] == df1.id


async def test_vincular_documento_fuente_concurrent_insert_no_lanza(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Simulates a race: another request already inserted the same vinculo row
    between our idempotency SELECT and the INSERT. The IntegrityError raised by
    the unique constraint must be caught (via a savepoint) and treated as an
    idempotent no-op instead of propagating."""
    from unittest.mock import patch

    from app.models.documento_cuenta_cobro import DocumentoRequisitoVinculo

    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")

    fila = await checklist_service._get_fila(db, cuenta.id, "RPC")

    # A concurrent request "wins the race": inserts the vinculo AND promotes it
    # to primary before our call runs its own idempotency check.
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=df.id))
    fila.documento_fuente_id = df.id
    fila.estado = EstadoRequisito.CARGADO
    await db.commit()

    original_execute = db.execute
    call_count = {"n": 0}

    async def _fake_execute(stmt, *args, **kwargs):
        call_count["n"] += 1
        # 3rd db.execute inside vincular_documento_fuente is the "ya_vinculado"
        # idempotency SELECT — fake it as empty to simulate the TOCTOU race
        # (the row already exists, but our SELECT ran before the concurrent commit).
        if call_count["n"] == 3:

            class _EmptyResult:
                def scalar_one_or_none(self) -> None:
                    return None

            return _EmptyResult()
        return await original_execute(stmt, *args, **kwargs)

    with patch.object(db, "execute", side_effect=_fake_execute):
        result_fila = await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)

    assert result_fila.estado == EstadoRequisito.CARGADO
    assert result_fila.documento_fuente_id == df.id


async def test_vincular_secop_no_limpia_documento_fuente(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A SECOP link must coexist with an existing uploaded document (mixed sources)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")
    await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    sd = SecopDocumento(
        id_documento_secop="DOC-MIX-1",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="rpc-secop.pdf",
        descripcion="RPC",
        datos_raw={},
    )
    db.add(sd)
    await db.commit()

    fila = await checklist_service.vincular_secop_documento(db, cuenta.id, "RPC", sd.id)
    await db.commit()

    assert fila.documento_fuente_id == df.id  # NOT cleared
    assert fila.secop_documento_id == sd.id
    assert fila.estado == EstadoRequisito.CARGADO  # uploaded doc still outranks detection

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    assert len(item["documentos_fuente"]) == 1
    assert len(item["secop_documentos"]) == 1


async def test_desvincular_uno_no_afecta_los_demas(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    df3 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-3.pdf")
    for df in (df1, df2, df3):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    # Unlink a NON-primary document — the primary and the remaining one stay.
    fila = await checklist_service.desvincular(db, cuenta.id, "RPC", documento_fuente_id=df2.id)
    await db.commit()

    assert fila.documento_fuente_id == df1.id
    assert fila.estado == EstadoRequisito.CARGADO

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    ids = {d["id"] for d in item["documentos_fuente"]}
    assert ids == {df1.id, df3.id}


async def test_desvincular_primario_promueve_siguiente(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    for df in (df1, df2):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    # Unlink the PRIMARY (df1) — df2 must be promoted, estado stays CARGADO.
    fila = await checklist_service.desvincular(db, cuenta.id, "RPC", documento_fuente_id=df1.id)
    await db.commit()

    assert fila.documento_fuente_id == df2.id
    assert fila.estado == EstadoRequisito.CARGADO


async def test_desvincular_uno_preserva_cumplido_manual(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Unlinking ONE of several links must not clobber a manually-set estado
    (CUMPLIDO_MANUAL/NO_APLICA) with the auto-derived estado."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    for df in (df1, df2):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    fila = await checklist_service.marcar_cumplido_manual(db, cuenta.id, "RPC")
    await db.commit()
    assert fila.estado == EstadoRequisito.CUMPLIDO_MANUAL

    fila = await checklist_service.desvincular(db, cuenta.id, "RPC", documento_fuente_id=df2.id)
    await db.commit()

    assert fila.estado == EstadoRequisito.CUMPLIDO_MANUAL


async def test_desvincular_todos_los_links_vuelve_a_pendiente(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    for df in (df1, df2):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    await checklist_service.desvincular(db, cuenta.id, "RPC", documento_fuente_id=df1.id)
    await db.commit()
    fila = await checklist_service.desvincular(db, cuenta.id, "RPC", documento_fuente_id=df2.id)
    await db.commit()

    assert fila.documento_fuente_id is None
    assert fila.secop_documento_id is None
    assert fila.estado == EstadoRequisito.PENDIENTE

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    assert item["documentos_fuente"] == []


async def test_desvincular_legacy_sin_argumentos_remueve_todo(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """No-args call keeps the pre-existing behaviour: remove EVERY link at once."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    df1 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _make_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    for df in (df1, df2):
        await checklist_service.vincular_documento_fuente(db, cuenta.id, "RPC", df.id)
    await db.commit()

    fila = await checklist_service.desvincular(db, cuenta.id, "RPC")
    await db.commit()

    assert fila.documento_fuente_id is None
    assert fila.estado == EstadoRequisito.PENDIENTE
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "RPC")
    assert item["documentos_fuente"] == []
