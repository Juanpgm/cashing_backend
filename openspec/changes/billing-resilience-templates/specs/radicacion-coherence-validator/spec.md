# Radicación Coherence Validator Specification

## Purpose

Read-only rule engine that runs before radicación to catch defect classes verified in
shipped packages (stale cuota numbering, copied financial blocks, PILA mismatch, stale
filenames, obligación mapping drift, stale Adición clauses). Blocks radicación on HARD
findings; surfaces SOFT findings as warnings.

## Rule Catalog

| ID | Rule | Grounding defect | Severity | Rationale |
|----|------|------------------|----------|-----------|
| R1 | Internal "Cuota Número" field MUST equal the cuota's stored `posicion`/`numero_cuota` | Final cuota shipped with a stale internal number, MD5-identical to prior cuota | HARD | Wrong cuota number reaches the client; identifies the wrong payment period |
| R2 | Accumulated value and seguridad-social block MUST differ from the prior cuota unless the contract genuinely has zero new activity | Accumulated value + seg-social block byte-copied unchanged between cuotas | HARD | Financial misstatement sent to a paying client/organism |
| R3 | PILA planilla number MUST match across all documents of the SAME cuota (planilla vs comprobante) | Planilla number mismatched between docs of one cuota | HARD | Documents contradict each other; invalidates the seg-social justification |
| R4 | Month names in filenames/labels MUST match the cuota's actual period | Stale month names found in filenames | SOFT | Cosmetic/traceability issue; does not invalidate content |
| R5 | Obligación-to-evidence mapping MUST resolve uniquely via normalized-text match, tolerant to an obligation-count shift (e.g., 8→7) | Letter-based mapping (`_LETTERS[idx]`) silently shifted when count changed 8→7 | HARD | Silent misattribution of evidence to the wrong obligación |
| R6 | After a recorded Adición event (new RPC/CDP), generated clause text and document references MUST reflect the new RPC/CDP, not pre-Adición identifiers | Adición N°1 added a new RPC/CDP; clause text stayed stale | HARD | Stale legal references invalidate the invoice's legal basis |

Resolves proposal §9.1: only R4 is SOFT (cosmetic, non-blocking); R1/R2/R3/R5/R6 are HARD
because each represents financially or legally incorrect content reaching a third party —
matching the severity of the real defects that motivated this change.

## Requirements

### Requirement: Coherence check runs before radicación

The system MUST run all catalog rules over the target cuenta/cuota before allowing
`radicar_cuenta` to proceed.

#### Scenario: All rules pass

- GIVEN a cuenta whose cuota data passes R1-R6
- WHEN the coherence check runs pre-radicación
- THEN no findings are returned and radicación proceeds

#### Scenario: HARD finding blocks radicación

- GIVEN a cuota where the stored `numero_cuota` does not match the internal "Cuota
  Número" field (R1)
- WHEN the coherence check runs
- THEN it returns a HARD finding for R1 and `radicar_cuenta` raises `COHERENCE_CHECK_FAILED`
- AND radicación does not proceed

#### Scenario: SOFT finding warns without blocking

- GIVEN a cuota whose filename contains a stale month name (R4)
- WHEN the coherence check runs
- THEN it returns a SOFT finding for R4 and radicación proceeds with the finding attached
  as a warning

#### Scenario: Obligación mapping survives count shift

- GIVEN a contract whose obligación count changed from 8 to 7 mid-contract
- WHEN R5 maps evidence to obligaciones using normalized text
- THEN each evidence item resolves to the correct obligación by text, not by position
- AND no false HARD finding is raised solely due to the count change

#### Scenario: Ambiguous obligación mapping raises a finding

- GIVEN two obligaciones whose normalized text is indistinguishable after the count shift
- WHEN R5 cannot resolve a unique mapping
- THEN a HARD finding is raised for R5

#### Scenario: Adición without updated clause text

- GIVEN a contract with a recorded Adición event introducing a new RPC/CDP
- WHEN the generated cuota document still references the pre-Adición RPC/CDP in clause text
- THEN R6 raises a HARD finding and `COHERENCE_CHECK_FAILED` blocks radicación

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `validar_coherencia_cuenta` | read-only | Runs the full rule catalog over a cuenta/cuota; returns findings list (rule id, severity, message) |

## Error Codes

- `COHERENCE_CHECK_FAILED` — one or more HARD findings exist; radicación is blocked;
  findings list attached.

## Deferred to sdd-design

- Exact organism/entidad identification used to resolve per-contract clause/RPC context
  for R6 (relates to proposal §9.6).
