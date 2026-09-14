"""Empirical proof (radicacion-sin-friccion, Phase 4, slice 4.2, point 4):
`_ejecutar_clasificacion`'s background run uses the SAME request-scoped `db`
session (`AsyncSession` from `Depends(get_db)`) that
`clasificar_evidencias_cuenta` received — is that actually safe, or does it
race the session's teardown the way slice 3.10a's `asyncio.Task`-inside-an-
SSE-generator bug did (`app/services/agent_chat_service.py`,
`test_agent_chat_stream_service.py`)?

TRUST ONLY WHAT THIS TEST PROVES: FastAPI's native `BackgroundTasks` and a
hand-rolled `asyncio.Task` have DIFFERENT lifecycle guarantees.
`clasificar_evidencias_cuenta` uses `background_tasks: BackgroundTasks`
(`Depends`-injected, scheduled via `background_tasks.add_task`), not a raw
`asyncio.create_task`. Per FastAPI's own documented order of events for a
`yield`-based dependency combined with `BackgroundTasks` (fastapi.tiangolo.com
/tutorial/dependencies/dependencies-with-yield/#dependencies-with-yield-and-
except, "Background Tasks" section): code after `yield` in a dependency runs
AFTER the response has been sent, "including background tasks" — i.e.
`get_db`'s post-`yield` `session.commit()` (and the `async with
async_session_factory()`'s connection close) only happens once every
scheduled `BackgroundTasks` entry has ALREADY finished running. This is a
DIFFERENT mechanism from slice 3.10a's bug: that code ran a raw
`asyncio.Task` OUTSIDE any dependency's exit-stack tracking, so a real client
disconnect could tear the session down while the task was still in flight.

This test calls the REAL `POST .../evidencias/clasificar` endpoint through the
app's actual `get_db` dependency injection (`httpx.AsyncClient` over
`ASGITransport`, in-process — no client disconnect simulated, matching the
normal-completion case this point is about) and asserts the classification job
reaches `done` and produces real links — proving `_ejecutar_clasificacion` ran
to completion against a still-open, usable session, not a closed one. Per the
project's pinned versions (`fastapi==0.138.2`, `starlette==1.3.1`) — see
`pyproject.toml`/`requirements.txt`.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob, EstadoClasificacionJob
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CLAS-BGSESS-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(contrato_id=contrato.id, mes=3, anio=2024, estado=EstadoCuentaCobro.BORRADOR, valor=1)
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales de consultoria y asesoria",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


@pytest.fixture
async def actividad_stub(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(cuenta_cobro_id=cuenta_cobro.id, descripcion="Evidencias sin clasificar")
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _crear_evidencia(db: AsyncSession, actividad: Actividad, nombre: str, texto: str) -> Evidencia:
    ev = Evidencia(
        actividad_id=actividad.id,
        storage_key=f"evidencias/test/{nombre}",
        nombre_archivo=nombre,
        tipo_archivo="text/plain",
        tamano_bytes=len(texto),
        texto_extraido=texto,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


def _fake_llm(complete_content: str = "[1]") -> Any:
    llm = AsyncMock()
    llm.embed = AsyncMock(side_effect=RuntimeError("no network in tests"))
    llm.complete = AsyncMock(return_value=MagicMock(content=complete_content))
    return llm


async def test_background_classification_runs_to_completion_via_real_endpoint(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    obligacion: Obligacion,
    actividad_stub: Actividad,
) -> None:
    """The classification job reaches `done` (not stuck `pending`, not a 500
    from a closed session) after a SINGLE real HTTP request completes — proof
    that `_ejecutar_clasificacion` ran to completion against a still-open,
    usable `db` session under FastAPI's real `BackgroundTasks` + `Depends(get_db)`
    interaction (empirical, not assumed)."""
    ev = await _crear_evidencia(db, actividad_stub, "informe.txt", "Informe tecnico mensual de consultoria y asesoria")

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fake_llm("[1]")):
        resp = await client.post(
            f"/api/v1/cuentas-cobro/{cuenta_cobro.id}/evidencias/clasificar", headers=test_user["headers"]
        )
    assert resp.status_code == 202, resp.text

    # By the time `client.post()` returns, FastAPI's BackgroundTasks have
    # already run to completion — they execute inside the awaited
    # request/response cycle, BEFORE `Depends(get_db)`'s post-`yield` code
    # (which commits and closes the session) runs. If the background task
    # instead raced a closed session (the 3.10a bug's failure mode), the job
    # would be stuck at `pending` (the RUNNING/commit inside
    # `_ejecutar_clasificacion` would raise on a dead session and never reach
    # `done`), or the endpoint call itself would have surfaced a 500.
    job = (
        await db.execute(
            select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_cobro.id)
        )
    ).scalar_one()
    assert job.status == EstadoClasificacionJob.DONE.value
    assert job.procesadas == 1

    links = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == ev.id))).scalars().all()
    )
    assert len(links) >= 1  # real work happened — not a smoke-test-only 202
