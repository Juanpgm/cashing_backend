"""listar_archivos_comprimido must send a browser User-Agent (bug fix).

community.secop.gov.co 403s UA-less requests for RetrieveFile downloads —
already fixed once for the checklist sniff path (see app/core/secop_http.py,
test_checklist_secop_sniff.py). This is the exact call site the user hit
("No se pudo descargar el archivo: Client error '403 Forbidden' ...").
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.core.secop_http import SECOP_BROWSER_USER_AGENT
from app.models.secop import SecopDocumento
from app.services import secop_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_AsyncClientReal = httpx.AsyncClient


def _client_con_transport(transport: httpx.MockTransport) -> Any:
    def _factory(**kw: Any) -> httpx.AsyncClient:
        kw["transport"] = transport
        return _AsyncClientReal(**kw)

    return _factory


async def _seed_zip_doc(db: AsyncSession, *, url: str = "https://s/paquete.zip") -> SecopDocumento:
    doc = SecopDocumento(
        id_documento_secop="DOC-ZIP-001",
        nombre_archivo="paquete.zip",
        extension="zip",
        url_descarga=url,
        datos_raw={},
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def test_listar_archivos_comprimido_envia_user_agent_de_navegador(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers_recibidos: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        headers_recibidos.update(request.headers)
        buf = _zip_bytes()
        return httpx.Response(200, content=buf)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", _client_con_transport(transport))
    doc = await _seed_zip_doc(db)

    result = await secop_service.listar_archivos_comprimido(db, doc.id)

    assert headers_recibidos.get("user-agent") == SECOP_BROWSER_USER_AGENT
    assert result.error is None


async def test_listar_archivos_comprimido_403_da_mensaje_amigable_sin_filtrar_detalle_tecnico(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"<html>forbidden</html>")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", _client_con_transport(transport))
    doc = await _seed_zip_doc(db)

    result = await secop_service.listar_archivos_comprimido(db, doc.id)

    assert result.error is not None
    assert "No se pudo descargar el archivo" in result.error
    # The raw httpx exception text (URL, status line) stays out of the user message.
    assert "Client error" not in result.error
    assert "403" not in result.error


def _zip_bytes() -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "contenido")
    return buf.getvalue()
