"""Tests for `secret_scan_service` — real-leak-SHAPED but entirely SYNTHETIC corpus.

The corpus below models the SHAPE of the credential leak this feature closes (a
Postgres/Neon connection string with an embedded password, plus a Neon-style API
key) using entirely FAKE values. Never copy real credential values into this file.
"""

from __future__ import annotations

import pytest
from app.services.secret_scan_service import SecretHallazgo, escanear_paquete

pytestmark = pytest.mark.asyncio

_LEAK_CORPUS_TEXTO = (
    "Notas internas\n"
    "DATABASE_URL=postgresql://fake_user:fake_pass_123@ep-fake-000000"
    ".us-east-1.aws.neon.tech/neondb\n"
    "NEON_API_KEY=napi_a8K3mXpQ9rZtL2wVbN7cJhF4sYdE1oIu\n"
)


async def test_escanear_paquete_detecta_corpus_de_fuga_real() -> None:
    hallazgos = await escanear_paquete([("PENDIENTE.txt", _LEAK_CORPUS_TEXTO.encode("utf-8"))])

    assert hallazgos
    assert all(isinstance(h, SecretHallazgo) for h in hallazgos)
    tipos = {h.tipo for h in hallazgos}
    # Belt-and-suspenders explicit regex (design D2) catches the connection string.
    assert "Postgres/Neon Connection String" in tipos
    # detect-secrets' own built-in plugin set independently catches the same URL's
    # embedded password and the env-style API key assignment.
    assert "Basic Auth Credentials" in tipos
    assert "Secret Keyword" in tipos

    # Findings never carry the secret value itself — only file/type/line.
    for h in hallazgos:
        assert h.archivo == "PENDIENTE.txt"
        assert "fake_pass_123" not in h.tipo
        assert "napi_" not in h.tipo


async def test_escanear_paquete_payload_limpio_no_reporta_hallazgos() -> None:
    limpio = "Carpeta de evidencias\nContrato: CTR-001\nObligaciones: 3\n"
    hallazgos = await escanear_paquete([("LEEME.txt", limpio.encode("utf-8"))])
    assert hallazgos == []


async def test_escanear_paquete_ignora_miembros_binarios_no_decodificables() -> None:
    binario = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\xff\xd9\x80\x81\x00\xff"
    hallazgos = await escanear_paquete([("foto.jpg", binario)])
    assert hallazgos == []


async def test_escanear_paquete_multiple_miembros_reporta_archivo_correcto() -> None:
    hallazgos = await escanear_paquete(
        [
            ("limpio.txt", b"Sin nada raro aqui.\n"),
            ("PENDIENTE.txt", _LEAK_CORPUS_TEXTO.encode("utf-8")),
        ]
    )
    archivos = {h.archivo for h in hallazgos}
    assert archivos == {"PENDIENTE.txt"}
