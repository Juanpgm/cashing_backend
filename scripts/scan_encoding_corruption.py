"""Read-only scan for U+FFFD (replacement character) corruption in Contrato text fields.

Purpose
-------
Quantify how many stored `contratos` rows carry the U+FFFD replacement character in
text fields (`entidad`, `numero_contrato`, `objeto`), so the blast radius of an
encoding incident can be measured instead of guessed.

What a hit MEANS (and does not mean)
------------------------------------
U+FFFD means some decoder already replaced a byte it could not decode; the original
character is gone and cannot be recovered by transforming the stored string. It does
NOT tell you WHICH decoder did it. Do not assume the source data is corrupt: a live
probe against SECOP's own dataset for the record previously blamed for this
(NIT 890399011) returns the entity name correctly accented — see
`tests/test_secop_service_mapear.py::test_live_secop_returns_accented_entity_name`,
runnable with `-m live`. Suspect our own ingest paths first, particularly any
`decode(..., errors="replace")`.

If rows show up here, the recovery move is to RE-INGEST them from the source
(re-import the contract from SECOP, re-extract the document), not to patch the
strings in place.

Safety
------
READ-ONLY: only SELECT statements, never a write. It reads whatever `DATABASE_URL`
is configured (via app.core.config), so point it at a LOCAL/dev database only. To
check production, have someone with safe read access run the equivalent query
through a read replica or an approved read-only console.

Usage
-----
    uv run python scripts/scan_encoding_corruption.py

Output: count of affected rows per field, plus up to 5 truncated samples per field
so values can be spot-checked without dumping full text.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import async_session_factory
from app.models.contrato import Contrato
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Escape, never the raw glyph: this file's whole job is detecting encoding damage, so
# a bad re-encode of this very source must not be able to silently break the needle
# it compares against. app/services/secop_service.py spells it the same way.
REPLACEMENT_CHAR = "\ufffd"

# (attribute name on the Contrato model, human label)
_FIELDS_TO_CHECK = (
    ("entidad", "entidad"),
    ("numero_contrato", "numero_contrato"),
    ("objeto", "objeto"),
)

_MAX_SAMPLES = 5

# Windows consoles default to cp1252, which cannot encode U+FFFD at all: without this
# the script would raise UnicodeEncodeError on exactly the rows it exists to report.
# `backslashreplace` (not `replace`) is deliberate — damaged characters print as a
# visible `\ufffd` escape instead of being mangled a second time on the way to the
# terminal. Confusing a terminal's re-encode for real data is what produced the
# "SECOP is already corrupt" misdiagnosis this script's docstring warns about.
# Precedent: scripts/smoke_formato_clone.py does the same for its own output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


async def _scan_field(session: AsyncSession, field_name: str) -> tuple[int, list[tuple[str, str]]]:
    """Return (count, samples) of rows whose `field_name` contains U+FFFD.

    The count comes from an aggregate so it stays a single row on the wire; only the
    handful of sample rows are actually materialized.
    """
    column = getattr(Contrato, field_name)
    predicate = column.contains(REPLACEMENT_CHAR)

    count = (await session.execute(select(func.count()).select_from(Contrato).where(predicate))).scalar_one()

    sample_rows = (await session.execute(select(Contrato.id, column).where(predicate).limit(_MAX_SAMPLES))).all()
    samples = [(str(row_id), (value or "")[:120]) for row_id, value in sample_rows]
    return count, samples


async def main() -> None:
    async with async_session_factory() as session:
        total_contratos = (await session.execute(select(func.count()).select_from(Contrato))).scalar_one()
        print(f"Total contratos rows scanned: {total_contratos}\n")

        any_found = False
        for field_name, label in _FIELDS_TO_CHECK:
            count, samples = await _scan_field(session, field_name)
            print(f"[{label}] rows with U+FFFD: {count}")
            for row_id, preview in samples:
                print(f"    id={row_id}  value={preview!r}")
            if count:
                any_found = True
        print()
        if not any_found:
            print("No U+FFFD corruption found in the scanned fields for this database.")
        else:
            print(
                "NOTE: U+FFFD means the original character was already replaced by some "
                "decoder before it reached this column. Do NOT try to repair these values "
                "with a string transform — the information is gone. Re-ingest the affected "
                "rows from their source instead, and trace which decoder replaced the byte "
                "(see the module docstring: our own ingest paths are the first suspects, "
                "not SECOP)."
            )


if __name__ == "__main__":
    asyncio.run(main())
