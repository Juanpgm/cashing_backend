"""Tests for app.services.checklist_service."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    EstadoRequisito,
)
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
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


async def test_previsualizar_checklist_reflects_structured_requisitos_without_persisting(
    db: AsyncSession, contrato: Contrato
) -> None:
    """billing-resilience-templates, slice #7, tasks 7.4-7.5: given structured
    (freshly-inferred, NOT YET persisted) requisitos, a checklist preview must
    reflect them WITHOUT writing anything to the DB until confirmed via the
    existing `POST /definir`."""
    from app.schemas.requisito_cuenta import RequisitoEstructuradoItem
    from sqlalchemy import select

    cuenta = await _make_cuenta(db, contrato, mes=1)
    catalogo = await checklist_service.listar_catalogo(db)

    candidato = RequisitoEstructuradoItem(
        id=None,
        codigo="POLIZA_CUMPLIMIENTO",
        etiqueta="Póliza de cumplimiento",
        categoria="polizas",
        obligatorio=True,
        solo_primera_cuenta=False,
        permite_autogen=False,
        origen="inferido",
    )

    preview = checklist_service.previsualizar_checklist(cuenta, catalogo, [candidato], modo="augment")

    codigos = {f["requisito_codigo"] for f in preview}
    assert "CONTRATO" in codigos  # standard catalog still included (augment)
    assert "POLIZA_CUMPLIMIENTO" in codigos  # the structured candidate is reflected

    # Nothing was persisted: no DocumentoCuentaCobro / RequisitoCuenta rows exist.
    filas = (
        (await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id)))
        .scalars()
        .all()
    )
    assert list(filas) == []


async def test_previsualizar_checklist_solo_primera_cuenta_hidden_on_later_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    from app.schemas.requisito_cuenta import RequisitoEstructuradoItem

    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    catalogo = await checklist_service.listar_catalogo(db)

    candidato = RequisitoEstructuradoItem(
        id=None,
        codigo="FICHA_TECNICA_CUSTOM",
        etiqueta="Ficha técnica custom",
        solo_primera_cuenta=True,
        origen="inferido",
    )

    preview = checklist_service.previsualizar_checklist(cuenta2, catalogo, [candidato], modo="augment")

    codigos = {f["requisito_codigo"] for f in preview}
    assert "FICHA_TECNICA_CUSTOM" not in codigos


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


# ── C1 (H8): explicit tipo is a first-class auto-link signal ────────────────


def test_score_fuente_tipo_explicito_alcanza_0950() -> None:
    """A user-declared tipo (content-verified for CONTRATO per A1) scores 0.950 —
    at least as strong as any non-override categoria confianza (classifier CAP),
    still below the manual-override 1.000."""
    doc = DocumentoFuente(
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
        categoria=CategoriaDocumento.OTROS,
        nombre="informe.pdf",
        storage_key="k/i",
    )
    assert checklist_service._score_fuente_para_requisito(doc, "INFORME_ACTIVIDADES") == Decimal("0.950")


def test_score_fuente_tipo_contrato_default_mantiene_0750() -> None:
    """tipo=contrato is the upload endpoint's DEFAULT — not a deliberate
    declaration — so it keeps the old 0.750 and never outranks a genuine
    high-confidence categoria for the CONTRATO row."""
    doc = DocumentoFuente(
        tipo=TipoDocumentoFuente.CONTRATO,
        categoria=CategoriaDocumento.OTROS,
        nombre="x.pdf",
        storage_key="k/x",
    )
    assert checklist_service._score_fuente_para_requisito(doc, "CONTRATO") == Decimal("0.750")


async def test_auto_vincular_tipo_explicito_con_categoria_otros(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Live case 027.2025: docs uploaded with an exact tipo (INFORME_ACTIVIDADES)
    but categoria='otros' stayed Pendiente — the explicit tipo alone must
    auto-link them to their requisito."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()

    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key="k/informe",
        nombre="documento sin señales en el nombre.pdf",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
        categoria=CategoriaDocumento.OTROS,
    )
    db.add(doc)
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    r = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == "INFORME_ACTIVIDADES",
        )
    )
    fila = r.scalar_one()
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == doc.id


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


# ── EVIDENCIAS derived from real coverage ───────────────────────────────────
# Regression: attaching evidencias to actividades never updates the persisted
# EVIDENCIAS row, so it stayed PENDIENTE forever and blocked radicación even
# with full coverage. construir_checklist_completo now derives its effective
# estado from the arbol (same LISTO semantics as the evidence packager).


async def _make_obligacion(db: AsyncSession, contrato: Contrato, orden: int) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id, descripcion=f"Obligación {orden}", tipo=TipoObligacion.GENERAL, orden=orden
    )
    db.add(ob)
    await db.flush()
    return ob


async def _make_actividad_con_evidencia(
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
    *,
    evidencia: bool = True,
    solo_enlace: bool = False,
) -> Actividad:
    act = Actividad(cuenta_cobro_id=cuenta.id, obligacion_id=obligacion.id, descripcion="Actividad")
    db.add(act)
    await db.flush()
    if evidencia:
        if solo_enlace:
            ev = Evidencia(
                actividad_id=act.id, nombre_archivo="Correo soporte", fuente="gmail", url="https://mail.example.com/x"
            )
        else:
            ev = Evidencia(
                actividad_id=act.id,
                storage_key=f"evidencias/{act.id}/soporte.pdf",
                nombre_archivo="soporte.pdf",
                tipo_archivo="application/pdf",
                tamano_bytes=1024,
            )
        db.add(ev)
        await db.flush()
    return act


async def _fila_evidencias(db: AsyncSession, cuenta: CuentaCobro) -> DocumentoCuentaCobro:
    from sqlalchemy import select

    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == "EVIDENCIAS",
        )
    )
    return res.scalar_one()


async def _marcar_resto_cumplido(db: AsyncSession, cuenta: CuentaCobro) -> None:
    """Satisfy every checklist row EXCEPT the EVIDENCIAS one (left PENDIENTE)."""
    from sqlalchemy import select

    catalogo = await checklist_service.listar_catalogo(db)
    res = await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id))
    for fila in res.scalars().all():
        if fila.requisito_codigo == "EVIDENCIAS":
            continue
        req = next(c for c in catalogo if c.codigo == fila.requisito_codigo)
        fila.estado = EstadoRequisito.CUMPLIDO_MANUAL if req.obligatorio else EstadoRequisito.NO_APLICA
    await db.commit()


async def test_evidencias_cobertura_completa_desbloquea_radicacion(db: AsyncSession, contrato: Contrato) -> None:
    """Full coverage → EVIDENCIAS counts as cumplido and radicacion_lista is True,
    without persisting anything on the row (derived, read-only GET)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    ob1 = await _make_obligacion(db, contrato, 1)
    ob2 = await _make_obligacion(db, contrato, 2)
    await _make_actividad_con_evidencia(db, cuenta, ob1)
    await _make_actividad_con_evidencia(db, cuenta, ob2)
    await db.commit()
    await _marcar_resto_cumplido(db, cuenta)

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "EVIDENCIAS")
    assert item["estado"] == EstadoRequisito.CARGADO
    assert "EVIDENCIAS" not in payload["resumen"]["lista_pendientes"]
    assert payload["resumen"]["radicacion_lista"] is True
    # Derived, not persisted: the row on disk stays PENDIENTE.
    assert (await _fila_evidencias(db, cuenta)).estado == EstadoRequisito.PENDIENTE


async def test_evidencias_cobertura_parcial_sigue_pendiente(db: AsyncSession, contrato: Contrato) -> None:
    """One obligación without evidencias → EVIDENCIAS stays PENDIENTE."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    ob1 = await _make_obligacion(db, contrato, 1)
    ob2 = await _make_obligacion(db, contrato, 2)
    await _make_actividad_con_evidencia(db, cuenta, ob1)
    await _make_actividad_con_evidencia(db, cuenta, ob2, evidencia=False)
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta)

    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "EVIDENCIAS")
    assert item["estado"] == EstadoRequisito.PENDIENTE
    assert "EVIDENCIAS" in payload["resumen"]["lista_pendientes"]
    assert payload["resumen"]["radicacion_lista"] is False


async def test_evidencias_solo_enlace_cuenta_como_cobertura(db: AsyncSession, contrato: Contrato) -> None:
    """A link-only evidencia (no stored file) counts as coverage — packager parity."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    ob1 = await _make_obligacion(db, contrato, 1)
    await _make_actividad_con_evidencia(db, cuenta, ob1, solo_enlace=True)
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta)

    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "EVIDENCIAS")
    assert item["estado"] == EstadoRequisito.CARGADO
    assert "EVIDENCIAS" not in payload["resumen"]["lista_pendientes"]


async def test_evidencias_cumplido_manual_se_preserva(db: AsyncSession, contrato: Contrato) -> None:
    """A manual override on the EVIDENCIAS row wins over the derived estado."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    ob1 = await _make_obligacion(db, contrato, 1)
    await _make_actividad_con_evidencia(db, cuenta, ob1)
    await db.commit()
    await checklist_service.marcar_cumplido_manual(db, cuenta.id, "EVIDENCIAS")
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta)

    item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "EVIDENCIAS")
    assert item["estado"] == EstadoRequisito.CUMPLIDO_MANUAL
    assert "EVIDENCIAS" not in payload["resumen"]["lista_pendientes"]


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
