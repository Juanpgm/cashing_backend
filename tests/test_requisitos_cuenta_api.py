"""Tests for the per-cuenta requisitos API (/cuentas-cobro/{id}/requisitos)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.requisito_cuenta import RequisitoCuenta
from app.schemas.agent import LLMResponse
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-REQ-API-001",
        objeto="Servicios requisitos API",
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


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    class _FakeLLM:
        async def complete(self, *a, **k) -> LLMResponse:
            return LLMResponse(content=content, model="fake", prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(), raising=True)


async def test_inferir_texto_no_escribe_db(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_llm(
        monkeypatch,
        '{"requisitos": [{"codigo": "RUP", "etiqueta": "Registro único de proponentes"}]}',
    )
    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos/inferir",
        headers=test_user["headers"],
        json={"texto": "El proponente debe aportar el RUP vigente."},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["requisitos"]) == 1

    # Nothing persisted, gate still unresolved.
    res = await db.execute(select(RequisitoCuenta).where(RequisitoCuenta.cuenta_cobro_id == cuenta.id))
    assert res.scalars().first() is None
    res2 = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta.id))
    assert res2.scalar_one().requisitos_modo is None


async def test_definir_persiste_set_y_modo(client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro) -> None:
    payload = {
        "modo": "augment",
        "requisitos": [
            {
                "codigo": "POLIZA_CUMPLIMIENTO",
                "etiqueta": "Póliza de cumplimiento",
                "obligatorio": True,
                "keywords_deteccion": ["poliza", "cumplimiento"],
            }
        ],
    }
    d = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos",
        headers=test_user["headers"],
        json=payload,
    )
    assert d.status_code == 200, d.text

    g = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos", headers=test_user["headers"])
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["modo"] == "augment"
    assert [r["codigo"] for r in body["requisitos"]] == ["POLIZA_CUMPLIMIENTO"]
    assert body["requisitos"][0]["id"] is not None


async def test_definir_reemplazar_drops_standard_keeps_evidencias(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    d = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos",
        headers=test_user["headers"],
        json={
            "modo": "reemplazar",
            "requisitos": [{"codigo": "RUP", "etiqueta": "RUP", "keywords_deteccion": ["rup"]}],
        },
    )
    assert d.status_code == 200, d.text

    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    codigos = {i["requisito"]["codigo"] for i in r.json()["items"]}
    assert "RUP" in codigos
    assert "EVIDENCIAS" in codigos
    assert "RPC" not in codigos  # standard dropped in reemplazar mode


async def test_definir_redefine_overwrites_previous_set(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    base_url = f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos"
    await client.post(
        base_url,
        headers=test_user["headers"],
        json={"modo": "augment", "requisitos": [{"codigo": "AAA", "etiqueta": "A"}]},
    )
    await client.post(
        base_url,
        headers=test_user["headers"],
        json={"modo": "augment", "requisitos": [{"codigo": "BBB", "etiqueta": "B"}]},
    )
    g = await client.get(base_url, headers=test_user["headers"])
    assert [r["codigo"] for r in g.json()["requisitos"]] == ["BBB"]


async def test_inferir_ownership_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fake = uuid.uuid4()
    r = await client.post(
        f"/api/v1/cuentas-cobro/{fake}/requisitos/inferir",
        headers=test_user["headers"],
        json={"texto": "algo"},
    )
    assert r.status_code == 404


# ── checklist/primera-cuota-2026-09-16, round 4, BLOCKER: `definir_set` is
# destructive-then-rebuild, but round 3's settled-cuenta guard turned the
# rebuild half into a no-op. On an ENVIADA/APROBADA/PAGADA cuenta the deletes
# still ran and nothing came back, permanently erasing mandatory requisitos
# from a radicated compliance record — and the truncated checklist then read
# `radicacion_lista=True`. The endpoint must refuse outright instead. ──


@pytest.mark.parametrize(
    "estado",
    [EstadoCuentaCobro.ENVIADA, EstadoCuentaCobro.APROBADA, EstadoCuentaCobro.PAGADA],
)
async def test_definir_set_rechaza_una_cuenta_cerrada_sin_tocar_el_checklist(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, estado: EstadoCuentaCobro
) -> None:
    from app.core.exceptions import CHECKLIST_CUENTA_CERRADA, ValidationError
    from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
    from app.schemas.requisito_cuenta import RequisitoCuentaItem
    from app.services import checklist_service, requisito_cuenta_service

    user = test_user["user"]
    await checklist_service.asegurar_checklist(db, cuenta)
    cuenta.requisitos_modo = "estandar"
    await db.commit()

    # The realistic settled shape: every requisito satisfied by hand except one
    # mandatory row the user unlinked ("I need to replace the contract PDF"),
    # which `PATCH /checklist/{codigo}` allows on a settled cuenta.
    filas = (
        (await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id)))
        .scalars()
        .all()
    )
    for fila in filas:
        if fila.requisito_codigo != "CONTRATO":
            fila.estado = EstadoRequisito.CUMPLIDO_MANUAL
    cuenta.estado = estado
    await db.commit()

    antes = await checklist_service.construir_checklist_completo(db, cuenta)
    codigos_antes = sorted(i["requisito"]["codigo"] for i in antes["items"])
    assert "CONTRATO" in codigos_antes
    assert antes["resumen"]["radicacion_lista"] is False

    with pytest.raises(ValidationError) as exc:
        await requisito_cuenta_service.definir_set(db, user.id, cuenta.id, "estandar", [])
    assert exc.value.code == CHECKLIST_CUENTA_CERRADA

    despues = await checklist_service.construir_checklist_completo(db, cuenta)
    assert sorted(i["requisito"]["codigo"] for i in despues["items"]) == codigos_antes
    assert despues["resumen"]["radicacion_lista"] is False

    # And the custom requisitos of a settled cuenta survive an attempted re-apply.
    db.add(RequisitoCuenta(cuenta_cobro_id=cuenta.id, codigo="POLIZA", etiqueta="Póliza", orden=500))
    await db.commit()
    with pytest.raises(ValidationError):
        await requisito_cuenta_service.definir_set(
            db, user.id, cuenta.id, "augment", [RequisitoCuentaItem(codigo="OTRO", etiqueta="Otro")]
        )
    vivos = (
        (await db.execute(select(RequisitoCuenta).where(RequisitoCuenta.cuenta_cobro_id == cuenta.id))).scalars().all()
    )
    assert [r.codigo for r in vivos] == ["POLIZA"]


async def test_definir_set_sigue_permitido_en_una_cuenta_abierta(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """Control for the guard above: BORRADOR (and RECHAZADA, which is
    deliberately absent from `_ESTADOS_CUENTA_CERRADA`) still redefine freely."""
    from app.schemas.requisito_cuenta import RequisitoCuentaItem
    from app.services import requisito_cuenta_service

    user = test_user["user"]
    for estado in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        cuenta.estado = estado
        await db.commit()
        resultado = await requisito_cuenta_service.definir_set(
            db, user.id, cuenta.id, "augment", [RequisitoCuentaItem(codigo="POLIZA", etiqueta="Póliza")]
        )
        assert [r.codigo for r in resultado.requisitos] == ["POLIZA"]


async def test_definir_endpoint_devuelve_422_en_una_cuenta_cerrada(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """The reachable half of the BLOCKER: the post-creation gate UI is offered on
    a radicated cuenta whenever `requisitos_modo` is NULL (neither `radicar_cuenta`
    nor `preparar_radicacion` sets it), so `POST /requisitos` is one click away.
    It must answer 422 + CHECKLIST_CUENTA_CERRADA, not 200."""
    from app.core.exceptions import CHECKLIST_CUENTA_CERRADA

    cuenta.estado = EstadoCuentaCobro.APROBADA
    await db.commit()

    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/requisitos",
        headers=test_user["headers"],
        json={"modo": "estandar", "requisitos": []},
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == CHECKLIST_CUENTA_CERRADA
