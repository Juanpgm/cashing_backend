"""Tests for `definir_requisitos_checklist` (app/tools/catalog/requisitos.py).

THE blocker this closes: `crear_cuenta_cobro` deliberately leaves
`requisitos_modo=NULL` (app/services/cuenta_cobro_service.py). Before this tool
existed, no agent tool could ever call `requisito_cuenta_service.definir_set` —
so right after creating a cuenta, `resumen_checklist` returned an empty
checklist and `generar_informe_actividades`/`generar_informe_supervision`
raised "Definí primero los requisitos..." with no way for the agent to recover.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import DomainError, NotFoundError
from app.core.security import hash_password
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.usuario import Usuario
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_S3 = "app.services.document_service._get_storage"


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"reqtool_{suffix}@example.com",
        nombre=f"Requisitos Tool User {suffix}",
        cedula=f"7070{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"REQ-{suffix}",
        objeto="Objeto de prueba para requisitos tool",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"7070{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _crear_cuenta(ctx: ToolContext, contrato_id: uuid.UUID, mes: int = 4) -> uuid.UUID:
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato_id), "mes": mes, "anio": 2026},
    )
    return cuenta_response.id


def test_tool_registered() -> None:
    assert "definir_requisitos_checklist" in TOOL_REGISTRY


async def test_definir_requisitos_checklist_happy_path_estandar(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "01")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    result = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id)})

    assert result.modo == "estandar"
    assert result.cuenta_cobro_id == cuenta_id
    assert result.requisitos_custom == 0
    assert result.items, "expected the standard catalog to materialize checklist rows"
    # `resumen.total` counts only obligatorio requisitos; `items` includes every
    # materialized row (obligatorio + optional) — the former is always <= the latter.
    assert result.resumen.total > 0
    assert result.resumen.total <= len(result.items)


async def test_definir_requisitos_checklist_default_modo_is_estandar(db: AsyncSession) -> None:
    """`modo` is optional on the tool — omitting it must behave exactly like `estandar`,
    since that's the safe default when the user has no entity requirements document."""
    user, contrato = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    result = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id)})

    assert result.modo == "estandar"


async def test_definir_requisitos_checklist_unblocks_checklist_and_informe(db: AsyncSession) -> None:
    """THE regression this tool exists to fix: after `crear_cuenta_cobro`,
    `resumen_checklist` is empty/undefined and `generar_informe_actividades` raises
    'Definí primero los requisitos...'. Calling `definir_requisitos_checklist` (modo
    estandar) must unblock BOTH, given at least one activity exists."""
    user, contrato = await _make_user_with_contrato(db, "03")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    # Before: checklist is undefined.
    before = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta_id)})
    assert before.requisitos_definidos is False
    assert before.items == []

    # Before: informe generation is blocked by the missing checklist gate.
    with pytest.raises(DomainError, match="Definí primero los requisitos"):
        await invoke_tool("generar_informe_actividades", ctx, {"cuenta_id": str(cuenta_id)})

    # Apply the gate.
    result = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id)})
    assert result.modo == "estandar"

    # After: checklist is now defined and non-empty.
    after = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta_id)})
    assert after.requisitos_definidos is True
    assert after.items != []

    # Register an activity so the informe generator has something to write about.
    obligacion = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(obligacion)
    await db.flush()
    db.add(
        Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=obligacion.id,
            descripcion="Actividad realizada durante el período",
            justificacion="Justificación de la actividad realizada",
            fecha_realizacion=date(2026, 4, 10),
        )
    )
    await db.commit()

    # After: informe generation no longer raises the "Definí primero" gate error.
    storage = _fake_storage()
    with patch(_PATCH_S3, return_value=storage):
        informe_result = await invoke_tool("generar_informe_actividades", ctx, {"cuenta_id": str(cuenta_id)})
    assert informe_result.estado == "cargado"
    storage.upload.assert_called_once()


async def test_definir_requisitos_checklist_rejects_other_users_cuenta(db: AsyncSession) -> None:
    user_a, _contrato_a = await _make_user_with_contrato(db, "04a")
    user_b, contrato_b = await _make_user_with_contrato(db, "04b")

    ctx_b = ToolContext(db=db, usuario=user_b)
    cuenta_b_id = await _crear_cuenta(ctx_b, contrato_b.id)

    ctx_a = ToolContext(db=db, usuario=user_a)
    with pytest.raises(DomainError):
        await invoke_tool("definir_requisitos_checklist", ctx_a, {"cuenta_id": str(cuenta_b_id)})

    # No data leakage: cuenta B's requisitos_modo must remain untouched by A's attempt.
    cuenta_b = await db.get(CuentaCobro, cuenta_b_id)
    assert cuenta_b is not None
    assert cuenta_b.requisitos_modo is None


async def test_definir_requisitos_checklist_unknown_cuenta_raises_not_found(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "05")
    ctx = ToolContext(db=db, usuario=user)

    with pytest.raises(NotFoundError):
        await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(uuid.uuid4())})


async def test_definir_requisitos_checklist_augment_idempotent_no_duplicate_rows(db: AsyncSession) -> None:
    """`definir_set` is documented as replacing the custom set every call — calling
    the tool twice with the SAME custom requisito must not accumulate duplicate
    `RequisitoCuenta` rows (dedup by codigo, and the previous set is dropped first)."""
    user, contrato = await _make_user_with_contrato(db, "06")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    payload = {
        "cuenta_id": str(cuenta_id),
        "modo": "augment",
        "requisitos": [
            {
                "codigo": "POLIZA",
                "etiqueta": "Póliza de cumplimiento",
                "obligatorio": True,
            }
        ],
    }

    first = await invoke_tool("definir_requisitos_checklist", ctx, payload)
    second = await invoke_tool("definir_requisitos_checklist", ctx, payload)

    assert first.requisitos_custom == 1
    assert second.requisitos_custom == 1

    rows = await db.execute(
        select(RequisitoCuenta).where(
            RequisitoCuenta.cuenta_cobro_id == cuenta_id,
            RequisitoCuenta.codigo == "POLIZA",
            RequisitoCuenta.activo.is_(True),
        )
    )
    assert len(rows.scalars().all()) == 1


# --- F4: definir_requisitos_checklist is destructive on re-call once documents ---
# are linked (RequisitoCuenta bulk-delete CASCADEs DocumentoCuentaCobro, including
# CARGADO rows). Defensive fix at the TOOL layer: short-circuit instead of
# re-calling definir_set once the cuenta has real progress.


async def test_definir_requisitos_checklist_recall_with_no_progress_still_applies(db: AsyncSession) -> None:
    """Branch 1 (guard NOT triggered): requisitos_modo is already set but NO
    checklist row has progressed past pendiente — re-calling must still apply
    normally (this is the routine idempotent-custom-set case, not the
    destructive one)."""
    user, contrato = await _make_user_with_contrato(db, "07")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    first = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id), "modo": "estandar"})
    assert first.aviso is None

    second = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id), "modo": "augment"})
    assert second.aviso is None
    assert second.modo == "augment"


async def test_definir_requisitos_checklist_recall_with_linked_documents_is_a_noop(db: AsyncSession) -> None:
    """Branch 2 (guard TRIGGERED): the cuenta already has requisitos_modo set AND
    at least one non-pendiente checklist row (a document was linked) — a re-call
    must NOT destroy it. Proves the exact data-loss shape: RequisitoCuenta bulk
    delete CASCADEs DocumentoCuentaCobro via ondelete=CASCADE, wiping a CARGADO
    row's link."""
    from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
    from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente

    user, contrato = await _make_user_with_contrato(db, "08")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    await invoke_tool(
        "definir_requisitos_checklist",
        ctx,
        {
            "cuenta_id": str(cuenta_id),
            "modo": "augment",
            "requisitos": [{"codigo": "POLIZA", "etiqueta": "Póliza de cumplimiento", "obligatorio": True}],
        },
    )

    # Simulate a linked document on the custom requisito row (what a real
    # importar_documento call would have produced).
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta_id,
        storage_key="fake/key",
        nombre="poliza.pdf",
        tipo=TipoDocumentoFuente.INSTRUCCIONES,
    )
    db.add(doc)
    await db.flush()

    fila_res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo.is_(None),
            DocumentoCuentaCobro.requisito_cuenta_id.is_not(None),
        )
    )
    fila_poliza = fila_res.scalars().first()
    assert fila_poliza is not None, "expected the custom POLIZA checklist row to exist"
    fila_poliza.documento_fuente_id = doc.id
    fila_poliza.estado = EstadoRequisito.CARGADO
    await db.commit()

    result = await invoke_tool(
        "definir_requisitos_checklist",
        ctx,
        {"cuenta_id": str(cuenta_id), "modo": "estandar"},  # attempting to REPLACE the set
    )

    assert result.aviso is not None
    assert result.modo == "augment", "modo must stay unchanged — the re-call must be a no-op"

    # The custom requisito row (and its CARGADO link) must survive untouched.
    rows = await db.execute(
        select(RequisitoCuenta).where(
            RequisitoCuenta.cuenta_cobro_id == cuenta_id,
            RequisitoCuenta.codigo == "POLIZA",
            RequisitoCuenta.activo.is_(True),
        )
    )
    assert len(rows.scalars().all()) == 1, "the custom requisito must not have been deleted"

    fila_after = await db.get(DocumentoCuentaCobro, fila_poliza.id)
    assert fila_after is not None, "the CASCADE-deleted checklist row must not have been wiped"
    assert fila_after.estado == EstadoRequisito.CARGADO
    assert fila_after.documento_fuente_id == doc.id
