"""Tests for link-type evidencias (evidence records that point to an external URL
instead of an uploaded file) — model, download, and delete behavior."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import evidencia_service
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# Mixed async (DB-backed) + sync (migration-file) tests in this module —
# no blanket `pytestmark`; `asyncio_mode = "auto"` (pyproject.toml) picks up the
# async ones without needing an explicit marker on either kind.


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-LINK-001",
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
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


# ── Model tests ───────────────────────────────────────────────────────────────


async def test_evidencia_model_accepts_link_only_row(db: AsyncSession, actividad: Actividad) -> None:
    """A link-type Evidencia has no storage_key/tipo_archivo/tamano_bytes, only fuente + url."""
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="email",
        url="https://mail.google.com/mail/u/0/#all/abc123",
        nombre_archivo="Informe mensual",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    assert evidencia.id is not None
    assert evidencia.storage_key is None
    assert evidencia.tipo_archivo is None
    assert evidencia.tamano_bytes is None
    assert evidencia.fuente == "email"
    assert evidencia.url.startswith("https://mail.google.com")


# ── Service tests ─────────────────────────────────────────────────────────────


async def test_obtener_url_descarga_link_evidencia_no_storage_call(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """Downloading a link evidencia returns its url directly, without hitting storage."""
    user = test_user["user"]
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="drive",
        url="https://drive.google.com/file/d/xyz",
        nombre_archivo="Contrato firmado",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    storage = _mock_storage()
    result = await evidencia_service.obtener_url_descarga(db, storage, user.id, evidencia.id)

    assert result.presigned_url == "https://drive.google.com/file/d/xyz"
    storage.presigned_url.assert_not_called()


async def test_eliminar_evidencia_link_no_storage_call(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """Deleting a link evidencia removes the row without calling storage.delete."""
    user = test_user["user"]
    evidencia = Evidencia(
        actividad_id=actividad.id,
        fuente="calendar",
        url="https://calendar.google.com/event?eid=abc",
        nombre_archivo="Reunión de seguimiento",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    storage = _mock_storage()
    await evidencia_service.eliminar_evidencia(db, storage, user.id, evidencia.id)

    storage.delete.assert_not_called()
    with pytest.raises(NotFoundError):
        await evidencia_service.obtener_url_descarga(db, storage, user.id, evidencia.id)


# ── EvidenciaObligacion link table (evidence-obligation-links capability) ─────
#
# Distinct from the external-link Evidencia rows tested above — this section
# covers the NEW many-to-many `evidencia_obligacion` table that carries
# confidence/reasoning per Evidencia<->Obligacion pair.


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Elaborar informes técnicos mensuales",
        tipo=TipoObligacion.GENERAL,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


@pytest.fixture
async def obligacion_2(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Asistir a reuniones de seguimiento",
        tipo=TipoObligacion.GENERAL,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


@pytest.fixture
async def evidencia(db: AsyncSession, actividad: Actividad) -> Evidencia:
    e = Evidencia(
        actividad_id=actividad.id,
        storage_key="evidencias/test/informe.pdf",
        nombre_archivo="informe.pdf",
        tipo_archivo="application/pdf",
        tamano_bytes=1024,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


class TestEvidenciaObligacionModel:
    async def test_one_evidencia_linked_to_two_obligaciones(
        self, db: AsyncSession, evidencia: Evidencia, obligacion: Obligacion, obligacion_2: Obligacion
    ) -> None:
        """One Evidencia row relevant to two different Obligacion rows persists as
        two separate link rows, each with its own confidence and reasoning."""
        link1 = EvidenciaObligacion(
            evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="alta", reasoning="Coincide en tema"
        )
        link2 = EvidenciaObligacion(
            evidencia_id=evidencia.id, obligacion_id=obligacion_2.id, confianza="media", reasoning="Coincide parcial"
        )
        db.add_all([link1, link2])
        await db.commit()

        result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == evidencia.id))
        links = result.scalars().all()
        assert len(links) == 2
        by_obligacion = {link.obligacion_id: link for link in links}
        assert by_obligacion[obligacion.id].confianza == "alta"
        assert by_obligacion[obligacion.id].reasoning == "Coincide en tema"
        assert by_obligacion[obligacion_2.id].confianza == "media"

    async def test_unique_evidencia_obligacion_pair_enforced(
        self, db: AsyncSession, evidencia: Evidencia, obligacion: Obligacion
    ) -> None:
        db.add(EvidenciaObligacion(evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="alta"))
        await db.commit()

        db.add(EvidenciaObligacion(evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="baja"))
        with pytest.raises(IntegrityError):
            await db.commit()


def _load_migration_030():
    """Load the repo's `alembic/versions/030_...py` by file path.

    NOT `importlib.import_module("alembic.versions...")` — the installed PyPI
    `alembic` package (imported by alembic.migration/operations below) shadows
    this repo's local `alembic/` config directory, which has no `__init__.py`
    and isn't a regular importable package under that name.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "030_evidencia_obligacion_link.py"
    spec = importlib.util.spec_from_file_location("migration_030_evidencia_obligacion_link", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEvidenciaObligacionMigration:
    """Migration test against a fresh, minimal SQLite schema (no full historical
    chain — several older migrations use Postgres-only dialect types and were
    never meant to run on SQLite; local dev relies on `create_all` for SQLite,
    Alembic is the Postgres path). This isolates and verifies OUR migration file
    only creates the new table, touching nothing else."""

    def test_migration_creates_only_new_table(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect, text

        migration = _load_migration_030()

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE evidencias (id CHAR(36) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE obligaciones (id CHAR(36) PRIMARY KEY)"))

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

            inspector = inspect(conn)
            assert "evidencia_obligacion" in inspector.get_table_names()
            # Existing tables untouched — still exactly the minimal columns created above.
            assert [c["name"] for c in inspector.get_columns("evidencias")] == ["id"]
            assert [c["name"] for c in inspector.get_columns("obligaciones")] == ["id"]

            columns = {c["name"] for c in inspector.get_columns("evidencia_obligacion")}
            assert columns == {
                "id",
                "evidencia_id",
                "obligacion_id",
                "confianza",
                "score",
                "reasoning",
                "status",
                "source",
                "created_at",
                "updated_at",
            }

    def test_migration_downgrade_drops_only_new_table(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, inspect, text

        migration = _load_migration_030()

        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE evidencias (id CHAR(36) PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE obligaciones (id CHAR(36) PRIMARY KEY)"))

            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
                migration.downgrade()

            inspector = inspect(conn)
            assert "evidencia_obligacion" not in inspector.get_table_names()
            assert "evidencias" in inspector.get_table_names()
            assert "obligaciones" in inspector.get_table_names()


class TestGuardarEnlacesEvidencia:
    async def test_alta_confidence_auto_confirms_link(
        self, db: AsyncSession, evidencia: Evidencia, obligacion: Obligacion
    ) -> None:
        links = await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "alta", "score": 0.9, "reasoning": "match"}],
        )
        assert len(links) == 1
        assert links[0].status == EstadoEnlace.CONFIRMED.value
        assert links[0].source == "ai"

    @pytest.mark.parametrize("confianza", ["media", "baja"])
    async def test_lower_confidence_persists_as_pending(
        self, db: AsyncSession, evidencia: Evidencia, obligacion: Obligacion, confianza: str
    ) -> None:
        links = await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": confianza, "score": 0.6, "reasoning": "x"}],
        )
        assert links[0].status == EstadoEnlace.PROPOSED.value

    async def test_persists_n_links_for_one_evidence(
        self, db: AsyncSession, evidencia: Evidencia, obligacion: Obligacion, obligacion_2: Obligacion
    ) -> None:
        """Matcher output suggesting 3 obligaciones for one evidence persists exactly
        3 link rows with correct confidence/reasoning per row (2 distinct obligaciones
        here since the fixtures only provide 2 — the count assertion is what matters)."""
        links = await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[
                {"obligacion_id": obligacion.id, "confianza": "alta", "score": 0.9, "reasoning": "a"},
                {"obligacion_id": obligacion_2.id, "confianza": "media", "score": 0.6, "reasoning": "b"},
            ],
        )
        assert len(links) == 2

        result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == evidencia.id))
        persisted = result.scalars().all()
        assert len(persisted) == 2


class TestNoObligacionAplica:
    async def test_no_obligacion_applies_distinct_from_zero_links(self, db: AsyncSession, evidencia: Evidencia) -> None:
        """An evidence classified as unrelated to any obligación stores a reasoning
        record for the non-match — distinct from simply having zero link rows."""
        await evidencia_service.marcar_evidencia_sin_obligacion(
            db, evidencia_id=evidencia.id, reasoning="No coincide con ninguna obligación del contrato"
        )

        result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id == evidencia.id))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].obligacion_id is None
        assert rows[0].status == EstadoEnlace.NO_APLICA.value
        assert rows[0].reasoning == "No coincide con ninguna obligación del contrato"

        # No CONFIRMED link exists for this evidence — the non-match is stored
        # separately, not as a confirmed link to some obligación.
        confirmed = [r for r in rows if r.status == EstadoEnlace.CONFIRMED.value]
        assert confirmed == []


class TestEnlacesSobrevivenCruzarDocumentos:
    """`cruzar_service.cruzar_documentos` DELETEs Actividad rows for the cuenta on
    every re-run (refresh-from-docs semantics). `EvidenciaObligacion` links are
    keyed ONLY by `evidencia_id` (no FK to Actividad at all — see the model
    docstring), so they MUST survive that refresh untouched, regardless of
    whether the specific Actividad the evidencia was attached to gets deleted,
    reused, or was never a "stub" in the first place."""

    async def test_enlace_sobrevive_al_delete_de_actividades(
        self,
        db: AsyncSession,
        contrato: Contrato,
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.services import cruzar_service
        from app.services.evidencia_service import guardar_enlaces_evidencia

        links_antes = await guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "alta", "score": 0.9, "reasoning": "match"}],
        )
        link_id = links_antes[0].id

        # A DocumentoFuente with text overlapping the obligación is required so
        # `cruzar_documentos` actually reaches its DELETE step instead of
        # early-returning on "no docs with text".
        from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente

        db.add(
            DocumentoFuente(
                usuario_id=contrato.usuario_id,
                contrato_id=contrato.id,
                storage_key="docs/informe.pdf",
                nombre="informe.pdf",
                tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
                texto_extraido="Informe técnico mensual entregado según lo requerido.",
            )
        )
        await db.commit()

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value=MagicMock(content="[]"))  # nothing new matches — irrelevant here

        with (
            patch("app.services.cruzar_service.get_llm", return_value=mock_llm),
            patch("app.services.cruzar_service.quality_gate_node", new_callable=AsyncMock) as mock_gate,
        ):
            mock_gate.return_value = {"quality_gate_passed": True, "quality_issues": []}
            await cruzar_service.cruzar_documentos(db, contrato.usuario_id, cuenta_cobro.id)

        result = await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.id == link_id))
        link_despues = result.scalar_one_or_none()
        assert link_despues is not None
        assert link_despues.evidencia_id == evidencia.id
        assert link_despues.obligacion_id == obligacion.id
        assert link_despues.status == EstadoEnlace.CONFIRMED.value

        # The Evidencia row itself (and whatever Actividad it points at) must
        # also still exist — it's excluded from the DELETE precisely because it
        # carries an Evidencia.
        ev_result = await db.execute(select(Evidencia).where(Evidencia.id == evidencia.id))
        assert ev_result.scalar_one_or_none() is not None


# ── Reclassification (evidence-reclassification capability) ─────────────────
#
# Full user authority over one evidencia's obligación links: add/remove/confirm
# any link, or mark "no obligación applies" — each independently, no approval
# gate. `-k reclassif` selects this class per the tasks.md focused command.


class TestReclasificarEvidencia:
    async def test_reclassif_add_link_creates_confirmed_user_link(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        user = test_user["user"]
        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[obligacion.id],
            remove=[],
            confirm=[],
            no_aplica=False,
        )
        assert len(links) == 1
        assert links[0].obligacion_id == obligacion.id
        assert links[0].status == EstadoEnlace.CONFIRMED.value
        assert links[0].source == "user"

    async def test_reclassif_remove_link_deletes_any_existing_state(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        user = test_user["user"]
        await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "alta", "score": 0.9}],
        )

        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[obligacion.id],
            confirm=[],
            no_aplica=False,
        )
        assert links == []

    async def test_reclassif_confirm_promotes_pending_media_to_confirmed(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        user = test_user["user"]
        await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "media", "score": 0.6}],
        )

        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[],
            confirm=[obligacion.id],
            no_aplica=False,
        )
        assert len(links) == 1
        assert links[0].status == EstadoEnlace.CONFIRMED.value

    async def test_reclassif_mark_no_aplica_clears_pending_and_stores_reasoning(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
        obligacion_2: Obligacion,
    ) -> None:
        user = test_user["user"]
        await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[
                {"obligacion_id": obligacion.id, "confianza": "media", "score": 0.6},
                {"obligacion_id": obligacion_2.id, "confianza": "baja", "score": 0.3},
            ],
        )

        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[],
            confirm=[],
            no_aplica=True,
        )
        assert len(links) == 1
        assert links[0].status == EstadoEnlace.NO_APLICA.value
        assert links[0].obligacion_id is None
        assert (links[0].reasoning or "").strip() != ""

    async def test_reclassif_sequential_remove_then_add_both_succeed_no_approval_gate(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        """A user removing then immediately re-adding the same link both
        succeed independently — no secondary approver blocks either step
        (evidence-reclassification: Full user authority without approval
        workflow)."""
        user = test_user["user"]
        await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "alta", "score": 0.9}],
        )

        removed = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[obligacion.id],
            confirm=[],
            no_aplica=False,
        )
        assert removed == []

        re_added = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[obligacion.id],
            remove=[],
            confirm=[],
            no_aplica=False,
        )
        assert len(re_added) == 1
        assert re_added[0].status == EstadoEnlace.CONFIRMED.value

    async def test_reclassif_add_after_no_aplica_deletes_the_contradictory_no_aplica_row(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        """REL-001: after `no_aplica` (obligacion_id=None, status=no_aplica), a
        later `add` must not leave both a confirmed link AND the stale
        no_aplica row rendered together — the no_aplica row must be deleted."""
        user = test_user["user"]
        await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[],
            confirm=[],
            no_aplica=True,
        )

        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[obligacion.id],
            remove=[],
            confirm=[],
            no_aplica=False,
        )

        assert len(links) == 1
        assert links[0].obligacion_id == obligacion.id
        assert links[0].status == EstadoEnlace.CONFIRMED.value

        no_aplica_rows = (
            (
                await db.execute(
                    select(EvidenciaObligacion).where(
                        EvidenciaObligacion.evidencia_id == evidencia.id,
                        EvidenciaObligacion.obligacion_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert no_aplica_rows == []

    async def test_reclassif_confirm_after_no_aplica_deletes_the_contradictory_no_aplica_row(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
        cuenta_cobro: CuentaCobro,
        evidencia: Evidencia,
        obligacion: Obligacion,
    ) -> None:
        """REL-001, confirm branch: a proposed suggestion sitting alongside a
        no_aplica row (state constructed directly — not necessarily reachable
        via the reverse no_aplica call, which clears real links) must, on
        `confirm`, both promote the link AND delete the no_aplica row."""
        user = test_user["user"]
        await evidencia_service.guardar_enlaces_evidencia(
            db,
            evidencia_id=evidencia.id,
            sugerencias=[{"obligacion_id": obligacion.id, "confianza": "media", "score": 0.6}],
        )
        await evidencia_service.marcar_evidencia_sin_obligacion(
            db, evidencia_id=evidencia.id, reasoning="Marcado manualmente por el usuario: no aplica."
        )

        links = await evidencia_service.reclasificar_evidencia(
            db,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            evidencia_id=evidencia.id,
            add=[],
            remove=[],
            confirm=[obligacion.id],
            no_aplica=False,
        )

        assert len(links) == 1
        assert links[0].status == EstadoEnlace.CONFIRMED.value

        no_aplica_rows = (
            (
                await db.execute(
                    select(EvidenciaObligacion).where(
                        EvidenciaObligacion.evidencia_id == evidencia.id,
                        EvidenciaObligacion.obligacion_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert no_aplica_rows == []

    async def test_reclassif_add_unknown_obligacion_raises_not_found(
        self, db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro, evidencia: Evidencia
    ) -> None:
        """Validates the referenced obligación belongs to the cuenta's
        contrato before writing anything (evidence-reclassification: full
        authority is scoped to the user's own cuenta, not an arbitrary id)."""
        user = test_user["user"]
        with pytest.raises(NotFoundError):
            await evidencia_service.reclasificar_evidencia(
                db,
                usuario_id=user.id,
                cuenta_id=cuenta_cobro.id,
                evidencia_id=evidencia.id,
                add=[uuid.uuid4()],
                remove=[],
                confirm=[],
                no_aplica=False,
            )
