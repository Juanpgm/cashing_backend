"""Tests for evidencia service and API upload/download endpoints."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import evidencia_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PDF_MAGIC = b"%PDF-1.4 sample pdf content here"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-EVI-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="SENA",
        dependencia="Sistemas",
        supervisor_nombre="Pedro",
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


@pytest.fixture
async def actividad(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=cuenta_cobro.id,
        descripcion="Reunión de seguimiento",
        fecha_realizacion=date(2024, 3, 15),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key.pdf"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


# ── Service tests ──────────────────────────────────────────────────────────────


async def test_subir_evidencia(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    result = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="informe.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    assert result.nombre_archivo == "informe.pdf"
    assert result.tamano_bytes == len(_PDF_MAGIC)
    assert result.presigned_url == "https://s3.example.com/presigned"


async def test_subir_evidencia_extension_invalida(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    storage = _mock_storage()
    with pytest.raises(ValidationError):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            filename="virus.exe",
            content_type="application/octet-stream",
            data=b"MZ...",
        )


async def test_listar_evidencias(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    for i in range(2):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            filename=f"doc{i}.pdf",
            content_type="application/pdf",
            data=_PDF_MAGIC,
        )
    evidencias = await evidencia_service.listar_evidencias(db, user.id, actividad.id)
    assert len(evidencias) == 2


async def test_obtener_url_descarga(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="doc.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    result = await evidencia_service.obtener_url_descarga(db, storage, user.id, uploaded.id)
    assert result.presigned_url == "https://s3.example.com/presigned"
    assert result.nombre_archivo == "doc.pdf"


async def test_eliminar_evidencia(db: AsyncSession, test_user: dict[str, Any], actividad: Actividad) -> None:
    from app.core.exceptions import NotFoundError

    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="del.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )
    await evidencia_service.eliminar_evidencia(db, storage, user.id, uploaded.id)
    storage.delete.assert_called_once()

    with pytest.raises(NotFoundError):
        await evidencia_service.obtener_url_descarga(db, storage, user.id, uploaded.id)


# ── subir_evidencia / subir_evidencias create a CONFIRMED/user link when the
# target actividad has an obligacion_id (evidence-obligation-links: cobertura
# must count evidence manually attached to an obligación-linked actividad,
# same as evidence uploaded through any other path) ────────────────────────


@pytest.fixture
async def actividad_con_obligacion(
    db: AsyncSession, cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=cuenta_cobro.id,
        obligacion_id=obligacion_informes.id,
        descripcion="Entrega de informe técnico",
        fecha_realizacion=date(2024, 3, 15),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    # `cuenta_cobro.actividades` is `lazy="selectin"` — already eagerly loaded
    # (and cached empty) the moment the `cuenta_cobro` fixture was refreshed,
    # before this Actividad existed. Patch the in-memory collection directly
    # so cobertura_service's later query sees it (same pattern as
    # `obligacion_informes` patching `contrato.obligaciones` above).
    cuenta_cobro.actividades = [*cuenta_cobro.actividades, a]
    return a


async def test_subir_evidencia_crea_enlace_confirmado_cuando_actividad_tiene_obligacion(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    obligacion_informes: Obligacion,
    actividad_con_obligacion: Actividad,
) -> None:
    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion
    from app.schemas.cobertura import EstadoCobertura
    from app.services import cobertura_service

    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad_con_obligacion.id,
        filename="informe.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )

    result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == uploaded.id))
    link = result.scalars().one()
    assert link.obligacion_id == obligacion_informes.id
    assert link.status == EstadoEnlace.CONFIRMED.value
    assert link.source == "user"
    assert link.confianza is None

    # `Actividad.evidencias` is `lazy="selectin"` — already eagerly loaded
    # (and cached empty) by the fixture's own `db.refresh(a)` before this
    # Evidencia existed. Refresh it now so cobertura_service's later query
    # sees the newly uploaded evidencia (same expire_on_commit=False gotcha
    # patched via `contrato.obligaciones`/`cuenta_cobro.actividades` above).
    await db.refresh(actividad_con_obligacion)

    cobertura = await cobertura_service.calcular_cobertura(db, user.id, cuenta_cobro.id)
    item = next(i for i in cobertura.obligaciones if i.obligacion_id == obligacion_informes.id)
    assert item.num_evidencias == 1
    assert item.estado != EstadoCobertura.SIN_EVIDENCIA


async def test_subir_evidencia_sin_obligacion_no_crea_enlace(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.models.evidencia_obligacion import EvidenciaObligacion

    user = test_user["user"]
    storage = _mock_storage()
    uploaded = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        filename="informe.pdf",
        content_type="application/pdf",
        data=_PDF_MAGIC,
    )

    result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == uploaded.id))
    assert result.scalars().all() == []


async def test_subir_evidencias_crea_enlace_confirmado_cuando_actividad_tiene_obligacion(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_cobro: CuentaCobro,
    obligacion_informes: Obligacion,
    actividad_con_obligacion: Actividad,
) -> None:
    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion

    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad_con_obligacion.id,
        archivos=[
            ("informe1.pdf", "application/pdf", _PDF_MAGIC),
            ("informe2.pdf", "application/pdf", _PDF_MAGIC),
        ],
    )

    for resultado in resultados:
        result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == resultado.id))
        link = result.scalars().one()
        assert link.obligacion_id == obligacion_informes.id
        assert link.status == EstadoEnlace.CONFIRMED.value
        assert link.source == "user"
        assert link.confianza is None


async def test_subir_evidencias_sin_obligacion_no_crea_enlace(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.models.evidencia_obligacion import EvidenciaObligacion

    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        archivos=[("informe.pdf", "application/pdf", _PDF_MAGIC)],
    )

    result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == resultados[0].id))
    assert result.scalars().all() == []


async def test_evidencia_actividad_otro_usuario_falla(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.core.exceptions import NotFoundError

    # Use a random unknown UUID as the "other user"
    other_user_id = uuid.uuid4()
    storage = _mock_storage()
    with pytest.raises(NotFoundError):
        await evidencia_service.subir_evidencia(
            db=db,
            storage=storage,
            usuario_id=other_user_id,
            actividad_id=actividad.id,
            filename="spy.pdf",
            content_type="application/pdf",
            data=_PDF_MAGIC,
        )


# ── subir_evidencias_cuenta (cuenta-scoped upload, no pre-existing actividad) ──


@pytest.fixture
async def obligacion_informes(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes tecnicos mensuales de consultoria y asesoria especializada",
        tipo=TipoObligacion.ESPECIFICA,
        orden=1,
        etiqueta="OB1",
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # `contrato` was already loaded (and its lazy="selectin" `obligaciones` populated
    # as empty) by the `contrato` fixture before this Obligacion existed — set the
    # in-memory collection directly so later queries in this same session/test see it
    # (same pattern as tests/test_generar_actividades_agente.py).
    contrato.obligaciones = [ob]
    return ob


def _txt_file(text: str) -> bytes:
    return text.encode("utf-8")


def _fast_matcher_llm() -> Any:
    """Deterministic, network-free stand-in for `evidence_matcher_node`'s
    `get_llm()` — `subir_evidencias_cuenta` now ALSO runs the batched matcher
    (link-table classification) after the pre-existing per-file stub
    classification, so every test exercising it needs this to stay fast (no
    real embed()/complete() calls) per the "LLM/embeddings always mocked in
    tests" convention. `embed()` fails immediately (matcher fails open to
    keyword-only ranking); `complete()` rejects everything — irrelevant to
    these tests' assertions, which are about the OLD per-file stub path."""
    from unittest.mock import AsyncMock, MagicMock

    llm = AsyncMock()
    llm.embed = AsyncMock(side_effect=RuntimeError("no network in tests"))
    llm.complete = AsyncMock(return_value=MagicMock(content="[]"))
    return llm


async def test_subir_evidencias_cuenta_clasifica_por_keyword(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    from unittest.mock import patch

    user = test_user["user"]
    storage = _mock_storage()
    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fast_matcher_llm()):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                (
                    "informe.txt",
                    "text/plain",
                    _txt_file("Informe tecnico mensual de consultoria y asesoria especializada entregado."),
                )
            ],
        )
    assert len(resultados) == 1
    assert resultados[0].clasificado is True
    assert resultados[0].obligacion_id == obligacion_informes.id
    assert resultados[0].obligacion_etiqueta == "OB1"


async def test_subir_evidencias_cuenta_sin_match_comparte_un_stub(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    from unittest.mock import patch

    user = test_user["user"]
    storage = _mock_storage()
    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fast_matcher_llm()):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("foto1.txt", "text/plain", _txt_file("contenido totalmente ajeno sin relacion alguna")),
                ("foto2.txt", "text/plain", _txt_file("otro contenido igualmente ajeno y distinto")),
            ],
        )
    assert len(resultados) == 2
    assert all(r.clasificado is False and r.obligacion_id is None for r in resultados)
    # Both unclassified files must share the SAME stub Actividad, not one each.
    assert resultados[0].actividad_id == resultados[1].actividad_id

    count_result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_cobro.id))
    assert len(count_result.scalars().all()) == 1


async def test_subir_evidencias_cuenta_reutiliza_actividad_misma_obligacion(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    from unittest.mock import patch

    user = test_user["user"]
    storage = _mock_storage()
    texto = "Informe tecnico mensual de consultoria y asesoria especializada entregado."
    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fast_matcher_llm()):
        r1 = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("informe1.txt", "text/plain", _txt_file(texto))],
        )
        r2 = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("informe2.txt", "text/plain", _txt_file(texto))],
        )
    assert r1[0].actividad_id == r2[0].actividad_id

    count_result = await db.execute(
        select(Actividad).where(
            Actividad.cuenta_cobro_id == cuenta_cobro.id, Actividad.obligacion_id == obligacion_informes.id
        )
    )
    assert len(count_result.scalars().all()) == 1


async def test_subir_evidencias_cuenta_rechaza_lote_si_un_archivo_invalido(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ValidationError

    user = test_user["user"]
    storage = _mock_storage()
    with pytest.raises(ValidationError):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("ok.txt", "text/plain", _txt_file("contenido valido")),
                ("malware.exe", "application/octet-stream", b"MZ..."),
            ],
        )
    storage.upload.assert_not_called()
    result = await db.execute(select(Evidencia))
    assert result.scalars().all() == []
    result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_cobro.id))
    assert result.scalars().all() == []


# ── subir_evidencias_cuenta wires the batched matcher + link-table persistence ─
# (evidence-classification-pipeline: Every evidence resolves to a suggestion or
# explicit non-match)


async def test_subir_evidencias_cuenta_cada_evidencia_resuelve_a_sugerencia_o_no_aplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    """Every uploaded evidencia must end with >=1 confidence-bucketed
    EvidenciaObligacion link OR an explicit 'no obligación applies' link with
    reasoning — never silently unclassified in the link table."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion

    user = test_user["user"]
    storage = _mock_storage()

    matcher_llm = AsyncMock()
    matcher_llm.complete = AsyncMock(return_value=MagicMock(content="[1]"))

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=matcher_llm):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                (
                    "informe.txt",
                    "text/plain",
                    _txt_file("Informe tecnico mensual de consultoria y asesoria especializada entregado."),
                ),
                ("ajeno.txt", "text/plain", _txt_file("contenido totalmente ajeno sin relacion alguna")),
            ],
        )

    matched_id, no_match_id = resultados[0].id, resultados[1].id

    matched_links = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == matched_id)))
        .scalars()
        .all()
    )
    assert len(matched_links) >= 1
    assert matched_links[0].obligacion_id == obligacion_informes.id
    assert matched_links[0].confianza in {"alta", "media", "baja"}

    no_match_links = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == no_match_id)))
        .scalars()
        .all()
    )
    assert len(no_match_links) == 1
    assert no_match_links[0].status == EstadoEnlace.NO_APLICA.value
    assert no_match_links[0].obligacion_id is None
    assert (no_match_links[0].reasoning or "").strip() != ""


# ── Fast upload response — background classification (evidence-classification-
# jobs: Fast upload response) ────────────────────────────────────────────────


async def test_subir_evidencias_cuenta_con_background_tasks_no_clasifica_antes_de_retornar(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    """When a real `BackgroundTasks` is passed (the API route's path), the
    multi-obligación link-table classification is ENQUEUED, not run inline —
    zero `EvidenciaObligacion` rows exist until the queued task actually runs."""
    from unittest.mock import patch

    from app.models.evidencia_obligacion import EvidenciaObligacion
    from fastapi import BackgroundTasks
    from sqlalchemy import select as sa_select

    user = test_user["user"]
    storage = _mock_storage()
    bg = BackgroundTasks()

    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=_fast_matcher_llm()):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("informe.txt", "text/plain", _txt_file("Informe tecnico mensual de consultoria"))],
            background_tasks=bg,
        )

    # Response is ready (rows persisted), classification not run yet.
    assert len(bg.tasks) == 1
    links_antes = (await db.execute(sa_select(EvidenciaObligacion))).scalars().all()
    assert links_antes == []

    await bg()  # simulate Starlette running the queued background task

    links_despues = (
        (await db.execute(sa_select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == resultados[0].id)))
        .scalars()
        .all()
    )
    assert len(links_despues) >= 1


# ── RES-003: enqueue failure must not turn a successful upload into a 500 ──


async def test_subir_evidencias_cuenta_enqueue_falla_no_rompe_la_respuesta(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, obligacion_informes: Obligacion
) -> None:
    """If `encolar_clasificacion` raises AFTER files are already committed
    (e.g. a job-row unique-constraint race between two concurrent uploads),
    the upload must still succeed and return the per-file resultados — the
    user can trigger classification later via the retry button (RES-002)."""
    from unittest.mock import patch

    from app.models.evidencia import Evidencia
    from fastapi import BackgroundTasks

    user = test_user["user"]
    storage = _mock_storage()
    bg = BackgroundTasks()

    with (
        patch("app.services.evidencia_service.get_llm", return_value=_fast_matcher_llm()),
        patch(
            "app.services.evidence_classification_service.encolar_clasificacion",
            side_effect=RuntimeError("job row unique constraint race"),
        ),
        patch("app.services.evidencia_service.logger") as mock_logger,
    ):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("informe.txt", "text/plain", _txt_file("Informe tecnico mensual de consultoria"))],
            background_tasks=bg,
        )

    assert len(resultados) == 1
    ev_rows = (
        (await db.execute(select(Evidencia).where(Evidencia.actividad_id == resultados[0].actividad_id)))
        .scalars()
        .all()
    )
    assert len(ev_rows) == 1

    mock_logger.warning.assert_called_once()
    args, kwargs = mock_logger.warning.call_args
    assert args[0] == "clasificacion_enqueue_failed"
    assert kwargs["cuenta_cobro_id"] == str(cuenta_cobro.id)


async def test_subir_evidencias_cuenta_sin_obligaciones_marca_todo_sin_aplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """A contrato with zero obligaciones can't match anything — every uploaded
    evidencia still resolves explicitly to 'no obligación applies', not silence."""
    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion

    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("archivo.txt", "text/plain", _txt_file("cualquier contenido"))],
    )

    links = (
        (await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == resultados[0].id)))
        .scalars()
        .all()
    )
    assert len(links) == 1
    assert links[0].status == EstadoEnlace.NO_APLICA.value


async def test_subir_evidencias_cuenta_otro_usuario_falla(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    from app.core.exceptions import ForbiddenError

    other_user_id = uuid.uuid4()
    storage = _mock_storage()
    with pytest.raises(ForbiddenError):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=other_user_id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("spy.txt", "text/plain", _txt_file("contenido"))],
        )
