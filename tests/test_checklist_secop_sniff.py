"""Borderline content sniff in detectar_desde_secop (clasificacion-documentos-secop, D2).

The sniff downloads + text-extracts a SECOP doc AT MOST ONCE EVER (memoized in
secop_documentos.texto_extraido/texto_estado), only for Contrato/CDP/RPC filas
that are PENDIENTE without a confident (>= 0.700) candidate, bounded by 5 docs
per requisito and 6 downloads per scan. No LLM, no embeddings.
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
    EstadoRequisito,
)
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ── fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-SNIFF-001",
        objeto="Servicios de sniff",
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


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int = 1) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    await checklist_service.asegurar_checklist(db, cuenta=cc)
    await db.commit()
    return cc


def _secop_doc(
    contrato: Contrato,
    nombre: str,
    *,
    url: str | None = None,
    fecha: date | None = None,
    **kwargs: Any,
) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        url_descarga=url,
        fecha_carga=fecha,
        datos_raw={},
        **kwargs,
    )


def _docx_bytes(texto: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(texto)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


TEXTO_CONTRATO = (
    "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES No. 99\n"
    "Entre la entidad y el contratista se celebra el presente contrato, cuyo clausulado se detalla."
)
TEXTO_RPC = "REGISTRO PRESUPUESTAL DEL COMPROMISO No. 555\nRegistro de compromiso presupuestal."
TEXTO_AMBIGUO = "contrato con clausulado y registro presupuestal de la vigencia"
TEXTO_IRRELEVANTE = "acta administrativa interna de la dependencia sin datos utiles"


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the download seam; log every call, serve payloads, raise on demand."""
    llamadas: list[str] = []
    payloads: dict[str, bytes] = {}
    errores: set[str] = set()

    async def _fake(url: str, timeout: float = checklist_service._SNIFF_HTTP_TIMEOUT) -> bytes:
        llamadas.append(url)
        if url in errores:
            raise httpx.ConnectError("simulated download failure")
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)
    return {"llamadas": llamadas, "payloads": payloads, "errores": errores}


async def _fila(db: AsyncSession, cuenta_id: uuid.UUID, codigo: str) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo == codigo,
        )
    )
    return res.scalar_one()


async def _candidatos(db: AsyncSession, cuenta_id: uuid.UUID, codigo: str) -> list[DocumentoChecklistCandidato]:
    res = await db.execute(
        select(DocumentoChecklistCandidato).where(
            DocumentoChecklistCandidato.cuenta_cobro_id == cuenta_id,
            DocumentoChecklistCandidato.requisito_codigo == codigo,
        )
    )
    return list(res.scalars().all())


# ── 2.1 trigger band + bounded cost ─────────────────────────────────────────


async def test_score_confiado_no_dispara_sniff(db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]) -> None:
    """Confident (>= 0.700) filename scores on CONTRATO, RPC and CDP → zero downloads."""
    db.add_all(
        [
            _secop_doc(contrato, "Contrato firmado minuta clausulado.pdf", url="https://s/contrato.pdf"),
            _secop_doc(contrato, "RPC registro presupuestal compromiso presupuestal.pdf", url="https://s/rpc.pdf"),
            _secop_doc(contrato, "CDP certificado de disponibilidad presupuestal.pdf", url="https://s/cdp.pdf"),
        ]
    )
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == []
    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert cands and all(c.score_origen == "nombre" for c in cands)


async def test_borderline_dispara_sniff_y_autolinkea(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    """A zero-score doc whose CONTENT is unambiguous contrato → 0.850 → auto-link."""
    url = "https://s/generico.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(contrato, "documento_generico.docx", url=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    # Downloaded exactly once even though CONTRATO, RPC and CDP all sniffed it.
    assert descargas["llamadas"] == [url]
    await db.refresh(doc)
    assert doc.texto_estado == "ok"
    assert doc.texto_extraido is not None and "CONTRATO DE PRESTACI" in doc.texto_extraido

    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id
    assert fila.confianza_deteccion == Decimal("0.850")
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert [c.score_origen for c in cands] == ["contenido"]
    assert cands[0].score == Decimal("0.850")


async def test_cap_5_por_requisito_y_6_descargas_por_scan(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    """7 CONTRATO-borderline docs → only 5 downloaded; RPC pool then hits the
    global 6-download budget after one more."""
    urls_contrato = []
    for i in range(7):
        url = f"https://s/contrato-{i}.docx"
        urls_contrato.append(url)
        descargas["payloads"][url] = _docx_bytes(TEXTO_IRRELEVANTE)
        db.add(_secop_doc(contrato, f"contrato adicional {i}.docx", url=url, fecha=date(2024, 1, i + 1)))
    urls_rpc = []
    for i in range(3):
        url = f"https://s/rpc-{i}.docx"
        urls_rpc.append(url)
        descargas["payloads"][url] = _docx_bytes(TEXTO_IRRELEVANTE)
        db.add(_secop_doc(contrato, f"registro presupuestal {i}.docx", url=url, fecha=date(2024, 2, i + 1)))
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    llamadas = descargas["llamadas"]
    assert len(llamadas) == 6
    assert len([u for u in llamadas if u in urls_contrato]) == 5  # per-requisito cap
    assert len([u for u in llamadas if u in urls_rpc]) == 1  # global budget cut


# ── 2.2 memoization + failure fallback ──────────────────────────────────────


async def test_memoizacion_segundo_scan_cero_descargas(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    url = "https://s/ambiguo.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_AMBIGUO)
    doc = _secop_doc(contrato, "documento_generico.docx", url=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == [url]
    await db.refresh(doc)
    assert doc.texto_estado == "ok"
    # Ambiguous content → 0.600: candidate only, no auto-link.
    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.PENDIENTE
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert [(c.score, c.score_origen) for c in cands] == [(Decimal("0.600"), "contenido")]

    # Second scan: re-scores the STORED text — zero new downloads, same result.
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()
    assert descargas["llamadas"] == [url]
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert [(c.score, c.score_origen) for c in cands] == [(Decimal("0.600"), "contenido")]


async def test_error_de_descarga_persiste_estado_y_continua(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    url = "https://s/roto.docx"
    descargas["errores"].add(url)
    doc = _secop_doc(contrato, "contrato adicional.docx", url=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    resultado = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == [url]
    await db.refresh(doc)
    assert doc.texto_estado == "error"
    assert doc.texto_extraido is None
    # Scan completed and the filename-based score survives as candidato.
    assert "CONTRATO" in resultado
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert [(c.score, c.score_origen) for c in cands] == [(Decimal("0.333"), "nombre")]

    # A persistently-failing doc is retried on the NEXT scan (error is not
    # terminal like ok/sin_texto) — still one attempt per scan.
    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()
    assert descargas["llamadas"] == [url, url]


async def test_error_transitorio_se_reintenta_y_recupera_en_scan_siguiente(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    """A transient download failure must not poison the doc forever: the next
    scan retries it, and a subsequent success recovers content scoring."""
    url = "https://s/intermitente.docx"
    descargas["errores"].add(url)  # first scan: fails
    doc = _secop_doc(contrato, "documento_generico.docx", url=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == [url]
    await db.refresh(doc)
    assert doc.texto_estado == "error"

    # The network recovers before the next scan.
    descargas["errores"].discard(url)
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == [url, url]  # retried, not skipped
    await db.refresh(doc)
    assert doc.texto_estado == "ok"
    assert doc.texto_extraido is not None and "CONTRATO DE PRESTACI" in doc.texto_extraido

    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.confianza_deteccion == Decimal("0.850")


async def test_extraer_texto_contenido_bug_no_colapsa_a_sin_texto(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected parse exception (NOT the documented 'genuinely non-textual'
    ValueError) must map to the retryable 'error' state, never to the terminal
    'sin_texto' state."""
    url = "https://s/bug-de-parseo.docx"
    descargas["payloads"][url] = b"contenido-cualquiera"
    doc = _secop_doc(contrato, "documento_generico.docx", url=url)
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    def _boom(data: bytes, filename: str) -> str:
        raise RuntimeError("fallo inesperado del parser")

    monkeypatch.setattr(checklist_service, "extraer_texto_contenido", _boom)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    await db.refresh(doc)
    assert doc.texto_estado == "error"


# ── 2.3 invariants ──────────────────────────────────────────────────────────


async def test_contrato_unico_con_dos_borderline(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    for i in range(2):
        url = f"https://s/doble-{i}.docx"
        descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
        db.add(_secop_doc(contrato, f"documento_generico_{i}.docx", url=url, fecha=date(2024, 1, i + 1)))
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    vinculos = (
        (
            await db.execute(
                select(DocumentoRequisitoVinculo).where(DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(vinculos) == 1  # only ONE authoritative Contrato link
    cands = await _candidatos(db, cuenta.id, "CONTRATO")
    assert len(cands) == 2  # the other stays a candidate
    assert all(c.score == Decimal("0.850") for c in cands)


async def test_rpc_append_only_preservado(db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]) -> None:
    rpc_original = _secop_doc(contrato, "RPC registro presupuestal compromiso presupuestal.pdf")
    url = "https://s/modificacion.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_RPC)
    modificacion = _secop_doc(contrato, "modificacion_generica.docx", url=url)
    db.add_all([rpc_original, modificacion])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.vincular_secop_documento(db, cuenta.id, "RPC", rpc_original.id)
    await db.commit()

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    fila = await _fila(db, cuenta.id, "RPC")
    assert fila.secop_documento_id == rpc_original.id  # original link untouched

    # User confirms the modificación → appended, never replacing the original.
    await checklist_service.vincular_secop_documento(db, cuenta.id, "RPC", modificacion.id)
    await db.commit()
    vinculos = (
        (
            await db.execute(
                select(DocumentoRequisitoVinculo).where(DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id)
            )
        )
        .scalars()
        .all()
    )
    assert {v.secop_documento_id for v in vinculos} == {rpc_original.id, modificacion.id}


async def test_override_manual_excluido_del_sniff(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    """A doc whose categoria was manually overridden is never re-sniffed."""
    url = "https://s/override.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    doc = _secop_doc(
        contrato,
        "documento_generico.docx",
        url=url,
        categoria=CategoriaDocumento.OTROS,
        categoria_override=True,
    )
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == []  # override → not downloaded, not re-scored
    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert await _candidatos(db, cuenta.id, "CONTRATO") == []


async def test_override_a_contrato_autolinkea_sin_descarga(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(
        contrato,
        "documento_generico.docx",
        url="https://s/nunca.docx",
        categoria=CategoriaDocumento.CONTRATO,
        categoria_override=True,
    )
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert descargas["llamadas"] == []  # categoria override scores 1.000 by itself
    fila = await _fila(db, cuenta.id, "CONTRATO")
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc.id


async def test_cero_llamadas_llm_y_embeddings(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    llm_calls: list[Any] = []

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        llm_calls.append(args)
        raise AssertionError("LLM/embedding must never be called by the sniff")

    monkeypatch.setattr(litellm, "acompletion", _spy)
    monkeypatch.setattr(litellm, "aembedding", _spy)

    url = "https://s/mixto.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_CONTRATO)
    db.add_all(
        [
            _secop_doc(contrato, "RPC registro presupuestal compromiso presupuestal.pdf"),  # confident
            _secop_doc(contrato, "documento_generico.docx", url=url),  # borderline
        ]
    )
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    assert llm_calls == []
    assert descargas["llamadas"] == [url]


# ── 2.4 real download seam: streamed, bounded by bytes (RISK-001) ───────────
#
# These bypass the ``descargas`` fixture on purpose: it monkeypatches
# ``_descargar_secop_bytes`` wholesale, so it can't exercise the real
# streaming/size-cap behaviour of that function. Uses httpx.MockTransport —
# no real network.


_AsyncClientReal = httpx.AsyncClient


def _client_con_transport(transport: httpx.MockTransport) -> Any:
    """Rebuild AsyncClient with a mocked transport, keeping the app's own kwargs."""

    def _factory(**kw: Any) -> httpx.AsyncClient:
        kw["transport"] = transport
        return _AsyncClientReal(**kw)

    return _factory


async def test_descargar_secop_bytes_aborta_stream_al_exceder_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body without Content-Length that grows past the 25 MiB cap must be
    aborted mid-stream — the cap bounds what is DOWNLOADED, not just kept."""
    chunk = b"x" * (10 * 1024 * 1024)  # 10 MiB per chunk, 5 chunks = 50 MiB total
    yielded: list[int] = []

    async def cuerpo() -> Any:
        for i in range(5):
            yielded.append(i)
            yield chunk

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=cuerpo())

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", _client_con_transport(transport))

    with pytest.raises(ValueError, match="excede"):
        await checklist_service._descargar_secop_bytes("https://s/enorme.docx")

    # Aborts once the accumulated size crosses the cap (3 chunks = 30 MiB > 25 MiB):
    # the remaining chunks of the body are never pulled from the stream.
    assert len(yielded) < 5


async def test_descargar_secop_bytes_rechaza_content_length_temprano(monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized Content-Length is rejected before any body byte is read."""
    leido: list[bytes] = []

    async def cuerpo() -> Any:
        leido.append(b"nunca deberia leerse")
        yield b"x" * 1024

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(50 * 1024 * 1024)}, content=cuerpo())

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", _client_con_transport(transport))

    with pytest.raises(ValueError, match="excede"):
        await checklist_service._descargar_secop_bytes("https://s/enorme2.docx")

    assert leido == []


# ── 2.5 per-download timeout clamped to remaining budget (RES-001) ─────────
#
# Unit-level: exercises _asegurar_texto_extraido directly with a spy replacing
# _descargar_secop_bytes, so the timeout it receives can be asserted. No real
# network, no DB (SecopDocumento is a plain in-memory instance).


def _doc_suelto(url: str) -> SecopDocumento:
    return SecopDocumento(
        id=uuid.uuid4(),
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        nombre_archivo="doc.docx",
        url_descarga=url,
        datos_raw={},
    )


async def test_timeout_por_descarga_se_clampa_al_presupuesto_restante(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-download timeout must never exceed the remaining scan budget:
    worst case must stay bounded by the ~20s budget, not the fixed 10s timeout."""
    timeouts: list[float] = []

    async def _spy(url: str, timeout: float) -> bytes:
        timeouts.append(timeout)
        return _docx_bytes(TEXTO_CONTRATO)

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _spy)

    presupuesto = checklist_service._PresupuestoSniff()
    presupuesto.inicio -= 18.0  # 18s of the 20s budget already elapsed → ~2s left

    doc = _doc_suelto("https://s/clamp.docx")
    await checklist_service._asegurar_texto_extraido(doc, presupuesto)

    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 2.1  # clamped well below the 10s default


async def test_budget_por_debajo_del_epsilon_se_salta_la_descarga(monkeypatch: pytest.MonkeyPatch) -> None:
    """When remaining budget is at/below the epsilon, skip the download
    entirely instead of firing a near-zero-timeout request doomed to fail."""
    llamado = False

    async def _spy(url: str, timeout: float) -> bytes:
        nonlocal llamado
        llamado = True
        return b""

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _spy)

    presupuesto = checklist_service._PresupuestoSniff()
    presupuesto.inicio -= 19.9  # ~0.1s left, below the epsilon

    doc = _doc_suelto("https://s/clamp2.docx")
    await checklist_service._asegurar_texto_extraido(doc, presupuesto)

    assert llamado is False
    assert doc.texto_estado is None
