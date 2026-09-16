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


def contract_number_variants(numero: str | None) -> list[str]:
    """Return contract-number variants ordered from most to least specific.

    Handles dotted numbers (DAGMA/Cali style), numbers that already use
    slashes/hyphens (e.g. "CTO-123/2025"), and returns `[]` for
    `None`/empty/whitespace-only input. Never emits a token shorter than 3
    characters or a bare 4-digit year on its own.
    """
    raw = (numero or "").strip()
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


def _normalize_entidad(entidad: str | None) -> str:
    """Strip a trailing legal-entity suffix (S.A.S, E.S.E, Ltda, ...) so the
    entidad name is usable as a full-text search term without matching only
    on boilerplate."""
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

    if not lines:
        return ""
    return "Contexto del contrato:\n" + "\n".join(lines)


def contract_search_terms(contexto: dict[str, object] | None) -> list[str]:
    """Combine contract-number variants + normalized entidad + salient objeto
    keywords into a single, deduplicated, order-preserving term list — the
    shared vocabulary fed into the Gmail/Drive/Calendar query builders."""
    from app.agent.prompts.email_evidence import _extract_keywords

    contexto = contexto or {}
    numero = contexto.get("numero_contrato")
    entidad = contexto.get("entidad")
    objeto = contexto.get("objeto")

    terms: list[str] = list(contract_number_variants(numero if isinstance(numero, str) else None))

    entidad_norm = _normalize_entidad(entidad if isinstance(entidad, str) else None)
    if len(entidad_norm) >= _MIN_TOKEN_LEN:
        terms.append(entidad_norm)

    if isinstance(objeto, str) and objeto.strip():
        terms.extend(_extract_keywords(objeto)[:5])

    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out
