"""Tests for actividad service and API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.core.security import create_access_token, hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.actividad import ActividadCreate, ActividadUpdate
from app.services import actividad_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ACT-001",
        objeto="Servicios de consultoría tecnológica",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Ana Supervisora",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── Service tests ──────────────────────────────────────────────────────────────


async def test_crear_actividad(db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro) -> None:
    user = test_user["user"]
    data = ActividadCreate(
        descripcion="Reunión de seguimiento con el equipo de desarrollo",
        fecha_realizacion=date(2024, 3, 15),
    )
    result = await actividad_service.crear_actividad(db, user.id, cuenta_cobro.id, data)
    assert result.descripcion == data.descripcion
    assert result.cuenta_cobro_id == cuenta_cobro.id


async def test_listar_actividades(db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro) -> None:
    user = test_user["user"]
    for i in range(3):
        await actividad_service.crear_actividad(
            db,
            user.id,
            cuenta_cobro.id,
            ActividadCreate(descripcion=f"Actividad {i}", fecha_realizacion=date(2024, 3, i + 1)),
        )
    result = await actividad_service.listar_actividades(db, user.id, cuenta_cobro.id)
    assert len(result) == 3


async def test_actualizar_actividad(db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro) -> None:
    user = test_user["user"]
    created = await actividad_service.crear_actividad(
        db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Reunión de seguimiento del proyecto")
    )
    updated = await actividad_service.actualizar_actividad(
        db, user.id, cuenta_cobro.id, created.id, ActividadUpdate(descripcion="Informe de actividades actualizado")
    )
    assert updated.descripcion == "Informe de actividades actualizado"


async def test_eliminar_actividad(db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro) -> None:
    user = test_user["user"]
    created = await actividad_service.crear_actividad(
        db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad a eliminar de prueba")
    )
    await actividad_service.eliminar_actividad(db, user.id, cuenta_cobro.id, created.id)
    lista = await actividad_service.listar_actividades(db, user.id, cuenta_cobro.id)
    assert all(a.id != created.id for a in lista)


async def test_crear_actividad_cuenta_cobro_enviada_falla(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    cuenta_cobro.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    with pytest.raises(ValidationError):
        await actividad_service.crear_actividad(
            db, user.id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad que debería estar bloqueada")
        )


async def test_crear_actividad_cuenta_cobro_not_found(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.core.exceptions import NotFoundError

    user = test_user["user"]
    with pytest.raises(NotFoundError):
        await actividad_service.crear_actividad(
            db, user.id, uuid.uuid4(), ActividadCreate(descripcion="Actividad en cuenta cobro inexistente")
        )


# ── API tests ──────────────────────────────────────────────────────────────────


async def test_api_crear_actividad(client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro) -> None:
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={"descripcion": "Actividad de prueba para API", "fecha_realizacion": "2024-03-10"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["descripcion"] == "Actividad de prueba para API"


async def test_api_listar_actividades(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    # Create via API
    await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={"descripcion": "Actividad para test de lista"},
    )
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


# ── GET actividades 500 on link-evidence with null tipo_archivo/tamano_bytes ──
#
# Same bug class as the checklist endpoint (already fixed in
# app/schemas/checklist.py::ArbolEvidenciaItem): link evidence created by the
# Gmail/Drive/Calendar discovery agent (evidence_persist_service.py) sets
# storage_key=None, tipo_archivo=None, tamano_bytes=None on purpose (there is
# no file, only a URL). EvidenciaResponse in app/schemas/actividad.py still
# declared all three as required non-nullable str/int, so pydantic raised a
# ValidationError while building ActividadResponse and it propagated as an
# UNHANDLED 500 on GET /cuentas-cobro/{id}/actividades (and would equally hit
# the POST/PATCH endpoints, which share the same response_model). Must return
# 200 with storage_key/tipo_archivo/tamano_bytes as null.
#
# Concurrent/race: N/A — this is a read of already-committed rows within one
# request; there is no write contention window in this endpoint.
# Network/server failure: N/A — this fix does not touch any outbound call.


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual con evidencias de prueba",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


async def test_api_listar_actividades_sin_evidencias_returns_200(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """Boundary: an actividad with zero evidencias must serialize fine."""
    await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={"descripcion": "Actividad sin evidencias asociadas"},
    )
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any(a["evidencias"] == [] for a in body)


async def test_api_listar_actividades_con_evidencia_link_null_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Empty/null/malformed: the exact all-null link-evidence shape
    (storage_key/tipo_archivo/tamano_bytes all None) reproduces the live 500."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia de Gmail", obligacion_id=obligacion.id),
    )
    evidencia = Evidencia(
        actividad_id=act.id,
        fuente="gmail",
        url="https://mail.google.com/mail/u/0/#inbox/abc123",
        nombre_archivo="Correo de soporte",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ev = next(e for a in body for e in a["evidencias"] if e["nombre_archivo"] == "Correo de soporte")
    assert ev["storage_key"] is None
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None


async def test_api_listar_actividades_solo_evidencia_archivo_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Boundary: an actividad with only file-evidence (both fields set)."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia de archivo", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente=None,
            url=None,
            nombre_archivo="soporte.pdf",
            storage_key="k/soporte.pdf",
            tipo_archivo="application/pdf",
            tamano_bytes=2048,
        )
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    ev = next(e for a in r.json() for e in a["evidencias"] if e["nombre_archivo"] == "soporte.pdf")
    assert ev["storage_key"] == "k/soporte.pdf"
    assert ev["tipo_archivo"] == "application/pdf"
    assert ev["tamano_bytes"] == 2048


async def test_api_listar_actividades_evidencia_mixta_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Boundary: mixed evidencias in the same actividad — one uploaded file
    (both fields set) and one link (both fields null) — both must serialize."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia mixta", obligacion_id=obligacion.id),
    )
    db.add_all(
        [
            Evidencia(
                actividad_id=act.id,
                fuente=None,
                url=None,
                nombre_archivo="soporte-mixto.pdf",
                storage_key="k/soporte-mixto.pdf",
                tipo_archivo="application/pdf",
                tamano_bytes=4096,
            ),
            Evidencia(
                actividad_id=act.id,
                fuente="drive",
                url="https://drive.google.com/file/d/xyz",
                nombre_archivo="Evidencia en Drive",
                storage_key=None,
                tipo_archivo=None,
                tamano_bytes=None,
            ),
        ]
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    evidencias = [e for a in r.json() for e in a["evidencias"]]
    nombres = {e["nombre_archivo"] for e in evidencias}
    assert {"soporte-mixto.pdf", "Evidencia en Drive"} <= nombres


async def test_api_listar_actividades_cuenta_enviada_con_evidencia_link_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """State-machine: a cuenta already radicada (estado=ENVIADA) with legacy
    link-evidence must still be readable via GET, not just BORRADOR."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad previa a radicar la cuenta", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente="calendar",
            url="https://calendar.google.com/event?eid=abc",
            nombre_archivo="Reunión de seguimiento",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    cuenta_cobro.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text


async def test_api_listar_actividades_evidencia_parcial_solo_storage_key_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Empty/null/malformed — partial-null legacy state: storage_key/tipo_archivo/
    tamano_bytes are three independently-nullable columns (see app.models.evidencia),
    so a row with storage_key set but tipo_archivo/tamano_bytes still null (or the
    inverse) is a plausible mixed state the all-null / all-set tests above don't
    cover. Must serialize reflecting exactly that partial shape, not coerce it."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia parcialmente migrada", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente=None,
            url=None,
            nombre_archivo="legacy-sin-metadata.dat",
            storage_key="k/legacy-sin-metadata.dat",
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    ev = next(e for a in r.json() for e in a["evidencias"] if e["nombre_archivo"] == "legacy-sin-metadata.dat")
    assert ev["storage_key"] == "k/legacy-sin-metadata.dat"
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None


# ── PATCH /actividades/{id} 500 on link-evidence with null tipo_archivo/tamano_bytes ──
#
# Same bug class and same EvidenciaResponse fix as the GET tests above — PATCH
# shares response_model=ActividadResponse, so it hit the identical unhandled
# 500 pre-fix. Only reachable while the cuenta de cobro is in estado BORRADOR
# (see actividad_service._ESTADOS_EDITABLES); the fixture cuenta_cobro already
# starts in BORRADOR.
#
# Concurrent/race: N/A — single-request read-modify-write within one test DB
# transaction, no contention window exercised here.
# Network/server failure: N/A — no outbound call on this path.


async def test_api_actualizar_actividad_con_evidencia_link_null_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Regression: PATCH /api/v1/actividades/{id} must not 500 when the
    actividad carries a link-evidencia row (storage_key/tipo_archivo/
    tamano_bytes all None) — the exact shape that reproduced the live bug."""
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia de Gmail a actualizar", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente="gmail",
            url="https://mail.google.com/mail/u/0/#inbox/def456",
            nombre_archivo="Correo de soporte a actualizar",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    await db.commit()

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Actividad revisada y justificada con evidencia adjunta"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(act.id)
    assert body["justificacion"] == "Actividad revisada y justificada con evidencia adjunta"
    ev = next(e for e in body["evidencias"] if e["nombre_archivo"] == "Correo de soporte a actualizar")
    assert ev["storage_key"] is None
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None


# ── EvidenciaResponse (actividad.py) missing fuente/url ────────────────────────
#
# app.schemas.actividad.EvidenciaResponse never declared `fuente`/`url`, so a
# link-evidencia (Gmail/Drive/Calendar) returned 200 in the Actividades tab but
# with those fields silently dropped — no way for a client to open the link or
# tell it apart from a corrupt/empty file row. Reference shape already correct
# in app.schemas.evidencia.EvidenciaResponse (used by GET /evidencias/actividades/{id}).


async def test_api_listar_actividades_evidencia_link_expone_fuente_y_url(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia de Gmail con link", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente="gmail",
            url="https://mail.google.com/mail/u/0/#inbox/link789",
            nombre_archivo="Correo con enlace",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    ev = next(e for a in r.json() for e in a["evidencias"] if e["nombre_archivo"] == "Correo con enlace")
    assert ev["fuente"] == "gmail"
    assert ev["url"] == "https://mail.google.com/mail/u/0/#inbox/link789"


async def test_api_actualizar_actividad_evidencia_link_expone_fuente_y_url(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    act = await actividad_service.crear_actividad(
        db,
        test_user["user"].id,
        cuenta_cobro.id,
        ActividadCreate(descripcion="Actividad con evidencia de Drive con link", obligacion_id=obligacion.id),
    )
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente="drive",
            url="https://drive.google.com/file/d/link999",
            nombre_archivo="Archivo en Drive con enlace",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    await db.commit()

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Justificación actualizada con evidencia de Drive"},
    )
    assert r.status_code == 200, r.text
    ev = next(e for e in r.json()["evidencias"] if e["nombre_archivo"] == "Archivo en Drive con enlace")
    assert ev["fuente"] == "drive"
    assert ev["url"] == "https://drive.google.com/file/d/link999"


async def test_api_crear_actividad_evidencia_link_expone_fuente_y_url_en_lectura_posterior(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """POST /cuentas-cobro/{id}/actividades had zero fuente/url coverage. Reading
    the endpoint first (per task instructions) surfaced a route-shadowing bug:
    app/api/v1/cuentas_cobro.py registers the SAME path ("/{cuenta_id}/actividades",
    prefix "/cuentas-cobro") and, because its router is include_router'd BEFORE
    actividades_router in app/api/router.py, FastAPI resolves every POST here to
    cuenta_cobro_service.agregar_actividad + the OLDER, leaner
    app.schemas.cuenta_cobro.ActividadResponse — which has no `evidencias` field at
    all (and no `cuenta_cobro_id`). The actividades.py POST handler + its fixed
    ActividadResponse (with fuente/url) are effectively dead code for this route.
    That routing bug is out of scope to fix here (separate, larger change), so this
    test pins the ACTUAL live POST contract and then verifies fuente/url on the
    unambiguous subsequent read (GET /cuentas-cobro/{id}/actividades, which is NOT
    shadowed and does use the fixed schema) — same regression class as the
    checklist árbol and evidencia.py fixes."""
    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
        json={
            "descripcion": "Actividad creada vía POST con evidencia de Calendar",
            "obligacion_id": str(obligacion.id),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The live (shadowing) route's response has no `evidencias` key at all —
    # see docstring. Guard this explicitly so a future routing fix is noticed
    # here instead of silently changing behavior underneath this test.
    assert "evidencias" not in body
    actividad_id = body["id"]

    db.add(
        Evidencia(
            actividad_id=uuid.UUID(actividad_id),
            fuente="calendar",
            url="https://calendar.google.com/event?eid=abc123",
            nombre_archivo="Reunión de seguimiento",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    ev = next(e for a in r.json() for e in a["evidencias"] if e["nombre_archivo"] == "Reunión de seguimiento")
    assert ev["fuente"] == "calendar"
    assert ev["url"] == "https://calendar.google.com/event?eid=abc123"


# ── State-machine, auth/ownership, not-found, validation, persistence gaps ────


@pytest.fixture
async def other_user(db: AsyncSession) -> dict[str, Any]:
    """A second, independent user (different contrato/cuenta ownership tree)."""
    user = Usuario(
        email="otro_actividades@example.com",
        nombre="Otro Usuario",
        cedula="987654321",
        password_hash=hash_password("OtherPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(subject=str(user.id), role=user.rol)
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def test_api_actualizar_actividad_cuenta_enviada_rechaza(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """State-machine: PATCH on an actividad whose cuenta is ENVIADA (not in
    _ESTADOS_EDITABLES = {BORRADOR}) must be rejected with the ValidationError
    status code (422), not silently accepted and not a generic non-200."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad antes de radicar la cuenta")
    )
    cuenta_cobro.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Intento de edición bloqueado por estado"},
    )
    assert r.status_code == 422, r.text


async def test_api_actualizar_actividad_cuenta_aprobada_rechaza(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """State-machine: same as ENVIADA but for APROBADA."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad antes de aprobar la cuenta")
    )
    cuenta_cobro.estado = EstadoCuentaCobro.APROBADA
    await db.commit()

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Intento de edición bloqueado por estado"},
    )
    assert r.status_code == 422, r.text


async def test_api_listar_actividades_otro_usuario_404(
    client: AsyncClient,
    test_user: dict[str, Any],
    other_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
) -> None:
    """Auth/ownership: GET actividades for a cuenta owned by ANOTHER user 404s,
    via _get_cuenta_cobro_owned's Contrato.usuario_id filter."""
    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=other_user["headers"],
    )
    assert r.status_code == 404, r.text


async def test_api_actualizar_actividad_otro_usuario_404(
    client: AsyncClient,
    test_user: dict[str, Any],
    other_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """Auth/ownership: PATCH on an actividad belonging to ANOTHER user's cuenta
    404s, not 403 and not 200 — same ownership check as GET."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad propiedad del usuario A")
    )

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=other_user["headers"],
        json={"justificacion": "Intento de edición desde otro usuario"},
    )
    assert r.status_code == 404, r.text


async def test_api_actualizar_actividad_no_encontrada_404(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
) -> None:
    """Not-found: PATCH with a non-existent actividad_id 404s."""
    r = await client.patch(
        f"/api/v1/actividades/{uuid.uuid4()}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Actividad que no existe"},
    )
    assert r.status_code == 404, r.text


async def test_api_actualizar_actividad_descripcion_muy_corta_422(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """Validation boundary: descripcion below min_length=10 (ActividadUpdate) 422s."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad válida inicial")
    )

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "corta"},  # 5 chars < min_length=10
    )
    assert r.status_code == 422, r.text


async def test_api_actualizar_actividad_justificacion_muy_larga_422(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """Validation boundary: justificacion above max_length=5000 (ActividadUpdate) 422s."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad válida inicial")
    )

    r = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "x" * 5001},
    )
    assert r.status_code == 422, r.text


async def test_api_actualizar_actividad_persiste_en_re_get(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta_cobro: CuentaCobro,
) -> None:
    """Persistence: the PATCH write must actually commit — re-GET the
    actividad via the list endpoint and confirm the new value is there,
    not just trust the PATCH response body."""
    act = await actividad_service.crear_actividad(
        db, test_user["user"].id, cuenta_cobro.id, ActividadCreate(descripcion="Actividad antes de actualizar")
    )

    patch_resp = await client.patch(
        f"/api/v1/actividades/{act.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"justificacion": "Justificación que debe persistir en la base de datos"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    get_resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert get_resp.status_code == 200, get_resp.text
    persisted = next(a for a in get_resp.json() if a["id"] == str(act.id))
    assert persisted["justificacion"] == "Justificación que debe persistir en la base de datos"
