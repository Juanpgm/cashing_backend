"""Tests for app.services.checklist_service."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
    EstadoRequisito,
)
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.requisito_cuenta import RequisitoCuenta
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
    """Mirrors `cuenta_cobro_service.crear_cuenta_cobro`'s own `posicion` derivation
    (checklist/primera-cuota-2026-09-16): the first `CuentaCobro` inserted for a
    given contrato is PRIMERA, every later one RECURRENTE. Before this fix every
    test cuenta silently defaulted to the model's RECURRENTE default regardless
    of intent — harmless while nivel-contrato codes were unconditionally exempt
    from `solo_primera_cuenta`, but load-bearing now that CEDULA/RUT/RPC/CDP (and
    conditionally CONTRATO) actually key off `_is_first_cuenta`.
    """
    existe_previa = (
        await db.execute(select(CuentaCobro.id).where(CuentaCobro.contrato_id == contrato.id).limit(1))
    ).scalar_one_or_none()
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        posicion=PosicionCuota.RECURRENTE if existe_previa is not None else PosicionCuota.PRIMERA,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── _CATALOGO_SEED flags (WU1) ──────────────────────────────────────────────


def test_catalogo_seed_rpc_cdp_contrato_son_solo_primera_cuenta() -> None:
    """checklist/primera-cuota-2026-09-16, rule 1/2: RPC, CDP and CONTRATO join
    CEDULA/RUT as solo_primera_cuenta in the code-side catalog seed. This alone
    only affects a brand-new/test DB (`_seed_catalogo_si_vacio` never UPDATEs an
    already-seeded row) — existing deployments need migration
    043_checklist_primera_cuota_flags."""
    seed_by_codigo = {item["codigo"]: item for item in checklist_service._CATALOGO_SEED}
    for codigo in ("CEDULA", "RUT", "RPC", "CDP", "CONTRATO"):
        assert seed_by_codigo[codigo]["solo_primera_cuenta"] is True, codigo
    # obligatorio values are untouched by this change.
    assert seed_by_codigo["CONTRATO"]["obligatorio"] is True
    assert seed_by_codigo["RPC"]["obligatorio"] is True
    assert seed_by_codigo["CDP"]["obligatorio"] is False


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


async def test_asegurar_checklist_identity_docs_hidden_on_later_cuenta(db: AsyncSession, contrato: Contrato) -> None:
    """checklist/primera-cuota-2026-09-16: CEDULA, RUT, RPC and CDP are requested
    ONLY on the first cuota — on a later cuota they must not appear as rows at
    all, unconditionally (rule 1). ACTA_INICIO is untouched by this rule and
    keeps its pre-existing "always visible, shared nivel-contrato doc" behaviour.
    CONTRATO is covered separately (it can reappear — see the CONTRATO tests)."""
    # Earlier cuenta
    await _make_cuenta(db, contrato, mes=1)
    # Later cuenta
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CEDULA" not in codigos
    assert "RUT" not in codigos
    assert "RPC" not in codigos
    assert "CDP" not in codigos
    assert "ACTA_INICIO" in codigos  # unaffected, out of scope for this rule


async def test_asegurar_checklist_contrato_hidden_on_later_cuenta_when_doc_and_obligaciones_exist(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16 rule 2: CONTRATO is first-cuota-only
    too, but ONLY when both exceptions are absent — a shared contract-level
    CONTRATO document exists AND the contrato has at least one Obligacion."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)

    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=None,
            storage_key="k/contrato",
            nombre="contrato.pdf",
            tipo=TipoDocumentoFuente.CONTRATO,
        )
    )
    db.add(
        Obligacion(
            contrato_id=contrato.id,
            descripcion="Obligación 1",
            tipo=TipoObligacion.GENERAL,
            orden=1,
        )
    )
    await db.commit()

    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" not in codigos


async def test_asegurar_checklist_contrato_reaparece_sin_documento_compartido(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Rule 2a: no shared contract-level CONTRATO document → CONTRATO reappears
    on a later cuota (obligaciones present, isolating this exception)."""
    await _make_cuenta(db, contrato, mes=1)

    db.add(
        Obligacion(
            contrato_id=contrato.id,
            descripcion="Obligación 1",
            tipo=TipoObligacion.GENERAL,
            orden=1,
        )
    )
    await db.commit()

    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" in codigos
    # The other 4 identity docs stay hidden — only CONTRATO reappears.
    assert "CEDULA" not in codigos
    assert "RUT" not in codigos
    assert "RPC" not in codigos
    assert "CDP" not in codigos


async def test_asegurar_checklist_contrato_reaparece_sin_obligaciones(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Rule 2b: the contrato has zero Obligacion rows (could not be computed) →
    CONTRATO reappears on a later cuota, even with a shared document present."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)

    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=None,
            storage_key="k/contrato",
            nombre="contrato.pdf",
            tipo=TipoDocumentoFuente.CONTRATO,
        )
    )
    await db.commit()

    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" in codigos
    assert "CEDULA" not in codigos
    assert "RUT" not in codigos


async def test_asegurar_checklist_custom_solo_primera_cuenta_unaffected(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Rule 3: custom (augment) `RequisitoCuenta` rows keep their existing
    `solo_primera_cuenta` behaviour — hidden on a later cuenta, same as before
    this change, with no CONTRATO-style exception."""
    cuenta1 = await _make_cuenta_custom(db, contrato, mes=1)
    await _make_requisito_custom(db, cuenta1, "POLIZA_CUMPLIMIENTO", "Póliza de cumplimiento")
    custom_first_only = RequisitoCuenta(
        cuenta_cobro_id=cuenta1.id,
        codigo="CARNET_VACUNAS",
        etiqueta="Carnet de vacunas",
        obligatorio=True,
        solo_primera_cuenta=True,
        keywords_deteccion=[],
        orden=501,
        origen="inferido",
        activo=True,
    )
    db.add(custom_first_only)
    await db.commit()

    cuenta2 = await _make_cuenta_custom(db, contrato, mes=2)
    # Custom rows are per-cuenta (only propagate to cuentas created after them via
    # `listar_requisitos_cuenta`, which is cuenta_cobro_id-scoped) — recreate the
    # same first-only custom item on cuenta2 to exercise the hiding rule itself.
    await _make_requisito_custom(db, cuenta2, "POLIZA_CUMPLIMIENTO", "Póliza de cumplimiento")
    custom_first_only_c2 = RequisitoCuenta(
        cuenta_cobro_id=cuenta2.id,
        codigo="CARNET_VACUNAS",
        etiqueta="Carnet de vacunas",
        obligatorio=True,
        solo_primera_cuenta=True,
        keywords_deteccion=[],
        orden=501,
        origen="inferido",
        activo=True,
    )
    db.add(custom_first_only_c2)
    await db.commit()

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    custom_codigos = {
        (await db.get(RequisitoCuenta, f.requisito_cuenta_id)).codigo for f in filas if f.requisito_cuenta_id
    }
    assert "POLIZA_CUMPLIMIENTO" in custom_codigos  # not solo_primera_cuenta → always applies
    assert "CARNET_VACUNAS" not in custom_codigos  # solo_primera_cuenta, later cuenta → hidden


async def test_asegurar_checklist_sin_primera_activa_trata_cuenta_como_primera(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 2, finding #2 (CRITICAL): if
    the contrato's PRIMERA cuenta was deleted (and nothing promoted a
    replacement — see `test_eliminar_cuenta_cobro_promueve_siguiente_a_primera_
    cuando_borra_la_primera` for that half of the fix), the surviving
    RECURRENTE cuenta must still get CEDULA/RUT/RPC/CDP/CONTRATO — otherwise
    those identity/budget documents become permanently unrequested for the
    whole contrato. `_construir_checklist_aplica_ctx` fails safe to
    is_first=True whenever the contrato has zero active PRIMERA cuentas."""
    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    # Hard-delete cuota 1 directly (isolates the checklist-side fail-safe from
    # `eliminar_cuenta_cobro`'s own promotion fix, tested separately).
    await db.delete(cuenta1)
    await db.commit()
    await db.refresh(cuenta2)
    assert cuenta2.posicion == PosicionCuota.RECURRENTE

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert {"CEDULA", "RUT", "RPC", "CDP", "CONTRATO"} <= codigos


async def test_asegurar_checklist_sin_primera_activa_solo_la_cuenta_mas_antigua_falla_segura(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #2 (WARNING): the
    fail-safe asked "does an ACTIVE PRIMERA exist?", so a soft-deleted
    (tombstoned) cuota 1 — a shape migration 025's backfill actively produces —
    made EVERY cuota of the contrato fail safe to first and re-materialize all
    five identity/budget rows, i.e. exactly the spam rule 1 exists to remove.
    The question that actually matters is "is THIS cuenta the earliest
    surviving one?", so only ONE cuenta fails safe."""
    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    cuenta3 = await _make_cuenta(db, contrato, mes=3)
    cuenta1.deleted_at = datetime(2024, 5, 1, tzinfo=UTC)
    await db.commit()

    filas2 = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()
    filas3 = await checklist_service.asegurar_checklist(db, cuenta3)
    await db.commit()

    solo_primera = {"CEDULA", "RUT", "RPC", "CDP"}
    assert solo_primera <= {f.requisito_codigo for f in filas2}
    assert not solo_primera & {f.requisito_codigo for f in filas3}


async def test_asegurar_checklist_custom_mapeado_respeta_su_propio_solo_primera_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 2, finding #5 (WARNING): a
    custom requisito explicitly mapped (`mapea_a_estandar`) to a standard code
    that rule 1 hides on a later cuenta (RUT here) must still materialize as
    the standard row when the CUSTOM item's OWN `solo_primera_cuenta=False`
    says it applies — an explicit mapping overrides the catalog rule instead
    of silently dropping the requisito with no row and no signal."""
    cuenta1 = await _make_cuenta_custom(db, contrato, mes=1)
    await _make_requisito_custom(
        db, cuenta1, "RUT_ACTUALIZADO", "RUT actualizado", mapea_a_estandar="RUT", solo_primera_cuenta=False
    )

    cuenta2 = await _make_cuenta_custom(db, contrato, mes=2)
    await _make_requisito_custom(
        db, cuenta2, "RUT_ACTUALIZADO", "RUT actualizado", mapea_a_estandar="RUT", solo_primera_cuenta=False
    )

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "RUT" in codigos  # mapped standard row must still materialize, not vanish


async def test_asegurar_checklist_custom_mapeado_no_anula_la_reaparicion_de_contrato(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #4 (WARNING): an
    explicit `mapea_a_estandar` mapping must be ADDITIVE to the catalog rule,
    never replace it. A custom item mapped to CONTRATO whose OWN
    `solo_primera_cuenta=True` used to cancel CONTRATO's rule-2 reappearance
    exception, so a contrato with no shared document and no obligaciones got NO
    row at all — neither standard (hidden by the mapping) nor custom (skipped
    because it maps to a standard code), i.e. the exact silent drop the mapping
    override exists to prevent."""
    await _make_cuenta_custom(db, contrato, mes=1)
    cuenta2 = await _make_cuenta_custom(db, contrato, mes=2)
    # No shared contract-level CONTRATO document and no Obligacion rows → both
    # halves of rule 2 fire, so CONTRATO must reappear on this later cuenta.
    await _make_requisito_custom(
        db,
        cuenta2,
        "CONTRATO_FIRMADO",
        "Contrato firmado",
        mapea_a_estandar="CONTRATO",
        solo_primera_cuenta=True,
    )

    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    codigos = {f.requisito_codigo for f in filas}
    assert "CONTRATO" in codigos


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


# ── modo_efectivo ────────────────────────────────────────────────────────────
# Pure function — no DB fixtures needed, `CuentaCobro()` here is a plain
# in-memory object never added to a session.


def test_modo_efectivo_defaults_null_to_estandar() -> None:
    cuenta = CuentaCobro(requisitos_modo=None)
    assert checklist_service.modo_efectivo(cuenta) == "estandar"


@pytest.mark.parametrize("modo", ["estandar", "augment", "reemplazar"])
def test_modo_efectivo_never_overwrites_an_explicit_choice(modo: str) -> None:
    cuenta = CuentaCobro(requisitos_modo=modo)
    assert checklist_service.modo_efectivo(cuenta) == modo


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


async def test_previsualizar_checklist_custom_mapeado_respeta_su_propio_solo_primera_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Mirrors `test_asegurar_checklist_custom_mapeado_respeta_su_propio_solo_
    primera_cuenta` for the preview path (round 2, finding #5)."""
    from app.schemas.requisito_cuenta import RequisitoEstructuradoItem

    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    catalogo = await checklist_service.listar_catalogo(db)

    candidato = RequisitoEstructuradoItem(
        id=None,
        codigo="RUT_ACTUALIZADO",
        etiqueta="RUT actualizado",
        mapea_a_estandar="RUT",
        solo_primera_cuenta=False,
        origen="inferido",
    )

    preview = checklist_service.previsualizar_checklist(cuenta2, catalogo, [candidato], modo="augment")

    codigos = {f["requisito_codigo"] for f in preview}
    assert "RUT" in codigos


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


@pytest.mark.parametrize(
    "estado,esperado",
    [
        (EstadoRequisito.CARGADO, True),
        (EstadoRequisito.DETECTADO, True),
        (EstadoRequisito.CUMPLIDO_MANUAL, True),
        (EstadoRequisito.NO_APLICA, True),
        (EstadoRequisito.PENDIENTE, False),
    ],
)
def test_es_estado_satisfecho(estado: EstadoRequisito, esperado: bool) -> None:
    """Single source of truth for "is this checklist row done" — shared by
    `computar_resumen` and `stepper_state_service._step5_formato` (WU6,
    checklist/primera-cuota-2026-09-16)."""
    assert checklist_service.es_estado_satisfecho(estado) is esperado


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


# ── lista_pendientes_desc (BUG A: bare UUID in CHECKLIST_INCOMPLETE message) ─
# billing-resilience-templates prodfix: `lista_pendientes` stays the load-bearing
# ref (codigo | str(uuid) — see `_get_fila`), but a NEW `lista_pendientes_desc`
# carries a human-readable "{codigo} — {etiqueta}" label, index-aligned with
# `lista_pendientes`, so user-facing messages never leak a bare UUID.


async def _make_cuenta_custom(db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024) -> CuentaCobro:
    """A cuenta in 'augment' mode so custom RequisitoCuenta rows materialize.

    Same `posicion` auto-derivation as `_make_cuenta` — see its docstring.
    """
    existe_previa = (
        await db.execute(select(CuentaCobro.id).where(CuentaCobro.contrato_id == contrato.id).limit(1))
    ).scalar_one_or_none()
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="augment",
        posicion=PosicionCuota.RECURRENTE if existe_previa is not None else PosicionCuota.PRIMERA,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _make_requisito_custom(
    db: AsyncSession,
    cuenta: CuentaCobro,
    codigo: str,
    etiqueta: str,
    *,
    activo: bool = True,
    obligatorio: bool = True,
    mapea_a_estandar: str | None = None,
    solo_primera_cuenta: bool = False,
) -> RequisitoCuenta:
    rc = RequisitoCuenta(
        cuenta_cobro_id=cuenta.id,
        codigo=codigo,
        etiqueta=etiqueta,
        obligatorio=obligatorio,
        solo_primera_cuenta=solo_primera_cuenta,
        keywords_deteccion=[],
        orden=500,
        origen="inferido",
        activo=activo,
        mapea_a_estandar=mapea_a_estandar,
    )
    db.add(rc)
    await db.commit()
    await db.refresh(rc)
    return rc


async def _completar_menos(
    db: AsyncSession, filas: list[DocumentoCuentaCobro], catalogo: list, codigo_dejar_pendiente: str | None
) -> None:
    """Mark every obligatorio row cumplido_manual/no_aplica EXCEPT one left pendiente."""
    cat_by_codigo = {c.codigo: c for c in catalogo}
    for fila in filas:
        if fila.requisito_codigo == codigo_dejar_pendiente:
            continue
        if fila.requisito_codigo is not None:
            req = cat_by_codigo.get(fila.requisito_codigo)
            if req is None:
                continue
            fila.estado = EstadoRequisito.CUMPLIDO_MANUAL if req.obligatorio else EstadoRequisito.NO_APLICA
        # custom rows left as-is by this helper — callers set them explicitly.
    await db.commit()


async def test_computar_resumen_lista_pendientes_desc_zero_pendientes(db: AsyncSession, contrato: Contrato) -> None:
    """Boundary: 0 pendientes → both lists empty."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente=None)

    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["lista_pendientes"] == []
    assert resumen["lista_pendientes_desc"] == []


async def test_computar_resumen_lista_pendientes_desc_only_catalog_pendiente(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Boundary: only a catalog requisito pending → desc uses '{codigo} — {etiqueta}'."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente="RPC")

    resumen = checklist_service.computar_resumen(filas, catalogo)
    req_rpc = next(c for c in catalogo if c.codigo == "RPC")
    assert resumen["lista_pendientes"] == ["RPC"]
    assert resumen["lista_pendientes_desc"] == [f"RPC — {req_rpc.etiqueta}"]


async def test_computar_resumen_lista_pendientes_desc_only_custom_pendiente(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Boundary + regression for BUG A: only a CUSTOM requisito pending → desc
    must carry its codigo/etiqueta, never the bare requisito_cuenta_id UUID."""
    cuenta = await _make_cuenta_custom(db, contrato, mes=1)
    rc = await _make_requisito_custom(db, cuenta, "POLIZA_CUMPLIMIENTO", "Póliza de cumplimiento")
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente=None)
    # The custom row is left PENDIENTE (default) — every catalog row above is
    # now resolved by `_completar_menos`.

    resumen = checklist_service.computar_resumen(filas, catalogo, custom_by_id={rc.id: rc})
    assert resumen["lista_pendientes"] == [str(rc.id)]
    assert resumen["lista_pendientes_desc"] == ["POLIZA_CUMPLIMIENTO — Póliza de cumplimiento"]
    # The raw UUID must never leak into the human-readable list.
    assert str(rc.id) not in resumen["lista_pendientes_desc"][0]


async def test_computar_resumen_lista_pendientes_desc_mixed_index_aligned(db: AsyncSession, contrato: Contrato) -> None:
    """Boundary: mixed catalog + custom pendientes stay index-aligned between
    `lista_pendientes` and `lista_pendientes_desc`."""
    cuenta = await _make_cuenta_custom(db, contrato, mes=1)
    rc = await _make_requisito_custom(db, cuenta, "POLIZA_CUMPLIMIENTO", "Póliza de cumplimiento")
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    # Leave RPC (catalog) and the custom row both pendiente; complete the rest.
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente="RPC")

    resumen = checklist_service.computar_resumen(filas, catalogo, custom_by_id={rc.id: rc})
    req_rpc = next(c for c in catalogo if c.codigo == "RPC")
    assert len(resumen["lista_pendientes"]) == len(resumen["lista_pendientes_desc"]) == 2
    for ref, desc in zip(resumen["lista_pendientes"], resumen["lista_pendientes_desc"], strict=True):
        if ref == "RPC":
            assert desc == f"RPC — {req_rpc.etiqueta}"
        elif ref == str(rc.id):
            assert desc == "POLIZA_CUMPLIMIENTO — Póliza de cumplimiento"
        else:
            pytest.fail(f"unexpected ref in lista_pendientes: {ref}")


async def test_computar_resumen_only_no_aplica_radicacion_no_vacuously_lista(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Boundary: only NO_APLICA obligatorio items (no cumplidos, no pendientes)
    must NOT vacuously report radicacion_lista=True."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    cat_by_codigo = {c.codigo: c for c in catalogo}
    for fila in filas:
        if fila.requisito_codigo is None:
            continue
        req = cat_by_codigo.get(fila.requisito_codigo)
        if req is None:
            continue
        fila.estado = EstadoRequisito.NO_APLICA
    await db.commit()

    resumen = checklist_service.computar_resumen(filas, catalogo)
    assert resumen["pendientes"] == 0
    assert resumen["total"] == 0
    assert resumen["radicacion_lista"] is False
    assert resumen["lista_pendientes"] == []
    assert resumen["lista_pendientes_desc"] == []


async def test_computar_resumen_orphaned_custom_requisito_skipped_from_both_lists(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Empty/null: a custom row whose RequisitoCuenta was deactivated (not in
    custom_by_id) must be skipped from BOTH lists without raising."""
    cuenta = await _make_cuenta_custom(db, contrato, mes=1)
    rc = await _make_requisito_custom(db, cuenta, "POLIZA_CUMPLIMIENTO", "Póliza de cumplimiento")
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente=None)

    # custom_by_id does NOT contain rc.id — simulates a deactivated/orphaned row
    # (`listar_requisitos_cuenta` only returns activo=True rows).
    resumen = checklist_service.computar_resumen(filas, catalogo, custom_by_id={})

    assert str(rc.id) not in resumen["lista_pendientes"]
    assert resumen["lista_pendientes_desc"] == []


async def test_computar_resumen_custom_etiqueta_blank_falls_back_to_bare_codigo(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Empty/null: a custom requisito with a blank/whitespace etiqueta falls
    back to its bare codigo in the human-readable desc (never the UUID)."""
    cuenta = await _make_cuenta_custom(db, contrato, mes=1)
    rc = await _make_requisito_custom(db, cuenta, "CUSTOM_SIN_ETIQUETA", "   ")
    filas = await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    catalogo = await checklist_service.listar_catalogo(db)
    await _completar_menos(db, filas, catalogo, codigo_dejar_pendiente=None)

    resumen = checklist_service.computar_resumen(filas, catalogo, custom_by_id={rc.id: rc})
    assert resumen["lista_pendientes_desc"] == ["CUSTOM_SIN_ETIQUETA"]


# ── construir_checklist_completo: read-time filter of legacy rows (WU3) ─────
# checklist/primera-cuota-2026-09-16: `asegurar_checklist` now refuses to
# MATERIALIZE new CEDULA/RUT/RPC/CDP/CONTRATO rows on a later cuenta (rule 1/2),
# but a row created before this fix shipped still sits in the DB. These tests
# insert such a "legacy" row directly (bypassing asegurar_checklist) and assert
# `construir_checklist_completo` hides it anyway, with no data migration.


async def test_construir_checklist_completo_hides_legacy_identity_rows_on_later_cuenta(
    db: AsyncSession, contrato: Contrato
) -> None:
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    # Simulate rows materialized before this fix: a direct insert, not via
    # asegurar_checklist (which would now correctly skip these codes).
    for codigo in ("CEDULA", "RUT", "RPC", "CDP"):
        db.add(
            DocumentoCuentaCobro(
                cuenta_cobro_id=cuenta2.id,
                requisito_codigo=codigo,
                estado=EstadoRequisito.PENDIENTE,
            )
        )
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    codigos_items = {i["requisito"]["codigo"] for i in payload["items"]}
    assert not codigos_items & {"CEDULA", "RUT", "RPC", "CDP"}
    assert not set(payload["resumen"]["lista_pendientes"]) & {"CEDULA", "RUT", "RPC", "CDP"}


async def test_construir_checklist_completo_hides_legacy_contrato_row_when_no_exception(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """A legacy CONTRATO row also hides on a later cuenta once a shared document
    AND at least one Obligacion exist (no reappearance exception applies)."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)

    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=None,
            storage_key="k/contrato",
            nombre="contrato.pdf",
            tipo=TipoDocumentoFuente.CONTRATO,
        )
    )
    db.add(Obligacion(contrato_id=contrato.id, descripcion="Obligación 1", tipo=TipoObligacion.GENERAL, orden=1))
    await db.commit()

    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    db.add(
        DocumentoCuentaCobro(
            cuenta_cobro_id=cuenta2.id,
            requisito_codigo="CONTRATO",
            estado=EstadoRequisito.PENDIENTE,
        )
    )
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    codigos_items = {i["requisito"]["codigo"] for i in payload["items"]}
    assert "CONTRATO" not in codigos_items
    assert "CONTRATO" not in payload["resumen"]["lista_pendientes"]


async def test_construir_checklist_completo_conserva_fila_legacy_con_documento(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 2, finding #3 (WARNING): the
    read-time filter must only hide EMPTY legacy rows (pendiente, no linked
    document) — a legacy CEDULA row that already carries a real uploaded
    document must stay visible, not vanish along with its document from every
    reader. It is marked `heredado` so it doesn't re-enter `lista_pendientes`."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    # cuenta_cobro_id=None: CEDULA is a nivel-contrato requisito, so a REALISTIC
    # legacy row points at the shared contract-level document (round 3, finding
    # #5 — the round-2 fixture used a cuenta-scoped doc, a tier mismatch the app
    # itself self-heals away, which made the test pass for the wrong reason).
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key="k/cedula",
        nombre="cedula.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    db.add(doc)
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta2.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.CARGADO,
        documento_fuente_id=doc.id,
    )
    db.add(fila)
    await db.flush()
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=doc.id))
    await db.commit()

    # The auto-link pass runs on this row before it is read back, so the
    # guarantee is asserted against post-self-heal state, not a snapshot.
    payload = await checklist_service.construir_checklist_completo(db, cuenta2, auto_vincular=True)

    codigos_items = {i["requisito"]["codigo"] for i in payload["items"]}
    assert "CEDULA" in codigos_items
    cedula_item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "CEDULA")
    assert cedula_item["heredado"] is True
    assert "CEDULA" not in payload["resumen"]["lista_pendientes"]


async def test_auto_vincular_self_heal_promueve_el_vinculo_del_tier_correcto(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #5 (WARNING): the
    tier self-heal deleted the vinculo matching the out-of-tier primary and then
    cleared the row to PENDIENTE — even when a perfectly valid, correctly-tiered
    vinculo was still attached. The row then read as empty and disappeared from
    every reader together with a document that is still linked. Promote the
    oldest surviving in-pool vinculo instead, exactly like `desvincular` does."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    doc_malo = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta2.id,  # cuenta-scoped → out of tier for nivel-contrato CEDULA
        storage_key="k/cedula-mala",
        nombre="cedula-vieja.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    doc_bueno = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,  # shared contract-level → correct tier
        storage_key="k/cedula-buena",
        nombre="cedula.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    db.add_all([doc_malo, doc_bueno])
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta2.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.CARGADO,
        documento_fuente_id=doc_malo.id,
    )
    db.add(fila)
    await db.flush()
    db.add_all(
        [
            DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=doc_malo.id),
            DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=doc_bueno.id),
        ]
    )
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta2)
    await db.commit()
    await db.refresh(fila)

    assert fila.documento_fuente_id == doc_bueno.id
    assert fila.estado == EstadoRequisito.CARGADO
    visibles = {f.requisito_codigo for f in await checklist_service.listar_filas_visibles(db, cuenta2)}
    assert "CEDULA" in visibles


async def test_auto_vincular_self_heal_deriva_el_estado_del_vinculo_secop_restante(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #3 (WARNING): the
    self-heal hard-coded estado=PENDIENTE, contradicting `_estado_segun_vinculos`
    — the single derivation every other estado writer uses. A row whose SECOP
    link survives the repair must read DETECTADO, not PENDIENTE."""
    user = test_user["user"]
    cuenta1 = await _make_cuenta(db, contrato, mes=1)

    sdoc = SecopDocumento(
        id_documento_secop="DOC-SELFHEAL",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="cedula.pdf",
        descripcion="Cédula",
        datos_raw={},
    )
    doc_malo = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta1.id,  # out of tier for nivel-contrato CEDULA
        storage_key="k/cedula-mala",
        nombre="cedula-vieja.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    db.add_all([sdoc, doc_malo])
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta1.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.CARGADO,
        documento_fuente_id=doc_malo.id,
        secop_documento_id=sdoc.id,
    )
    db.add(fila)
    await db.flush()
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=doc_malo.id))
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, secop_documento_id=sdoc.id))
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta1)
    await db.commit()
    await db.refresh(fila)

    assert fila.documento_fuente_id is None
    assert fila.secop_documento_id == sdoc.id
    assert fila.estado == EstadoRequisito.DETECTADO


@pytest.mark.parametrize("estado", [EstadoRequisito.NO_APLICA, EstadoRequisito.CUMPLIDO_MANUAL])
async def test_construir_checklist_completo_conserva_fila_con_decision_manual(
    db: AsyncSession, contrato: Contrato, estado: EstadoRequisito
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3 finding #6 CORRECTED by round
    4 findings #2/#3: "has content" means a real artifact OR an explicit human
    decision. Round 3 hid an artifact-free NO_APLICA/CUMPLIDO_MANUAL row on the
    premise that it could only be legacy data; `DELETE /documentos/{id}` builds
    exactly that shape at runtime (see
    `test_fila_con_estado_manual_sobrevive_al_borrado_de_su_documento`), and the
    constancia handed to the supervisor prints those estados by name. The row
    stays visible and flagged `heredado`, so it still contributes nothing to the
    radicar gate — visibility, not arithmetic, is what is restored."""
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    db.add(DocumentoCuentaCobro(cuenta_cobro_id=cuenta2.id, requisito_codigo="CEDULA", estado=estado))
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    item = next((i for i in payload["items"] if i["requisito"]["codigo"] == "CEDULA"), None)
    assert item is not None
    assert item["heredado"] is True
    assert "CEDULA" not in payload["resumen"]["lista_pendientes"]
    visibles = {f.requisito_codigo for f in await checklist_service.listar_filas_visibles(db, cuenta2)}
    assert "CEDULA" in visibles


async def test_construir_checklist_completo_conserva_fila_legacy_con_solo_un_vinculo(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #5 (WARNING): a row
    whose primary slot is empty but that still holds a `DocumentoRequisito
    Vinculo` carries a real document — `auto_vincular_documentos_fuente`'s
    tier self-heal can leave exactly that shape — so it must count as content
    and stay visible instead of silently dropping the linked document from
    every reader."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key="k/cedula-vinculo",
        nombre="cedula.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    db.add(doc)
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta2.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.PENDIENTE,
        documento_fuente_id=None,
    )
    db.add(fila)
    await db.flush()
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=doc.id))
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    cedula_item = next((i for i in payload["items"] if i["requisito"]["codigo"] == "CEDULA"), None)
    assert cedula_item is not None
    assert cedula_item["heredado"] is True
    assert "CEDULA" not in payload["resumen"]["lista_pendientes"]
    visibles = {f.requisito_codigo for f in await checklist_service.listar_filas_visibles(db, cuenta2)}
    assert "CEDULA" in visibles


async def test_construir_checklist_completo_fila_heredada_pendiente_no_bloquea_la_radicacion(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #3 (WARNING): a
    `heredado` row (kept visible only because it carries content, for a
    requisito that no longer applies to this cuota) must never re-enter
    `lista_pendientes` — the schema documents that guarantee but nothing
    enforced it, so a heredado row left PENDIENTE hard-blocked radicación on a
    requisito the cuota is formally not being asked for."""
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    sdoc = SecopDocumento(
        id_documento_secop="DOC-HEREDADO",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="cedula.pdf",
        descripcion="Cédula",
        datos_raw={},
    )
    db.add(sdoc)
    await db.flush()
    # Legacy row: content (a SECOP link) but still PENDIENTE.
    db.add(
        DocumentoCuentaCobro(
            cuenta_cobro_id=cuenta2.id,
            requisito_codigo="CEDULA",
            estado=EstadoRequisito.PENDIENTE,
            secop_documento_id=sdoc.id,
        )
    )
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    cedula_item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "CEDULA")
    assert cedula_item["heredado"] is True
    assert "CEDULA" not in payload["resumen"]["lista_pendientes"]


async def test_construir_checklist_completo_muestra_custom_mapeado_a_codigo_de_primera_cuota(
    db: AsyncSession, contrato: Contrato
) -> None:
    """checklist/primera-cuota-2026-09-16, round 3, finding #1 (CRITICAL): a
    custom requisito explicitly mapped to a standard code rule 1 hides
    (`mapea_a_estandar='RUT'`, own `solo_primera_cuenta=False`) materializes as
    the standard RUT row — but the read-time filter applied the plain catalog
    rule, so the row was invisible in EVERY reader (items, resumen, the radicar
    gate, the constancia seam). Materialized == visible: the mapping must be
    threaded into the visibility filter too, and the row is a genuine current-
    cuota requisito, NOT `heredado`."""
    await _make_cuenta_custom(db, contrato, mes=1)
    cuenta2 = await _make_cuenta_custom(db, contrato, mes=2)
    await _make_requisito_custom(
        db, cuenta2, "RUT_ACTUALIZADO", "RUT actualizado", mapea_a_estandar="RUT", solo_primera_cuenta=False
    )

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)
    await db.commit()

    rut_item = next((i for i in payload["items"] if i["requisito"]["codigo"] == "RUT"), None)
    assert rut_item is not None
    assert rut_item["heredado"] is False
    # obligatorio + pendiente + genuinely applies → it must block the radicar gate.
    assert "RUT" in payload["resumen"]["lista_pendientes"]

    visibles = {f.requisito_codigo for f in await checklist_service.listar_filas_visibles(db, cuenta2)}
    assert "RUT" in visibles


async def test_construir_checklist_completo_contrato_reaparecido_no_desaparece_al_subir_documento(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 2, finding #1 (CRITICAL):
    CONTRATO reappears (rule 2a, no shared doc yet) and materializes for
    cuenta2. Uploading the shared document flips `tiene_doc_contrato_
    compartido` back to True, which self-defeatingly re-hid CONTRATO at read
    time — dropping the very document the row asked the user for. Row
    existence + real content is the decision: the row must stay visible."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)
    db.add(Obligacion(contrato_id=contrato.id, descripcion="Obligación 1", tipo=TipoObligacion.GENERAL, orden=1))
    await db.commit()

    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    filas = await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()
    assert "CONTRATO" in {f.requisito_codigo for f in filas}  # reappeared (rule 2a)

    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,  # nivel-contrato upload → shared, cuenta_cobro_id NULL
        storage_key="k/contrato",
        nombre="contrato.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
    )
    db.add(doc)
    await db.commit()
    await checklist_service.vincular_documento_fuente(db, cuenta2.id, "CONTRATO", doc.id)
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    codigos_items = {i["requisito"]["codigo"] for i in payload["items"]}
    assert "CONTRATO" in codigos_items
    contrato_item = next(i for i in payload["items"] if i["requisito"]["codigo"] == "CONTRATO")
    assert contrato_item["estado"] == EstadoRequisito.CARGADO


async def test_construir_checklist_completo_first_cuenta_unaffected(db: AsyncSession, contrato: Contrato) -> None:
    """Regression guard: the first cuenta's own checklist response is untouched
    by the read-time filter — every standard row still shows up."""
    cuenta = await _make_cuenta(db, contrato, mes=1)

    payload = await checklist_service.construir_checklist_completo(db, cuenta)

    codigos_items = {i["requisito"]["codigo"] for i in payload["items"]}
    assert {"CEDULA", "RUT", "RPC", "CDP", "CONTRATO"} <= codigos_items


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


# ── arbol_evidencias: link-evidencia null file fields (BUG B) ───────────────
# app/models/evidencia.py's tipo_archivo/tamano_bytes are nullable (link-only
# evidencias created by evidence_persist_service leave both NULL) but
# ArbolEvidenciaItem required them non-nullable → GET /checklist 500s on any
# cuenta with legacy/link evidencia. Also asserts fuente/url are surfaced —
# without them a link-evidencia is unopenable/unidentifiable in the tree.


async def test_listar_arbol_evidencias_link_only_passes_fuente_y_url(db: AsyncSession, contrato: Contrato) -> None:
    """Empty/null: an all-null-file link-evidencia must not blow up building the
    tree, and its `fuente`/`url` must be passed through in the resulting dict."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await checklist_service.asegurar_checklist(db, cuenta)
    ob1 = await _make_obligacion(db, contrato, 1)
    await _make_actividad_con_evidencia(db, cuenta, ob1, solo_enlace=True)
    await db.commit()

    arbol = await checklist_service.listar_arbol_evidencias(db, cuenta)

    evidencias = [e for obl in arbol for act in obl["actividades"] for e in act["evidencias"]]
    assert len(evidencias) == 1
    ev = evidencias[0]
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None
    assert ev["fuente"] == "gmail"
    assert ev["url"] == "https://mail.example.com/x"


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


# ── checklist/primera-cuota-2026-09-16, round 4, findings #2/#3: an explicit
# human decision (CUMPLIDO_MANUAL / NO_APLICA) IS content. Round 3 redefined
# content as "a real artifact" on the premise that an artifact-free manual
# estado is a LEGACY shape; it is not — `DELETE /documentos/{id}` produces it at
# runtime, because `desvincular` deliberately preserves a manual override when
# the last link goes away. ──


async def test_fila_con_estado_manual_sobrevive_al_borrado_de_su_documento(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Runtime reproduction, no hand-built legacy row: on a later cuota the
    CONTRATO row materializes (rule 2, no shared contract document yet), the
    user links a document and marks the requisito cumplido manually, a SECOND
    shared contract document appears, and the first one is deleted through
    `DELETE /documentos/{id}`. CONTRATO then stops applying, and the row — whose
    only remaining content is the user's own decision — must not vanish from
    every reader."""
    from unittest.mock import AsyncMock, patch

    from app.services import document_service

    user = test_user["user"]
    db.add(
        Obligacion(contrato_id=contrato.id, descripcion="Obligación contractual", tipo=TipoObligacion.GENERAL, orden=1)
    )
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    await checklist_service.asegurar_checklist(db, cuenta2)
    await db.commit()

    def _doc(nombre: str) -> DocumentoFuente:
        return DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=None,
            storage_key=f"k/{nombre}",
            nombre=nombre,
            tipo=TipoDocumentoFuente.CONTRATO,
        )

    doc_a = _doc("contrato-a.pdf")
    db.add(doc_a)
    await db.commit()
    await checklist_service.vincular_documento_fuente(db, cuenta2.id, "CONTRATO", doc_a.id)
    await checklist_service.marcar_cumplido_manual(db, cuenta2.id, "CONTRATO")
    db.add(_doc("contrato-b.pdf"))
    await db.commit()

    fake = AsyncMock()
    fake.delete = AsyncMock()
    with patch("app.services.document_service._get_storage", return_value=fake):
        await document_service.eliminar_documento(db, user.id, doc_a.id)

    fila = (
        await db.execute(
            select(DocumentoCuentaCobro).where(
                DocumentoCuentaCobro.cuenta_cobro_id == cuenta2.id,
                DocumentoCuentaCobro.requisito_codigo == "CONTRATO",
            )
        )
    ).scalar_one()
    assert fila.estado == EstadoRequisito.CUMPLIDO_MANUAL
    assert fila.documento_fuente_id is None

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)
    item = next((i for i in payload["items"] if i["requisito"]["codigo"] == "CONTRATO"), None)
    assert item is not None, "the user's manual decision must survive deleting the document"
    assert item["heredado"] is True
    assert "CONTRATO" not in payload["resumen"]["lista_pendientes"]
    visibles = {f.requisito_codigo for f in await checklist_service.listar_filas_visibles(db, cuenta2)}
    assert "CONTRATO" in visibles


async def test_fila_pendiente_sin_artefacto_sigue_oculta(db: AsyncSession, contrato: Contrato) -> None:
    """The other half of the round-4 rule: only a HUMAN-set estado counts as
    content. A PENDIENTE placeholder with no artifact is still an empty legacy
    row and stays hidden (round 3, finding #6 — unchanged)."""
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    db.add(
        DocumentoCuentaCobro(cuenta_cobro_id=cuenta2.id, requisito_codigo="CEDULA", estado=EstadoRequisito.PENDIENTE)
    )
    await db.commit()

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)

    assert "CEDULA" not in {i["requisito"]["codigo"] for i in payload["items"]}


async def test_self_heal_borra_todo_vinculo_fuera_del_pool_no_solo_el_primario(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """checklist/primera-cuota-2026-09-16, round 4, finding #4 (WARNING): the
    tier self-heal deleted only the vinculo matching the row's PRIMARY slot, so
    any other wrong-tier vinculo survived — and once the primary slot is empty
    line `if fila.documento_fuente_id is None: continue` means the self-heal can
    never revisit the row, making the orphan permanent. Combined with vinculos
    counting as content, that orphan alone kept a rejected, wrong-tier document
    rendering on a cuota where the requisito does not apply."""
    user = test_user["user"]
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)

    # CEDULA is nivel-contrato, so its pool is the SHARED (cuenta_cobro_id IS
    # NULL) documents — both of these, scoped to the cuenta, are out of it.
    docs = []
    for nombre in ("cedula-mal-tier-1.pdf", "cedula-mal-tier-2.pdf"):
        d = DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=cuenta2.id,
            storage_key=f"k/{nombre}",
            nombre=nombre,
            tipo=TipoDocumentoFuente.CEDULA,
        )
        db.add(d)
        docs.append(d)
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta2.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.CARGADO,
        documento_fuente_id=docs[0].id,
    )
    db.add(fila)
    await db.flush()
    for d in docs:
        db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=d.id))
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta2)
    await db.commit()

    restantes = (
        (
            await db.execute(
                select(DocumentoRequisitoVinculo).where(
                    DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert restantes == [], "every out-of-pool vinculo must go, not just the primary one"

    payload = await checklist_service.construir_checklist_completo(db, cuenta2)
    assert "CEDULA" not in {i["requisito"]["codigo"] for i in payload["items"]}


async def test_self_heal_conserva_los_vinculos_que_si_estan_en_el_pool(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Control for the cleanup above: the self-heal must remove only wrong-tier
    links. A correctly-tiered vinculo is still promoted into the primary slot
    (round 3, finding #5) and keeps the row alive."""
    user = test_user["user"]
    cuenta1 = await _make_cuenta(db, contrato, mes=1)

    malo = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta1.id,
        storage_key="k/cedula-mal.pdf",
        nombre="cedula-mal.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    bueno = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=None,
        storage_key="k/cedula-bien.pdf",
        nombre="cedula-bien.pdf",
        tipo=TipoDocumentoFuente.CEDULA,
    )
    db.add_all([malo, bueno])
    await db.flush()
    fila = DocumentoCuentaCobro(
        cuenta_cobro_id=cuenta1.id,
        requisito_codigo="CEDULA",
        estado=EstadoRequisito.CARGADO,
        documento_fuente_id=malo.id,
    )
    db.add(fila)
    await db.flush()
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=malo.id))
    db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=bueno.id))
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, cuenta1)
    await db.commit()
    await db.refresh(fila)

    assert fila.documento_fuente_id == bueno.id
    assert fila.estado == EstadoRequisito.CARGADO
    restantes = (
        (
            await db.execute(
                select(DocumentoRequisitoVinculo.documento_fuente_id).where(
                    DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(restantes) == {bueno.id}
