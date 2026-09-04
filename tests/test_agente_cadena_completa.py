"""End-to-end proof that the agent's tool chain composes into a full radicación.

Every other test in this suite exercises ONE tool. This one drives the whole
sequence the chat agent is expected to follow, through the real `invoke_tool`
dispatch path, and asserts the cuenta de cobro actually reaches
`radicacion_lista` and can be filed.

That distinction matters: the bug this feature set exists to fix was invisible
to per-tool tests. `crear_cuenta_cobro` leaves `requisitos_modo = NULL` (a gate
built for the UI), so before `definir_requisitos_checklist` existed the chain
died one step after creating a cuenta — with every individual tool still
passing its own test. `test_chain_dies_without_definir_requisitos` below pins
that dead end so it can never silently come back.

Only storage is faked (this suite has no S3 egress); every DB write, ownership
check, checklist derivation and state transition is real.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import ValidationError
from app.core.security import hash_password
from app.models.actividad import Actividad, JustificacionOrigen
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_DOC_S3 = "app.services.document_service._get_storage"
_PATCH_EVI_S3 = "app.tools.catalog.evidencias._get_storage"

# The contract-level supports a contratista uploads once, and the one
# cuenta-level support that changes every month.
_SOPORTES: list[tuple[str, str]] = [
    ("contrato", "contrato.txt"),
    ("rpc", "rpc.txt"),
    ("cedula", "cedula.txt"),
    ("rut", "rut.txt"),
    ("acta_inicio", "acta_inicio.txt"),
    ("seguridad_social", "planilla_pila.txt"),
]


_OBLIGACIONES_REALISTAS: list[str] = [
    "Apoyar la formulación y el seguimiento de los planes de acción del área asignada.",
    "Elaborar los informes técnicos mensuales requeridos por la supervisión del contrato.",
    "Participar en las mesas de trabajo interinstitucionales convocadas por la entidad.",
    "Realizar el cargue y la depuración de la información en los sistemas de la entidad.",
    "Acompañar las jornadas de socialización con la comunidad en el territorio asignado.",
]


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    storage.presigned_url = AsyncMock(return_value="https://example.com/presigned")
    return storage


async def _sembrar_contratista(db: AsyncSession, suffix: str, obligaciones: int = 3) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"cadena_{suffix}@example.com",
        nombre=f"Cadena {suffix}",
        cedula=f"7272{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"CADENA-{suffix}",
        objeto="Prestación de servicios profesionales de apoyo a la gestión",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"7272{suffix}",
    )
    db.add(contrato)
    await db.flush()

    # Deliberately distinct texts: coherence rule R5 blocks radicación when two
    # obligaciones are near-identical, because evidence assignment by text would
    # be ambiguous. Near-duplicate fixture text made this test fail at the gate —
    # correctly — so the seed mirrors how a real contract reads.
    for i, descripcion in enumerate(_OBLIGACIONES_REALISTAS[:obligaciones]):
        db.add(
            Obligacion(
                contrato_id=contrato.id,
                descripcion=descripcion,
                tipo=TipoObligacion.ESPECIFICA,
                orden=i,
                etiqueta=f"OE{i + 1}",
            )
        )

    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _adjuntar(ctx: ToolContext, filename: str) -> None:
    ctx.attachments[filename] = ToolAttachment(
        filename=filename,
        content_type="text/plain",
        data=f"contenido de prueba para {filename}".encode(),
    )


@pytest.mark.asyncio
async def test_cadena_completa_hasta_radicar(db: AsyncSession) -> None:
    """Drive the documented playbook end to end and assert the cuenta is filed."""
    user, contrato = await _sembrar_contratista(db, "full")
    ctx = ToolContext(db=db, usuario=user)

    # 1. Discover the contract the way the agent must (never inventing a UUID).
    contratos = await invoke_tool("listar_contratos", ctx, {})
    assert len(contratos.contratos) == 1
    contrato_id = str(contratos.contratos[0].id)
    assert contrato_id == str(contrato.id)

    # 2. Create the cuenta de cobro.
    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": contrato_id, "mes": 4, "anio": 2026})
    cuenta_id = str(cuenta.id)

    # 3. THE step that used to be impossible from chat.
    definido = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": cuenta_id})
    assert definido.modo == "estandar"
    assert definido.aviso is None
    assert len(definido.items) > 0

    # 4. Upload every mandatory support, each with its own tipo — impossible
    #    before `importar_documento` accepted more than 3 tipos.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        for tipo, filename in _SOPORTES:
            await _adjuntar(ctx, filename)
            await invoke_tool(
                "importar_documento",
                ctx,
                {"filename": filename, "tipo": tipo, "cuenta_cobro_id": cuenta_id},
            )

    await invoke_tool("auto_vincular_documentos", ctx, {"cuenta_id": cuenta_id})

    # 5. Actividades from the contract's obligaciones (deterministic, no LLM).
    actividades = await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": cuenta_id})
    assert actividades.creadas == 3

    # 6. Evidence files, through the tool (exercises validation + storage + the
    #    find-or-create stub Actividad path).
    evidencias = [f"evidencia_{i + 1}.txt" for i in range(3)]
    for filename in evidencias:
        await _adjuntar(ctx, filename)
    with patch(_PATCH_EVI_S3, return_value=_fake_storage()):
        await invoke_tool("subir_evidencias_desde_chat", ctx, {"cuenta_id": cuenta_id, "filenames": evidencias})

    # 6b. Uploading files is NOT enough on its own: EVIDENCIAS is derived state
    #     (`checklist_service.construir_checklist_completo`) requiring EVERY
    #     obligación to be covered — by an evidencia attached to its actividad, a
    #     CONFIRMED link, or a real justificación. Attaching evidence to an
    #     obligación needs the LLM classifier, which is unreachable in this suite,
    #     so the files land on the shared "sin clasificar" stub and coverage stays
    #     incomplete. Pinning that here documents the dependency instead of hiding
    #     it — everything else is already satisfied at this point.
    parcial = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": cuenta_id})
    assert "EVIDENCIAS" in parcial.resumen.lista_pendientes

    # 6c. Stand in for what `persistir_evidencias` does in production (Gmail/Drive
    #     discovery writes one justificación per obligación with origen="llm" —
    #     `informe_service._tiene_justificacion_real`). Neither the LLM nor OAuth is
    #     reachable offline, so the coverage is written directly; every gate that
    #     consumes it below is the real one.
    acts = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))).scalars().all()
    cubiertas = [a for a in acts if a.obligacion_id is not None]
    assert len(cubiertas) == 3, "crear_actividades_desde_obligaciones debía dejar una actividad por obligación"
    for act in cubiertas:
        act.justificacion = "Justificación de cobertura para la obligación, generada durante el período."
        act.justificacion_origen = JustificacionOrigen.LLM
    await db.commit()

    # 7. The two mandatory informes, generated entirely from data already stored.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        await invoke_tool("generar_informe_actividades", ctx, {"cuenta_id": cuenta_id})
        await invoke_tool("generar_informe_supervision", ctx, {"cuenta_id": cuenta_id})

    # 8. The checklist must now report the cuenta as ready to file.
    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": cuenta_id})
    assert resumen.resumen.radicacion_lista is True, (
        f"quedaron requisitos pendientes: {resumen.resumen.lista_pendientes}"
    )

    # 9. File it.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        radicada = await invoke_tool("radicar_cuenta", ctx, {"cuenta_id": cuenta_id})
    assert radicada.estado == EstadoCuentaCobro.ENVIADA

    persistida = await db.get(CuentaCobro, cuenta.id)
    assert persistida is not None
    assert persistida.estado == EstadoCuentaCobro.ENVIADA
    assert persistida.fecha_envio is not None


@pytest.mark.asyncio
async def test_chain_dies_without_definir_requisitos(db: AsyncSession) -> None:
    """Regression pin for the original dead end.

    A cuenta whose checklist mode was never set must still report an empty,
    not-ready checklist and refuse to generate informes — this is the exact
    state the agent got permanently stuck in before `definir_requisitos_checklist`
    existed. If someone ever makes `crear_cuenta_cobro` materialise the checklist
    itself, this test should be revisited deliberately, not deleted casually.
    """
    user, contrato = await _sembrar_contratista(db, "dead")
    ctx = ToolContext(db=db, usuario=user)

    cuenta = await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 5, "anio": 2026})
    cuenta_id = str(cuenta.id)

    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": cuenta_id})
    assert resumen.requisitos_definidos is False
    assert resumen.items == []
    assert resumen.resumen.radicacion_lista is False

    with pytest.raises(ValidationError):
        await invoke_tool("generar_informe_actividades", ctx, {"cuenta_id": cuenta_id})

    # And the escape hatch works: defining the checklist unblocks it.
    definido = await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": cuenta_id})
    assert definido.modo == "estandar"
    assert len(definido.items) > 0

    reabierto = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": cuenta_id})
    assert reabierto.requisitos_definidos is True
    assert len(reabierto.items) > 0
