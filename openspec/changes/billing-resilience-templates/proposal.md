# Proposal — billing-resilience-templates

Status: proposed. Scope: `cashing-backend` only. Encodes the user-approved P0/P1/P2
intent plus two explicit reinforcement requests (template ingestion, requisito
comprehension). Grounded in real defects found in production packages (DAGMA + SYJ;
full detail in engram `docmodel/*`).

Depends on: sdd-explore result (engram `sdd/billing-resilience-templates/explore`).
Coordinates with in-flight change `backend-local-first-sync` (owns migration `024`,
extracts `_process_uploaded_document` from `document_service.upload_document`).

---

## 1. Intent — problem and why now

The radicación path emits packages that ship human errors straight to the client.
Real defects verified in shipped folders: final cuota was a stale MD5 copy of the
prior one (wrong internal "Cuota Número"); accumulated value and the seguridad-social
block copied unchanged between cuotas; PILA planilla number mismatched between the
planilla doc and the comprobante in the SAME cuota; stale month names in filenames;
obligación count changed 8→7 mid-contract so letter-based mapping silently shifted;
an Adición N°1 added a new RPC/CDP while clause text stayed stale; and — worst —
live Neon DB credentials sat inside an evidence folder about to be zipped and sent to
a client. Meanwhile the generator emits one fixed 4-col layout that matches NEITHER
organism's requested format (DAGMA 2-col, COEMPRESAR 3-col with literal anexo refs).

Today there is no pre-radicación coherence check, no secret scan before packaging, no
first-class cuota position, no organism-aware template, and requisito understanding is
a flat LLM list with no structure extraction. The domain model cannot even express
"this is the final cuota" or "this obligation is filled once in cuota 1 then blank".

Success looks like: radicación is gated by a coherence validator and a mandatory
secret scan; packages follow a per-organism numbered folder structure with an explicit
LISTO/PENDIENTE split; the model carries cuota position, one-time obligations, INFORME
FINAL/PARCIAL, and contract Adición events; informes adapt to each organism's ingested
template with a progressive, always-draft narrative; and all of it is exposed as tools
in `app/tools/` (one surface for API + `/mcp`) under strict TDD.

## 2. Scope

### In scope

- **P0a — Pre-radicación coherence validator.** New read-only service that runs a
  rule set over a cuenta before radicación: stale "Cuota Número" vs stored position;
  accumulated value / seg-social block unchanged vs prior cuota; PILA number match
  between planilla and comprobante; stale month names in filenames; obligación mapping
  by TEXT not letter; Adición-refreshed RPC/CDP vs stale clause text. Emits structured
  findings + new error codes. Blocks radicación on hard failures.
- **P0b — Cuota packager (hardened).** Extend `generar_zip_evidencias` from
  placeholder to real: fetch real bytes from `StoragePort`, build per-organism numbered
  folder structure, run a **mandatory non-negotiable secret scan**, and enforce a
  LISTO/PENDIENTE gate (mirrors the human LEEME workflow). Exposed as a tool.
- **P1 — Domain model.** First-class cuota position (`primera` / `recurrente` /
  `final`) with stored `numero_cuota`; one-time obligation semantics (filled in cuota
  1, blank after); INFORME FINAL vs PARCIAL flag; contract Adición as tracked events
  (Adición → new RPC/CDP, `valor_adicion`, prórroga). Migrations `025+`.
- **P2 — Adaptive generation.** Per-organism informe templates; progressive narrative
  (informe N generated with cuotas 1..N-1 context, ALWAYS a labeled draft).
- **P2a — Template ingestion (reinforcement).** Ingest an institutional format/template
  (DOCX/PDF), extract its structure, and ADAPT generated content to that organism's
  requested form.
- **P2b — Requisito comprehension (reinforcement).** Strengthen requirement-document
  understanding to generate the checklist AND prepare the radicación end-to-end.
- All capabilities land as `app/tools/` entries following `model → schema → service →
  api → test`. Strict TDD active (`uv run python -m pytest`).

### Out of scope

- Frontend / UI for LISTO-PENDIENTE review, template preview, or draft editing.
- Resurrecting the orphaned graph subsystems (`requirements_ingestion`,
  `entity_profile`, `template_resolver`, Phase-5 `doc_assembly`/`folder_organizer`).
  We extend the LIVE service paths, never the dead nodes.
- Consolidating prior justificaciones into the final cuota — verified NOT done in
  either organism; final = INFORME FINAL checkbox only.
- Speculatively filling legal/financial fields — intentionally left blank per human
  workflow; validator flags, it does not invent.
- A background job runner, batch StoragePort methods, or real-time push.
- Any `backend-local-first-sync` work (motivo_rechazo, sync engine, presigned upload).

## 3. Locked product decisions

- **Extend live paths, not orphans.** `requisito_inference_service` (not
  `requirements_ingestion`), `informe_service` + `checklist_autogen_service._GENERADORES`
  seam, `generar_zip_evidencias`, and the `app/tools/` registry are the extension
  points. The graph-based duplicates stay dead.
- **Obligación mapping by TEXT, not letter.** The positional `_LETTERS[idx]` display
  divergence from `Obligacion.etiqueta` is a live bug class; the validator and generator
  map obligations by normalized text, tolerant to 8→7 count shifts.
- **Secret scan is a hard gate.** No package is emitted if a secret is detected. This
  is non-negotiable (real credential leak found).
- **Generated narrative is always a borrador.** Every generated informe is explicitly
  labeled draft for human adjustment; one-time obligations blank after cuota 1.
- **Cuota position is stored, not inferred.** Replaces the purely positional
  `_is_first_cuenta()` (smallest anio,mes) with a persisted field; final cuota is
  explicit, not "last so far".

## 4. Capabilities (contract with sdd-spec)

### New Capabilities
- `radicacion-coherence-validator`: pre-radicación rule engine + findings model + error codes.
- `cuota-packager`: real-bytes numbered-folder zip + mandatory secret scan + LISTO/PENDIENTE gate.
- `cuota-position-model`: stored cuota position, `numero_cuota`, one-time obligations, INFORME FINAL/PARCIAL flag.
- `contract-addition-events`: Adición tracked as events (new RPC/CDP, valor_adicion, prórroga).
- `template-ingestion`: ingest DOCX/PDF institutional template, extract structure, adapt generation.
- `adaptive-informe-generation`: per-organism templates + progressive draft narrative with prior-cuota context.
- `requisito-comprehension`: structured requirement understanding driving checklist + end-to-end radicación prep.

### Modified Capabilities
- None at the OpenSpec `openspec/specs/` level (no existing spec files for these live
  services; the sibling change owns the only spec folder). Behavior changes are
  captured by the new capabilities above.

## 5. Approach per priority (mapped to seams)

| Priority | Seam(s) from exploration | Change |
|---|---|---|
| P0a | new `services/coherence_validator_service.py`; `core/exceptions.py` (only 3 codes exist); tool in `app/tools/`; gate before `cuenta_cobro_service.radicar_cuenta` L716-747 | Rule set over stored cuenta + prior cuota; blocks radicación on hard findings |
| P0b | `informe_service.generar_zip_evidencias` L366-460 (already has folder/README/LEEME skeleton); `StoragePort` (4 methods, N sequential downloads); NEW secret-scan util | Real bytes, per-organism structure, secret scan, LISTO/PENDIENTE gate |
| P1 | `CuentaCobro` model (no position/numero/final); `Contrato` (single `valor_adicion` scalar, no events); `checklist_service._is_first_cuenta` L266-278, `_CATALOGO_SEED` `solo_primera_cuenta` L80-226 | Add position/numero_cuota/informe_final; Adición event table; one-time obligation flag |
| P2 | `informe_service` generators L213-353 (hardcoded 4-col, signature has no organism/position/prior-context); `_GENERADORES` dict L42-51 | Add organism + position + prior-cuota params; per-organism template selection |
| P2a | `requisito_inference_service.inferir_requisitos` L88-168 (flat list, no structure); CONTRATO vision fallback chain (document_service L407-426) as reusable resilient reader | Extract template STRUCTURE from DOCX/PDF; persist per-organism format |
| P2b | `requisito_inference_service` + `checklist_service.asegurar_checklist` L300-369 + packager | Structured requisito → checklist → radicación-prep orchestration |

New error codes (extend `core/exceptions.py`, only `NotFound/AlreadyExists/InsufficientCredits` + `CHECKLIST_INCOMPLETE` exist today): `COHERENCE_CHECK_FAILED`, `SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE` (LISTO gate), `CUOTA_POSITION_CONFLICT`. Update `test_radicar._CODIGOS_OBLIGATORIOS` mirror when the catalog changes.

## 6. Slice plan (auto-chain, stacked-to-main, target <400 lines each)

Ordered so each PR is independently shippable and merges to main in order. Migration
numbers start at `025` (`024` reserved by `backend-local-first-sync`; second-merger
rebases).

| # | Slice | Migration | Est. lines | Depends on |
|---|---|---|---|---|
| 1 | **Coherence validator (P0a)** — service + 4 error codes + tool + tests. Read-only; blocks radicación. | none | ~350 | — |
| 2 | **Packager hardening (P0b)** — real bytes, per-organism numbered folders, secret-scan util, LISTO/PENDIENTE gate, tool. | none | ~380 | — (parallel-safe with #1) |
| 3 | **Cuota position model (P1a)** — `CuentaCobro.numero_cuota`/`posicion`/`informe_final`; replace `_is_first_cuenta`; migrate. | `025` | ~320 | #1 (validator reads position) |
| 4 | **Contract Adición events (P1b)** — event table (RPC/CDP/valor_adicion/prórroga); one-time obligation flag. | `026` | ~350 | #3 |
| 5 | **Template ingestion (P2a)** — extract DOCX/PDF template structure in `requisito_inference_service`; per-organism format persistence. | `027` | ~380 | #4 |
| 6 | **Adaptive generation (P2b)** — per-organism templates + progressive draft narrative with cuotas 1..N-1 context. | none | ~380 | #3, #5 |
| 7 | **Requisito comprehension + e2e prep (P2c)** — structured requisito → checklist → radicación-prep orchestration tool. | none | ~300 | #1, #2, #5 |

Precise PR boundaries are finalized in `sdd-tasks`. Slices #1 and #2 carry the highest
value (resilience) and lowest structural risk — ship first.

## 7. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `upload_document` overlap with `backend-local-first-sync` (it extracts `_process_uploaded_document`, reserves `024`) | High | Slices #5/#7 touch reading paths (`extraer_texto_documento`, `requisito_inference_service`), NOT `upload_document` body. Sequence document-touching slices AFTER sync merges; use migrations `025+`; second-merger rebases numbering. |
| Obligación letter divergence (`_LETTERS[idx]` vs `etiqueta`) causing wrong mapping | High | Map by normalized TEXT everywhere in validator/generator; add regression test for 8→7 count shift. |
| StoragePort has no batch/list → N sequential downloads for packager | Med | Accept sequential download bounded by 10MB cap; note as cost line; no new port methods this change. |
| Secret scan false negatives shipping a leak | Med | Gate is hard-fail-closed; layered patterns (keys, DB URLs, tokens) + test corpus from the real leak; PENDIENTE on any match. |
| Resurrecting orphaned graph nodes by mistake | Med | Explicit locked decision (§3): extend live services only; do not import `agent/nodes/requirements_ingestion` / `template_resolver`. |
| Migration collision on `024` | Med | Plan `025+`; whoever merges second rebases to next free number. |
| Journey ledger (`test_full_radicacion_journey`) friction from new gates | Low | New blocking steps must update JourneyLedger deliberately; weigh UX in sdd-spec. |
| Template-structure extraction unreliable on messy PDFs | Med | Reuse resilient CONTRATO vision fallback chain; degrade to flat list (current behavior) on failure. |

## 8. Rollback plan

- Slices #1, #2, #6, #7 (no migration): revert the commit/PR; tools de-register with the
  code removal since `TOOL_REGISTRY` is populated from module imports.
- Slices #3, #4, #5 (migrations `025`–`027`): `alembic downgrade -1` per slice, then
  revert code. New columns/tables are additive (nullable / new tables) so downgrade is
  clean; no destructive data change.
- The coherence gate and secret-scan gate are feature-flaggable if they prove too
  strict in early radicaciones (flag decided in sdd-design).

## 9. Open questions for sdd-spec / sdd-design

1. Coherence validator: which findings are HARD (block radicación) vs SOFT (warn in
   PENDIENTE)? Full rule catalog + severities.
2. Secret-scan detector: pattern set + whether to use an existing lib (detect-secrets,
   already in pre-commit) vs a bespoke scanner over extracted package bytes.
3. Cuota position: derive `numero_cuota` from contract `numero_cuotas` (which does not
   exist yet on `Contrato`) or store per-cuota only? Backfill strategy for existing rows.
4. Contract Adición events: new dedicated table vs reuse an event/audit pattern; how
   prórroga interacts with position/final detection.
5. Template ingestion: storage/model for the extracted per-organism template (new model
   vs extend `Plantilla`, which is currently HTML→PDF cuenta-de-cobro only).
6. Per-organism selection key: how an organism/entidad is identified on a Contrato
   (no `organismo` field exists on `RequisitoDocumento` today).
7. Progressive narrative: exact prior-cuota context window and how "always draft" is
   surfaced to the API/tool caller.
8. Feature-flag surface for the two new radicación gates.

## 10. Review Workload Forecast

- **Chained PRs recommended: Yes** — 7 cohesive slices, 3 with migrations; total far
  exceeds a single 400-line PR.
- **400-line budget risk: High** for the whole change; **Low–Medium per slice** as
  planned (each targeted ≤~380 lines).
- **Decision needed before apply: Yes** — confirm slice ordering and the
  `upload_document`/migration sequencing against `backend-local-first-sync` before
  starting slices that touch document-reading paths (#5, #7).

## 11. Success criteria

- [ ] Radicación is blocked when the coherence validator finds a hard defect (stale
      cuota number, copied accumulated value, PILA mismatch, stale month, letter-shift).
- [ ] No package is produced when the secret scan detects a credential; a real-leak
      test corpus is caught.
- [ ] Packages follow a per-organism numbered folder structure with a LISTO/PENDIENTE
      split derived from real requisito state.
- [ ] `CuentaCobro` carries stored position/`numero_cuota`/`informe_final`; final cuota
      is explicit, not "latest so far".
- [ ] Contract Adición is recorded as an event with new RPC/CDP + valor_adicion +
      prórroga; obligación mapping survives an 8→7 count shift.
- [ ] An ingested organism template (DOCX/PDF) changes the generated informe's
      structure; the narrative is progressive and labeled draft.
- [ ] Every capability is invocable as a tool via `TOOL_REGISTRY` (API + `/mcp`), with
      service + API tests green under `uv run python -m pytest`.

## 12. Language contract

Prose is English. Spanish domain nouns preserved verbatim: cuenta de cobro, contrato,
obligación, requisito, radicación, cuota, Adición, prórroga, RPC, CDP, PILA, planilla,
seguridad social, informe, SECOP, LISTO, PENDIENTE, borrador. Identifiers and enum
values stay as-is.
