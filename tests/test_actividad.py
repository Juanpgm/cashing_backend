"""Tests for actividad service and API."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.core.security import hash_password
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.usuario import Usuario
from app.schemas.actividad import ActividadCreate, ActividadResponse, ActividadUpdate
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


# ── EvidenciaResponse shape bug (storage_key/tipo_archivo/tamano_bytes required, ──
# ── no fuente/url) — GET/PATCH actividades 500 or drop data on link-evidencia ────
#
# Note on the "Network/concurrent" edge-case category: this endpoint family is a
# pure DB read/write-then-read path with no outbound HTTP/email/drive/storage call
# on the request path (link-evidencia rows are pre-seeded directly via the ORM in
# these tests, mirroring how `evidence_persist_service` writes them out-of-band).
# There is nothing to time out or retry here, so no network/concurrency test is
# included for this bug — see `test_evidence_persist.py` for coverage of the actual
# network-calling discovery/persist path.


@pytest.fixture
async def actividad(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=cuenta_cobro.id,
        descripcion="Actividad con evidencias de prueba",
        fecha_realizacion=date(2024, 3, 12),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
async def evidencia_archivo(db: AsyncSession, actividad: Actividad) -> Evidencia:
    """A stored-file evidencia — storage_key/tipo_archivo/tamano_bytes set, no fuente/url."""
    e = Evidencia(
        actividad_id=actividad.id,
        storage_key=f"evidencias/{actividad.id}/informe.pdf",
        nombre_archivo="informe.pdf",
        tipo_archivo="application/pdf",
        tamano_bytes=2048,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


@pytest.fixture
async def evidencia_enlace(db: AsyncSession, actividad: Actividad) -> Evidencia:
    """A link evidencia (Gmail/Drive/Calendar discovery shape) — storage_key/tipo_archivo/
    tamano_bytes all NULL, fuente/url set. This is the shape that 500s / drops data today."""
    e = Evidencia(
        actividad_id=actividad.id,
        storage_key=None,
        nombre_archivo="Correo: Informe de avance marzo",
        tipo_archivo=None,
        tamano_bytes=None,
        fuente="email",
        url="https://mail.google.com/mail/u/0/#inbox/18d3f0a1b2c3d4e5",
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


@pytest.fixture
async def otro_usuario_cuenta_cobro(db: AsyncSession) -> CuentaCobro:
    """A cuenta de cobro belonging to a DIFFERENT user (not `test_user`) — for
    cross-tenant ownership checks on the actividades endpoints."""
    otro_user = Usuario(
        email="otro-usuario-act@example.com",
        nombre="Otro Usuario",
        cedula="192837465",
        telefono="+573001112233",
        password_hash=hash_password("OtherPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro_user)
    await db.commit()
    await db.refresh(otro_user)

    otro_contrato = Contrato(
        usuario_id=otro_user.id,
        numero_contrato="CTR-ACT-AJENO-001",
        objeto="Servicios de consultoría ajenos",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Otro Supervisor",
    )
    db.add(otro_contrato)
    await db.commit()
    await db.refresh(otro_contrato)

    otro_cc = CuentaCobro(
        contrato_id=otro_contrato.id,
        mes=4,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(otro_cc)
    await db.commit()
    await db.refresh(otro_cc)
    return otro_cc


@pytest.fixture
async def actividad_ajena(db: AsyncSession, otro_usuario_cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=otro_usuario_cuenta_cobro.id,
        descripcion="Actividad de otro usuario, no visible",
        fecha_realizacion=date(2024, 4, 5),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


# ── Boundary: zero / only-link / only-file / mixed evidencias ───────────────────


async def test_actividad_response_sin_evidencias(actividad: Actividad) -> None:
    """Zero evidencias must validate cleanly to an empty list."""
    resp = ActividadResponse.model_validate(actividad)
    assert resp.evidencias == []


async def test_actividad_response_solo_evidencia_archivo(
    actividad: Actividad, evidencia_archivo: Evidencia, db: AsyncSession
) -> None:
    await db.refresh(actividad, attribute_names=["evidencias"])
    resp = ActividadResponse.model_validate(actividad)
    assert len(resp.evidencias) == 1
    ev = resp.evidencias[0]
    assert ev.storage_key == evidencia_archivo.storage_key
    assert ev.tipo_archivo == "application/pdf"
    assert ev.tamano_bytes == 2048
    assert ev.fuente is None
    assert ev.url is None


async def test_actividad_response_solo_evidencia_enlace(
    actividad: Actividad, evidencia_enlace: Evidencia, db: AsyncSession
) -> None:
    """RED: today `EvidenciaResponse` requires storage_key/tipo_archivo/tamano_bytes as
    non-nullable str/str/int and has no fuente/url field at all — validating a
    link-evidencia ORM row against it raises a pydantic ValidationError (which the
    endpoint surfaces as an unhandled 500), and even if it were made nullable without
    adding fuente/url, the caller-visible fuente/url data would still be silently
    dropped. This test asserts the FULL correct shape once fixed."""
    await db.refresh(actividad, attribute_names=["evidencias"])
    resp = ActividadResponse.model_validate(actividad)
    assert len(resp.evidencias) == 1
    ev = resp.evidencias[0]
    assert ev.storage_key is None
    assert ev.tipo_archivo is None
    assert ev.tamano_bytes is None
    assert ev.fuente == "email"
    assert ev.url == "https://mail.google.com/mail/u/0/#inbox/18d3f0a1b2c3d4e5"


async def test_actividad_response_evidencias_mixtas(
    actividad: Actividad,
    evidencia_archivo: Evidencia,
    evidencia_enlace: Evidencia,
    db: AsyncSession,
) -> None:
    await db.refresh(actividad, attribute_names=["evidencias"])
    resp = ActividadResponse.model_validate(actividad)
    assert len(resp.evidencias) == 2
    by_id = {ev.id: ev for ev in resp.evidencias}
    archivo = by_id[evidencia_archivo.id]
    enlace = by_id[evidencia_enlace.id]
    assert archivo.storage_key is not None and archivo.fuente is None
    assert enlace.storage_key is None and enlace.fuente == "email" and enlace.url is not None


# ── Same bug via the real HTTP endpoint (not just the schema in isolation) ──────


async def test_api_get_actividades_con_evidencia_enlace_no_500(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    actividad: Actividad,
    evidencia_enlace: Evidencia,
) -> None:
    """The GET list endpoint must not 500 on link-evidencia and must return fuente/url
    in the actual response body (not just a 200 status)."""
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    encontrada = next(a for a in body if a["id"] == str(actividad.id))
    assert len(encontrada["evidencias"]) == 1
    ev = encontrada["evidencias"][0]
    assert ev["storage_key"] is None
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None
    assert ev["fuente"] == "email"
    assert ev["url"] == "https://mail.google.com/mail/u/0/#inbox/18d3f0a1b2c3d4e5"


# ── State-machine: PATCH rejected on a non-editable cuenta de cobro ──────────────


async def test_api_actualizar_actividad_estado_no_editable_rechaza(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    actividad: Actividad,
) -> None:
    cuenta_cobro.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    resp = await client.patch(
        f"/api/v1/actividades/{actividad.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "Intento de edición bloqueado por estado"},
    )
    assert resp.status_code == 422


# ── Auth/ownership: cuenta/actividad belonging to another user → 404 ────────────


async def test_api_get_actividades_de_otro_usuario_404(
    client: AsyncClient, test_user: dict[str, Any], otro_usuario_cuenta_cobro: CuentaCobro
) -> None:
    resp = await client.get(
        f"/api/v1/cuentas-cobro/{otro_usuario_cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert resp.status_code == 404


async def test_api_patch_actividad_de_otro_usuario_404(
    client: AsyncClient,
    test_user: dict[str, Any],
    otro_usuario_cuenta_cobro: CuentaCobro,
    actividad_ajena: Actividad,
) -> None:
    resp = await client.patch(
        f"/api/v1/actividades/{actividad_ajena.id}",
        params={"cuenta_cobro_id": str(otro_usuario_cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "No debería poder editar esto"},
    )
    assert resp.status_code == 404


# ── Not-found: PATCH with a non-existent actividad_id ────────────────────────────


async def test_api_patch_actividad_inexistente_404(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    resp = await client.patch(
        f"/api/v1/actividades/{uuid.uuid4()}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "Actividad que no existe"},
    )
    assert resp.status_code == 404


# ── Validation boundary: ActividadUpdate.descripcion min_length=10 / max_length=2000 ──


async def test_api_patch_actividad_descripcion_muy_corta_rechazada(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, actividad: Actividad
) -> None:
    resp = await client.patch(
        f"/api/v1/actividades/{actividad.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "corta"},  # 5 chars, below min_length=10
    )
    assert resp.status_code == 422


async def test_api_patch_actividad_descripcion_muy_larga_rechazada(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, actividad: Actividad
) -> None:
    resp = await client.patch(
        f"/api/v1/actividades/{actividad.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": "a" * 2001},  # 1 char over max_length=2000
    )
    assert resp.status_code == 422


# ── Persistence: PATCH then re-GET to confirm the write actually committed ──────


async def test_api_patch_actividad_persiste_tras_re_get(
    client: AsyncClient, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, actividad: Actividad
) -> None:
    nueva_descripcion = "Descripción actualizada y verificada tras un nuevo GET"
    patch_resp = await client.patch(
        f"/api/v1/actividades/{actividad.id}",
        params={"cuenta_cobro_id": str(cuenta_cobro.id)},
        headers=test_user["headers"],
        json={"descripcion": nueva_descripcion},
    )
    assert patch_resp.status_code == 200

    get_resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/actividades",
        headers=test_user["headers"],
    )
    assert get_resp.status_code == 200
    encontrada = next(a for a in get_resp.json() if a["id"] == str(actividad.id))
    assert encontrada["descripcion"] == nueva_descripcion
