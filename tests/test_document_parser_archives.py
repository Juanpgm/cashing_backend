"""Tests for archive expansion and text-fallback in the document parser.

The agent chat lets users drop `.zip`/`.tar.gz` archives and arbitrary text files;
`parse_document` must expand archives (recursively) and decode plain text, while still
refusing genuine binaries and bounding zip-bomb-style input.
"""

from __future__ import annotations

import io
import tarfile
import zipfile

import pytest
from app.agent.tools.document_parser import parse_archive, parse_document, parse_text


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_targz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestPlainText:
    def test_decodes_utf8(self) -> None:
        assert parse_text("informe de actividades — julio".encode()) == "informe de actividades — julio"

    def test_rejects_binary(self) -> None:
        with pytest.raises(ValueError):
            parse_text(b"\x00\x01\x02\x03\xff\xfe\x00\x00binarypayload\x00")


class TestZip:
    def test_expands_zip_members_with_headers(self) -> None:
        content = _make_zip(
            {
                "acta.txt": "acta de inicio del contrato".encode(),
                "notas.md": "# Notas\nentrega mensual".encode(),
            }
        )
        result = parse_document(content, "soportes.zip")
        assert "### acta.txt" in result
        assert "acta de inicio del contrato" in result
        assert "### notas.md" in result
        assert "entrega mensual" in result

    def test_skips_binary_members_but_keeps_text(self) -> None:
        content = _make_zip(
            {
                "readme.txt": "contenido legible".encode(),
                "tool.exe": b"MZ\x00\x00\x01\x02binary",
            }
        )
        result = parse_document(content, "mixto.zip")
        assert "contenido legible" in result
        assert "tool.exe" not in result  # binary member skipped entirely

    def test_nested_zip_is_expanded(self) -> None:
        inner = _make_zip({"interno.txt": "documento anidado".encode()})
        outer = _make_zip({"paquete.zip": inner, "raiz.txt": "documento raiz".encode()})
        result = parse_document(outer, "outer.zip")
        assert "documento raiz" in result
        assert "documento anidado" in result

    def test_member_count_cap(self) -> None:
        files = {f"f{i}.txt": f"contenido {i}".encode() for i in range(80)}
        result = parse_archive(_make_zip(files), "muchos.zip")
        assert "se omitieron miembros adicionales" in result


class TestTarGz:
    def test_expands_targz(self) -> None:
        content = _make_targz({"informe.txt": "resumen ejecutivo".encode()})
        result = parse_document(content, "backup.tar.gz")
        assert "### informe.txt" in result
        assert "resumen ejecutivo" in result


class TestDispatch:
    def test_unknown_extension_falls_back_to_text(self) -> None:
        assert parse_document(b"col1,col2\n1,2", "datos.csv") == "col1,col2\n1,2"

    def test_binary_extension_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_document(b"\x89PNG\r\n\x1a\n", "foto.png")
