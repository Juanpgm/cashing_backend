"""Diagnose document extraction: PDF text + LLM obligation parsing."""
import asyncio
import sys

PDF_PATH = r"A:\SEGUIMIENTO SECRE PRIVADA\CUOTA 1\2.Contrato Secop II.pdf"


def test_pdf_extraction():
    from app.agent.tools.document_parser import parse_pdf

    with open(PDF_PATH, "rb") as f:
        content = f.read()

    print(f"File size: {len(content)} bytes")
    texto = parse_pdf(content)
    print(f"Extracted text length: {len(texto)} chars")
    print("--- First 500 chars ---")
    print(texto[:500])
    print("--- Obligation keywords ---")
    texto_upper = texto.upper()
    for kw in [
        "OBLIGACIONES DEL CONTRATISTA",
        "OBLIGACIONES ESPECIFICAS",
        "OBLIGACIONES ESPECÍFICAS",
        "OBLIGACIONES GENERALES",
        "OBLIGACION",
        "OBLIGACIÓN",
    ]:
        count = texto_upper.count(kw.upper())
        if count:
            print(f'  "{kw}" found {count} times')
    return texto


async def test_llm_extraction(texto: str):
    from app.adapters.llm import get_llm
    from app.agent.prompts.obligaciones import OBLIGACIONES_SYSTEM, OBLIGACIONES_USER
    from app.core.config import settings
    from app.services.document_service import (
        _extract_obligation_sections,
        _parse_obligaciones_llm,
    )
    from app.schemas.agent import LLMMessage

    model = settings.LLM_EXTRACTION_MODEL or settings.LLM_DEFAULT_MODEL
    print(f"\nLLM model: {model}")

    chunks = _extract_obligation_sections(texto)
    print(f"Obligation chunks: {len(chunks)}")
    for i, chunk in enumerate(chunks):
        print(f"  Chunk {i}: {len(chunk)} chars")

    if not chunks:
        print("ERROR: No chunks to process!")
        return

    # Test first chunk
    chunk = chunks[0]
    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    messages = [
        LLMMessage(role="system", content=OBLIGACIONES_SYSTEM),
        LLMMessage(role="user", content=OBLIGACIONES_USER.format(texto_contrato=chunk)),
    ]

    print(f"\nSending chunk 0 ({len(chunk)} chars) to LLM...")
    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=4096)
        print(f"LLM response: {len(resp.content)} chars, {resp.total_tokens} tokens")
        print("--- Raw response (first 1000 chars) ---")
        print(resp.content[:1000])
        print("--- Parsed obligations ---")
        parsed = _parse_obligaciones_llm(resp.content)
        print(f"Parsed: {len(parsed)} obligations")
        for ob in parsed[:5]:
            print(f"  [{ob.tipo}] {ob.descripcion[:80]}")
        if len(parsed) > 5:
            print(f"  ... and {len(parsed) - 5} more")
    except Exception as e:
        print(f"LLM FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=== PDF Extraction Test ===")
    texto = test_pdf_extraction()
    if not texto:
        print("FATAL: No text extracted from PDF!")
        sys.exit(1)
    print(f"\n=== LLM Obligation Extraction Test ===")
    asyncio.run(test_llm_extraction(texto))
