"""Background evidence classification job (evidence-classification-jobs
capability): one active job per cuenta, background run classifies unlinked
evidencias, forced per-file failure surfaces as a retryable state while
evidence rows and successfully-classified siblings persist."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.config import settings
from app.models.actividad import Actividad
from app.models.clasificacion_job import ClasificacionEvidenciasJob, EstadoClasificacionJob
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import evidence_classification_service, evidencia_service
from fastapi import BackgroundTasks
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-JOB-001",
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
    """The shared "sin clasificar" stub every upload attaches to before the
    link-table job assigns real obligación links."""
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


# ── 5.3 / one active job per cuenta ──────────────────────────────────────────


class TestEncolarClasificacion:
    async def test_encolar_upserts_one_job_row_per_cuenta(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
        bg = BackgroundTasks()

        job1 = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
        job2 = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

        assert job1.id == job2.id
        rows = (
            (
                await db.execute(
                    select(ClasificacionEvidenciasJob).where(
                        ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_cobro.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == EstadoClasificacionJob.PENDING.value
        assert rows[0].total == 1


# ── Background run classifies unlinked evidencias (evidence-classification-
# jobs: Background classification execution) ─────────────────────────────────


class TestEjecutarClasificacion:
    async def test_background_run_classifies_unlinked_evidencias_and_marks_job_done(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        ev = await _crear_evidencia(
            db, actividad_stub, "informe.txt", "Informe tecnico mensual de consultoria y asesoria"
        )

        with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fake_llm("[1]")):
            bg = BackgroundTasks()
            await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
            await evidence_classification_service._ejecutar_clasificacion(db, cuenta_cobro.id)

        job = (
            await db.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_cobro.id)
            )
        ).scalar_one()
        assert job.status == EstadoClasificacionJob.DONE.value
        assert job.procesadas == 1

        links = (
            (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == ev.id)))
            .scalars()
            .all()
        )
        assert len(links) >= 1

    async def test_retry_reprocesses_only_still_unlinked_evidencias(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        """A second run must NOT re-touch an evidencia that already has a
        confirmed link — only genuinely unresolved evidencias are candidates
        (idempotent, retry-safe via link upsert)."""
        ev_ya_clasificada = await _crear_evidencia(db, actividad_stub, "ya.txt", "contenido ya clasificado")
        db.add(EvidenciaObligacion(evidencia_id=ev_ya_clasificada.id, obligacion_id=obligacion.id, confianza="alta"))
        await db.commit()

        ev_pendiente = await _crear_evidencia(
            db, actividad_stub, "pendiente.txt", "Informe tecnico mensual de consultoria y asesoria"
        )

        with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fake_llm("[1]")):
            bg = BackgroundTasks()
            job = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
            assert job.total == 1  # only the still-unlinked evidencia counts
            await evidence_classification_service._ejecutar_clasificacion(db, cuenta_cobro.id)

        pendiente_links = (
            (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == ev_pendiente.id)))
            .scalars()
            .all()
        )
        assert len(pendiente_links) >= 1


# ── 5.5 Retryable failure state + evidence persistence independent of outcome ─


class TestFalloRetryable:
    async def test_forced_failure_on_one_evidencia_marks_job_failed_but_sibling_still_classifies(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        ev_ok = await _crear_evidencia(db, actividad_stub, "ok.txt", "Informe tecnico mensual de consultoria")
        ev_bad = await _crear_evidencia(db, actividad_stub, "bad.txt", "Informe tecnico mensual de asesoria")
        # Capture plain UUIDs up front — `_ejecutar_clasificacion`'s rollback on
        # the forced failure expires every ORM object in this shared session
        # (SQLAlchemy always expires on rollback), so any later attribute
        # access on these fixture objects would need a sync lazy-load outside
        # an awaited context. Plain UUIDs sidestep that entirely.
        cuenta_id, ev_ok_id, ev_bad_id, actividad_id = (
            cuenta_cobro.id,
            ev_ok.id,
            ev_bad.id,
            actividad_stub.id,
        )

        real_guardar = evidencia_service.guardar_enlaces_evidencia

        async def _guardar_side_effect(db_, evidencia_id, sugerencias):  # type: ignore[no-untyped-def]
            if evidencia_id == ev_bad_id:
                raise RuntimeError("forced failure")
            return await real_guardar(db_, evidencia_id, sugerencias)

        with (
            patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fake_llm("[1,2]")),
            patch("app.services.evidencia_service.guardar_enlaces_evidencia", side_effect=_guardar_side_effect),
        ):
            bg = BackgroundTasks()
            await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_id)
            await evidence_classification_service._ejecutar_clasificacion(db, cuenta_id)

        job = (
            await db.execute(
                select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
            )
        ).scalar_one()
        assert job.status == EstadoClasificacionJob.FAILED.value
        assert job.error

        ok_links = (
            (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == ev_ok_id)))
            .scalars()
            .all()
        )
        assert len(ok_links) >= 1  # the sibling classified normally, unaffected by ev_bad's failure

        bad_links = (
            (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == ev_bad_id)))
            .scalars()
            .all()
        )
        assert bad_links == []

        # Evidencia rows persist regardless of classification outcome.
        ev_rows = (await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividad_id))).scalars().all()
        assert {e.id for e in ev_rows} == {ev_ok_id, ev_bad_id}


# ── RES-002: stale `running` job recovery + idempotent fresh no-op ──────────


class TestEncolarClasificacionJobRunning:
    async def test_stale_running_job_is_reset_and_reenqueued(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
        job = ClasificacionEvidenciasJob(
            cuenta_cobro_id=cuenta_cobro.id, status=EstadoClasificacionJob.RUNNING.value, total=5, procesadas=2
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        # Bypass the `onupdate=func.now()` trigger (only fires on ORM-tracked
        # UPDATE) with a Core `update()` statement, to simulate a job that
        # crashed/restarted long ago without ever updating again.
        stale_time = datetime.now(UTC) - timedelta(seconds=settings.EVIDENCE_JOB_STALE_SECONDS + 30)
        await db.execute(
            update(ClasificacionEvidenciasJob)
            .where(ClasificacionEvidenciasJob.id == job.id)
            .values(updated_at=stale_time)
        )
        await db.commit()
        await db.refresh(job)

        bg = BackgroundTasks()
        result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

        assert len(bg.tasks) == 1  # re-enqueued
        assert result.status == EstadoClasificacionJob.PENDING.value
        assert result.total == 1
        assert result.procesadas == 0
        assert result.error is None

    async def test_fresh_running_job_is_idempotent_no_op(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")
        job = ClasificacionEvidenciasJob(
            cuenta_cobro_id=cuenta_cobro.id, status=EstadoClasificacionJob.RUNNING.value, total=5, procesadas=2
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        bg = BackgroundTasks()
        result = await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)

        assert len(bg.tasks) == 0  # NOT re-enqueued — avoids a duplicate concurrent run
        assert result.status == EstadoClasificacionJob.RUNNING.value
        assert result.total == 5  # unchanged — current state returned, not reset
        assert result.procesadas == 2


# ── RES-002 (folds in progress signal): job.procesadas increments per file ──


class TestProgresoIncremental:
    async def test_procesadas_increments_per_file_processed(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, actividad_stub: Actividad
    ) -> None:
        """`job.procesadas` must advance as each evidencia finishes, not only
        once at the very end — the staleness/progress signal that lets a
        caller distinguish "slow" from "stuck" (RES-002/RES-004/REL-002)."""
        ev1 = await _crear_evidencia(db, actividad_stub, "sin_texto1.txt", "")
        ev2 = await _crear_evidencia(db, actividad_stub, "sin_texto2.txt", "")
        job = ClasificacionEvidenciasJob(cuenta_cobro_id=cuenta_cobro.id, total=2, procesadas=0)
        db.add(job)
        await db.commit()
        await db.refresh(job)

        seen_before_each_increment: list[int] = []
        real_marcar = evidencia_service.marcar_evidencia_sin_obligacion

        async def _marcar_side_effect(db_: AsyncSession, evidencia_id: Any, reasoning: str) -> Any:
            seen_before_each_increment.append(job.procesadas)
            return await real_marcar(db_, evidencia_id, reasoning)

        with patch("app.services.evidencia_service.marcar_evidencia_sin_obligacion", side_effect=_marcar_side_effect):
            await evidencia_service._clasificar_y_enlazar_lote(db, [], [ev1, ev2], job=job)

        assert job.procesadas == 2
        assert seen_before_each_increment == [0, 1]


# ── RES-001: unguarded failure-recovery handler ──────────────────────────────


class TestRecuperacionDeFalloNoLevantaExcepcion:
    async def test_recovery_failure_is_caught_logged_and_never_raised(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        """If the batch raises AND the rollback()/refresh() recovery sequence
        itself raises (dead session/connection), `_ejecutar_clasificacion`
        must swallow it — never leave the job stuck `running` by propagating
        an unhandled exception out of the background task — and emit a
        structured `clasificacion_job_recovery_failed` log instead."""
        await _crear_evidencia(db, actividad_stub, "a.txt", "Informe tecnico mensual de consultoria")

        async def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("batch failure")

        with (
            patch("app.services.evidencia_service._clasificar_y_enlazar_lote", side_effect=_boom),
            patch.object(db, "rollback", side_effect=RuntimeError("rollback also broken")),
            patch("app.services.evidence_classification_service.logger") as mock_logger,
        ):
            bg = BackgroundTasks()
            await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
            # Must NOT raise even though both the batch AND the recovery path fail.
            await evidence_classification_service._ejecutar_clasificacion(db, cuenta_cobro.id)

        mock_logger.error.assert_called_once()
        args, kwargs = mock_logger.error.call_args
        assert args[0] == "clasificacion_job_recovery_failed"
        assert kwargs["cuenta_cobro_id"] == str(cuenta_cobro.id)
        assert "rollback also broken" in kwargs["error"]

    async def test_happy_path_emits_structured_done_log(
        self, db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion: Obligacion, actividad_stub: Actividad
    ) -> None:
        await _crear_evidencia(db, actividad_stub, "informe.txt", "Informe tecnico mensual de consultoria")

        with (
            patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fake_llm("[1]")),
            patch("app.services.evidence_classification_service.logger") as mock_logger,
        ):
            bg = BackgroundTasks()
            await evidence_classification_service.encolar_clasificacion(db, bg, cuenta_cobro.id)
            await evidence_classification_service._ejecutar_clasificacion(db, cuenta_cobro.id)

        mock_logger.info.assert_any_call(
            "clasificacion_job_done", cuenta_cobro_id=str(cuenta_cobro.id), total=1, procesadas=1
        )


# ── obtener_estado_clasificacion — status contract shape ────────────────────


class TestObtenerEstadoClasificacion:
    async def test_estado_reporta_pendiente_clasificado_y_failed_retryable(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        obligacion: Obligacion,
        actividad_stub: Actividad,
    ) -> None:
        user = test_user["user"]
        ev_clasificada = await _crear_evidencia(db, actividad_stub, "clasificada.txt", "texto")
        db.add(EvidenciaObligacion(evidencia_id=ev_clasificada.id, obligacion_id=obligacion.id, confianza="alta"))
        ev_sin_clasificar = await _crear_evidencia(db, actividad_stub, "pendiente.txt", "texto")
        job = ClasificacionEvidenciasJob(
            cuenta_cobro_id=cuenta_cobro.id,
            status=EstadoClasificacionJob.FAILED.value,
            total=2,
            procesadas=1,
            error="forced failure",
        )
        db.add(job)
        await db.commit()

        estado = await evidence_classification_service.obtener_estado_clasificacion(db, user.id, cuenta_cobro.id)

        assert estado.status == EstadoClasificacionJob.FAILED.value
        assert estado.total == 2
        assert estado.procesadas == 1
        assert estado.error == "forced failure"

        por_id = {item.id: item for item in estado.evidencias}
        assert por_id[ev_clasificada.id].estado == "clasificado"
        assert por_id[ev_clasificada.id].obligaciones[0].obligacion_id == obligacion.id
        assert por_id[ev_sin_clasificar.id].estado == "failed_retryable"
        assert por_id[ev_sin_clasificar.id].obligaciones == []
