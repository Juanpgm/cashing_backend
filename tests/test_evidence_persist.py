"""Tests for persisting discovered evidence (POST /cuentas-cobro/{id}/evidencias/persistir).

Covers the service function `evidence_persist_service.persistir_evidencias` and its
API endpoint: turning the output of the evidence-discovery agent into real
Actividad + Evidencia rows so coverage (`cobertura_service`) reflects it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.exceptions import NotFoundError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.cobertura import EstadoCobertura
from app.schemas.google_workspace import EvidenceLink, ObligacionJustificada
from app.services import cobertura_service, evidence_persist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _make_user(db: AsyncSession, *, email: str = "persist@test.com") -> Usuario:
    user = Usuario(
        email=email,
        nombre="Persist User",
        cedula="987654321",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="CTR-PERSIST-001",
        objeto="Prestación de servicios",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(contrato)
    await db.flush()
    return contrato


async def _make_obligacion(db: AsyncSession, contrato_id: uuid.UUID, orden: int = 1) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=f"Obligación contractual {orden}",
        tipo=TipoObligacion.ESPECIFICA,
        orden=orden,
    )
    db.add(ob)
    await db.flush()
    return ob


async def _make_cuenta(db: AsyncSession, contrato_id: uuid.UUID) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=3,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cuenta)
    await db.flush()
    return cuenta


@pytest.fixture
async def scenario(db: AsyncSession) -> dict[str, Any]:
    # NOTE: deliberately flush (not commit+refresh) the CuentaCobro. Its
    # `actividades` relationship uses `lazy="selectin"`, which is loaded
    # eagerly on refresh/reload — refreshing here would cache an empty
    # collection on this identity-mapped instance and hide the Actividad
    # created later by persistir_evidencias in the same session.
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()
    return {"user": user, "contrato": contrato, "obligacion": obligacion, "cuenta": cuenta}


def _obligacion_justificada(
    obligacion: Obligacion,
    *,
    justificacion: str = "Entregué el informe mensual.",
    actividad: str = "Elaboré y entregué el informe mensual de actividades del contrato.",
) -> ObligacionJustificada:
    return ObligacionJustificada(
        obligacion_id=str(obligacion.id),
        descripcion=obligacion.descripcion,
        actividad=actividad,
        justificacion=justificacion,
        evidencias=[
            EvidenceLink(
                source="email",
                titulo="Informe mensual",
                link="https://mail.google.com/mail/u/0/#all/abc123",
                fecha="2024-03-10",
            )
        ],
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_persistir_crea_actividad_y_evidencia_y_cubre_obligacion(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [_obligacion_justificada(obligacion)]

    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_creadas == 1
    assert summary.evidencias_creadas == 1
    assert summary.evidencias_omitidas == 0

    cobertura = await cobertura_service.calcular_cobertura(db, user.id, cuenta.id)
    item = next(i for i in cobertura.obligaciones if i.obligacion_id == obligacion.id)
    assert item.estado == EstadoCobertura.CUBIERTA


async def test_persistir_crea_enlace_confirmado_evidencia_obligacion(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """`cobertura_service` counts CONFIRMED `EvidenciaObligacion` links only —
    the discovery/persist path must write one per persisted evidencia, or
    coverage falls back to SIN_EVIDENCIA even though evidence was persisted."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [_obligacion_justificada(obligacion)]

    await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    evidencia = result.scalars().one()

    result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == evidencia.id))
    link = result.scalars().one()
    assert link.obligacion_id == obligacion.id
    assert link.status == EstadoEnlace.CONFIRMED.value
    assert link.source == "ai"
    assert link.confianza == "alta"


async def test_persistir_es_idempotente(db: AsyncSession, scenario: dict[str, Any]) -> None:
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [_obligacion_justificada(obligacion)]

    await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)
    segunda = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert segunda.actividades_creadas == 0
    assert segunda.evidencias_creadas == 0
    assert segunda.evidencias_omitidas == 1

    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    assert len(result.scalars().all()) == 1


async def test_persistir_reemplaza_justificacion_existente(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Regenerating REPLACES the existing draft text (product decision): the user
    clicks "Generar" again precisely to discard the previous wording. An empty
    generation must never blank existing text, though."""
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    user = scenario["user"]

    existente = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion=obligacion.descripcion,
        justificacion="Borrador anterior que el usuario quiere descartar.",
    )
    db.add(existente)
    await db.commit()
    await db.refresh(existente)

    entrada = [_obligacion_justificada(obligacion, justificacion="Justificación generada por el agente.")]
    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_creadas == 0
    assert summary.actividades_actualizadas == 1
    assert summary.evidencias_creadas == 1

    await db.refresh(existente)
    assert existente.justificacion == "Justificación generada por el agente."


async def test_persistir_generacion_vacia_no_borra_texto_existente(db: AsyncSession, scenario: dict[str, Any]) -> None:
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    user = scenario["user"]

    existente = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Descripción previa.",
        justificacion="Justificación previa.",
    )
    db.add(existente)
    await db.commit()
    await db.refresh(existente)

    entrada = [_obligacion_justificada(obligacion, actividad="", justificacion="")]
    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_actualizadas == 0
    await db.refresh(existente)
    assert existente.descripcion == "Descripción previa."
    assert existente.justificacion == "Justificación previa."


async def test_persistir_seed_no_pisa_justificacion_real(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Workstream B (B3): a `seed`-origin incoming justificación must NEVER overwrite
    an existing `llm`/`manual` one — the seed fallback would silently discard real
    Gemini-authored text on a later, degraded regeneration."""
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    user = scenario["user"]

    existente = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion=obligacion.descripcion,
        justificacion="Justificación real de Gemini.",
        justificacion_origen="llm",
    )
    db.add(existente)
    await db.commit()
    await db.refresh(existente)

    entrada = [_obligacion_justificada(obligacion, justificacion="Texto seed degradado.")]
    entrada[0].origen = "seed"
    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_actualizadas == 0
    await db.refresh(existente)
    assert existente.justificacion == "Justificación real de Gemini."
    assert existente.justificacion_origen == "llm"


async def test_persistir_llm_pisa_justificacion_seed_existente(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Workstream B (B3): `llm`/`manual`-origin incoming text SHOULD replace an
    existing `seed` one — preserves the pre-existing "regenerate discards the
    draft" behavior for genuinely real text."""
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    user = scenario["user"]

    existente = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion=obligacion.descripcion,
        justificacion="Texto seed anterior.",
        justificacion_origen="seed",
    )
    db.add(existente)
    await db.commit()
    await db.refresh(existente)

    entrada = [_obligacion_justificada(obligacion, justificacion="Justificación real de Gemini.")]
    entrada[0].origen = "llm"
    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_actualizadas == 1
    await db.refresh(existente)
    assert existente.justificacion == "Justificación real de Gemini."
    assert existente.justificacion_origen == "llm"


async def test_persistir_usa_actividad_no_descripcion_ni_justificacion(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """Actividad.descripcion must come from `actividad`, never echo the obligación
    text nor the justificación — that was the root cause of DOCX informes showing
    "Actividad realizada" == "Justificación" or == the obligación text."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [
        _obligacion_justificada(
            obligacion,
            actividad="Elaboré y presenté el informe de avance mensual al supervisor.",
            justificacion="El informe demuestra el cumplimiento de la obligación de reportar avances.",
        )
    ]

    await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    result = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion.id))
    actividad = result.scalar_one()
    assert actividad.descripcion == "Elaboré y presenté el informe de avance mensual al supervisor."
    assert actividad.descripcion != obligacion.descripcion
    assert actividad.descripcion != actividad.justificacion


async def test_persistir_sin_actividad_usa_fallback_deterministico_no_obligacion(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """Backward compatibility: a client posting the OLD payload shape (no `actividad`
    field — schema default "") must still get a descripcion that is NOT the
    obligación text and NOT the justificación (deterministic fallback instead)."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [_obligacion_justificada(obligacion, actividad="", justificacion="Justificación del agente.")]

    await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    result = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion.id))
    actividad = result.scalar_one()
    assert actividad.descripcion != obligacion.descripcion
    assert actividad.descripcion != "Justificación del agente."
    assert "evidencia" in actividad.descripcion.lower()


async def test_persistir_multiples_actividades_misma_obligacion_no_lanza_multiple_results(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """Regression: if /cruzar (or any other flow) already created MORE THAN ONE
    Actividad for the same cuenta+obligación, persistir_evidencias must not raise
    MultipleResultsFound — it must deterministically pick one (preferring the
    Actividad still missing a justificación) and attach the evidence there."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]

    primera = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Evidencia documental: doc1.pdf",
        justificacion="Ya tiene justificación de /cruzar.",
    )
    segunda = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Evidencia documental: doc2.pdf",
        justificacion="",
    )
    db.add_all([primera, segunda])
    await db.commit()

    entrada = [_obligacion_justificada(obligacion, justificacion="Justificación generada por el agente.")]

    summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.actividades_creadas == 0
    assert summary.evidencias_creadas == 1

    await db.refresh(segunda)
    await db.refresh(primera)
    # Attached to the one that still lacked a justificación, not the one that had it.
    assert segunda.justificacion == "Justificación generada por el agente."
    assert primera.justificacion == "Ya tiene justificación de /cruzar."


async def test_persistir_otro_usuario_falla_404(db: AsyncSession, scenario: dict[str, Any]) -> None:
    obligacion, cuenta = scenario["obligacion"], scenario["cuenta"]
    other_user_id = uuid.uuid4()
    entrada = [_obligacion_justificada(obligacion)]

    with pytest.raises(NotFoundError):
        await evidence_persist_service.persistir_evidencias(db, other_user_id, cuenta.id, entrada)


async def test_persistir_obligacion_de_otro_contrato_del_mismo_usuario_falla(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """A user cannot inject an Actividad referencing an obligación from a DIFFERENT
    contrato of their own — obligacion_id must belong to the cuenta's own contrato."""
    user, cuenta = scenario["user"], scenario["cuenta"]

    otro_contrato = await _make_contrato(db, user.id)
    obligacion_ajena = await _make_obligacion(db, otro_contrato.id)
    await db.commit()

    entrada = [_obligacion_justificada(obligacion_ajena)]

    with pytest.raises(ValidationError):
        await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)


async def test_persistir_obligacion_de_otro_usuario_falla(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """Attack scenario: user A persists an actividad on their own cuenta referencing
    user B's obligación. Must be rejected — obligacion.contrato_id must match
    cuenta.contrato_id, regardless of who owns each contrato."""
    user, cuenta = scenario["user"], scenario["cuenta"]

    other_user = await _make_user(db, email="other@test.com")
    other_contrato = await _make_contrato(db, other_user.id)
    obligacion_de_otro = await _make_obligacion(db, other_contrato.id)
    await db.commit()

    entrada = [_obligacion_justificada(obligacion_de_otro)]

    with pytest.raises(ValidationError):
        await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    # No Actividad should have been created from the rejected request.
    result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    assert result.scalars().all() == []


# ── API test ──────────────────────────────────────────────────────────────────


async def test_persistir_endpoint_happy_path(client, db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()
    await db.refresh(obligacion)
    await db.refresh(cuenta)

    payload = {
        "obligaciones": [
            {
                "obligacion_id": str(obligacion.id),
                "descripcion": obligacion.descripcion,
                "justificacion": "Entregué el informe mensual.",
                "evidencias": [
                    {
                        "source": "email",
                        "titulo": "Informe mensual",
                        "link": "https://mail.google.com/mail/u/0/#all/abc123",
                        "fecha": "2024-03-10",
                    }
                ],
            }
        ]
    }

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/persistir",
        headers=test_user["headers"],
        json=payload,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["actividades_creadas"] == 1
    assert data["evidencias_creadas"] == 1


# ── Evidence link URL scheme validation (stored-XSS guard) ────────────────────


def _payload_con_link(obligacion: Obligacion, link: str) -> dict[str, Any]:
    return {
        "obligaciones": [
            {
                "obligacion_id": str(obligacion.id),
                "descripcion": obligacion.descripcion,
                "justificacion": "Entregué el informe mensual.",
                "evidencias": [
                    {
                        "source": "email",
                        "titulo": "Informe mensual",
                        "link": link,
                        "fecha": "2024-03-10",
                    }
                ],
            }
        ]
    }


async def test_persistir_endpoint_rechaza_link_javascript_scheme(
    client, db: AsyncSession, test_user: dict[str, Any]
) -> None:
    """A `javascript:` URL must never be accepted — it would be stored verbatim
    into Evidencia.url and later rendered as an href by the frontend (stored XSS)."""
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/persistir",
        headers=test_user["headers"],
        json=_payload_con_link(obligacion, "javascript:alert(1)"),
    )

    assert resp.status_code == 422


async def test_persistir_endpoint_rechaza_link_data_scheme(client, db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/persistir",
        headers=test_user["headers"],
        json=_payload_con_link(obligacion, "data:text/html,<script>alert(1)</script>"),
    )

    assert resp.status_code == 422


async def test_persistir_endpoint_rechaza_link_vacio(client, db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/persistir",
        headers=test_user["headers"],
        json=_payload_con_link(obligacion, ""),
    )

    assert resp.status_code == 422


async def test_persistir_endpoint_acepta_link_https(client, db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    contrato = await _make_contrato(db, user.id)
    obligacion = await _make_obligacion(db, contrato.id)
    cuenta = await _make_cuenta(db, contrato.id)
    await db.commit()

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/evidencias/persistir",
        headers=test_user["headers"],
        json=_payload_con_link(obligacion, "https://mail.google.com/mail/u/0/#all/xyz789"),
    )

    assert resp.status_code == 200


# ── A1: snapshot real bytes for discovered evidence (Problema A) ──────────────


def _fake_storage_upload_ok() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


async def test_persistir_email_con_attachment_id_descarga_bytes_reales(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """A `source="email"` link carrying `message_id`+`attachment_id` must download
    the real bytes via `EmailPort.get_attachment` and set `storage_key`/`tipo_archivo`
    /`tamano_bytes` on the persisted Evidencia — not just store the link."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    contenido = b"contenido real del adjunto de gmail"

    entrada = [
        _obligacion_justificada(obligacion),
    ]
    entrada[0].evidencias = [
        EvidenceLink(
            source="email",
            titulo="Informe mensual",
            link="https://mail.google.com/mail/u/0/#all/m1",
            fecha="2024-03-10",
            provider="google",
            message_id="m1",
            attachment_id="a1",
            mime_type="application/pdf",
        )
    ]

    storage = _fake_storage_upload_ok()
    gmail = AsyncMock()
    gmail.get_attachment = AsyncMock(return_value=contenido)

    with (
        patch("app.adapters.email.gmail_adapter.GmailAdapter", return_value=gmail),
        patch("app.services.informe_service.get_evidencia_storage", return_value=storage),
    ):
        summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.evidencias_creadas == 1
    gmail.get_attachment.assert_awaited_once_with(user.id, "m1", "a1")
    storage.upload.assert_awaited_once()
    _, kwargs = storage.upload.call_args
    assert kwargs["data"] == contenido
    assert kwargs["content_type"] == "application/pdf"

    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    evidencia = result.scalars().one()
    assert evidencia.storage_key is not None and evidencia.storage_key.startswith("evidencias/")
    assert evidencia.tipo_archivo == "application/pdf"
    assert evidencia.tamano_bytes == len(contenido)
    assert evidencia.url == "https://mail.google.com/mail/u/0/#all/m1"  # url kept as reference


async def test_persistir_drive_con_file_id_descarga_bytes_reales(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """A `source="drive"` link carrying `file_id` must download via
    `DrivePort.download_file`."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    contenido = b"contenido real del archivo de drive"

    entrada = [_obligacion_justificada(obligacion)]
    entrada[0].evidencias = [
        EvidenceLink(
            source="drive",
            titulo="Informe mensual.pdf",
            link="https://drive.google.com/file/d/f1/view",
            fecha="2024-03-10",
            provider="google",
            file_id="f1",
            mime_type="application/pdf",
        )
    ]

    storage = _fake_storage_upload_ok()
    drive = AsyncMock()
    drive.download_file = AsyncMock(return_value=contenido)

    with (
        patch("app.adapters.drive.drive_adapter.DriveAdapter", return_value=drive),
        patch("app.services.informe_service.get_evidencia_storage", return_value=storage),
    ):
        summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.evidencias_creadas == 1
    drive.download_file.assert_awaited_once_with(user.id, "f1")

    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    evidencia = result.scalars().one()
    assert evidencia.storage_key is not None and evidencia.storage_key.startswith("evidencias/")
    assert evidencia.tamano_bytes == len(contenido)


async def test_persistir_descarga_fallida_no_bloquea_y_queda_como_enlace(
    db: AsyncSession, scenario: dict[str, Any]
) -> None:
    """Fail-open: a download error must not sink the whole persist call — the
    Evidencia is still created, just link-only (storage_key=None), same as before
    A1 existed."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]

    entrada = [_obligacion_justificada(obligacion)]
    entrada[0].evidencias = [
        EvidenceLink(
            source="email",
            titulo="Informe mensual",
            link="https://mail.google.com/mail/u/0/#all/m1",
            fecha="2024-03-10",
            provider="google",
            message_id="m1",
            attachment_id="a1",
        )
    ]

    gmail = AsyncMock()
    gmail.get_attachment = AsyncMock(side_effect=RuntimeError("gmail down"))

    with patch("app.adapters.email.gmail_adapter.GmailAdapter", return_value=gmail):
        summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    assert summary.evidencias_creadas == 1
    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    evidencia = result.scalars().one()
    assert evidencia.storage_key is None
    assert evidencia.url == "https://mail.google.com/mail/u/0/#all/m1"


async def test_persistir_link_generico_no_intenta_descargar(db: AsyncSession, scenario: dict[str, Any]) -> None:
    """A link with no provider file id (the pre-A1 shape, or a calendar event) never
    triggers a download attempt — stays link-only, exactly as before this change."""
    user, obligacion, cuenta = scenario["user"], scenario["obligacion"], scenario["cuenta"]
    entrada = [_obligacion_justificada(obligacion)]  # default EvidenceLink: no provider ids

    with patch("app.adapters.email.gmail_adapter.GmailAdapter") as gmail_cls:
        summary = await evidence_persist_service.persistir_evidencias(db, user.id, cuenta.id, entrada)

    gmail_cls.assert_not_called()
    assert summary.evidencias_creadas == 1
    result = await db.execute(select(Evidencia).join(Actividad).where(Actividad.obligacion_id == obligacion.id))
    evidencia = result.scalars().one()
    assert evidencia.storage_key is None
