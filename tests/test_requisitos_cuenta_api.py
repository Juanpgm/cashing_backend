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
