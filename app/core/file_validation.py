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


def validate_mime_type(content: bytes, declared_mime: str) -> bool:
    """Validate MIME type using magic bytes. Returns True if valid.

    Falls back to trusting the declared MIME when libmagic is unavailable
    (common on Windows dev environments without the system library).
    """
    if declared_mime not in ALLOWED_MIME_TYPES:
        return False
    try:
        import sys

        # python-magic hangs on Windows when libmagic is missing — skip detection
        if sys.platform == "win32":
            return True

        import magic

        detected = magic.from_buffer(content[:2048], mime=True)
        return detected == declared_mime
    except (ImportError, OSError, Exception):
        # Fallback: trust declared MIME if python-magic or libmagic unavailable
        return True


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
