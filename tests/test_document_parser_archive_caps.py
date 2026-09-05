"""`extract_archive_member_texts` must honour the same text cap as `parse_archive`.

`parse_archive` truncates its concatenation at `_ARCHIVE_MAX_TEXT_CHARS`, but the
per-member variant returned every member's FULL text with no cap at all. It feeds
obligation extraction (`_resolver_texto_obligaciones_archivo`), so an archive with
a few huge text members pushed unbounded text into the LLM chunker — the exact
cost/memory blow-up the cap exists to prevent.
"""

from __future__ import annotations

import io
import zipfile

from app.agent.tools.document_parser import (
    _ARCHIVE_MAX_TEXT_CHARS,
    extract_archive_member_texts,
)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestPerMemberTextIsCapped:
    def test_a_single_oversized_member_is_truncated_to_the_cap(self) -> None:
        enorme = ("obligacion contractual " * (_ARCHIVE_MAX_TEXT_CHARS // 10)).encode()
        content = _make_zip({"grande.txt": enorme})

        miembros = extract_archive_member_texts(content, "soportes.zip")

        assert len(miembros) == 1
        assert len(miembros[0][1]) <= _ARCHIVE_MAX_TEXT_CHARS, (
            f"member text was not capped: {len(miembros[0][1])} chars"
        )

    def test_the_total_across_members_is_capped(self) -> None:
        mitad = ("clausula contractual " * (_ARCHIVE_MAX_TEXT_CHARS // 20)).encode()
        content = _make_zip({f"parte-{i}.txt": mitad for i in range(4)})

        miembros = extract_archive_member_texts(content, "soportes.zip")

        total = sum(len(texto) for _, texto in miembros)
        assert total <= _ARCHIVE_MAX_TEXT_CHARS, f"total across members was not capped: {total} chars"
