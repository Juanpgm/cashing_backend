"""File upload validation: MIME type, extension, size."""

import re

ALLOWED_MIME_TYPES: dict[str, list[str]] = {
    "application/pdf": [".pdf"],
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [".pptx"],
    "application/vnd.ms-excel": [".xls"],
    "text/plain": [".txt"],
}

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB

# Safe filename pattern: alphanumeric, hyphens, underscores, dots
SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-][a-zA-Z0-9_\-\.]*$")


def sanitize_filename(filename: str) -> str:
    """Strip path components and unsafe characters from filename."""
    # Remove path separators
    name = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    # Remove anything non-safe
    name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name)
    # Prevent double extensions like .pdf.exe
    parts = name.rsplit(".", maxsplit=1)
    if len(parts) == 2:
        base = parts[0].replace(".", "_")
        return f"{base}.{parts[1]}"
    return name


_MAGIC_SIGNATURES: dict[str, list[bytes]] = {
    "application/pdf": [b"%PDF"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/png": [b"\x89PNG"],
    "application/zip": [b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [b"PK\x03\x04"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [b"PK\x03\x04"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [b"PK\x03\x04"],
    "application/msword": [b"\xd0\xcf\x11\xe0"],
    "text/plain": [],  # No magic bytes for plain text — accept any content
    "text/html": [],
    "text/csv": [],
}


def validate_mime_type(content: bytes, declared_content_type: str) -> bool:
    """Validate file content matches declared MIME type using magic bytes.

    Uses libmagic when available. Falls back to manual magic-byte signatures
    for common types so validation still runs on platforms without libmagic.
    """
    # Normalize declared type (strip parameters like charset)
    base_type = declared_content_type.split(";")[0].strip().lower()

    if base_type not in ALLOWED_MIME_TYPES:
        return False

    signatures = _MAGIC_SIGNATURES.get(base_type)
    if signatures is None:
        # Unknown type not in our signatures map — try libmagic, permissive fallback
        try:
            import magic

            detected = magic.from_buffer(content[:2048], mime=True)
            return detected == base_type
        except (ImportError, OSError, Exception):
            return True  # Can't validate — allow through

    # Empty signature list means we accept any content for this type (e.g. text/plain)
    if not signatures:
        return True

    # Try libmagic first for precise detection; fall back to manual magic bytes
    try:
        import magic

        detected = magic.from_buffer(content[:2048], mime=True)
        return detected == base_type
    except (ImportError, OSError, Exception):
        return any(content[: len(sig)] == sig for sig in signatures)


def get_safe_filename(filename: str) -> str:
    """Return a storage-safe version of the filename (ASCII, no spaces).

    Replaces spaces and special characters with underscores, preserving the
    original extension. Used to build storage keys that are safe across all
    object-storage providers.
    """
    name, _, ext = filename.rpartition(".")
    if not name:
        name, ext = ext, ""
    safe_name = re.sub(r"[^\w\-]", "_", name)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_") or "documento"
    return f"{safe_name}.{ext}" if ext else safe_name


def validate_file_extension(filename: str) -> bool:
    """Check if filename has an allowed extension. Sanitizes first so display names with spaces are accepted."""
    # Reject path traversal attempts before sanitizing
    if ".." in filename or filename.startswith("/"):
        return False
    # Use the sanitized form for extension check — original names with spaces/accents are fine
    safe = sanitize_filename(filename)
    lower = safe.lower()
    for extensions in ALLOWED_MIME_TYPES.values():
        for ext in extensions:
            if lower.endswith(ext):
                return True
    return False


def validate_file_size(size_bytes: int) -> bool:
    """Check file doesn't exceed max size."""
    return 0 < size_bytes <= MAX_FILE_SIZE_BYTES
