"""End-to-end proof that the REAL agent chat LOOP (`chat_with_tools`, not direct
`invoke_tool` dispatch) composes the documented 10-step radicación playbook end
to end, driven by `LLM_PROVIDER=fake` — the real, outcome-aware, ID-threading
fake adapter (`app/adapters/llm/fake_adapter.py`, slice 0.6), NOT a test-only
`ScriptedLLM` stand-in.

Sibling to `tests/test_agente_cadena_completa.py`, which drives the SAME tool
chain but via direct `invoke_tool()` calls, bypassing `chat_with_tools`
entirely. That suite proves the TOOLS compose; this one proves the CHAT LOOP
layer around them does too — turn/iteration budget, tool-visibility phase
gating, ID threading across the simulated LLM boundary, and malformed-argument
error shaping — none of which the direct-invocation suite can exercise, since
it never goes through `chat_with_tools` at all.

The documented sequence under test (radicacion-sin-friccion 1.9 audit; source:
`agent_chat_service.SYSTEM_PROMPT_TEMPLATE`'s "Orden canónico de punta a
punta", verbatim, and mirrored 1:1 by `fake_adapter.HAPPY_PATH_SEQUENCE`):

  1. listar_contratos                          (discover contrato_id)
  2. crear_cuenta_cobro                        (mes/año)
  3. definir_requisitos_checklist              (modo=estandar)
  4. importar_documento x6                     (CONTRATO, RPC, CEDULA, RUT,
                                                 ACTA_INICIO, SEGURIDAD_SOCIAL —
                                                 the 6 mandatory upload-only
                                                 requisitos, checklist_service.
                                                 _CATALOGO_SEED)
     + auto_vincular_documentos
  5. crear_actividades_desde_obligaciones      (deterministic, no LLM)
  6. subir_evidencias_desde_chat               (user attached soportes in chat)
  7. generar_informe_actividades
     generar_informe_supervision
  8. resumen_checklist                         (confirm what's left)
  9. preparar_radicacion                       (package evidence ZIP)
 10. radicar_cuenta                            (submit)

Turn split, discovered empirically (not designed up front — see the test's own
comment at the point this mattered): `FakeLLMPort` has no model of
`SYSTEM_PROMPT_TEMPLATE`'s "radicar_cuenta needs explicit confirmation, never
call it autonomously" rule (that's a real-reasoning-model prompt instruction,
not a scripted gate), so it keeps advancing the SAME turn as long as
iterations remain and nothing fails. Turn 1 therefore drives steps 1-8 AND
attempts step 9 (17 tool calls total, inside MAX_TOOL_ITERATIONS=20) — where it
genuinely fails with CHECKLIST_INCOMPLETE (EVIDENCIAS pending; the classifier
that would resolve it needs a real LLM/Gmail connection, unreachable here — see
`_cover_evidencia_justificaciones`), retries once (slice 0.6 outcome-aware
retry), and gives up. After a DB stand-in covers EVIDENCIAS (mirroring
`test_agente_cadena_completa.py`'s own same-reason stand-in), turn 2 resumes
from the cross-turn recap and drives steps 9 AND 10 in one go.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.config import settings
from app.models.actividad import Actividad, JustificacionOrigen
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import agent_chat_service
from app.tools.context import ToolAttachment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import ContratoFactory

_PATCH_DOC_S3 = "app.services.document_service._get_storage"
_PATCH_EVI_S3 = "app.tools.catalog.evidencias._get_storage"
_PATCH_PAQUETE_S3 = "app.services.radicacion_prep_service._get_storage"

# Mirrors tests/test_agente_cadena_completa.py's `_SOPORTES` — same filenames as
# `fake_adapter._IMPORTAR_DOCUMENTO_OVERRIDES` so the scripted tool calls resolve
# against real attachments instead of "Attachment not found".
_SOPORTES: list[tuple[str, str]] = [
    ("contrato", "contrato.txt"),
    ("rpc", "rpc.txt"),
    ("cedula", "cedula.txt"),
    ("rut", "rut.txt"),
    ("acta_inicio", "acta_inicio.txt"),
    ("seguridad_social", "planilla_pila.txt"),
]
_EVIDENCIAS = ["evidencia_1.txt", "evidencia_2.txt", "evidencia_3.txt"]

_OBLIGACIONES_REALISTAS: list[str] = [
    "Apoyar la formulación y el seguimiento de los planes de acción del área asignada.",
    "Elaborar los informes técnicos mensuales requeridos por la supervisión del contrato.",
    "Participar en las mesas de trabajo interinstitucionales convocadas por la entidad.",
]


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    storage.presigned_url = AsyncMock(return_value="https://example.com/presigned")
    return storage


async def _seed_contratista(db: AsyncSession) -> tuple[object, object]:
    """Seeds a real usuario/contrato via the shared factories (radicacion-sin-
    friccion 4.1, `tests/factories.py`) — `Obligacion` has no factory yet (not
    one of the five core models that module covers), so its rows are added
    directly, mirroring `test_agente_cadena_completa.py::_sembrar_contratista`.
    """
    contrato = await ContratoFactory.create_async(db)
    for i, descripcion in enumerate(_OBLIGACIONES_REALISTAS):
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
    await db.refresh(contrato)
    usuario = contrato.usuario
    await db.refresh(usuario)
    return usuario, contrato


def _attachments(filenames: list[str]) -> dict[str, ToolAttachment]:
    return {
        name: ToolAttachment(
            filename=name,
            content_type="text/plain",
            data=f"contenido de prueba para {name}".encode(),
        )
        for name in filenames
    }


async def _cover_evidencia_justificaciones(db: AsyncSession, cuenta_id: uuid.UUID) -> None:
    """Stand-in for what `descubrir_evidencias` + `persistir_evidencias` do in
    production against a real Gmail/Drive/Calendar connection (one real
    justificación per obligación, `origen=LLM`) — mirrors
    `test_agente_cadena_completa.py::test_cadena_completa_hasta_radicar`'s own
    documented stand-in for the EXACT same reason: the evidence-to-obligación
    classifier (`app.agent.nodes.evidence_matcher.clasificar_evidencia`) calls
    `get_llm().complete()` with plain (non-tool-calling) messages — under
    `LLM_PROVIDER=fake` those get routed by `FakeLLMPort`'s GENERIC playbook
    router (there is no signal in a bare classification prompt that lets it
    tell this call apart from a real chat turn), never a real classification
    answer, so uploaded evidence always lands on the shared "sin clasificar"
    stub regardless of how many files `subir_evidencias_desde_chat` uploaded.
    Solving that classification gap is `evidence_matcher`'s own concern, out of
    scope for this slice, which is about the CHAT LOOP composing tool calls —
    not about teaching a fake LLM to classify free text.
    """
    acts = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    cubiertas = [a for a in acts if a.obligacion_id is not None]
    assert len(cubiertas) == len(_OBLIGACIONES_REALISTAS), (
        "crear_actividades_desde_obligaciones debía dejar una actividad por obligación"
    )
    for act in cubiertas:
        act.justificacion = "Justificación de cobertura para la obligación, generada durante el período."
        act.justificacion_origen = JustificacionOrigen.LLM
    await db.commit()


@pytest.mark.asyncio
async def test_full_playbook_via_chat_loop_reaches_preparar_radicacion_and_radica(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the real `chat_with_tools` loop across 3 turns and asserts REAL
    outcomes (DB state, not just "the right tool was called") at each
    milestone: the cuenta really exists, the checklist really reflects every
    upload, the activities are really linked to obligaciones, the informes are
    really generated, `preparar_radicacion` really reports the packaged ZIP,
    and the cuenta really reaches ENVIADA."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    usuario, contrato = await _seed_contratista(db)

    turn1_attachments = _attachments([name for _tipo, name in _SOPORTES] + _EVIDENCIAS)

    with patch(_PATCH_DOC_S3, return_value=_fake_storage()), patch(_PATCH_EVI_S3, return_value=_fake_storage()):
        turn1 = await agent_chat_service.chat_with_tools(
            db,
            usuario,
            "Quiero radicar mi cuenta de cobro de este contrato, te adjunto todos los soportes.",
            None,
            turn1_attachments,
        )

    # `FakeLLMPort` has no model of `SYSTEM_PROMPT_TEMPLATE`'s "radicar_cuenta
    # needs explicit user confirmation, never call it autonomously in the same
    # turn" rule — that's a PROMPT instruction a real reasoning model follows,
    # not something the deterministic script enforces (documented finding, out
    # of scope for this slice — see the final apply report). It keeps
    # advancing the SAME turn as long as iterations remain and nothing fails,
    # so it drives all the way to `preparar_radicacion` inside turn 1 too —
    # where it genuinely (and correctly) FAILS with CHECKLIST_INCOMPLETE,
    # because EVIDENCIAS isn't coverable through the fake LLM alone (see
    # `_cover_evidencia_justificaciones`'s docstring). The outcome-aware
    # retry-once-then-give-up mechanic (slice 0.6) fires exactly as designed:
    # one retry of `preparar_radicacion`, still fails, the turn ends there.
    expected_turn1_sequence = [
        "listar_contratos",
        "crear_cuenta_cobro",
        "definir_requisitos_checklist",
        *(["importar_documento"] * len(_SOPORTES)),
        "auto_vincular_documentos",
        "crear_actividades_desde_obligaciones",
        "subir_evidencias_desde_chat",
        "generar_informe_actividades",
        "generar_informe_supervision",
        "resumen_checklist",
        "preparar_radicacion",
        "preparar_radicacion",
    ]
    assert [e.tool for e in turn1.tool_events] == expected_turn1_sequence
    statuses = [e.status for e in turn1.tool_events]
    assert statuses[:-2] == ["ok"] * (len(expected_turn1_sequence) - 2), [
        (e.tool, e.status, e.resumen) for e in turn1.tool_events
    ]
    assert statuses[-2:] == ["error", "error"]
    assert "EVIDENCIAS" in turn1.tool_events[-1].resumen
    assert turn1.session_id
    # 17 real tool calls, comfortably inside MAX_TOOL_ITERATIONS=20 for ONE turn.
    assert len(turn1.tool_events) < agent_chat_service.MAX_TOOL_ITERATIONS

    # --- REAL outcome assertions (not just "the tool was called") ----------
    # `preparar_radicacion`'s two failures each ran `db.rollback()` inside
    # `chat_with_tools`'s per-tool-call handler, which expires every object in
    # the shared `db` session (regardless of `expire_on_commit`) — refresh
    # `contrato` (created before that rollback, still needed below) first,
    # exactly like `test_fake_llm_adapter.py`'s own post-rollback tests do.
    await db.refresh(contrato)
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))).scalar_one()
    assert cuenta.requisitos_modo is not None

    documentos = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalars().all()
    )
    tipos_importados = {d.tipo.value for d in documentos}
    assert {tipo for tipo, _filename in _SOPORTES} <= tipos_importados

    actividades = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))).scalars().all()
    cubiertas = [a for a in actividades if a.obligacion_id is not None]
    assert len(cubiertas) == len(_OBLIGACIONES_REALISTAS)

    evidencias = (
        (await db.execute(select(Evidencia).join(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id)))
        .scalars()
        .all()
    )
    assert len(evidencias) == len(_EVIDENCIAS)

    informes = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta.id))).scalars().all()
    )
    assert {"informe_actividades", "informe_supervision"} <= {d.tipo.value for d in informes}

    # Stand-in for descubrir_evidencias + persistir_evidencias (see docstring)
    # — only possible NOW: crear_actividades_desde_obligaciones (mid-turn 1)
    # is what creates the Actividad rows this needs.
    await _cover_evidencia_justificaciones(db, cuenta.id)

    # --- Turn 2: resumes from the cross-turn recap. EVIDENCIAS is covered
    # now, so preparar_radicacion succeeds — and the SAME "no confirmation
    # gate in the fake" behavior noted above carries it straight through to
    # radicar_cuenta in this one turn too.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()), patch(_PATCH_PAQUETE_S3, return_value=_fake_storage()):
        turn2 = await agent_chat_service.chat_with_tools(
            db, usuario, "Ya cargué las evidencias, seguí con la radicación.", turn1.session_id, {}
        )

    assert [e.tool for e in turn2.tool_events] == ["preparar_radicacion", "radicar_cuenta"]
    assert all(e.status == "ok" for e in turn2.tool_events), [(e.tool, e.status, e.resumen) for e in turn2.tool_events]

    persistida = await db.get(CuentaCobro, cuenta.id)
    assert persistida is not None
    assert persistida.estado == EstadoCuentaCobro.ENVIADA
    assert persistida.fecha_envio is not None


@pytest.mark.asyncio
async def test_iteration_cap_hit_mid_upload_loop_survives_and_resumes_correctly(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAX_TOOL_ITERATIONS hits mid-way through the 6-call importar_documento
    loop (radicacion-sin-friccion 1.9 edge case). Two things must both hold:
    (1) whatever committed before the cap (the cuenta, the checklist
    definition, the 1 document already imported) survives intact; (2) a
    follow-up turn actually CONTINUES the loop from the 2nd override entry
    (rpc.txt) — not a duplicate re-upload of contrato.txt (queue restarted)
    and not a silent skip straight to auto_vincular_documentos (the bug fixed
    by `_count_recap_ok_entries`, see that commit).

    Capped after exactly ONE importar_documento call, not several: the
    cross-turn recap is deliberately bounded to `_RECAP_MAX_CHARS=240`
    (agent_chat_service, "a handful of tool:status id=value entries, never
    enough to meaningfully eat into the model's context budget") — an
    `importar_documento:ok` line carries TWO real UUIDs (documento_id,
    contrato_id), ~115 chars each, so only the SINGLE newest one reliably
    survives truncation once earlier entries (listar_contratos,
    crear_cuenta_cobro, definir_requisitos_checklist) compete for the same
    budget. `_count_recap_ok_entries` correctly counts however many entries
    the recap actually kept — it fixes the routing bug (resume position was
    ALWAYS wrong before), but resuming after MORE than ~1-2 interrupted
    importar_documento calls is bounded by the recap's own budget, a
    separate, pre-existing constraint this slice did not redesign. Flagged
    as a follow-up in the apply report, not silently glossed over here."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(agent_chat_service, "MAX_TOOL_ITERATIONS", 4)
    usuario, contrato = await _seed_contratista(db)

    turn1_attachments = _attachments([name for _tipo, name in _SOPORTES])
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        turn1 = await agent_chat_service.chat_with_tools(
            db, usuario, "Radicá mi cuenta, te adjunto los soportes.", None, turn1_attachments
        )

    assert [e.tool for e in turn1.tool_events] == [
        "listar_contratos",
        "crear_cuenta_cobro",
        "definir_requisitos_checklist",
        "importar_documento",
    ]
    assert all(e.status == "ok" for e in turn1.tool_events), [(e.tool, e.status, e.resumen) for e in turn1.tool_events]
    assert turn1.content == (
        "Alcancé el límite de pasos automáticos para esta solicitud. "
        "¿Quieres que continúe con la tarea o prefieres darme más detalles?"
    )

    # --- What committed before the cap survives intact ----------------------
    await db.refresh(contrato)
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))).scalar_one()
    assert cuenta.requisitos_modo is not None
    documentos_antes = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalars().all()
    )
    assert {d.tipo.value for d in documentos_antes} == {"contrato"}

    # --- Follow-up turn continues from the 2nd override entry, not a restart
    monkeypatch.setattr(agent_chat_service, "MAX_TOOL_ITERATIONS", 20)
    remaining_soportes = _SOPORTES[1:]
    turn2_attachments = _attachments([name for _tipo, name in remaining_soportes])
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        turn2 = await agent_chat_service.chat_with_tools(
            db, usuario, "Seguí, acá van los demás soportes.", turn1.session_id, turn2_attachments
        )

    resumed_imports = [e for e in turn2.tool_events if e.tool == "importar_documento"]
    assert len(resumed_imports) == len(remaining_soportes), [(e.tool, e.status, e.resumen) for e in turn2.tool_events]
    assert all(e.status == "ok" for e in resumed_imports), [(e.status, e.resumen) for e in resumed_imports]

    await db.refresh(contrato)
    documentos_despues = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalars().all()
    )
    # Exactly 6 documents total — no duplicate (a restart would produce 2 "contrato"
    # rows) and no gap (a skip would produce fewer than 6 distinct tipos).
    assert {d.tipo.value for d in documentos_despues} == {tipo for tipo, _filename in _SOPORTES}
    assert len(documentos_despues) == len(_SOPORTES)
