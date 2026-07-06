"""Tests for the auto-link / SECOP-detection interaction in checklist_service.

These cover the regression where `construir_checklist_completo` auto-linked
DocumentoFuente on every GET (default auto_vincular=True), filling PENDIENTE
rows and silently blocking `detectar_desde_secop` from ever finding a PENDIENTE
row to attach SECOP documents to.

The contract being verified:
  - GET-equivalent (construir_checklist_completo with default) is READ-ONLY:
    it ensures rows exist but never auto-links uploaded documents.
  - Auto-link runs ONLY when explicitly requested (auto_vincular=True or the
    dedicated /auto-vincular-documentos endpoint).
  - Auto-link and SECOP detection both compete for PENDIENTE rows; the default
    read-only GET keeps that competition from happening implicitly.
  - Auto-link reaches documents with contrato_id=NULL owned by the same user.
  - Auto-link never overwrites a row that is not PENDIENTE.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-AUTOLINK-001",
        objeto="Servicios para auto-link",
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


async def _make_cuenta(
    db: AsyncSession, contrato: Contrato, mes: int = 1, anio: int = 2024
) -> CuentaCobro:
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


async def _add_documento_fuente(
    db: AsyncSession,
    *,
    usuario_id: Any,
    contrato_id: Any | None,
    tipo: TipoDocumentoFuente,
    nombre: str,
    cuenta_cobro_id: Any | None = None,
    categoria: CategoriaDocumento = CategoriaDocumento.OTROS,
    categoria_confianza: float | None = None,
    categoria_override: bool = False,
) -> DocumentoFuente:
    df = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        storage_key=f"k/{nombre}",
        nombre=nombre,
        tipo=tipo,
        categoria=categoria,
        categoria_confianza=categoria_confianza,
        categoria_override=categoria_override,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)
    return df


async def _fila(
    db: AsyncSession, cuenta: CuentaCobro, codigo: str
) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == codigo,
        )
    )
    return res.scalar_one()


# ── construir_checklist_completo default is READ-ONLY ───────────────────────


async def test_get_default_does_not_autolink(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A matching uploaded doc must NOT be linked by the default GET path."""
    user = test_user["user"]
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.CONTRATO,
        nombre="contrato.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)

    # Default call (auto_vincular omitted) — simulates GET /checklist.
    await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.documento_fuente_id is None


async def test_explicit_autovincular_true_links_document(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """auto_vincular=True is the opt-in that actually links the document."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # CONTRATO is a contract-level requisito → its document is shared (cuenta_cobro_id NULL).
    df = await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.CONTRATO,
        nombre="contrato.pdf",
    )

    await checklist_service.construir_checklist_completo(
        db, cuenta, auto_vincular=True
    )
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == df.id


# ── The regression: GET must not block SECOP detection ──────────────────────


async def test_get_then_secop_detection_still_links_secop(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """The exact bug: an uploaded doc + a SECOP doc both match CONTRATO.

    Before the fix, GET auto-linked the uploaded doc, leaving no PENDIENTE row
    for SECOP detection. After the fix, GET is read-only, so SECOP detection
    finds the row PENDIENTE and links the SECOP document.
    """
    user = test_user["user"]
    # Uploaded personal doc (no contrato_id) that WOULD win auto-link.
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=None,
        tipo=TipoDocumentoFuente.CONTRATO,
        nombre="mi-contrato.pdf",
    )
    # SECOP doc matching CONTRATO keywords.
    db.add(
        SecopDocumento(
            id_documento_secop="DOC-CTR",
            numero_contrato=contrato.numero_contrato,
            nombre_archivo="Contrato firmado minuta clausulado.pdf",
            descripcion="Contrato",
            datos_raw={},
        )
    )
    await db.commit()

    cuenta = await _make_cuenta(db, contrato)

    # Simulate GET /checklist (default, read-only).
    await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.PENDIENTE  # not pre-filled by GET

    # Now SECOP detection (POST /refresh-secop).
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id is not None


async def test_autolink_first_then_secop_does_not_override(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """If auto-link runs explicitly first, the row is CARGADO and SECOP
    detection must not overwrite the user's uploaded document."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # CONTRATO is contract-level → shared document (cuenta_cobro_id NULL).
    df = await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.CONTRATO,
        nombre="contrato.pdf",
    )
    db.add(
        SecopDocumento(
            id_documento_secop="DOC-CTR-2",
            numero_contrato=contrato.numero_contrato,
            nombre_archivo="Contrato firmado minuta clausulado.pdf",
            descripcion="Contrato",
            datos_raw={},
        )
    )
    await db.commit()

    await checklist_service.asegurar_checklist(db, cuenta)
    await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == df.id

    # SECOP detection should leave the manually/auto uploaded doc intact.
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "CONTRATO")
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == df.id
    assert fila.secop_documento_id is None


# ── auto_vincular_documentos_fuente behaviour ───────────────────────────────


async def test_autolink_ignores_null_contrato_docs(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Docs with a NULL contrato_id must NOT auto-link to this checklist.

    Cross-contract leak guard: only documents explicitly tied to THIS contrato
    are auto-linked. A NULL-contrato doc (or one from another contract) must be
    ignored, even if it matches by tipo/categoria.
    """
    user = test_user["user"]
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=None,  # not tied to any contrato → must be ignored
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert vinculados == 0
    fila = await _fila(db, cuenta, "RUT")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.documento_fuente_id is None


async def test_autolink_self_heals_existing_foreign_link(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """An existing CARGADO row linked to a doc from another contract must be
    reset to PENDIENTE on auto-vincular (self-heal of legacy cross-contract links)."""
    user = test_user["user"]
    # Document belonging to a DIFFERENT contract, already linked into this checklist.
    otro_contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-OTRO-888",
        objeto="Otro",
        valor_total=5_000_000,
        valor_mensual=500_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Otra",
        supervisor_nombre="Sup",
    )
    db.add(otro_contrato)
    await db.commit()
    await db.refresh(otro_contrato)
    df_foraneo = await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=otro_contrato.id,
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut-foraneo.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    # Force a cross-contract link directly (simulating the old broadened query).
    fila = await _fila(db, cuenta, "RUT")
    fila.documento_fuente_id = df_foraneo.id
    fila.estado = EstadoRequisito.CARGADO
    await db.commit()

    # auto-vincular must detect the foreign link and reset the row.
    await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "RUT")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.documento_fuente_id is None


async def test_autolink_ignores_other_contract_docs(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A document tied to ANOTHER contrato of the same user must not leak in."""
    user = test_user["user"]
    otro_contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-OTRO-999",
        objeto="Otro contrato",
        valor_total=5_000_000,
        valor_mensual=500_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Otra",
        supervisor_nombre="Sup",
    )
    db.add(otro_contrato)
    await db.commit()
    await db.refresh(otro_contrato)

    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=otro_contrato.id,  # belongs to a DIFFERENT contrato
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut-otro.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert vinculados == 0
    fila = await _fila(db, cuenta, "RUT")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.documento_fuente_id is None


async def test_autolink_ignores_docs_of_other_user(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A null-contrato doc owned by a different user must NOT be linked."""
    from app.core.security import hash_password
    from app.models.usuario import Usuario

    other = Usuario(
        email="other@example.com",
        nombre="Other",
        cedula="999999999",
        telefono="+573009999999",
        password_hash=hash_password("OtherPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=10,
    )
    db.add(other)
    await db.commit()
    await db.refresh(other)

    await _add_documento_fuente(
        db,
        usuario_id=other.id,
        contrato_id=None,
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut-ajeno.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert vinculados == 0
    fila = await _fila(db, cuenta, "RUT")
    assert fila.estado == EstadoRequisito.PENDIENTE


async def test_autolink_skips_below_threshold(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A doc with no matching signal (tipo=instrucciones, categoria=OTROS)
    scores 0 and must not be linked to anything."""
    user = test_user["user"]
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.INSTRUCCIONES,  # not in TIPO_A_REQUISITO
        nombre="instrucciones.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert vinculados == 0


async def test_autolink_never_overwrites_non_pendiente_row(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Rows marked NO_APLICA (or any non-PENDIENTE state) are never touched."""
    user = test_user["user"]
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut.pdf",
    )
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)
    await checklist_service.marcar_no_aplica(db, cuenta.id, "RUT")
    await db.commit()

    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "RUT")
    assert fila.estado == EstadoRequisito.NO_APLICA
    assert fila.documento_fuente_id is None
    assert vinculados == 0


async def test_autolink_override_beats_tipo_when_competing(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """When two docs target the same requisito, categoria_override (score 1.000)
    wins over a plain tipo match (score 0.750)."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # Plain tipo match → 0.750
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        nombre="planilla-tipo.pdf",
    )
    # Override on the SEGURIDAD_SOCIAL category → 1.000
    df_override = await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        nombre="planilla-override.pdf",
        categoria=CategoriaDocumento.SEGURIDAD_SOCIAL,
        categoria_confianza=0.5,
        categoria_override=True,
    )
    await checklist_service.asegurar_checklist(db, cuenta)

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta, "SEGURIDAD_SOCIAL")
    assert fila.estado == EstadoRequisito.CARGADO
    assert fila.documento_fuente_id == df_override.id
    assert fila.confianza_deteccion == Decimal("1.000")


async def test_autolink_idempotent_second_run_links_nothing_new(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Running auto-link twice must not re-link already-linked rows."""
    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # RUT is contract-level → shared document (cuenta_cobro_id NULL).
    await _add_documento_fuente(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.RUT,
        nombre="rut.pdf",
    )
    await checklist_service.asegurar_checklist(db, cuenta)

    first = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()
    second = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert first >= 1
    assert second == 0
