"""Local files node — processes uploaded S3 files as evidence (Phase 4)."""

from __future__ import annotations

import asyncio
import io
import uuid

import structlog

from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.local_files")


def _extract_text_from_bytes(data: bytes, filename: str) -> str:
    """Best-effort text extraction from file bytes."""
    fname = filename.lower()

    # Plain text files
    if fname.endswith(".txt") or fname.endswith(".md"):
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    # PDF — try pdfminer if available
    if fname.endswith(".pdf"):
        try:
            from pdfminer.high_level import extract_text_to_fp
            from pdfminer.layout import LAParams

            output = io.StringIO()
            extract_text_to_fp(io.BytesIO(data), output, laparams=LAParams(), output_type="text", codec=None)
            return output.getvalue()
        except Exception:
            return f"[PDF: {filename} — texto no extraíble]"

    # DOCX
    if fname.endswith(".docx"):
        try:
            import zipfile

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                with z.open("word/document.xml") as f:
                    import re

                    xml = f.read().decode("utf-8", errors="replace")
                    text = re.sub(r"<[^>]+>", " ", xml)
                    text = re.sub(r"\s+", " ", text)
                    return text.strip()
        except Exception:
            return f"[DOCX: {filename} — texto no extraíble]"

    return f"[Archivo: {filename} — formato no soportado]"


async def _load_file_from_s3(file_id: uuid.UUID) -> tuple[bytes, str] | None:
    """Try to load file bytes from S3 adapter. Returns (bytes, filename) or None."""
    try:
        from app.adapters.storage import get_storage

        storage = get_storage()
        key = f"uploads/{file_id}"
        data = await storage.download(key)
        return data, f"{file_id}"
    except Exception:
        return None


async def local_files_node(state: AgentState) -> AgentState:
    """Process uploaded local files and extract text for evidence.

    Reads: uploaded_file_ids, _db
    Writes: local_evidence, current_phase
    """
    file_ids: list[uuid.UUID] = state.get("uploaded_file_ids") or []

    if not file_ids:
        return {
            **state,
            "local_evidence": [],
            "current_phase": "local_files",
        }

    tasks = [_load_file_from_s3(fid) for fid in file_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    local_evidence: list[dict] = []
    for file_id, result in zip(file_ids, results):
        if isinstance(result, Exception) or result is None:
            await logger.awarning("local_file_load_failed", file_id=str(file_id))
            continue
        data, filename = result
        text = await asyncio.get_event_loop().run_in_executor(
            None, _extract_text_from_bytes, data, filename
        )
        local_evidence.append({
            "file_id": file_id,
            "filename": filename,
            "text": text[:10000],  # cap at 10K chars
            "size_bytes": len(data),
        })

    await logger.ainfo("local_files_done", processed=len(local_evidence), requested=len(file_ids))

    return {
        **state,
        "local_evidence": local_evidence,
        "current_phase": "local_files",
    }
