"""Tests for the package listing + regenerate endpoints (radicacion-stepper,
work unit B5): `GET /cuentas-cobro/{id}/paquete` (read-only, storage-prefix
read + `obtener_estado_listo_pendiente`, no packaging) and
`POST /cuentas-cobro/{id}/paquete/regenerar` (thin delegate to
`radicacion_prep_service.preparar_radicacion` — never the lighter
`generar_zip_evidencias`, never re-implements its gates).

Mirrors the fixture/mocking conventions of `tests/test_radicacion_prep.py`.
Uses a stateful `_FakeStorage` (not a plain `AsyncMock`) because the
deterministic-key overwrite guard test needs real key tracking across two
sequential `preparar_radicacion` runs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.adapters.storage.port import StorageObjectInfo
from app.core.exceptions import ValidationError
from app.core.security import create_access_token, hash_password
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.services import informe_service, radicacion_prep_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CODIGOS_OBLIGATORIOS = [
    "CONTRATO",
    "RPC",
    "SEGURIDAD_SOCIAL",
    "INFORME_ACTIVIDADES",
    "INFORME_SUPERVISION",
    "EVIDENCIAS",
    "CEDULA",
    "RUT",
    "ACTA_INICIO",
]


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PAQUETE-EP-001",
        objeto="Servicios profesionales para pruebas de paquete endpoints",
        valor_total=24_000_000,
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


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # See test_stepper_state.py's identical comment: `contrato.obligaciones` was
    # already eagerly cached (empty) by the `contrato` fixture's own `db.refresh`
    # before this row existed — force a real reload so it reflects current state.
    await db.refresh(contrato)
    return ob


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: Any) -> None:
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text
    for codigo in _CODIGOS_OBLIGATORIOS:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


class _FakeStorage:
    """Stateful in-memory `StoragePort` fake — a plain `AsyncMock` can't assert
    the deterministic-key overwrite guarantee (it doesn't track real state
    across two sequential `preparar_radicacion` runs)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._objects[key] = data
        return key

    async def download(self, key: str) -> bytes:
        return self._objects[key]

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"fake://{key}"

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def list_objects(self, prefix: str) -> list[StorageObjectInfo]:
        return [StorageObjectInfo(key=k, size_bytes=len(v)) for k, v in self._objects.items() if k.startswith(prefix)]


@pytest.fixture
async def cuenta_lista(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
) -> CuentaCobro:
    """A cuenta whose checklist is fully complete and which has at least one
    evidencia on its only obligación (so LISTO/PENDIENTE reports LISTO)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion="Justificación",
        fecha_realizacion=date(2024, 1, 10),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)

    ev = Evidencia(
        actividad_id=act.id,
        storage_key=f"evidencias/{act.id}/foto.jpg",
        nombre_archivo="foto.jpg",
        tipo_archivo="image/jpeg",
        tamano_bytes=12,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    act.evidencias = [ev]
    await db.refresh(cuenta)
    return cuenta


# ── B5.1 — GET /paquete ──────────────────────────────────────────────────


async def test_get_paquete_returns_existe_false_when_no_package_yet(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr("app.services.paquete_service._get_storage", lambda *_a, **_k: storage)

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["existe"] is False
    assert body["cuenta_cobro_id"] == str(cuenta_lista.id)


async def test_get_paquete_returns_metadata_when_package_exists(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.paquete_service._get_storage", lambda *_a, **_k: storage)

    # Generate the real package first, through the same gate regenerate uses.
    resultado = await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta_lista.id)

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["existe"] is True
    assert body["filename"] == resultado.filename
    assert body["size_bytes"] == resultado.size_bytes
    assert body["listo_para_radicar"] is True
    assert body["pendientes"] == 0
    assert len(body["obligaciones"]) == 1


async def test_get_paquete_produces_no_storage_writes_and_no_packaging_call(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage()
    upload_spy = AsyncMock(wraps=storage.upload)
    storage.upload = upload_spy  # type: ignore[method-assign]
    monkeypatch.setattr("app.services.paquete_service._get_storage", lambda *_a, **_k: storage)

    packager_spy = AsyncMock(side_effect=AssertionError("GET /paquete must never call the packager"))
    monkeypatch.setattr(informe_service, "generar_zip_evidencias", packager_spy)

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    upload_spy.assert_not_awaited()
    packager_spy.assert_not_awaited()


# ── B5.2 — POST /paquete/regenerar ───────────────────────────────────────


async def test_post_regenerar_returns_prepara_radicacion_response(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar", headers=test_user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cuenta_cobro_id"] == str(cuenta_lista.id)
    assert body["storage_key"].startswith("paquetes/")
    assert body["filename"].endswith(".zip")
    assert body["listo_para_radicar"] is True
    assert body["pendientes"] == 0
    assert body["es_borrador"] is True


async def test_post_regenerar_overwrites_same_key_exactly_one_object_under_prefix(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic-key guard (design risk): re-running regenerate against the
    same cuota must overwrite the same storage key, never accumulate a second
    object under the cuenta's package prefix."""
    storage = _FakeStorage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    r1 = await client.post(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar", headers=test_user["headers"])
    assert r1.status_code == 200, r1.text
    r2 = await client.post(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar", headers=test_user["headers"])
    assert r2.status_code == 200, r2.text
    assert r1.json()["storage_key"] == r2.json()["storage_key"]

    prefix = f"paquetes/{test_user['user'].id}/{cuenta_lista.id}/"
    objetos = await storage.list_objects(prefix)
    assert len(objetos) == 1


async def test_post_regenerar_raises_coherence_check_failed_through_the_gate(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed gates still raise through regenerate — it does NOT bypass
    `preparar_radicacion`'s gates."""
    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    texto = "Planilla 555666777 pagada por el contratista, sin novedad."
    for cuenta in (cuenta1, cuenta2):
        doc = DocumentoFuente(
            usuario_id=contrato.usuario_id,
            contrato_id=contrato.id,
            cuenta_cobro_id=cuenta.id,
            storage_key=f"fake/ss-{cuenta.mes}.pdf",
            nombre=f"ss-{cuenta.mes}.pdf",
            tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
            texto_extraido=texto,
        )
        db.add(doc)
    await db.commit()

    await _completar_checklist(client, test_user["headers"], cuenta2.id)

    packager_spy = AsyncMock(side_effect=AssertionError("packager must not run when a HARD finding halts first"))
    monkeypatch.setattr(informe_service, "generar_zip_evidencias", packager_spy)

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta2.id}/paquete/regenerar", headers=test_user["headers"])

    assert r.status_code == 422, r.text
    assert r.json()["code"] == "COHERENCE_CHECK_FAILED"
    packager_spy.assert_not_awaited()


async def test_post_regenerar_raises_package_pendiente_through_the_gate(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    contrato: Contrato,
    obligacion: Obligacion,
) -> None:
    """No evidencia uploaded -> the packager's own `modo='final'` gate raises
    PACKAGE_PENDIENTE; regenerate does not swallow or re-implement it."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/paquete/regenerar", headers=test_user["headers"])

    assert r.status_code == 422, r.text
    assert r.json()["code"] == "PACKAGE_PENDIENTE"


async def test_post_regenerar_raises_secret_detected_through_the_gate(
    client: AsyncClient,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_secret(*_a: Any, **_k: Any) -> tuple[bytes, str]:
        raise ValidationError("secreto detectado", code="SECRET_DETECTED_IN_PACKAGE")

    monkeypatch.setattr(informe_service, "generar_zip_evidencias", _raise_secret)

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta_lista.id}/paquete/regenerar", headers=test_user["headers"])

    assert r.status_code == 422, r.text
    assert r.json()["code"] == "SECRET_DETECTED_IN_PACKAGE"


# ── B7 Fix 3 — cross-tenant negative tests ───────────────────────────────


async def _crear_otro_usuario_token(db: AsyncSession, *, email: str) -> str:
    otro = Usuario(
        email=email,
        nombre="Otro Usuario",
        cedula="987654321",
        password_hash=hash_password("OtroPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    return create_access_token(subject=str(otro.id), role=otro.rol)


async def test_get_paquete_cross_tenant_returns_403(client: AsyncClient, db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    token = await _crear_otro_usuario_token(db, email="otro-get-paquete@example.com")

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/paquete",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 403


async def test_post_regenerar_cross_tenant_returns_403(
    client: AsyncClient, db: AsyncSession, contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    token = await _crear_otro_usuario_token(db, email="otro-post-regenerar@example.com")

    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/paquete/regenerar",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 403


# ── B7 Fix 4 — soft-deleted Contrato must not leak through ────────────────


async def test_get_paquete_soft_deleted_contrato_returns_404(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    contrato.deleted_at = datetime.now(UTC)
    db.add(contrato)
    await db.commit()

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/paquete", headers=test_user["headers"])

    assert r.status_code == 404


async def test_post_regenerar_soft_deleted_contrato_returns_404(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    contrato.deleted_at = datetime.now(UTC)
    db.add(contrato)
    await db.commit()

    r = await client.post(f"/api/v1/cuentas-cobro/{cuenta.id}/paquete/regenerar", headers=test_user["headers"])

    assert r.status_code == 404
