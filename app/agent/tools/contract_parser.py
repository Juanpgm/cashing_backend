"""Shared contract parsing utilities — used by both agent nodes and document service."""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import ValidationError

from app.schemas.agent import (
    ContratoCamposLLM,
    ObligacionesLLMList,
    ObligacionExtraida,
    ObligacionItemLLM,
)

# Lenient: accepts accented/unaccented OBLIGACION, optional markdown bold,
# leading numbering ("1. ", "1) "), bullets ("- ", "* "), and whitespace.
# Supports both old 3-field format (|tipo|desc) and new 4-field format (|tipo|etiqueta|desc).
OBLIGACION_RE = re.compile(
    r"^(?:\d+[.)\-]\s*)?(?:[\-\*]\s*)?\*{0,2}OBLIGACI[OÓ]N\*{0,2}\s*\|\s*(general|espec[ií]fica)\s*\|(.+)$",
    re.IGNORECASE,
)
# Regex for pipe-delimited CAMPO lines from contract metadata extraction
CAMPO_RE = re.compile(r"^\*{0,2}CAMPO\*{0,2}\s*\|\s*(\w+)\s*\|\s*(.+)$", re.IGNORECASE)

# Valid field names for contract metadata extraction
CAMPO_VALID_FIELDS = {
    "numero_contrato",
    "objeto",
    "valor_total",
    "valor_mensual",
    "fecha_inicio",
    "fecha_fin",
    "supervisor_nombre",
    "cargo_supervisor",
    "entidad",
    "dependencia",
    "documento_proveedor",
    "pais",
    "departamento",
    "ciudad",
    "direccion_ejecucion",
}

# Max chars per LLM call for obligation extraction.
# ~3-4 chars per token; 8000 chars ≈ 2500 tokens + prompt ≈ 4000 total.
MAX_CHUNK_CHARS = 8_000
# Overlap between chunks to avoid cutting mid-clause
CHUNK_OVERLAP = 500
# Bound for the whole-text-as-single-chunk fallback (no section header found,
# even OCR-tolerant) — generous enough for current vision/text models' context
# windows alongside the system prompt + few-shots + completion budget.
# ponytail: fixed bound, not a real per-model context lookup — upgrade to a
# sliding multi-chunk fallback if evidence shows obligations sections beyond
# this bound are being missed on very long noisy documents.
WHOLE_TEXT_FALLBACK_MAX_CHARS = MAX_CHUNK_CHARS * 4

# Keywords that signal the specific-obligations section (tier 1 = preferred).
# If tier-1 keywords are found we ONLY use those sections so the LLM never
# sees the "obligaciones generales" text and can't confuse them.
# Covers both "OBLIGACIONES ESPECÍFICAS" and "ACTIVIDADES ESPECÍFICAS" framings,
# which are semantically equivalent in Colombian public contracts.
OBLIGACION_SECTION_KW_TIER1 = [
    "OBLIGACIONES ESPECIFICAS",
    "OBLIGACIONES ESPECÍFICAS",
    "OBLIGACIONES ESPECIFICAS DEL CONTRATISTA",
    "OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA",
    "ACTIVIDADES ESPECIFICAS",
    "ACTIVIDADES ESPECÍFICAS",
    "ACTIVIDADES ESPECIFICAS DEL CONTRATISTA",
    "ACTIVIDADES ESPECÍFICAS DEL CONTRATISTA",
    "ACTIVIDADES ESPECIFICAS DEL OBJETO",
    "ACTIVIDADES ESPECÍFICAS DEL OBJETO",
    "ACTIVIDADES ESPECIFICAS DEL CONTRATO",
    "ACTIVIDADES ESPECÍFICAS DEL CONTRATO",
]
# Broader fallback (tier 2) — used only when tier-1 finds nothing.
# May contain specific obligations mixed with general duties; the LLM prompt
# filters out the general ones.
OBLIGACION_SECTION_KW_TIER2 = [
    "OBLIGACIONES DEL CONTRATISTA",
    "CLAUSULA DE OBLIGACIONES",
    "CLÁUSULA DE OBLIGACIONES",
    "OBLIGACIONES Y RESPONSABILIDADES",
    "OBLIGACIONES PARTICULARES",
    "OBLIGACIONES ESPECIALES",
    "OBLIGACIONES CONTRACTUALES",
    "ACTIVIDADES DEL CONTRATO",
    "ACTIVIDADES DEL CONTRATISTA",
    "ACTIVIDADES A DESARROLLAR",
    "ACTIVIDADES Y OBLIGACIONES",
    "RESPONSABILIDADES DEL CONTRATISTA",
    "FUNCIONES DEL CONTRATISTA",
    "COMPROMISOS DEL CONTRATISTA",
    "ALCANCE DEL CONTRATO",
    "OBJETO DEL CONTRATO",
    "ALCANCE DEL TRABAJO",
]


def _keyword_pattern(kw: str) -> re.Pattern[str]:
    """Compile a section keyword into an OCR-tolerant regex.

    Scanned/OCR'd text often flattens or mangles the whitespace between the
    words of a header (e.g. "OBLIGACIONESESPECIFICASDELCONTRATISTA" with no
    spaces at all, or irregular spacing). Escaping the keyword and swapping
    internal spaces for ``\\s*`` matches the header whether the spacing
    survived, was flattened, or was doubled — no fuzzy-matching dependency
    needed. Case is handled by matching against upper-cased text; accent loss
    is covered by both accented/unaccented literal keyword forms already
    present in ``OBLIGACION_SECTION_KW_TIER1``/``TIER2``.
    """
    return re.compile(r"\s*".join(re.escape(word) for word in kw.split(" ")))


_OBLIGACION_SECTION_PATTERNS_TIER1 = [_keyword_pattern(kw) for kw in OBLIGACION_SECTION_KW_TIER1]
_OBLIGACION_SECTION_PATTERNS_TIER2 = [_keyword_pattern(kw) for kw in OBLIGACION_SECTION_KW_TIER2]


def extract_obligation_sections(texto: str) -> list[str]:
    """Extract ALL obligation-rich sections from the contract text.

    Scans for every occurrence of every section keyword (OCR-tolerant: flattened
    or irregular spacing between header words still matches) and builds windows
    around each. Overlapping windows are merged. Returns a list of text chunks
    to process independently.

    Uses a two-tier keyword strategy:
      tier-1 = "OBLIGACIONES ESPECÍFICAS" — preferred, uses ONLY these sections.
      tier-2 = broader keywords — used only when tier-1 finds nothing.
    This prevents the LLM from seeing "obligaciones generales" text.

    Fallback: when NO header (even OCR-tolerant) is found anywhere in the text —
    e.g. OCR mangling broke individual letters, not just spacing — returns the
    whole text as a SINGLE bounded chunk instead of blind fixed-size slices, so
    the section-aware LLM prompt (with its noisy/OCR few-shot example) is the
    safety net instead of disconnected fragments with no section context.
    """
    texto_upper = texto.upper()

    def _find_ranges(patterns: list[re.Pattern[str]]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(texto_upper):
                idx = m.start()
                start = max(0, idx - 300)
                end = min(len(texto), idx + MAX_CHUNK_CHARS * 2)
                ranges.append((start, end))
        return ranges

    ranges = _find_ranges(_OBLIGACION_SECTION_PATTERNS_TIER1)
    if not ranges:
        ranges = _find_ranges(_OBLIGACION_SECTION_PATTERNS_TIER2)

    if not ranges:
        return [texto[:WHOLE_TEXT_FALLBACK_MAX_CHARS]]

    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))

    final_chunks: list[str] = []
    for s, e in merged:
        segment = texto[s:e]
        if len(segment) <= MAX_CHUNK_CHARS:
            final_chunks.append(segment)
        else:
            pos = 0
            while pos < len(segment):
                final_chunks.append(segment[pos : pos + MAX_CHUNK_CHARS])
                pos += MAX_CHUNK_CHARS - CHUNK_OVERLAP
                if pos + CHUNK_OVERLAP >= len(segment):
                    break

    return final_chunks


def parse_obligaciones_llm(response: str) -> list[ObligacionExtraida]:
    """Parse pipe-delimited OBLIGACION lines from LLM output.

    Tolerant to: leading/trailing whitespace, markdown bold markers (**OBLIGACION**),
    extra spaces around pipes, mixed case tipo values, accented characters,
    leading numbering/bullets, and markdown code fences.
    """
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[^\n]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    result: list[ObligacionExtraida] = []
    orden = 0
    for line in cleaned.splitlines():
        m = OBLIGACION_RE.match(line.strip())
        if m:
            tipo_raw = m.group(1).lower().strip().replace("í", "i")
            rest = m.group(2)  # everything after |tipo|
            if tipo_raw != "especifica":
                continue
            # Support both formats:
            #   4-field (new): |tipo|etiqueta|descripcion
            #   3-field (old): |tipo|descripcion
            parts = rest.split("|", 1)
            if len(parts) == 2:
                etiqueta = parts[0].strip()
                descripcion = parts[1].strip().rstrip(".")
            else:
                etiqueta = ""
                descripcion = parts[0].strip().rstrip(".")
            if descripcion and len(descripcion) > 5:
                result.append(
                    ObligacionExtraida(descripcion=descripcion, tipo=tipo_raw, orden=orden, etiqueta=etiqueta)
                )
                orden += 1
    return result


def parse_campos_llm(response: str) -> dict[str, str]:
    """Parse pipe-delimited CAMPO lines from LLM output into a dict."""
    result: dict[str, str] = {}
    for line in response.splitlines():
        m = CAMPO_RE.match(line.strip())
        if m:
            field_name = m.group(1).lower().strip()
            value = m.group(2).strip()
            if field_name in CAMPO_VALID_FIELDS and value:
                result[field_name] = value
    return result


# ── Structured (JSON) parsing — structured-first, regex fallback ────────────


def obligacion_items_to_extraidas(items: list[ObligacionItemLLM]) -> list[ObligacionExtraida]:
    """Convert structured obligation items to ObligacionExtraida.

    Keeps only ``especifica`` obligations and drops noise (<= 5 chars), matching
    the behaviour of the legacy ``parse_obligaciones_llm`` regex parser.
    """
    result: list[ObligacionExtraida] = []
    orden = 0
    for it in items:
        tipo = it.tipo.lower().strip().replace("í", "i")
        descripcion = it.descripcion.strip().rstrip(".")
        if tipo != "especifica":
            continue
        if descripcion and len(descripcion) > 5:
            result.append(
                ObligacionExtraida(descripcion=descripcion, tipo="especifica", orden=orden, etiqueta=it.etiqueta)
            )
            orden += 1
    return result


def parse_campos_structured(raw: str) -> dict[str, str]:
    """Parse contract metadata from a structured JSON response.

    Falls back to the legacy pipe-delimited parser when the response is not
    valid JSON (e.g. a non-structured fallback model in the chain).
    """
    try:
        campos = ContratoCamposLLM.model_validate_json(raw)
    except ValidationError:
        return parse_campos_llm(raw)
    return {k: v for k, v in campos.model_dump().items() if v}


def parse_obligaciones_structured(raw: str) -> list[ObligacionExtraida]:
    """Parse obligations from a structured JSON response.

    Falls back to the legacy pipe-delimited parser when the response is not
    valid JSON.
    """
    try:
        parsed = ObligacionesLLMList.model_validate_json(raw)
    except ValidationError:
        return parse_obligaciones_llm(raw)
    return obligacion_items_to_extraidas(parsed.obligaciones)


# ── Page-furniture stripper (entity-agnostic header/footer boilerplate) ─────
#
# Scanned SECOP contracts often interleave running header/footer text (entity
# address, NIT, phone/fax/email/web, page markers, entity banners) into the
# OCR transcription, sometimes glued mid-word onto real content when a page
# break lands inside a sentence. Left uncleaned, this text (a) leaks into the
# obligation description, and (b) can hide the next item's enumeration marker
# from the line-based splitter (`_split_items`), merging two obligations into
# one. `strip_page_furniture` removes only LABELED fields (never unlabeled
# prose/banners — too risky to strip generically) and turns real page markers
# into a newline, since a page marker IS the page boundary: reinstating a line
# break there lets `_split_items` recover a marker the page break glued onto
# the previous line.

# Anchored on the label token itself (no leading \b) so OCR joins like
# "FLORESTATELEFONO:" still match — only the label + its value are removed,
# never the word it happens to be glued to. Value char classes exclude "\n" so
# a run never swallows unrelated content on a following line.
_FURNITURE_PHONE_RE = re.compile(r"TEL[EÉ]FONOS?\s*:?\s*[\d \t.\-()/]+", re.IGNORECASE)
_FURNITURE_FAX_RE = re.compile(r"FAX\s*:?\s*[\d \t.\-()/]+", re.IGNORECASE)
_FURNITURE_EMAIL_RE = re.compile(r"E-?MAIL\s*:?\s*\S+@\S+", re.IGNORECASE)
_FURNITURE_WEB_RE = re.compile(r"WEB\s*:?\s*(?:https?://)?www\.\S+", re.IGNORECASE)
# Colombian NIT: NNN.NNN.NNN-D (check digit is always exactly one digit — the
# `(?!\d)` keeps a glued following digit, e.g. a swallowed enumeration marker,
# from being absorbed into the match).
_FURNITURE_NIT_RE = re.compile(r"NIT\.?\s*:?\s*\d{1,3}(?:[.\s]\d{3}){0,3}-\d(?!\d)", re.IGNORECASE)
_FURNITURE_INLINE_PATTERNS = (
    _FURNITURE_PHONE_RE,
    _FURNITURE_FAX_RE,
    _FURNITURE_EMAIL_RE,
    _FURNITURE_WEB_RE,
    _FURNITURE_NIT_RE,
)
# Explicit "Página X de Y" / "Pág. X/Y" page markers — always a real page boundary.
_FURNITURE_PAGE_LABELED_RE = re.compile(r"P[áa]g(?:ina)?\.?\s*\d{1,3}\s*(?:de|/)\s*\d{1,3}", re.IGNORECASE)
# Bare "3 de 6" marker. Requires a digit IMMEDIATELY after "de" (no words in
# between) so real prose like "3 de los 6 comités" never matches. Only applied
# once at least one other footer signal was found in the text ("a detected
# footer run"), to avoid false positives on unrelated bare number pairs.
_FURNITURE_PAGE_BARE_RE = re.compile(r"\b\d{1,3}\s+de\s+\d{1,3}\b", re.IGNORECASE)

# Strong footer signature used to reject a polluted verbatim result (Fix B).
_FOOTER_SIGNATURE_RE = re.compile(
    r"\d{3}\.\d{3}\.\d{3}-\d|\S+@\S+\.\S+|TEL[EÉ]FONOS?\s*:|FAX\s*:|E-?MAIL\s*:|WEB\s*:|NIT\.?\s*:",
    re.IGNORECASE,
)


def strip_page_furniture(texto: str) -> str:
    """Strip entity-agnostic page header/footer boilerplate from contract text.

    Removes LABELED contact/identification fields (teléfono, fax, e-mail, web,
    NIT) and explicit page markers ("Página X de Y", bare "3 de 6"). Page
    markers are replaced with a newline instead of a space so the line-based
    obligation splitter can recover an item marker a flattened page break
    glued onto the previous line.

    Deliberately conservative: does NOT touch unlabeled prose, ALL-CAPS entity
    banners, or slogans — a false positive there would delete real obligation
    wording. That residual noise is left to the hardened LLM prompt (see
    ``app/agent/prompts/obligaciones.py``), which also receives text already
    cleaned by this function.
    """
    if not texto:
        return texto
    cleaned = texto
    removed_any = False
    for pattern in _FURNITURE_INLINE_PATTERNS:
        cleaned, n = pattern.subn(" ", cleaned)
        removed_any = removed_any or n > 0
    cleaned, n = _FURNITURE_PAGE_LABELED_RE.subn("\n", cleaned)
    removed_any = removed_any or n > 0
    if removed_any:
        cleaned, n = _FURNITURE_PAGE_BARE_RE.subn("\n", cleaned)
        removed_any = removed_any or n > 0
    if not removed_any:
        return texto
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*", "\n", cleaned)
    return cleaned.strip()


# ── Verbatim (deterministic) obligation extractor ──────────────────────────

# Section headers that mark the END of the specific-obligations block.
_END_SECTION_KW = [
    # Generic "CLÁUSULA X — ..." marker (any subsequent clause closes the block)
    "CLAUSULA ",
    "CLÁUSULA ",
    "OBLIGACIONES GENERALES",
    "OBLIGACIONES DEL CONTRATANTE",
    "OBLIGACIONES DE LA CONTRATANTE",
    "OBLIGACIONES DE LA ENTIDAD",
    "OBLIGACIONES DEL SUPERVISOR",
    "VALOR DEL CONTRATO",
    "VALOR Y FORMA DE PAGO",
    "FORMA DE PAGO",
    "PLAZO DE EJECUCION",
    "PLAZO DE EJECUCIÓN",
    "DURACION DEL CONTRATO",
    "DURACIÓN DEL CONTRATO",
    "SUPERVISION",
    "SUPERVISIÓN",
    "GARANTIAS",
    "GARANTÍAS",
    "CESION",
    "CESIÓN",
    "TERMINACION",
    "TERMINACIÓN",
    "INHABILIDADES",
    "CONFIDENCIALIDAD",
    "PROPIEDAD INTELECTUAL",
    "INDEMNIDAD",
    "DOMICILIO",
    "PERFECCIONAMIENTO",
    "REQUISITOS DE EJECUCION",
    "REQUISITOS DE EJECUCIÓN",
]

# Enumeration patterns at the start of an item:
#   "1. ", "1) ", "1.- ", "1.) "
#   "a) ", "A. ", "i) " (roman numerals up to xx)
#   "- ", "• ", "* "
_ENUM_RE = re.compile(
    r"^\s*(?:"
    r"(?P<num>\d{1,3})\s*[.)\-º°]+\s+"
    r"|(?P<alpha>[A-Za-z])\s*[.)]\s+"
    r"|(?P<roman>(?:i{1,3}|iv|v|vi{1,3}|ix|x{1,3}|xl|l|lx{1,3}|xc|c))\s*[.)]\s+"
    r"|[\-•·*]\s+"
    r")(?P<body>\S.*)$",
    re.IGNORECASE,
)


def _candidate_starts(texto_upper: str) -> list[int]:
    """Return offsets right AFTER every obligation-section header, tier-1 first.

    Unlike a first-match lookup, this yields ALL candidate sections so the caller
    can skip false positives (e.g. a header term mentioned in prose with no list)
    and fall through to the section that actually contains an enumerated list.
    Tier-1 ("OBLIGACIONES/ACTIVIDADES ESPECÍFICAS") candidates come before tier-2.
    """
    starts: list[int] = []
    for tier in (OBLIGACION_SECTION_KW_TIER1, OBLIGACION_SECTION_KW_TIER2):
        tier_offsets: set[int] = set()
        for kw in tier:
            pos = 0
            while True:
                idx = texto_upper.find(kw, pos)
                if idx == -1:
                    break
                tier_offsets.add(idx + len(kw))
                pos = idx + len(kw)
        starts.extend(sorted(tier_offsets))
    return starts


def _find_section_end(texto: str, start: int) -> int:
    """Find the end of the obligations block (start of the next major section).

    Only UPPERCASE heading occurrences count as boundaries, so a lowercase
    mention inside an obligation (e.g. "…asignadas por la supervisión…") never
    truncates the list. Precise trimming at the catch-all is left to
    ``_split_items``; this only needs to bound the block generously.
    """
    upper = texto.upper()
    end = len(texto)
    for kw in _END_SECTION_KW:
        pos = start
        while True:
            idx = upper.find(kw, pos)
            if idx == -1:
                break
            if texto[idx : idx + len(kw)] == kw:  # uppercase heading, not inline mention
                end = min(end, idx)
                break
            pos = idx + len(kw)
    return end


def _is_catch_all(text: str) -> bool:
    """Return True when text is a catch-all closing clause ('Las demás actividades…')."""
    t = text.lower()
    return "las dem" in t and any(w in t for w in ("asign", "encomiend", "relacionen", "correspondan"))


# Ordinal / clause headings that start the NEXT clause (Spanish public contracts).
_HEADING_RE = re.compile(
    r"^(?:CL[ÁA]USULA\b|PAR[ÁA]GRAFO\b|"
    r"(?:PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|S[ÉE]PTIMA|OCTAVA|NOVENA|"
    r"D[ÉE]CIMA|UND[ÉE]CIMA|DUOD[ÉE]CIMA|VIG[ÉE]SIMA)\b)"
)


def _is_section_break(line: str) -> bool:
    """Return True when a line is an UPPERCASE clause heading (next section start).

    Distinguishes a real heading ("SÉPTIMA.", "PARÁGRAFO:", "VALOR DEL CONTRATO")
    from a lowercase mention inside an obligation, so the catch-all item is not
    contaminated with the text that follows the list.
    """
    upper = line.upper()
    for kw in _END_SECTION_KW:
        if upper.startswith(kw) and line[: len(kw)].isupper():
            return True
    m = _HEADING_RE.match(line)
    return bool(m and line[: m.end()].isupper())


# Inline (mid-line) UPPERCASE clause headings. Government PDFs often flatten
# "…del contrato. SEXTA. Actividades…" onto a single line, hiding the boundary
# between clauses from a line-based splitter. The trailing "[.\s:]" keeps it to
# heading-like tokens (e.g. "SEXTA.", "PARÁGRAFO:"), and case-sensitive literals
# ensure only real UPPERCASE headings match — never lowercase prose.
_INLINE_HEADING_RE = re.compile(
    r"\s+(?="
    r"(?:CL[ÁA]USULA|PAR[ÁA]GRAFO|"
    r"PRIMERA|SEGUNDA|TERCERA|CUARTA|QUINTA|SEXTA|S[ÉE]PTIMA|OCTAVA|NOVENA|"
    r"D[ÉE]CIMA|UND[ÉE]CIMA|DUOD[ÉE]CIMA|VIG[ÉE]SIMA)"
    r"[.\s:])"
)


def _break_headings(block: str) -> str:
    """Put inline UPPERCASE clause headings on their own line.

    Lets the line-based splitter stop the obligations list at the next clause
    even when the PDF flattened the heading mid-line.
    """
    return _INLINE_HEADING_RE.sub("\n", block)


def _normalize_block(block: str) -> str:
    """De-hyphenate words split across line wraps (a common PDF artifact)."""
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", block)


def _reenumerate_flattened(block: str) -> str:
    """Re-insert line breaks before inline enumeration markers the PDF flattened.

    Only markers forming an increasing sequence starting at ``1`` / ``a`` AND
    followed by a capitalised word are treated as list items, so legal
    references like ``Ley 80.`` or stray figures are never mistaken for markers.
    Returns the block unchanged when no such sequence is found.
    """
    patterns: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], int]], ...] = (
        (re.compile(r"(?<![\w.,])(\d{1,2})[.)]\s+(?=[\"“'(]?[A-ZÁÉÍÓÚÑ])"), lambda m: int(m.group(1))),
        # Letter markers "A) B) C)" or "a) b) c)" — common in SECOP II contracts where the
        # PDF flattens them inline. Case-insensitive ("A"=1, "a"=1); requires ")" (not ".")
        # to avoid sentence-ending abbreviations being read as markers.
        (
            re.compile(r"(?<![\wÁÉÍÓÚÑáéíóúñ])([A-Za-z])\)\s+(?=[\"“'(]?[A-ZÁÉÍÓÚÑ])"),
            lambda m: ord(m.group(1).upper()) - 64,
        ),
    )
    for pattern, value_of in patterns:
        positions: list[int] = []
        expected = 1
        for m in pattern.finditer(block):
            if value_of(m) == expected:
                positions.append(m.start())
                expected += 1
        if len(positions) >= 2:
            out: list[str] = []
            prev = 0
            for p in positions:
                out.append(block[prev:p].rstrip())
                out.append("\n")
                prev = p
            out.append(block[prev:])
            return "".join(out)
    return block


def _split_items(block: str) -> list[tuple[str, str]]:
    """Split an obligations block into ``(marker, text)`` items.

    Walks the block line by line, accumulating text until the next enumeration
    marker (handles items that wrap across lines). The marker is the raw label
    from the contract (e.g. "A", "1", "a", "iii"); bullets/dashes yield "".
    Accumulation stops once the catch-all closer ("Las demás actividades…") has
    been emitted — everything after it is general-obligations text.
    """
    items: list[tuple[str, str]] = []
    current_lines: list[str] = []
    current_marker: list[str] = [""]  # single-element list so the closure can mutate it

    def _flush() -> None:
        if not current_lines:
            return
        joined = " ".join(s.strip() for s in current_lines if s.strip())
        joined = re.sub(r"\s+", " ", joined).strip()
        joined = joined.rstrip(" .;,:-")
        if len(joined) >= 10:
            items.append((current_marker[0], joined))
        current_lines.clear()

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Stop at the next clause heading so the catch-all item is not polluted
        # with the text that follows the obligations list.
        if (items or current_lines) and _is_section_break(line):
            _flush()
            break
        m = _ENUM_RE.match(line)
        if m:
            _flush()
            if items and _is_catch_all(items[-1][1]):
                break
            current_marker[0] = m.group("num") or m.group("alpha") or m.group("roman") or ""
            current_lines.append(m.group("body").strip())
        elif current_lines:
            current_lines.append(line)
    _flush()

    # Fallback: the catch-all may have merged into the preceding item's body
    # (marker without punctuation that _ENUM_RE didn't split). Drop everything
    # after the first item that contains the catch-all pattern.
    for i, (_, text) in enumerate(items):
        if _is_catch_all(text):
            del items[i + 1 :]
            break

    return items


# A single "item" longer than this is almost certainly an un-split block whose
# internal markers were lost — reject it so the caller escalates to the LLM.
_MAX_SINGLE_ITEM_CHARS = 400


def _extract_items_from_block(block: str) -> list[tuple[str, str]]:
    """Extract obligation items from one candidate section block.

    Tries a plain line-anchored split first; if that yields fewer than two items
    the block is likely a PDF-flattened single line, so it is repaired and
    re-split. A lone oversized item is rejected so the caller escalates to the
    LLM instead of returning a compressed blob.
    """
    block = _break_headings(_normalize_block(block))
    items = _split_items(block)
    if len(items) < 2:
        repaired = _reenumerate_flattened(block)
        if repaired != block:
            repaired_items = _split_items(repaired)
            if len(repaired_items) > len(items):
                items = repaired_items
    if len(items) == 1 and len(items[0][1]) > _MAX_SINGLE_ITEM_CHARS:
        return []
    return items


def _has_footer_pollution(items: list[tuple[str, str]]) -> bool:
    """Return True when any item's text still carries a strong footer signature.

    A NIT, an e-mail address, or a TEL/FAX/E-MAIL/WEB label inside an obligation's
    text means page furniture leaked into the verbatim extraction — the result
    should not be trusted and the caller must escalate to the LLM fallback instead.
    """
    return any(_FOOTER_SIGNATURE_RE.search(text) for _, text in items)


def extract_obligaciones_verbatim(texto: str) -> list[ObligacionExtraida]:
    """Extract enumerated obligation items from the contract text **verbatim**.

    Deterministic, regex-based extractor that preserves the EXACT wording from
    the contract. It scans every candidate "OBLIGACIONES/ACTIVIDADES
    ESPECÍFICAS" (or equivalent) header, slices each section up to the next
    major section, and returns the FIRST section that yields a clean enumerated
    list — skipping headers that are only mentioned in prose. Tolerates PDF text
    whose enumeration was flattened onto a single line.

    Each item's text spans from its marker until the next, with internal
    whitespace collapsed to single spaces; items shorter than 10 characters are
    dropped as noise and anything after the catch-all closer is excluded.

    Returns an empty list when no usable section is found, OR when any item
    still carries a strong footer signature (NIT / e-mail / TEL-FAX-WEB label —
    see ``_has_footer_pollution``) — verbatim text is only trustworthy on clean
    input, so a polluted result is discarded rather than returned as-is,
    letting the caller escalate to the LLM extractor instead.
    """
    if not texto:
        return []

    def _to_obligaciones(items: list[tuple[str, str]]) -> list[ObligacionExtraida]:
        return [
            ObligacionExtraida(descripcion=text, tipo="especifica", orden=i, etiqueta=marker)
            for i, (marker, text) in enumerate(items)
        ]

    texto_upper = texto.upper()
    fallback: list[tuple[str, str]] | None = None
    for start in _candidate_starts(texto_upper):
        end = _find_section_end(texto, start)
        items = _extract_items_from_block(texto[start:end])
        if not items:
            continue
        # The real specific-obligations list is the one that closes with the
        # catch-all ("Las demás actividades…") — prefer it over a general list
        # (e.g. seguridad social) that merely happens to enumerate items first.
        if _is_catch_all(items[-1][1]):
            return [] if _has_footer_pollution(items) else _to_obligaciones(items)
        if fallback is None:
            fallback = items
    if fallback is None or _has_footer_pollution(fallback):
        return []
    return _to_obligaciones(fallback)
