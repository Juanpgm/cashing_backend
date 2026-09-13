"""Reusable factory-boy builders for the five core domain models.

radicacion-sin-friccion, slice 4.1. Before this module, every test that needed
a `Usuario`/`Contrato`/`CuentaCobro` hand-rolled its own module-level `_make_user`
/ `_make_contrato` helpers (see e.g. `tests/test_cuota_position.py`,
`tests/test_cuenta_hard_delete.py`, `tests/test_purga_huerfanos.py`) — the same
~15 lines of boilerplate duplicated across a dozen files. These factories
replace that duplication with one importable, composable definition per model.

Pattern chosen (see the slice 4.1 audit): a plain `factory.Factory` per model,
NOT `factory.alchemy.SQLAlchemyModelFactory`. The audit found no existing
factory-boy usage anywhere in the suite to preserve (the `class.*Factory`
hits in `tests/conftest.py` were false positives — `tmp_path_factory`, a
pytest fixture name, not factory-boy), so there is no established pattern this
module needs to match. `SQLAlchemyModelFactory` is built around a *sync*
SQLAlchemy `Session` bound once at class-definition time; this repo's session
is an async `AsyncSession` created per-test by the `db` fixture
(`tests/conftest.py`), so wiring `SQLAlchemyModelFactory` here would mean either
a second, parallel DB-session mechanism or fighting the library's sync
assumptions with a custom async session manager per test. A plain
`factory.Factory` sidesteps that entirely:

- `Factory.build(**overrides)` — synchronous, no DB, no event loop. Returns a
  transient (unsaved) ORM instance with valid defaults. Nested relationships
  (e.g. `ContratoFactory.build().usuario`) are also transient `build()`s via
  `factory.SubFactory`.
- `await Factory.create_async(db, **overrides)` — a small async classmethod
  added here (there is no factory-boy built-in for this) that builds the
  instance, `db.add()`s it and `await db.flush()`es it against whatever
  `AsyncSession` the caller passes in — always the same `db` fixture every
  other test already uses, never a second session mechanism.

SubFactory chains persist their parents for free: SQLAlchemy's default
relationship cascade (`save-update, merge`) means `db.add(child)` transitively
adds every transient object reachable through a `relationship()` — a
`CuentaCobroFactory.create_async(db)` call with no `contrato=` override
therefore also inserts its (also-transient) `Contrato` and that `Contrato`'s
`Usuario` in the same flush, in FK-safe order, with no extra code here.

`RequisitoCuenta` is the one exception: the model only exposes the raw
`cuenta_cobro_id` FK column, not a `relationship()` to `CuentaCobro` (see
`app/models/requisito_cuenta.py`), so the cascade trick is unavailable there.
`RequisitoCuentaFactory.create_async()` creates its parent `CuentaCobro`
explicitly instead when the caller doesn't supply one.
"""

from __future__ import annotations

import uuid
from datetime import date

import factory
from app.core.security import hash_password
from app.models.actividad import Actividad, JustificacionOrigen
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.evidencia import Evidencia
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.usuario import RolUsuario, Usuario
from sqlalchemy.ext.asyncio import AsyncSession


class UsuarioFactory(factory.Factory):
    """Valid `Usuario`. `email` is `factory.Sequence`d so two builds in the
    same test never collide on the model's unique index."""

    class Meta:
        model = Usuario

    email = factory.Sequence(lambda n: f"factory-user-{n}@cashin.test")
    nombre = "Usuario de Prueba"
    cedula = factory.Sequence(lambda n: f"{100_000_000 + n}")
    telefono = "+573001234567"
    password_hash = factory.LazyFunction(lambda: hash_password("FactoryPass123!"))
    rol = RolUsuario.CONTRATISTA
    activo = True
    creditos_disponibles = 100

    @classmethod
    async def create_async(cls, db: AsyncSession, **kwargs: object) -> Usuario:
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance


class ContratoFactory(factory.Factory):
    """Valid `Contrato`, owned by a fresh `UsuarioFactory.build()` unless
    `usuario=` is overridden. `numero_contrato` is `factory.Sequence`d."""

    class Meta:
        model = Contrato

    usuario = factory.SubFactory(UsuarioFactory)
    numero_contrato = factory.Sequence(lambda n: f"CTR-FACTORY-{n:04d}")
    objeto = "Prestación de servicios de consultoría"
    valor_total = 36_000_000
    valor_mensual = 3_000_000
    fecha_inicio = date(2024, 1, 1)
    fecha_fin = date(2024, 12, 31)
    entidad = "Entidad de Prueba"
    dependencia = "Dependencia de Prueba"
    supervisor_nombre = "Supervisor de Prueba"

    @classmethod
    async def create_async(cls, db: AsyncSession, **kwargs: object) -> Contrato:
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance


class CuentaCobroFactory(factory.Factory):
    """Valid `CuentaCobro`, attached to a fresh `ContratoFactory.build()`
    (and that contrato's own fresh `Usuario`) unless `contrato=` is overridden.

    `mes` is `factory.Sequence`d (1-12, wrapping) so several cuentas created
    against the SAME explicit `contrato=` in one test don't collide on the
    `uq_contrato_mes_anio` unique constraint; pass `mes=`/`anio=` explicitly
    when a test needs a specific value.
    """

    class Meta:
        model = CuentaCobro

    contrato = factory.SubFactory(ContratoFactory)
    mes = factory.Sequence(lambda n: (n % 12) + 1)
    anio = 2024
    estado = EstadoCuentaCobro.BORRADOR
    valor = 3_000_000
    posicion = PosicionCuota.RECURRENTE
    informe_final = False

    @classmethod
    async def create_async(cls, db: AsyncSession, **kwargs: object) -> CuentaCobro:
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance


class ActividadFactory(factory.Factory):
    """Valid `Actividad`, attached to a fresh `CuentaCobroFactory.build()`
    (and its whole parent chain) unless `cuenta_cobro=` is overridden."""

    class Meta:
        model = Actividad

    cuenta_cobro = factory.SubFactory(CuentaCobroFactory)
    descripcion = "Actividad de prueba generada por factory"
    justificacion = None
    justificacion_origen = JustificacionOrigen.SEED
    fecha_realizacion = date(2024, 3, 15)

    @classmethod
    async def create_async(cls, db: AsyncSession, **kwargs: object) -> Actividad:
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance


class EvidenciaFactory(factory.Factory):
    """Valid stored-file `Evidencia` (storage_key/tipo_archivo/tamano_bytes
    set, fuente/url left None), attached to a fresh `ActividadFactory.build()`
    (and its whole parent chain) unless `actividad=` is overridden.

    For link-evidencia shape (Gmail/Drive/Calendar discovery), override
    explicitly, e.g. ``EvidenciaFactory.build(storage_key=None,
    tipo_archivo=None, tamano_bytes=None, fuente="email", url="https://...")``
    — see `tests/test_actividad.py`'s `evidencia_enlace` fixture for the shape.
    """

    class Meta:
        model = Evidencia

    actividad = factory.SubFactory(ActividadFactory)
    storage_key = factory.Sequence(lambda n: f"evidencias/factory/evidencia-{n}.pdf")
    nombre_archivo = "evidencia-factory.pdf"
    tipo_archivo = "application/pdf"
    tamano_bytes = 2048
    fuente = None
    url = None
    texto_extraido = None
    sha256 = None

    @classmethod
    async def create_async(cls, db: AsyncSession, **kwargs: object) -> Evidencia:
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance


class RequisitoCuentaFactory(factory.Factory):
    """Valid `RequisitoCuenta`.

    `RequisitoCuenta` has no `relationship()` to `CuentaCobro` — only the raw
    `cuenta_cobro_id` FK column — so the SubFactory-cascade trick the other
    factories use is unavailable. `.build()` gets a throwaway random UUID
    (valid shape, not a real row) so it stays usable standalone with no DB;
    `create_async()` replaces it with a real, flushed `CuentaCobro` unless the
    caller passes `cuenta_cobro=` (an already-created parent) or
    `cuenta_cobro_id=` explicitly.
    """

    class Meta:
        model = RequisitoCuenta

    cuenta_cobro_id = factory.LazyFunction(uuid.uuid4)
    codigo = factory.Sequence(lambda n: f"CUSTOM_{n:04d}")
    etiqueta = "Requisito custom de prueba"
    descripcion = None
    obligatorio = True
    solo_primera_cuenta = False
    tipo_documento_fuente = None
    keywords_deteccion = factory.LazyFunction(list)
    orden = 500
    mapea_a_estandar = None
    origen = "inferido"
    activo = True

    @classmethod
    async def create_async(
        cls, db: AsyncSession, *, cuenta_cobro: CuentaCobro | None = None, **kwargs: object
    ) -> RequisitoCuenta:
        if cuenta_cobro is None and "cuenta_cobro_id" not in kwargs:
            cuenta_cobro = await CuentaCobroFactory.create_async(db)
        if cuenta_cobro is not None:
            kwargs.setdefault("cuenta_cobro_id", cuenta_cobro.id)
        instance = cls.build(**kwargs)
        db.add(instance)
        await db.flush()
        return instance
