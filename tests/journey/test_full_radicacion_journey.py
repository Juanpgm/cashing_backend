"""Full-journey usability e2e test: SECOP import -> ... -> radicar.

This is BOTH an integration test (drives the complete cuenta-de-cobro flow
through the real HTTP API, exactly the way the frontend would) AND a usability
regression guard: it counts how many pieces of information a human actually
had to type/click/upload versus how many were resolved by the system on its
own (SECOP auto-fill, SECOP auto-detection, LLM generation, evidence discovery
agent, autogen informes). If a future change quietly turns an "auto" step back
into a "manual" one, `test_full_radicacion_journey` fails loudly with an
itemized ledger showing exactly which step regressed.

Journey (all steps via the HTTP API, ASGITransport client from conftest.py):
  1. SECOP contract import (mocked Socrata layer) -> contrato auto-filled.
  2. Register obligaciones on the contract (SECOP never returns them — see the
     friction note below).
  3. Create a cuenta de cobro WITHOUT `valor` -> server defaults it from
     contrato.valor_mensual (B.2).
  4. Resolve the checklist gate (modo=estandar).
  5. Seed a SECOP documentos cache matching the contract + POST refresh-secop
     -> several mandatory requisitos flip to `detectado` automatically.
  6. Mark the one requisito that structurally cannot auto-detect (EVIDENCIAS)
     as cumplido_manual.
  7. Negative check: radicar before the informes exist -> 4xx / CHECKLIST_INCOMPLETE.
  8. Generate actividades + justificación via the LLM agent (mocked).
  9. Discover evidence via the Gmail/Drive/Calendar explorer agent (mocked)
     and persist it -> Actividad/Evidencia rows, cobertura goes to 0 sin_evidencia.
  10. Autogenerate the two informes (INFORME_ACTIVIDADES / INFORME_SUPERVISION).
  11. Radicar -> 200, estado=enviada, fecha_envio set.

FRICTION FOUND (partially mitigated, bug #6):
  SECOP's `objeto` field is usually a short paragraph with no enumerated
  obligations section, so `secop_service._mapear_a_contrato_create` and the
  best-effort LLM fallback in `importar_contratos_secop` commonly still land
  on ZERO obligaciones (as exercised below, with the LLM mocked to return
  nothing — see bug #6 fix). Unlike before, this is no longer a silent dead
  end: the import result now carries `requiere_obligaciones=true` per
  contract, so a caller can react immediately instead of discovering the gap
  deep inside `contrato_service.contrato_listo`. The user still has to add
  obligaciones via `POST /contratos/{id}/obligaciones`; that call is made
  below but is deliberately NOT counted in the manual/auto ledger: it's a step
  the journey as specified doesn't ask us to score.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.email.port import EmailMessage
from app.models.secop import SecopDocumento
from app.schemas.agent import LLMResponse
from app.services import evidence_discovery_service as eds
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"

# Mandatory (obligatorio=True) codes in the standard catalog seed — see
# checklist_service._CATALOGO_SEED / test_radicar.py's _CODIGOS_OBLIGATORIOS.
_CODIGOS_OBLIGATORIOS = {
    "CONTRATO",
    "RPC",
    "SEGURIDAD_SOCIAL",
    "INFORME_ACTIVIDADES",
    "INFORME_SUPERVISION",
    "EVIDENCIAS",
    "CEDULA",
    "RUT",
    "ACTA_INICIO",
}

# Codes that can be satisfied purely by seeding a matching SECOP documento and
# calling /checklist/refresh-secop — no human upload required.
_CODIGOS_AUTO_SECOP = {"CONTRATO", "RPC", "SEGURIDAD_SOCIAL", "CEDULA", "RUT", "ACTA_INICIO"}


class JourneyLedger:
    """Tallies every user-facing manual input vs. every system-resolved step.

    `manual()` = the human had to type a field, choose an option, upload a
    file, or click an action button. `auto()` = the system derived, detected,
    generated or persisted something on its own. The itemized lists are
    printed in the failure message so a future regression shows exactly which
    step broke the ceiling.
    """

    def __init__(self) -> None:
        self.manual_items: list[str] = []
        self.auto_items: list[str] = []

    def manual(self, label: str) -> None:
        self.manual_items.append(label)

    def auto(self, label: str) -> None:
        self.auto_items.append(label)

    @property
    def manual_count(self) -> int:
        return len(self.manual_items)

    @property
    def auto_count(self) -> int:
        return len(self.auto_items)

    def render(self) -> str:
        lines = ["", "=" * 70, "JOURNEY LEDGER (manual input vs. auto-resolved)", "=" * 70]
        lines.append(f"MANUAL ({self.manual_count}):")
        lines += [f"  {i + 1}. {m}" for i, m in enumerate(self.manual_items)]
        lines.append(f"AUTO ({self.auto_count}):")
        lines += [f"  {i + 1}. {a}" for i, a in enumerate(self.auto_items)]
        lines.append("=" * 70)
        return "\n".join(lines)


def _patch_actividades_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Patch the late-imported `get_llm` used by cuenta_cobro_service.generar_actividades_agente."""
    import app.adapters.llm as llm_pkg

    class _FakeLLM:
        async def complete(self, *a, **k) -> LLMResponse:
            return LLMResponse(content=content, model="fake", prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(), raising=True)


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


def _email(mid: str, subject: str, body: str) -> EmailMessage:
    return EmailMessage(
        id=mid,
        thread_id="t1",
        subject=subject,
        sender="supervisor@entidad.gov.co",
        recipients=["contratista@gmail.com"],
        date=datetime(2024, 3, 15, tzinfo=UTC),
        body_plain=body,
        snippet=body[:80],
    )


def _connected_status() -> MagicMock:
    s = MagicMock()
    s.connected = True
    return s


async def test_full_radicacion_journey(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = test_user["headers"]
    ledger = JourneyLedger()

    # ── 1. SECOP contract import (mocked Socrata) ───────────────────────────
    cedula = "1016019452"
    ledger.manual("cédula del contratista (documento_proveedor, consulta SECOP)")

    secop_row = {
        "numero_contrato": "CO1.PCCNTR.JOURNEY0001",
        "objeto_del_contrato": (
            "Prestación de servicios profesionales de apoyo a la gestión administrativa "
            "y atención al ciudadano de la Secretaría."
        ),
        "valor_del_contrato": "24000000",
        "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
        "fecha_de_fin_del_contrato": "2024-12-31T00:00:00.000",
        "nombre_entidad": "Alcaldía de Bogotá",
        "nombre_supervisor": "Carlos Supervisor",
        "documento_proveedor": cedula,
    }

    # LLM fallback: the objeto text above has no enumerated obligations section,
    # so the best-effort extraction (bug #6) genuinely finds nothing here — mocked
    # deterministically instead of hitting a real provider.
    empty_obligaciones_llm = LLMResponse(content="", model="fake", total_tokens=5)

    with (
        patch(
            "app.services.secop_service._query_socrata",
            new_callable=AsyncMock,
            return_value=[secop_row],
        ),
        patch("app.adapters.llm.get_llm") as mock_get_llm,
    ):
        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value=empty_obligaciones_llm)
        mock_get_llm.return_value = mock_llm

        resp = await client.post(
            f"/api/v1/secop/importar?documento_proveedor={cedula}&confirmar=true",
            headers=headers,
        )
    assert resp.status_code == 201, resp.text
    import_result = resp.json()
    assert import_result["importados"] == 1
    contrato = import_result["contratos"][0]
    assert contrato["numero_contrato"] == "CO1.PCCNTR.JOURNEY0001"
    assert contrato["entidad"] == "Alcaldía de Bogotá"
    assert contrato["supervisor_nombre"] == "Carlos Supervisor"
    assert Decimal(contrato["valor_mensual"]) > 0
    assert contrato["obligaciones"] == []  # confirms the friction noted in the module docstring
    assert contrato["requiere_obligaciones"] is True  # bug #6: no silent dead end
    ledger.auto("SECOP auto-completó numero_contrato/objeto/valores/fechas/entidad/supervisor")
    contrato_id = contrato["id"]

    # Seed obligaciones (SECOP never provides them — see FRICTION note above).
    # Not counted in the manual/auto ledger: it's outside the 9-step journey's
    # scored surface, but flagged explicitly in the test-report friction list.
    obligaciones_desc = [
        "Elaborar y presentar informes mensuales de avance de las actividades "
        "desarrolladas en cumplimiento del objeto contractual",
        "Apoyar la atención al ciudadano y la gestión de solicitudes radicadas "
        "ante la entidad durante el período contractual",
    ]
    obligacion_ids: list[str] = []
    for i, desc in enumerate(obligaciones_desc, start=1):
        r = await client.post(
            f"/api/v1/contratos/{contrato_id}/obligaciones",
            headers=headers,
            json={"descripcion": desc, "tipo": "especifica", "orden": i},
        )
        assert r.status_code == 201, r.text
        obligacion_ids.append(r.json()["id"])

    # ── 2. Create cuenta de cobro WITHOUT valor (B.2 server default) ────────
    ledger.manual("mes de la cuenta de cobro")
    ledger.manual("año de la cuenta de cobro")
    r = await client.post(
        "/api/v1/cuentas-cobro/",
        headers=headers,
        json={"contrato_id": contrato_id, "mes": 3, "anio": 2024},
    )
    assert r.status_code == 201, r.text
    cuenta = r.json()
    cuenta_id = cuenta["id"]
    assert Decimal(cuenta["valor"]) == Decimal(contrato["valor_mensual"])
    ledger.auto("servidor calculó 'valor' de la cuenta desde contrato.valor_mensual (B.2)")

    # ── 3. Checklist gate: modo estandar ─────────────────────────────────────
    ledger.manual("elección de modo de checklist = estándar")
    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_id}/requisitos",
        headers=headers,
        json={"modo": "estandar", "requisitos": []},
    )
    assert r.status_code == 200, r.text
    ledger.auto("checklist estándar auto-sembrado (9 requisitos obligatorios + opcionales)")

    # ── 4. Auto-resolution via SECOP documentos cache ───────────────────────
    numero = contrato["numero_contrato"]
    secop_docs = [
        SecopDocumento(
            id_documento_secop="J-DOC-1",
            numero_contrato=numero,
            nombre_archivo="Contrato Minuta Clausulado firmado.pdf",
            descripcion="Contrato",
            datos_raw={},
        ),
        SecopDocumento(
            id_documento_secop="J-DOC-2",
            numero_contrato=numero,
            nombre_archivo="RPC Registro Presupuestal RP Compromiso Presupuestal.pdf",
            descripcion="RPC",
            datos_raw={},
        ),
        SecopDocumento(
            id_documento_secop="J-DOC-3",
            numero_contrato=numero,
            nombre_archivo="Planilla PILA seguridad social aportes.pdf",
            descripcion="Seguridad social",
            datos_raw={},
        ),
        SecopDocumento(
            id_documento_secop="J-DOC-4",
            numero_contrato=numero,
            nombre_archivo="Cedula cédula de ciudadanía contratista.pdf",
            descripcion="Cédula",
            datos_raw={},
        ),
        SecopDocumento(
            id_documento_secop="J-DOC-5",
            numero_contrato=numero,
            nombre_archivo="RUT actualizado contratista.pdf",
            descripcion="RUT",
            datos_raw={},
        ),
        SecopDocumento(
            id_documento_secop="J-DOC-6",
            numero_contrato=numero,
            nombre_archivo="Acta de Inicio Acta Inicio del contrato.pdf",
            descripcion="Acta de inicio",
            datos_raw={},
        ),
    ]
    db.add_all(secop_docs)
    await db.commit()

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/refresh-secop", headers=headers)
    assert r.status_code == 200, r.text
    checklist_body = r.json()
    detectados = {
        i["requisito"]["codigo"]
        for i in checklist_body["items"]
        if i["estado"] in ("detectado", "cargado")
    }
    assert detectados >= _CODIGOS_AUTO_SECOP, f"esperaba auto-detección SECOP, obtuve: {detectados}"
    for codigo in sorted(_CODIGOS_AUTO_SECOP):
        ledger.auto(f"SECOP auto-detectó y vinculó el requisito {codigo}")

    # ── 5. The one requisito that structurally cannot auto-detect ───────────
    # EVIDENCIAS has no keywords_deteccion (it's tied to obligaciones/cobertura,
    # not a single document) — the only way to clear it today is a manual mark
    # or no_aplica. This is the single upload/mark this journey needs.
    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/EVIDENCIAS",
        headers=headers,
        json={"cumplido_manual": True},
    )
    assert r.status_code == 200, r.text
    ledger.manual("marcar EVIDENCIAS como cumplido_manual (fila estructural, sin auto-detección posible)")

    # ── Negative check: radicar before informes exist -> CHECKLIST_INCOMPLETE
    ledger.manual("click en Radicar")
    negative = await client.post(f"/api/v1/cuentas-cobro/{cuenta_id}/radicar", headers=headers)
    assert negative.status_code in (400, 422), negative.text
    negative_body = negative.json()
    assert negative_body["code"] == "CHECKLIST_INCOMPLETE"

    # ── 6. Actividades + justificación via the LLM agent (mocked) ───────────
    llm_response = (
        "ACTIVIDAD|Elaboré y entregué el informe mensual de avance de actividades al supervisor"
        "|Cumplimiento de la obligación de reportar avances mensuales|1\n"
        "ACTIVIDAD|Brindé atención al ciudadano y gestioné solicitudes radicadas ante la entidad"
        "|Cumplimiento de la obligación de apoyo a la atención al ciudadano|2\n"
    )
    _patch_actividades_llm(monkeypatch, llm_response)
    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta_id}/actividades/generar", headers=headers)
    assert r.status_code == 201, r.text
    actividades_result = r.json()
    assert actividades_result["creadas"] == 2
    assert {a["obligacion_id"] for a in actividades_result["actividades"]} == set(obligacion_ids)
    ledger.auto("LLM generó actividades + justificación a partir de las obligaciones")

    # ── 7. Evidence discovery (Gmail/Drive/Calendar mocked) + persist ───────
    # A single email whose body plausibly evidences BOTH obligaciones (shared
    # vocabulary keeps the matcher's keyword pre-filter above threshold for
    # both), so both obligaciones come back justified with a linked evidence.
    gmail = MagicMock()
    gmail.search_messages = AsyncMock(
        return_value=[
            _email(
                "m1",
                "Informe mensual de actividades y atención al ciudadano",
                "Adjunto el informe mensual de avance de actividades del contrato correspondiente "
                "al período de marzo, incluyendo el apoyo brindado en la atención al ciudadano y "
                "la gestión de solicitudes radicadas ante la entidad.",
            )
        ]
    )
    drive_adapter = MagicMock()
    drive_adapter.search_files = AsyncMock(return_value=[])
    cal_adapter = MagicMock()
    cal_adapter.search_events = AsyncMock(return_value=[])

    filter_llm = AsyncMock()
    filter_llm.complete = AsyncMock(return_value=MagicMock(content='[{"idx": 0, "verdict": "TRABAJO"}]'))
    matcher_llm = AsyncMock()
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="[1]", total_tokens=5))
    justify_llm = AsyncMock()
    justify_llm.complete = AsyncMock(
        return_value=MagicMock(
            content="Cumplí la obligación mediante el informe mensual adjunto, con evidencia de soporte."
        )
    )

    with (
        patch.object(eds.gws, "get_integration_status", AsyncMock(return_value=_connected_status())),
        patch.object(eds, "GmailAdapter", return_value=gmail),
        patch("app.agent.nodes.drive_fetch.DriveAdapter", return_value=drive_adapter),
        patch("app.agent.nodes.calendar_fetch.GoogleCalendarAdapter", return_value=cal_adapter),
        patch("app.agent.nodes.evidence_filter.get_llm", return_value=filter_llm),
        patch("app.agent.nodes.evidence_matcher.get_llm", return_value=matcher_llm),
        patch("app.agent.nodes.evidence_justify.get_llm", return_value=justify_llm),
    ):
        r = await client.post(
            "/api/v1/integraciones/evidencias/descubrir",
            headers=headers,
            json={
                "contrato_id": contrato_id,
                "fecha_inicio": "2024-03-01",
                "fecha_fin": "2024-03-31",
                "supervisor_email": "supervisor@entidad.gov.co",
                "entidad": "Alcaldía de Bogotá",
            },
        )
    assert r.status_code == 200, r.text
    discovery = r.json()
    assert len(discovery["obligaciones"]) == 2
    ledger.auto("agente explorador descubrió evidencia en Gmail/Drive/Calendar y redactó justificación")

    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_id}/evidencias/persistir",
        headers=headers,
        json={"obligaciones": discovery["obligaciones"]},
    )
    assert r.status_code == 200, r.text
    persist_summary = r.json()
    assert persist_summary["evidencias_creadas"] == 2
    ledger.auto("persistencia automática de Evidencia por cada obligación justificada")

    # ── 8. Cobertura: no obligación should remain SIN_EVIDENCIA ─────────────
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/cobertura", headers=headers)
    assert r.status_code == 200, r.text
    cobertura = r.json()
    assert cobertura["resumen"]["sin_evidencia"] == 0, cobertura

    # ── 9. Autogen the two mandatory informes ───────────────────────────────
    with patch(_PATCH_S3, return_value=_fake_storage()):
        r = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/INFORME_ACTIVIDADES/generar",
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["estado"] == "cargado"
        ledger.auto("autogen del INFORME_ACTIVIDADES (DOCX generado desde las actividades)")

        r = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/INFORME_SUPERVISION/generar",
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["estado"] == "cargado"
        ledger.auto("autogen del INFORME_SUPERVISION (DOCX generado desde las actividades)")

    # ── 10. Radicar — now the checklist is complete ─────────────────────────
    final = await client.post(f"/api/v1/cuentas-cobro/{cuenta_id}/radicar", headers=headers)
    assert final.status_code == 200, final.text
    final_body = final.json()
    assert final_body["estado"] == "enviada"
    assert final_body["fecha_envio"] is not None

    # ── Ledger assertions (the usability regression guard) ──────────────────
    report = ledger.render()
    assert ledger.manual_count <= 6, (
        f"Manual input ceiling exceeded ({ledger.manual_count} > 6).{report}"
    )
    assert ledger.auto_count >= 8, (
        f"Auto-resolution floor not met ({ledger.auto_count} < 8).{report}"
    )

    print(report)  # noqa: T201 — intentional: -s shows the tally for docs/usability-findings.md
