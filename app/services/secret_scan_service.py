"""Secret scan service — mandatory fail-closed scan before evidence packaging.

Wraps `detect-secrets` (already a pre-commit dependency, see `.pre-commit-config.yaml`)
as a Python API, plus ONE explicit Postgres/Neon connection-string regex as a
belt-and-suspenders check for the exact credential class that leaked previously
(design decision D2, `openspec/changes/billing-resilience-templates/design.md`).

Deliberately uses `SecretsCollection.scan_file` (the same realistic, non-adhoc
pipeline `detect-secrets scan` runs) rather than the library's `scan_line` adhoc
helper: `scan_line` forces "eager" entropy matching that treats every bare word in
a line as a high-entropy candidate, which floods auto-generated Spanish prose
(README/manifest text, extracted document text) with false positives. The realistic
pipeline instead requires assignment-shaped content for its keyword/entropy
detectors — exactly the shape of the real leak (`KEY=VALUE` env-style credentials)
— while leaving plain prose untouched. Each member is scanned via a temp file with
a synthetic `.cfg` name (never the real member name) so `KeywordDetector`
dispatches its unquoted `KEY=VALUE` regex (see `FileType.CONFIG` in
`detect_secrets.util.filetype`) regardless of the member's actual extension.

Fail-closed: any hit blocks package emission entirely — see
`informe_service.generar_zip_evidencias`. Findings never carry the secret value
itself, not even partially — only the member name, the detector's TYPE label, and
the 1-based line number within that member.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import default_settings

logger = structlog.get_logger("service.secret_scan")

# Belt-and-suspenders (design D2): a Postgres/Neon connection string with an
# explicit user:password segment — the exact shape of the real leak this feature
# closes. Kept independent of detect-secrets' plugin set so a future change to
# that set can never silently drop coverage for this credential class.
_POSTGRES_URL_PATTERN = re.compile(r"postgres(?:ql)?://[^:\s]+:[^@\s]+@[^\s'\"]+", re.IGNORECASE)
_POSTGRES_URL_TIPO = "Postgres/Neon Connection String"

# Synthetic filename used ONLY for detect-secrets' file-type dispatch (never the
# real member name) — `.cfg` maps to `FileType.CONFIG`, which lets `KeywordDetector`
# match unquoted `KEY=VALUE` assignments (env-style credentials) regardless of the
# scanned member's actual extension.
_SCAN_FILENAME = "scan.cfg"


@dataclass(frozen=True)
class SecretHallazgo:
    """One secret detection. Never carries the secret value — only enough to
    locate and classify it, so a finding is always safe to log or surface."""

    archivo: str
    tipo: str
    linea: int


def _escanear_texto(archivo: str, texto: str) -> list[SecretHallazgo]:
    hallazgos: list[SecretHallazgo] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / _SCAN_FILENAME
        path.write_text(texto, encoding="utf-8")
        secrets = SecretsCollection()
        with default_settings():
            secrets.scan_file(str(path))
        for potenciales in secrets.data.values():
            for potencial in potenciales:
                hallazgos.append(SecretHallazgo(archivo=archivo, tipo=potencial.type, linea=potencial.line_number))

    for idx, linea in enumerate(texto.splitlines(), start=1):
        if _POSTGRES_URL_PATTERN.search(linea):
            hallazgos.append(SecretHallazgo(archivo=archivo, tipo=_POSTGRES_URL_TIPO, linea=idx))
    return hallazgos


async def escanear_paquete(miembros: list[tuple[str, bytes]]) -> list[SecretHallazgo]:
    """Scan every (archivo, contenido) member for secrets.

    Members whose bytes are not valid UTF-8 text are skipped here — the caller
    (`informe_service.generar_zip_evidencias`) is responsible for extracting
    scannable text out of binary evidence files (PDFs, images) before calling
    this function; this function only ever sees decodable text payloads.
    """
    hallazgos: list[SecretHallazgo] = []
    for archivo, contenido in miembros:
        try:
            texto = contenido.decode("utf-8")
        except UnicodeDecodeError:
            continue
        hallazgos.extend(_escanear_texto(archivo, texto))

    if hallazgos:
        await logger.awarning(
            "secret_scan.hallazgos_detectados",
            archivos=sorted({h.archivo for h in hallazgos}),
            tipos=sorted({h.tipo for h in hallazgos}),
        )
    return hallazgos
