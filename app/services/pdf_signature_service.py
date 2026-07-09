"""PDF digital signature (PAdES) via pyhanko.

⚠ LEGAL NOTICE: with the ephemeral self-signed development certificate this
service produces a *technically* valid PAdES signature but WITHOUT legally
recognised validity in Colombia. For legal validity, configure a certificate
issued by an accredited entity (e.g. Certicámara, Andes SCD) through
``PDF_SIGNATURE_CERT_PATH`` / ``PDF_SIGNATURE_KEY_PATH``.

Gated by ``settings.PDF_SIGNATURE_ENABLED`` — callers should check
:func:`firma_activa` before invoking :func:`firmar_pdf`.
"""

from __future__ import annotations

import datetime
import io
import os
import tempfile
import threading

import structlog

from app.core.config import settings

logger = structlog.get_logger("service.pdf_signature")

_signer = None  # cached pyhanko SimpleSigner (built lazily, once per process)
_lock = threading.Lock()

_SIGNATURE_FIELD = "CashInSignature"
_DEFAULT_REASON = "Constancia de cumplimiento contractual — CashIn"


def firma_activa() -> bool:
    """True when PDF signing is enabled by configuration."""
    return settings.PDF_SIGNATURE_ENABLED


def _generate_self_signed(dirpath: str) -> tuple[str, str]:
    """Write an ephemeral self-signed key+cert (PEM) into ``dirpath``; return their paths."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "CashIn Dev (sin validez legal)"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CashIn"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2024, 1, 1))
        .not_valid_after(datetime.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    key_path = os.path.join(dirpath, "signing_key.pem")
    cert_path = os.path.join(dirpath, "signing_cert.pem")
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _build_signer():
    from pyhanko.sign import signers

    cert_path = settings.PDF_SIGNATURE_CERT_PATH
    key_path = settings.PDF_SIGNATURE_KEY_PATH
    if cert_path and key_path:
        passphrase = (
            settings.PDF_SIGNATURE_KEY_PASSPHRASE.encode()
            if settings.PDF_SIGNATURE_KEY_PASSPHRASE
            else None
        )
        logger.info("pdf_signature_cert_configured")
        return signers.SimpleSigner.load(key_path, cert_path, key_passphrase=passphrase)

    # No configured cert → ephemeral self-signed (dev only, NO legal validity).
    logger.warning("pdf_signature_self_signed_dev_cert")
    with tempfile.TemporaryDirectory() as tmp:
        gen_key, gen_cert = _generate_self_signed(tmp)
        return signers.SimpleSigner.load(gen_key, gen_cert)


def _get_signer():
    global _signer
    with _lock:
        if _signer is None:
            _signer = _build_signer()
        return _signer


async def firmar_pdf(pdf_bytes: bytes, reason: str = _DEFAULT_REASON) -> bytes:
    """Return a PAdES-signed copy of ``pdf_bytes``.

    Adds an invisible signature over the whole document using the configured
    (or ephemeral self-signed) certificate. Async because it runs inside the
    request event loop; uses pyhanko's ``async_sign_pdf``. Idempotent per process:
    the signer is built once and reused.
    """
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers

    signer = _get_signer()
    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    meta = signers.PdfSignatureMetadata(field_name=_SIGNATURE_FIELD, reason=reason)
    out = await signers.async_sign_pdf(writer, meta, signer=signer)
    return out.getvalue()
