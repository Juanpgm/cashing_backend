"""Contract-number variants + combined search terms.

Root cause this closes: a dotted contract number in the DAGMA/Cali style
(e.g. "4161.010.26.1.027.2025") never survived `_extract_keywords` (which
turns dots into spaces and drops any token of length <= 4) and
`evidence_matcher._keyword_score`'s letters-only regex never matched digits
either — so the single most specific, highest-signal search/scoring term for
a contract was silently discarded everywhere. These helpers make the contract
number (and its common written variants) a first-class search/scoring term.
"""

from __future__ import annotations

import re

_SEGMENT_RE = re.compile(r"[A-Za-z0-9]+")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_MIN_TOKEN_LEN = 3
# A DERIVED variant must be at least this long to be trusted as a MATCH signal.
# "4161" (the entity's dependency code) and "027-2025" (the consecutive/year
# pair, shared by every contract in that range) are below it — they are fine as
# ranking hints but must never mark evidence as belonging to THIS contract.
_MIN_MATCH_LEN = 8

# Cap applied to the contract `objeto` before it is prefixed to an obligación's
# embedding input. Colombian objetos routinely run 500-2000 chars while an
# obligación is 50-150, so an untruncated shared prefix dominates the pooled
# vector and every obligación embeds to nearly the same point. Mirrors the cap
# `contract_header` already applies to the same field for prompts.
OBJETO_EMBED_MAX_CHARS = 300

# Legal-entity suffixes stripped from `entidad` before it's used as a search
# term (order matters: longer/more specific forms first so a shorter suffix
# doesn't leave a stray trailing "." behind).
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "s.a.s.",
    "s.a.s",
    "sas",
    "e.s.e.",
    "e.s.e",
    "ese",
    "s.a.",
    "s.a",
    "sa",
    "ltda.",
    "ltda",
    "e.u.",
    "e.u",
)


def contract_number_variants(numero: object | None) -> list[str]:
    """Return contract-number variants ordered from most to least specific.

    Handles dotted numbers (DAGMA/Cali style), numbers that already use
    slashes/hyphens (e.g. "CTO-123/2025"), and returns `[]` for
    `None`/empty/whitespace-only input. Never emits a token shorter than 3
    characters or a bare 4-digit year on its own.

    Accepts `object | None` rather than `str | None` because every caller reads
    it out of a loosely-typed `contrato_contexto: dict[str, str | int | float |
    None]`; a non-str value is coerced instead of raising `AttributeError`
    (round-2: this was a real mypy arg-type regression at three call sites).

    NOTE: this is the SCORING vocabulary. For provider QUERIES use
    `contract_query_variants`, which drops the forms no search backend can match.
    """
    if numero is None:
        return []
    raw = (numero if isinstance(numero, str) else str(numero)).strip()
    if not raw:
        return []

    segments = _SEGMENT_RE.findall(raw)
    candidates: list[str] = [raw]

    if len(segments) > 1:
        candidates.append("-".join(segments))
        candidates.append(" ".join(segments))
        candidates.append("".join(segments))
        candidates.append(".".join(segments))

        last_pair = segments[-2:]
        candidates.append("-".join(last_pair))
        candidates.append(".".join(last_pair))

        first = segments[0]
        if len(first) >= 4 and not _YEAR_RE.match(first):
            candidates.append(first)

    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        token = candidate.strip()
        if len(token) < _MIN_TOKEN_LEN:
            continue
        if _YEAR_RE.match(token):
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def contract_query_variants(numero: object | None) -> list[str]:
    """The subset of `contract_number_variants` worth spending a QUERY slot on.

    Round-2 fix (confirmed CRITICAL): all seven variants of a dotted DAGMA/Cali
    number were fired as provider queries, filling every budget. Five of them
    cannot usefully match anything:

    - `"4161 010 26 1 027 2025"` — Gmail/Drive tokenize a quoted phrase, and no
      real document writes the number spaced out like that;
    - `"41610102610272025"` — the digits-glued form appears in no document;
    - `"027-2025"` / `"027.2025"` — the last-pair forms are shared by every
      contract numbered in that consecutive range;
    - `"4161"` — the bare first segment is the ENTITY's dependency code, shared
      by every contract of that entity, so it is a pure noise magnet.

    All seven remain available for SCORING via `contract_number_variants` (a
    document that happens to spell the number differently should still rank),
    but only the raw form and ONE normalized (hyphen-joined) form are searched.
    """
    variants = contract_number_variants(numero)
    if not variants:
        return []

    raw = variants[0]
    out = [raw]

    segments = _SEGMENT_RE.findall(raw)
    if len(segments) > 1:
        hyphenated = "-".join(segments)
        if hyphenated != raw:
            out.append(hyphenated)
    return out


def entidad_search_term(entidad: str | None) -> str:
    """Entidad name usable as a full-text search term: a trailing legal-entity
    suffix (S.A.S, E.S.E, Ltda, ...) is stripped so the query matches on the
    distinctive name rather than on boilerplate every entity shares.

    Round-2: this logic (then `_normalize_entidad`) was reachable ONLY from
    `contract_search_terms`, which had no production caller at all and whose
    docstring falsely claimed to be "the shared vocabulary fed into the
    Gmail/Drive/Calendar query builders". That function is deleted; the useful
    half is kept and actually wired into the entidad query term.
    """
    name = (entidad or "").strip()
    if not name:
        return ""
    lowered = name.lower()
    for suffix in _LEGAL_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: len(name) - len(suffix)].strip().rstrip("-,.")
            break
    return name.strip()


def contract_header(contexto: dict[str, object] | None) -> str:
    """Shared Spanish contract-context header — número, entidad, objeto
    (truncated ~300 chars) and período — used by the matcher, work-noise
    filter and justifier prompts so the LLM always has contract context
    (evidencias/discovery-fix WU5, root cause #8: these prompts previously
    received NO contract information at all). Returns "" when contexto is
    empty/None, so callers can safely prepend it to any prompt unconditionally.
    """
    contexto = contexto or {}
    lines: list[str] = []

    numero = contexto.get("numero_contrato")
    if isinstance(numero, str) and numero.strip():
        lines.append(f"Número de contrato: {numero.strip()}")

    entidad = contexto.get("entidad")
    if isinstance(entidad, str) and entidad.strip():
        lines.append(f"Entidad: {entidad.strip()}")

    objeto = contexto.get("objeto")
    if isinstance(objeto, str) and objeto.strip():
        objeto_str = objeto.strip()
        if len(objeto_str) > 300:
            objeto_str = objeto_str[:300].rstrip() + "…"
        lines.append(f"Objeto: {objeto_str}")

    fecha_inicio = contexto.get("fecha_inicio")
    fecha_fin = contexto.get("fecha_fin")
    if fecha_inicio or fecha_fin:
        lines.append(f"Período: {fecha_inicio or '?'} a {fecha_fin or '?'}")

    # "¿Qué hiciste este mes?" — the contratista's own free-text summary
    # (evidencias/discovery-fix WU7b). Primary hint of what was actually
    # done this period, shared across matcher/noise/justify prompts.
    contexto_usuario = contexto.get("contexto_usuario")
    if isinstance(contexto_usuario, str) and contexto_usuario.strip():
        texto = contexto_usuario.strip()
        if len(texto) > 300:
            texto = texto[:300].rstrip() + "…"
        lines.append(f"Contexto del período (según el contratista): {texto}")

    if not lines:
        return ""
    return "Contexto del contrato:\n" + "\n".join(lines)


def contract_match_variants(numero: object | None) -> list[str]:
    """Variants trusted to decide "this evidence mentions THIS contract".

    Round-2 fix for a confirmed CRITICAL finding: `_contains_contract_number`
    tested every variant, including the bare first segment `4161`, with a plain
    substring check. `4161` is the entity's dependency code — shared by every
    contract DAGMA issues — so an unrelated `ORD-41612` order confirmation was
    treated as contractual evidence, disarming the noise filter and earning the
    ranking bonus.

    The raw number is always kept. Derived forms must be at least
    `_MIN_MATCH_LEN` chars AND must not be one of the explicitly weak forms
    (bare first segment, last-pair). All forms remain available for the looser
    `contract_number_variants` vocabulary.
    """
    variants = contract_number_variants(numero)
    if not variants:
        return []

    raw = variants[0]
    weak: set[str] = set()
    segments = _SEGMENT_RE.findall(raw)
    if len(segments) > 1:
        weak.add("-".join(segments[-2:]))
        weak.add(".".join(segments[-2:]))
        first = segments[0]
        if len(first) >= 4 and not _YEAR_RE.match(first):
            weak.add(first)

    return [v for v in variants if v == raw or (v not in weak and len(v) >= _MIN_MATCH_LEN)]
