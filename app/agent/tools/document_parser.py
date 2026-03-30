"""Document parser tool — extract text from PDF, DOCX, XLSX."""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger("agent.tools.document_parser")


def parse_pdf(content: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    import io

    import pdfplumber

    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def parse_docx(content: bytes) -> str:
    """Extract text from DOCX bytes using python-docx."""
    import io

    import docx

    doc = docx.Document(io.BytesIO(content))
    return "\n".join(para.text for para in doc.paragraphs if para.text.strip())


def parse_xlsx(content: bytes) -> str:
    """Extract text from XLSX bytes using openpyxl."""
    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    text_parts: list[str] = []
    for ws in wb.worksheets:
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            text_parts.append(f"[{ws.title}]\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(text_parts)


def parse_document(content: bytes, filename: str) -> str:
    """Auto-detect format and extract text."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return parse_pdf(content)
    if ext == ".docx":
        return parse_docx(content)
    if ext in (".xlsx", ".xls"):
        return parse_xlsx(content)
    msg = f"Unsupported file format: {ext}"
    raise ValueError(msg)
