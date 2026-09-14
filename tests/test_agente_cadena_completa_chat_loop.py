"""End-to-end proof that the REAL agent chat LOOP (`chat_with_tools`, not direct
`invoke_tool` dispatch) correctly threads tool calls — real id propagation,
phase-gating tool visibility, outcome-aware retries, and cross-turn
recap/resume — across a REALISTIC multi-turn conversation, ending in a
genuine `radicar_cuenta` success. Driven by `LLM_PROVIDER=fake` — the real,
outcome-aware, ID-threading fake adapter (`app/adapters/llm/fake_adapter.py`,
slice 0.6), NOT a test-only `ScriptedLLM` stand-in.

Sibling to `tests/test_agente_cadena_completa.py`, which drives the SAME tool
chain but via direct `invoke_tool()` calls, bypassing `chat_with_tools`
entirely. That suite proves the TOOLS compose; this one proves the CHAT LOOP
layer around them does too — turn/iteration budget, tool-visibility phase
gating, ID threading across the simulated LLM boundary, and malformed-argument
error shaping — none of which the direct-invocation suite can exercise, since
it never goes through `chat_with_tools` at all.

SCOPE, precisely (adversarial review, phase1-full-playbook-e2e, CRITICAL 3
follow-up — corrects an earlier "full 10-step playbook" framing that
overstated what this test proves). What this test DOES prove:
  - the chat loop's TOOL-CALLING MECHANICS across REAL turns: id threading
    through `known_ids`, the cross-turn recap, AND the recap's own
    `sticky_cuenta_id` carry-forward (`agent_chat_service.
    _build_tool_context_recap`) — phase-gating tool visibility, the
    outcome-aware retry-once-then-give-up mechanic, and resuming a repeating
    scripted tool from the recap's queue position.
  - a conversation shape that respects the real attachment cap a user is
    actually bound by (`app.api.v1.agent_chat.MAX_CHAT_FILES=6`): every turn
    attaches at most 6 files (turn 2 attaches the 6 mandatory checklist
    soportes, turn 3 attaches the 3 evidencias separately — mirrors how the
    checklist UI's own two distinct upload steps, mandatory soportes vs.
    evidencias, would actually shape a real conversation). No turn ever
    exceeds 6 files.

What this test does NOT prove: Google-based evidence discovery/classification
(`descubrir_evidencias`/`persistir_evidencias` against a real Gmail/Drive/
Calendar connection, and the LLM classifier that links an uploaded evidencia
to its obligación) never runs here — `LLM_PROVIDER=fake` has no way to reach
it (see `_cover_evidencia_justificaciones`'s own docstring below). That gap is
not new to this slice: `tests/test_agente_cadena_completa.py` stands in for
the exact same thing, the exact same way. Verifying the real Google
integration is explicitly out of scope for a deterministic test — it belongs
to `docs/prod-test-plan.md`'s "Capa 3 — Checklist manual" doctrine (a human,
not automation, per that document).

The documented tool sequence under test (radicacion-sin-friccion 1.9 audit;
source: `agent_chat_service.SYSTEM_PROMPT_TEMPLATE`'s "Orden canónico de punta
a punta", verbatim, and mirrored 1:1 by `fake_adapter.HAPPY_PATH_SEQUENCE`):

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

Turn split, discovered empirically (not designed up front — see the first
test's own comments at the points this mattered): `FakeLLMPort` has no model
of `SYSTEM_PROMPT_TEMPLATE`'s "radicar_cuenta needs explicit confirmation,
never call it autonomously" rule (that's a real-reasoning-model prompt
instruction, not a scripted gate) — it keeps advancing the SAME turn as long
as iterations remain and nothing fails, so a turn only ends where a step
genuinely fails. FOUR turns, not two — one more than a first restructuring
attempt used, because of a real mechanical interaction with the ALREADY
tracked recap-budget gap (`agent_chat_service._RECAP_MAX_CHARS`'s own TODO,
WARNING 4 of the apply report): `subir_evidencias_desde_chat` and
`preparar_radicacion` (steps 6 and 9) both REQUIRE `cuenta_id`, and only a
handful of tool names alias their output to `cuenta_id` for the recap to
find (`fake_adapter._ID_ALIASES` — `crear_cuenta_cobro`,
`definir_requisitos_checklist`, `resumen_checklist`, `preparar_radicacion`).
A turn boundary placed right after several `importar_documento` calls (no
`cuenta_id` in their own output) pushes every earlier `cuenta_id`-bearing
entry out of the 240-char recap budget — verified empirically (see git
history at this comment's own commit) — losing `cuenta_id` for the NEXT
turn, exactly the real reliability risk that TODO already documents. Placing
the FIRST turn boundary right after `definir_requisitos_checklist` (one of
the aliased tools) instead avoids that: `agent_chat_service.
_build_tool_context_recap`'s `sticky_cuenta_id` line then carries `cuenta_id`
forward turn-to-turn indefinitely, however many calls each later turn makes,
because its budget is reserved up front — see that function's own docstring.
This is honestly closer to a real conversation anyway: a real user announces
intent BEFORE attaching files, not always in the very first message.

  - Turn 1: announces intent, no attachments yet. `listar_contratos`,
    `crear_cuenta_cobro`, `definir_requisitos_checklist` succeed; the fake
    then (mechanically, no confirmation gate — see above) tries
    `importar_documento` anyway, fails twice (`contrato.txt` isn't attached
    this turn), gives up.
  - Turn 2: attaches the 6 soportes. Resumes from the recap at
    `importar_documento`, imports all 6, links them, creates the 3
    actividades, then tries `subir_evidencias_desde_chat`, fails twice
    (evidencias aren't attached this turn), gives up.
  - Turn 3: attaches the 3 evidencias. Resumes at `subir_evidencias_desde_chat`,
    succeeds, generates both informes, calls `resumen_checklist`, then tries
    `preparar_radicacion`, fails twice (EVIDENCIAS classification coverage
    isn't available until the DB stand-in runs between turns 3 and 4), gives
    up.
  - Turn 4: confirms, no attachments. Resumes at `preparar_radicacion`,
    succeeds, and (same no-confirmation-gate behavior) goes straight to
    `radicar_cuenta` in the same turn.
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
from app.models.usuario import Usuario
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


async def _seed_contratista(db: AsyncSession) -> tuple[Usuario, object]:
    """Seeds a real usuario/contrato via the shared factories (radicacion-sin-
    friccion 4.1, `tests/factories.py`) — `Obligacion` has no factory yet (not
    one of the five core models that module covers), so its rows are added
    directly, mirroring `test_agente_cadena_completa.py::_sembrar_contratista`.

    Fetches `usuario` via an explicit `select` (not the lazy `contrato.usuario`
    relationship attribute) — under certain pytest `-k` subset selections the
    lazy-load attribute access intermittently raised `MissingGreenlet`
    (asyncio/SQLAlchemy greenlet-context timing, reproducible only with
    specific `-k` test subsets, never in a full-file or full-suite run); an
    explicit awaited query has no such failure mode.
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
    usuario = (await db.execute(select(Usuario).where(Usuario.id == contrato.usuario_id))).scalar_one()
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
async def test_realistic_multi_turn_chat_loop_threads_tool_calls_to_a_real_radicar_cuenta(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the real `chat_with_tools` loop across 4 realistic turns — see
    the module docstring for exactly why 4 (not 2) and why the first turn
    boundary sits where it does — never attaching more than 6 files in any one
    turn (the real `MAX_CHAT_FILES=6` cap enforced by `app.api.v1.agent_chat`
    for a real user) — and asserts REAL outcomes (DB state, not just "the
    right tool was called") at each milestone: the cuenta really exists, the
    checklist really reflects every upload, the activities are really linked
    to obligaciones, the evidencias are really uploaded, the informes are
    really generated, `preparar_radicacion` really reports the packaged ZIP,
    and the cuenta really reaches ENVIADA.

    See the module docstring for the precise scope this proves (tool-calling
    mechanics across a realistic multi-turn shape) and does NOT prove (Google
    evidence discovery/classification — out of scope, covered by
    `docs/prod-test-plan.md`'s manual checklist)."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    usuario, contrato = await _seed_contratista(db)

    # --- Turn 1: announces intent, no attachments yet — see the module
    # docstring for why the turn boundary sits here rather than after the 6
    # soportes: `definir_requisitos_checklist` is one of the few tools
    # `fake_adapter._ID_ALIASES` maps to `cuenta_id`, which keeps `cuenta_id`
    # inside the 240-char recap budget for every later turn (via the recap's
    # own `sticky_cuenta_id` carry-forward) — a turn boundary placed later,
    # after several `importar_documento` calls, empirically loses it. The
    # fake then (mechanically — no confirmation-gate modeling, see the module
    # docstring) tries `importar_documento` anyway with nothing attached,
    # fails twice, gives up — a plausible enough beat for a real agent that
    # hasn't been given any files yet either.
    turn1 = await agent_chat_service.chat_with_tools(
        db,
        usuario,
        "Quiero radicar mi cuenta de cobro de este contrato.",
        None,
        {},
    )

    expected_turn1_sequence = [
        "listar_contratos",
        "crear_cuenta_cobro",
        "definir_requisitos_checklist",
        "importar_documento",
        "importar_documento",
    ]
    assert [e.tool for e in turn1.tool_events] == expected_turn1_sequence
    statuses1 = [e.status for e in turn1.tool_events]
    assert statuses1[:-2] == ["ok"] * (len(expected_turn1_sequence) - 2), [
        (e.tool, e.status, e.resumen) for e in turn1.tool_events
    ]
    assert statuses1[-2:] == ["error", "error"]
    assert "Attachment" in turn1.tool_events[-1].resumen
    assert "not found" in turn1.tool_events[-1].resumen
    assert turn1.session_id

    # --- REAL outcome assertions after turn 1 -------------------------------
    # `importar_documento`'s two failures each ran `db.rollback()` inside
    # `chat_with_tools`'s per-tool-call handler, which expires every object in
    # the shared `db` session (regardless of `expire_on_commit`) — refresh
    # `contrato` (created before that rollback, still needed below) first,
    # exactly like `test_fake_llm_adapter.py`'s own post-rollback tests do.
    await db.refresh(contrato)
    cuenta = (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))).scalar_one()
    assert cuenta.requisitos_modo is not None
    # Captured once, right after the fresh fetch above, while `cuenta` is not yet
    # expired: every later reference in this test only needs the (immutable)
    # primary key, never re-reads `cuenta` as a live ORM object. This is
    # deliberate (radicacion-sin-friccion slice 2.4a) — `cuenta` goes through
    # several more `db.rollback()`s below (each one fully expiring the session,
    # same as `usuario`/`convo`/`contrato`'s own comments explain), and nothing
    # here re-queries or `db.refresh()`s `cuenta` itself afterward. Before this
    # slice flipped `Contrato.cuentas_cobro` off `lazy="selectin"`, that
    # `await db.refresh(contrato)` calls below silently ALSO re-populated this
    # exact `cuenta` row as a side effect of the (now-removed) eager cascade —
    # an accidental refresh this test never asked for and shouldn't rely on.
    cuenta_id = cuenta.id

    # --- Turn 2: attaches only the 6 mandatory soportes — the most a real
    # user can ever attach in one message through `POST /api/v1/agent/chat`
    # (MAX_CHAT_FILES=6). Resumes from the recap at `importar_documento`
    # (newest `ok` entry: `definir_requisitos_checklist`), imports all 6,
    # links them, creates the 3 actividades, then genuinely (and correctly)
    # fails at `subir_evidencias_desde_chat` — its scripted files aren't
    # attached until turn 3 — retries once, gives up.
    turn2_attachments = _attachments([name for _tipo, name in _SOPORTES])
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        turn2 = await agent_chat_service.chat_with_tools(
            db,
            usuario,
            "Acá tenés los soportes.",
            turn1.session_id,
            turn2_attachments,
        )

    expected_turn2_sequence = [
        *(["importar_documento"] * len(_SOPORTES)),
        "auto_vincular_documentos",
        "crear_actividades_desde_obligaciones",
        "subir_evidencias_desde_chat",
        "subir_evidencias_desde_chat",
    ]
    assert [e.tool for e in turn2.tool_events] == expected_turn2_sequence
    statuses2 = [e.status for e in turn2.tool_events]
    assert statuses2[:-2] == ["ok"] * (len(expected_turn2_sequence) - 2), [
        (e.tool, e.status, e.resumen) for e in turn2.tool_events
    ]
    assert statuses2[-2:] == ["error", "error"]
    assert "Attachment" in turn2.tool_events[-1].resumen
    assert "not found" in turn2.tool_events[-1].resumen
    # 9 real tool calls, comfortably inside MAX_TOOL_ITERATIONS=20 for ONE turn.
    assert len(turn2.tool_events) < agent_chat_service.MAX_TOOL_ITERATIONS

    # --- REAL outcome assertions after turn 2 -------------------------------
    await db.refresh(contrato)
    documentos = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalars().all()
    )
    tipos_importados = {d.tipo.value for d in documentos}
    assert {tipo for tipo, _filename in _SOPORTES} <= tipos_importados

    actividades = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    cubiertas = [a for a in actividades if a.obligacion_id is not None]
    assert len(cubiertas) == len(_OBLIGACIONES_REALISTAS)

    # --- Turn 3: attaches the 3 evidencias separately — a genuine follow-up
    # message. Resumes from the recap at `subir_evidencias_desde_chat`
    # (newest `ok` entry: `crear_actividades_desde_obligaciones`; `cuenta_id`
    # itself survives via the recap's `sticky_cuenta_id` line, carried
    # forward from turn 1 regardless of how many calls turn 2 made — see the
    # module docstring). Now that the evidencia files are actually attached,
    # `subir_evidencias_desde_chat` succeeds and the chain advances through
    # both informes and `resumen_checklist` — then genuinely (and correctly)
    # fails at `preparar_radicacion`: EVIDENCIAS classification coverage (see
    # `_cover_evidencia_justificaciones`'s docstring above) isn't reachable
    # through the fake LLM alone. Retries once, gives up.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()), patch(_PATCH_EVI_S3, return_value=_fake_storage()):
        turn3 = await agent_chat_service.chat_with_tools(
            db,
            usuario,
            "Ya tengo las evidencias listas, te las adjunto.",
            turn2.session_id,
            _attachments(_EVIDENCIAS),
        )

    expected_turn3_sequence = [
        "subir_evidencias_desde_chat",
        "generar_informe_actividades",
        "generar_informe_supervision",
        "resumen_checklist",
        "preparar_radicacion",
        "preparar_radicacion",
    ]
    assert [e.tool for e in turn3.tool_events] == expected_turn3_sequence
    statuses3 = [e.status for e in turn3.tool_events]
    assert statuses3[:-2] == ["ok"] * (len(expected_turn3_sequence) - 2), [
        (e.tool, e.status, e.resumen) for e in turn3.tool_events
    ]
    assert statuses3[-2:] == ["error", "error"]
    assert "EVIDENCIAS" in turn3.tool_events[-1].resumen

    # --- REAL outcome assertions after turn 3 -------------------------------
    await db.refresh(contrato)
    evidencias = (
        (await db.execute(select(Evidencia).join(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id)))
        .scalars()
        .all()
    )
    assert len(evidencias) == len(_EVIDENCIAS)

    informes = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta_id))).scalars().all()
    )
    assert {"informe_actividades", "informe_supervision"} <= {d.tipo.value for d in informes}

    # Stand-in for descubrir_evidencias + persistir_evidencias (see docstring)
    # — only possible now that crear_actividades_desde_obligaciones (turn 2)
    # created the Actividad rows this needs.
    await _cover_evidencia_justificaciones(db, cuenta_id)

    # --- Turn 4: confirms with no new attachments — resumes from the recap at
    # `preparar_radicacion` (its newest `ok` entry is `resumen_checklist`), and
    # the SAME "no confirmation gate in the fake" behavior noted in the module
    # docstring carries it straight through to `radicar_cuenta` in this one
    # turn.
    with patch(_PATCH_DOC_S3, return_value=_fake_storage()), patch(_PATCH_PAQUETE_S3, return_value=_fake_storage()):
        turn4 = await agent_chat_service.chat_with_tools(
            db, usuario, "Ya se resolvieron las evidencias, seguí con la radicación.", turn3.session_id, {}
        )

    assert [e.tool for e in turn4.tool_events] == ["preparar_radicacion", "radicar_cuenta"]
    assert all(e.status == "ok" for e in turn4.tool_events), [(e.tool, e.status, e.resumen) for e in turn4.tool_events]

    persistida = await db.get(CuentaCobro, cuenta_id)
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
    separate, pre-existing constraint this slice did not redesign — TRACKED
    (not silently glossed over) as a real reliability gap for a real model
    too at `agent_chat_service._RECAP_MAX_CHARS`'s own TODO comment."""
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


@pytest.mark.asyncio
async def test_malformed_tool_args_surface_as_validation_error_not_network_failure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`FAKE_LLM_SCRIPT=malformed` (slice 0.6) corrupts `crear_cuenta_cobro`'s
    arguments (`mes=13`, violating `Field(ge=1, le=12)`) — `crear_cuenta_cobro`
    is always the 2ND scripted step (right after `listar_contratos`), so this
    fires early in the chain, not deep into it.

    SCOPE, corrected (adversarial review, phase1-full-playbook-e2e, WARNING 5
    follow-up): this is near-identical to `test_fake_llm_adapter.py`'s own
    `test_chat_with_tools_malformed_script_surfaces_a_validation_error_not_a_network_failure`
    — both fail at step 2 of the chain, so this does NOT meaningfully exercise
    "malformed args deep inside a full playbook run with real prior state" the
    way its name once implied. `FAKE_LLM_SCRIPT`'s `_MALFORMED_TARGET_TOOL` is
    a hardcoded module-level constant (`app.adapters.llm.fake_adapter`) always
    targeting `crear_cuenta_cobro` — retargeting it to a later tool would be a
    PRODUCTION constant change, not a test-local one, and would break those two
    existing pinned regression tests (`test_fake_llm_adapter.py`, slice
    0.4/0.6's own "never misdiagnose a validation error as a network failure"
    fix) that specifically assert `crear_cuenta_cobro` corruption. Not a small
    change — kept here as a NARROWER, honestly-scoped duplicate: what this test
    still adds over the one in `test_fake_llm_adapter.py` is that it also seeds
    a real contratista via `_seed_contratista` (obligaciones included) and
    asserts no orphan `CuentaCobro` row — a real-schema-validation-through-a-
    real-DB check the other test doesn't make."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(settings, "FAKE_LLM_SCRIPT", "malformed")
    usuario, contrato = await _seed_contratista(db)

    result = await agent_chat_service.chat_with_tools(db, usuario, "Radicá mi cuenta de cobro de este mes.", None, {})

    crear_events = [e for e in result.tool_events if e.tool == "crear_cuenta_cobro"]
    assert len(crear_events) == 2, [(e.tool, e.status, e.resumen) for e in result.tool_events]
    assert all(e.status == "error" for e in crear_events)
    assert all("mes" in e.resumen for e in crear_events)
    assert "No pude contactar al modelo" not in result.content

    # crear_cuenta_cobro never actually succeeded — no orphan CuentaCobro row.
    await db.refresh(contrato)
    cuentas = (await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == contrato.id))).scalars().all()
    assert cuentas == []


@pytest.mark.asyncio
async def test_failed_upload_on_the_fourth_of_six_leaves_the_three_earlier_uploads_intact(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through the 6 mandatory uploads (radicacion-sin-
    friccion 1.9 edge case) — the 4th file (RUT) is corrupted (>10MB,
    `validate_file_size`'s real `MAX_FILE_SIZE_BYTES` cap) — must leave the 3
    earlier SUCCESSFUL imports (CONTRATO, RPC, CEDULA) committed and intact,
    and produce NO DocumentoFuente row for the failed RUT upload.

    Renamed (adversarial review, phase1-full-playbook-e2e, WARNING 6
    follow-up) from ..._leaves_no_orphan_rows: that name, and two of its
    assertions (`Actividad`/`Evidencia` tables empty), implied this chain
    proves those tables stay clean under a real bug — it doesn't. This chain
    never reaches `crear_actividades_desde_obligaciones` or
    `subir_evidencias_desde_chat` at all (the fake's outcome-aware retry
    fails RUT twice and gives up before either step is ever scripted), so
    those tables were GUARANTEED empty regardless of whether the fix under
    test worked — the assertions proved nothing about the fix. The
    `DocumentoFuente` set assertion below is the one assertion with real
    teeth (it fails without the fix), and is what this test is renamed to
    describe."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    usuario, contrato = await _seed_contratista(db)

    attachments = _attachments([name for _tipo, name in _SOPORTES])
    # Corrupt exactly the RUT attachment (4th of 6, _SOPORTES[3]) — oversized,
    # so `importar_documento`'s own validate_file_size check rejects it before
    # any storage/DB write, independent of content-sniffing heuristics.
    corrupt_name = _SOPORTES[3][1]
    attachments[corrupt_name] = ToolAttachment(
        filename=corrupt_name,
        content_type="text/plain",
        data=b"x" * (11 * 1024 * 1024),
    )

    with patch(_PATCH_DOC_S3, return_value=_fake_storage()):
        result = await agent_chat_service.chat_with_tools(
            db, usuario, "Radicá mi cuenta, te adjunto los soportes.", None, attachments
        )

    assert [e.tool for e in result.tool_events] == [
        "listar_contratos",
        "crear_cuenta_cobro",
        "definir_requisitos_checklist",
        "importar_documento",
        "importar_documento",
        "importar_documento",
        "importar_documento",
        "importar_documento",
    ]
    statuses = [e.status for e in result.tool_events]
    assert statuses[:6] == ["ok"] * 6, [(e.tool, e.status, e.resumen) for e in result.tool_events]
    assert statuses[6:] == ["error", "error"]
    assert all("File exceeds maximum size" in e.resumen for e in result.tool_events[6:])

    await db.refresh(contrato)
    documentos = (
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalars().all()
    )
    # The 3 earlier successful uploads (contrato, rpc, cedula) remain; RUT (the
    # failed one) produced no row at all — not a partial/corrupt one.
    assert {d.tipo.value for d in documentos} == {"contrato", "rpc", "cedula"}
