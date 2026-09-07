"""Regression: real-world filenames rejected with `File type not allowed`.

Production report (2026-09-04, radicar stepper, step 2 "Evidenciar"): a user drops
`10.CERTIFICADO DEPENDIENTE - JACKELINE MAYA SANCHEZ..pdf` (note the DOUBLE DOT
before the extension) onto a per-cuenta checklist requisito and the UI shows
`File type not allowed: 10.CERTIFICADO DEPENDIENTE - JACKELINE MAYA SANCHEZ..pdf`.

The frontend calls `POST /documentos/upload-batch?tipo=otros&cuenta_cobro_id=..&
requisito_codigo=..` with the multipart field `files` (see
cashing-frontend/lib/checklist-api.ts :: uploadRequisitoFiles).

`validate_file_extension` rejects ANY filename containing `..` as a path-traversal
attempt (app/core/file_validation.py). `..` inside a *basename* is not traversal —
only a `..` path SEGMENT is. Windows/scanner exports produce `name..pdf` routinely.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.core.file_validation import sanitize_filename, validate_file_extension
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"

# Verbatim filename from the production report.
REAL_FILENAME = "10.CERTIFICADO DEPENDIENTE - JACKELINE MAYA SANCHEZ..pdf"
_PDF_MAGIC = b"%PDF-1.4\n"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_cuenta(db: AsyncSession, user_id: uuid.UUID) -> CuentaCobro:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-DOTDOT-001",
        objeto="Objeto de prueba",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2025,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)

    # Seed the checklist rows the real cuenta-creation flow always creates (A3:
    # upload_document now propagates a failed checklist link instead of
    # swallowing it).
    from app.services import checklist_service

    await checklist_service.asegurar_checklist(db, cuenta)
    await db.commit()
    return cuenta


class TestValidateFileExtensionDoubleDot:
    """Unit seam — the exact predicate that rejects."""

    def test_double_dot_before_extension_is_accepted(self) -> None:
        assert validate_file_extension(REAL_FILENAME) is True

    def test_sanitized_form_still_ends_in_pdf(self) -> None:
        assert sanitize_filename(REAL_FILENAME).lower().endswith(".pdf")

    def test_traversal_segment_is_still_rejected(self) -> None:
        """The security property the `..` check exists for must survive the fix."""
        assert validate_file_extension("../../etc/passwd.pdf") is False
        assert validate_file_extension("..\\..\\windows\\win.ini.pdf") is False
        assert validate_file_extension("/etc/passwd.pdf") is False

    def test_other_real_world_shapes_are_accepted(self) -> None:
        assert validate_file_extension("informe v2.1 final..pdf") is True
        assert validate_file_extension("ACTA 2025..PDF") is True
        assert validate_file_extension("planilla…resumen..pdf") is True

    def test_intermediate_traversal_segment_is_still_rejected(self) -> None:
        """`..` as a MIDDLE segment (not just a leading one) is still traversal."""
        assert validate_file_extension("a/../b.pdf") is False

    def test_dots_only_basename_with_extension_is_accepted(self) -> None:
        """A basename that is only dots plus an extension (e.g. `..pdf`) has no
        `..` PATH SEGMENT — the whole string is one segment, not two — so it is
        not traversal. Decision: accept it, same as any other filename that still
        resolves to a valid extension after sanitizing."""
        assert validate_file_extension("..pdf") is True


class TestUploadBatchRealFilename:
    """API seam — the exact endpoint + params the stepper sends."""

    async def test_stepper_upload_of_double_dot_pdf_succeeds(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"files": (REAL_FILENAME, _PDF_MAGIC + b"x" * 2048, "application/pdf")},
            )

        assert r.status_code == 201, r.text
        assert "Tipo de archivo no permitido" not in r.text

    async def test_control_same_pdf_without_double_dot_succeeds(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Control: identical bytes/content-type, single dot — this is the
        'FICHA TECNICA.pdf uploaded fine' case from the report."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload-batch",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"files": ("FICHA TECNICA.pdf", _PDF_MAGIC + b"x" * 2048, "application/pdf")},
            )

        assert r.status_code == 201, r.text

    async def test_single_upload_endpoint_has_the_same_defect(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """`POST /documentos/upload` (mobile + single-file callers) shares the check."""
        cuenta = await _crear_cuenta(db, test_user["user"].id)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"file": (REAL_FILENAME, _PDF_MAGIC + b"x" * 2048, "application/pdf")},
            )

        assert r.status_code == 201, r.text


class TestStepperMultiFileRateLimit:
    """Second production defect found while diagnosing the same report.

    `checklist-full-view.tsx :: handleFiles` fires ONE request per dropped file, in
    parallel, all against `POST /documentos/upload-batch` — which carries
    `@limiter.limit("3/minute")` keyed by REMOTE IP (app/api/v1/documentos.py:179,
    app/core/rate_limit.py). Dropping 4+ files at once 429s the 4th onwards, and the
    429 body is `{"error": ...}` (not `{"detail": ...}`), so the client's
    `formatErrorDetail(err.response.data.detail)` misses it and the user sees the raw
    axios string "Request failed with status code 429".
    """

    async def test_four_files_dropped_at_once_are_all_accepted(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.core.rate_limit import limiter

        cuenta = await _crear_cuenta(db, test_user["user"].id)
        limiter.enabled = True
        limiter.reset()
        statuses: list[int] = []
        try:
            with (
                patch(_PATCH_S3) as mock_storage_cls,
                patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
            ):
                mock_storage_cls.return_value = _mock_storage()
                for i in range(4):
                    r = await client.post(
                        "/api/v1/documentos/upload-batch",
                        headers=test_user["headers"],
                        params={
                            "tipo": "otros",
                            "cuenta_cobro_id": str(cuenta.id),
                            "requisito_codigo": "EVIDENCIAS",
                        },
                        files={"files": (f"soporte-{i}.pdf", _PDF_MAGIC + bytes([i]) * 2048, "application/pdf")},
                    )
                    statuses.append(r.status_code)
        finally:
            limiter.enabled = False

        assert 429 not in statuses, f"rate-limited mid-drop: {statuses}"


class TestEvidenceValidatorSharesTheDefect:
    """The same predicate is duplicated in the permissive evidence validator
    (app/core/file_validation.py:281), so stepper step 4 "Evidencias" rejects the
    same file with a different message: `Nombre de archivo no permitido: <name>`.
    """

    def test_evidence_upload_accepts_double_dot_filename(self) -> None:
        from app.core.exceptions import ValidationError
        from app.core.file_validation import validate_evidence_file

        content = _PDF_MAGIC + b"x" * 4096
        try:
            validate_evidence_file(REAL_FILENAME, len(content), "application/pdf", content)
        except ValidationError as exc:  # pragma: no cover - the bug
            pytest.fail(f"evidence validator rejected a legitimate filename: {exc}")

    def test_evidence_validator_still_rejects_traversal(self) -> None:
        from app.core.exceptions import ValidationError
        from app.core.file_validation import validate_evidence_file

        content = _PDF_MAGIC + b"x" * 4096
        with pytest.raises(ValidationError):
            validate_evidence_file("../../etc/passwd.pdf", len(content), "application/pdf", content)
