# Tasks: billing-resilience-templates

## Reconciliation Notes (spec ↔ design ran in parallel)

1. **Error-code wiring (spec §CUOTA_POSITION_CONFLICT vs COHERENCE_CHECK_FAILED) — RECONCILED, no conflict.**
   Design confirms the split spec implies: `CUOTA_POSITION_CONFLICT` is a **write-time**
   guard raised inside `cuenta_cobro_service.py` when persisting `numero_cuota`/`posicion`
   (duplicate `final`, duplicate `primera`) — slice #3. `COHERENCE_CHECK_FAILED` is the
   **validator umbrella**: `coherence_validator_service.validar_coherencia` only *returns*
   findings; the caller (`cuenta_cobro_service.radicar_cuenta`) raises the error on any
   HARD finding — slice #1. Both error codes live in `core/exceptions.py`, added in their
   respective slices. No blocker.

2. **Packager mode semantics (spec "standard" vs "strict/final" vs design's flat gate) — GENUINE CONFLICT, reconciled with a recorded decision, needs human confirmation at apply time.**
   Design's data-flow diagram and `generar_zip_evidencias(db, usuario_id, cuenta_id)`
   signature show an unconditional `pendiente? → PACKAGE_PENDIENTE` gate with **no mode
   parameter**, but the spec requires standard mode to emit a partial package with a
   PENDIENTE manifest section (non-blocking) and only strict/final mode to raise
   `PACKAGE_PENDIENTE`. **Reconciled decision**: add `modo: Literal["standard","final"] =
   "standard"` to `generar_zip_evidencias`; `PACKAGE_PENDIENTE` raises only when
   `modo == "final"`. `preparar_radicacion` (slice #7) calls the packager with
   `modo="final"`; direct/manual packaging calls default to `"standard"`. Encoded in
   slice #2 tasks below — flag for confirmation during `sdd-apply`.

3. **Prórroga vs `informe_final` (contract-addition-events spec vs design D4) — GENUINE CONFLICT, reconciled via a new SOFT rule, needs confirmation.**
   Spec says a prórroga makes the "previously expected final cuota... no longer treated
   as final by default." Design D4 keeps `informe_final` a manual flag that a prórroga
   never silently flips, only emitting a SOFT warning. **Reconciled decision**: keep
   design's manual-flag approach (safer/explicit, avoids silent data mutation) and encode
   the spec's warning requirement as a new coherence rule `R7 stale_final_after_prorroga`
   (SOFT), registered in slice #4 (extends the R1-R6 registry built in slice #1). No
   auto-unflagging of `informe_final`. Flag for confirmation during `sdd-apply`.

4. **R6 sequencing gap.** Design's `ValidationContext` bundles `adiciones` from slice #1,
   but the real `adiciones_contrato` table doesn't exist until slice #4. Slice #1 builds
   R6 against a stubbed/empty `adiciones` list (or `Contrato.valor_adicion` scalar as an
   interim signal); slice #4 wires `ValidationContext.adiciones` to the real table,
   completing R6. Encoded as an explicit task in both slices.

---

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~2,460 total across 7 slices (see per-slice column below) |
| 400-line budget risk | Medium (per-slice); High for the whole change taken together |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → PR 2 → PR 3 → PR 4 → PR 5 → PR 6 → PR 7 (stacked) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Medium

### Suggested Work Units

| Unit | Goal | Likely PR | Notes |
|------|------|-----------|-------|
| 1 | Coherence validator (R1-R6) + gate + tool | PR 1 | No migration; independent of #2 |
| 2 | Packager hardening + secret scan + LISTO/PENDIENTE | PR 2 | No migration; independent of #1; risk Medium-High (real bytes + scan + gate + 2 tools) |
| 3 | Cuota position model | PR 3 (migration 025) | Depends on #1 (validator reads position) |
| 4 | Adición events + R7 | PR 4 (migration 026) | Depends on #3; completes R6 |
| 5 | Template ingestion | PR 5 (migration 027) | Depends on #4; risk Medium-High (adds packager rewiring) |
| 6 | Adaptive generation | PR 6 | Depends on #3, #5 |
| 7 | Requisito comprehension + e2e prep | PR 7 | Depends on #1, #2, #5 |

---

## Slice 1 — Coherence validator (P0a) · PR 1 · no migration · ~350 lines

- [x] 1.1 RED: test asserting `COHERENCE_CHECK_FAILED` exists and maps to HTTP 422 in `core/exceptions.py`
- [x] 1.2 GREEN: add `COHERENCE_CHECK_FAILED` to `core/exceptions.py` + `domain_to_http()`
- [x] 1.3 RED: unit tests for `Severity`, `Finding` dataclass shape in new `tests/services/test_coherence_validator_service.py`
- [x] 1.4 GREEN: create `app/services/coherence_validator_service.py` — `Severity`, `Finding`, `ValidationContext`, empty `RULES` registry
- [x] 1.5 RED: test R1 stale `numero_cuota` vs internal "Cuota Número" (HARD)
- [x] 1.6 GREEN: implement `stale_cuota_numero` rule — **DEVIATION**: `numero_cuota`/`posicion` don't exist yet (land in slice #3, migration 025); expected number is derived from chronological (anio,mes) ordering among sibling cuentas (same technique as `checklist_service._is_first_cuenta`), compared against any explicit "Cuota N" mention found in actividades/documentos text. See apply-progress for full rationale.
- [x] 1.7 RED: test R2 copied accumulated value / seg-social block unchanged (HARD)
- [x] 1.8 GREEN: implement `copied_accumulated_value` rule
- [x] 1.9 RED: test R3 PILA planilla number mismatch within same cuota (HARD)
- [x] 1.10 GREEN: implement `pila_match` rule
- [x] 1.11 RED: test R4 stale month name in filename (SOFT, non-blocking)
- [x] 1.12 GREEN: implement `stale_month_in_filename` rule
- [x] 1.13 RED: tests R5 — 8→7 obligación-count-shift regression resolves by text; ambiguous mapping raises HARD
- [x] 1.14 GREEN: implement `obligacion_text_mapping` rule using `text_match.similar` (pairwise ambiguity over the contract's obligaciones — count-shift-tolerant by construction, never references a stored letter/index)
- [x] 1.15 RED: test R6 stale clause after Adición (HARD) — build against stubbed/empty `ValidationContext.adiciones` (see Reconciliation Note 4; real table lands slice #4)
- [x] 1.16 GREEN: implement `stale_clause_after_adicion` rule + `validar_coherencia()` entrypoint (loads context once, runs `RULES`)
- [x] 1.17 RED+GREEN: `schemas/coherence.py` — `FindingOut`, `ValidarCoherenciaResponse`
- [x] 1.18 RED: test `radicar_cuenta` raises `COHERENCE_CHECK_FAILED` on any HARD finding; SOFT-only proceeds with warning attached
- [x] 1.19 GREEN: wire gate into `cuenta_cobro_service.radicar_cuenta` (L716-747), guarded by new `COHERENCE_GATE_ENABLED` config flag (default `True`)
- [x] 1.20 RED+GREEN: `app/tools/catalog/coherence.py` — `validar_coherencia_cuenta` tool (read-only)
- [x] 1.21 Integration test: `invoke_tool("validar_coherencia_cuenta")` + `radicar` blocked end-to-end; flag off = bypass
- [x] 1.22 Update error-codes mirror with `COHERENCE_CHECK_FAILED` — **DEVIATION**: `tests/test_radicar.py::_CODIGOS_OBLIGATORIOS` is a list of checklist requisito codes, not error codes (it has no error-code mirror concept); the actual mirror pattern in this codebase is `tests/test_error_codes.py` (per-code scenario tests). Added `test_radicar_coherence_hard_finding_returns_coherence_check_failed_code` there instead.
- [x] 1.23 Local verification gate: `make format && make lint && uv run python -m pytest` green — ran `ruff check` (all clean) + targeted pytest (54 passed) + full suite (1159 passed, 12 deselected, 0 failed); mypy has pre-existing unrelated errors elsewhere in the project (153 before this change, confirmed via `git stash`), zero new errors in the 3 new coherence files.
- [x] 1.24 **No push to remote without explicit user OK** (hard session rule) — not pushed; 2 local commits only.

**Work-unit commits**: (a) 1.1-1.4 domain types → `feat(coherence): add validation context and finding types`; (b) 1.5-1.16 rules → one commit per rule pair or a single `feat(coherence): implement R1-R6 rule catalog` if reviewable at once; (c) 1.17-1.20 gate+tool → `feat(coherence): gate radicar_cuenta on coherence validator`; (d) 1.22 → included in (c).

---

## Slice 2 — Packager hardening (P0b) · PR 2 · no migration · ~380 lines (risk Medium-High)

- [x] 2.1 RED+GREEN: add `SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE` to `core/exceptions.py` + HTTP mapping
- [x] 2.2 RED: test `escanear_paquete()` catches real-leak corpus (Neon URL regex + API key) in `tests/services/test_secret_scan_service.py`
- [x] 2.3 GREEN: create `app/services/secret_scan_service.py` wrapping `detect-secrets` + belt-and-suspenders Postgres/Neon URL regex plugin — **DEVIATION**: uses `SecretsCollection.scan_file` (the realistic non-adhoc pipeline) rather than `scan_line`'s adhoc eager mode, which floods auto-generated Spanish prose with false positives (verified empirically: "Carpeta de evidencias" alone triggered 3 high-entropy hits under eager mode). Each member is scanned via a temp file with a synthetic `.cfg` name so `KeywordDetector` matches unquoted `KEY=VALUE` env-style assignments regardless of the member's real extension. See module docstring for full rationale.
- [x] 2.4 RED: test clean payload passes with no findings
- [x] 2.5 GREEN: confirm plugin set covers clean-pass path
- [x] 2.6 RED: test `generar_zip_evidencias` fetches real bytes from `StoragePort` (no placeholder text)
- [x] 2.7 GREEN: replace placeholder content with `StoragePort` downloads in `informe_service.generar_zip_evidencias` (L366-460)
- [x] 2.8 RED: test default numbered folder structure applied when no `PlantillaOrganismo` exists (fallback only — organism-specific lookup arrives slice #5)
- [x] 2.9 GREEN: implement default numbered folder builder (organism-specific branch stubbed to always-fallback until slice #5) — `_resolver_estructura_organismo()` stub always returns None; slice #5 task 5.15 replaces it
- [x] 2.10 RED: test package emission halts with `SECRET_DETECTED_IN_PACKAGE` when scan finds a hit; no zip bytes returned
- [x] 2.11 GREEN: wire mandatory secret scan before zip emission, guarded by new `SECRET_SCAN_GATE_ENABLED` config flag (default `True`, disabling documented emergency-only)
- [x] 2.12 RED: test standard mode emits partial package with PENDIENTE manifest section, no raise; final mode raises `PACKAGE_PENDIENTE` when incomplete
- [x] 2.13 GREEN: add `modo: Literal["standard","final"] = "standard"` param (Reconciliation Note 2); implement LISTO/PENDIENTE split + conditional raise — **DEVIATION**: LISTO/PENDIENTE is computed per-OBLIGACIÓN (has ≥1 evidencia attached in this cuenta) rather than via the full `checklist_service`/`RequisitoDocumento` catalog — the packager's domain is the evidence ZIP's own obligación-based folder structure, not the separate contract-level document checklist (RUT/CEDULA/PILA/etc.), which File Changes in design.md never lists `checklist_service.py` as touched for this slice.
- [x] 2.14 RED+GREEN: `obtener_estado_listo_pendiente` read-only helper (split without producing package)
- [x] 2.15 RED+GREEN: `schemas/paquete.py` — manifest/response schemas
- [x] 2.16 RED+GREEN: `app/tools/catalog/paquete.py` — `generar_paquete_evidencias` (write), `obtener_estado_listo_pendiente` (read) — **DEVIATION**: the tool's write handler uploads the resulting zip to storage (`paquetes/{usuario_id}/{cuenta_id}/{filename}`) and returns `PaqueteGeneradoResponse` (storage_key/filename/size_bytes) instead of raw bytes, per spec's Tool Surface note "creates zip artifact in storage" — an agent/MCP tool JSON response can't carry a binary payload. The tool never accepts `modo`; it always packages in `"standard"` mode per Reconciliation Note 2 (`modo="final"` is reserved for the slice #7 `preparar_radicacion` orchestrator calling the service directly).
- [x] 2.17 Integration test: flags off = bypass scan/gate — `tests/test_paquete_gate.py` (mirrors `test_coherence_gate.py`)
- [x] 2.18 Update `tests/test_radicar.py::_CODIGOS_OBLIGATORIOS` mirror with 2 new codes — **DEVIATION** (same as slice #1 task 1.22): `_CODIGOS_OBLIGATORIOS` is a checklist requisito-code list, not an error-code mirror; added `test_zip_evidencias_secreto_detectado_returns_code` + `test_zip_evidencias_modo_final_pendiente_returns_code` to `tests/test_error_codes.py` instead (the actual per-code mirror pattern in this codebase).
- [x] 2.19 Local verification gate: `make format && make lint && uv run python -m pytest` green — `ruff check` clean, `ruff format --check` clean (after one auto-format pass), full suite 1187 passed / 13 deselected (baseline 1169 passed + 18 new tests, 0 regressions); mypy: 2 new errors, both the same pre-existing accepted `get_storage() -> object` attr-defined pattern already present 5x in `document_service.py` — zero new errors in the genuinely new files (`secret_scan_service.py`, `schemas/paquete.py`).
- [x] 2.20 **No push to remote without explicit user OK** — not pushed; commits are local only.

**Work-unit commits**: (a) 2.1-2.5 secret scan service → `feat(paquete): add secret scan service with real-leak corpus tests`; (b) 2.6-2.9 real bytes/folders → `feat(paquete): fetch real bytes and build numbered folder structure`; (c) 2.10-2.16 gate+tools → `feat(paquete): enforce secret-scan and LISTO/PENDIENTE gates`. If (c) alone risks >150 lines combined with (a)+(b), split (c) into its own PR-2b before merge — flag during apply.

---

## Slice 3 — Cuota position model (P1a) · PR 3 · migration `025` · ~320 lines · depends on #1

- [x] 3.0a Carry-over from slice #2 verify (WARNING 1): add RED+GREEN test covering the binary-extraction scan path — a non-UTF8 payload whose `extraer_texto_documento`-extracted text contains a leak-shaped secret must trigger `SECRET_DETECTED_IN_PACKAGE` (mock the extractor; no real credentials).
- [x] 3.0b Carry-over from slice #2 verify (WARNING 2): add a clarifying note to `specs/cuota-packager/spec.md` + `design.md` distinguishing "obligación-level packaging completeness" (PACKAGE_PENDIENTE) from "requisito/checklist completeness" (CHECKLIST_INCOMPLETE) — slice #7's `preparar_radicacion` orchestrates BOTH gates.
- [x] 3.1 Checkpoint: confirm migration number `025` still free (rebase if `backend-local-first-sync` merged with different numbering) — confirmed via `alembic heads` (023 head, 024 reserved/unmerged by `backend-local-first-sync`); used 025.
- [x] 3.2 RED: model test — `CuentaCobro` accepts `numero_cuota: int|None`, `posicion: enum(primera|recurrente|final)`, `informe_final: bool = False`
- [x] 3.3 GREEN: add fields to `app/models/cuenta_cobro.py`
- [x] 3.4 RED+GREEN: `alembic/versions/025_*` — explicit `op.add_column` x3 (create_all no-ops column adds) + backfill window (per-contrato chronological anio/mes order)
- [x] 3.5 Neon verification: **BLOCKED** — no reachable Neon connection in this session (DATABASE_URL not readable/no live branch). Verified instead via (a) `alembic heads`/history parses cleanly with 025 as sole head, and (b) a dry-inspection script binding `alembic.operations.Operations` to a throwaway sqlite DB and calling `upgrade()`/`downgrade()` directly — confirmed correct per-contrato chronological backfill (out-of-insertion-order rows numbered correctly: 1/primera, 2/recurrente, 3/recurrente per contrato) and clean column removal on downgrade. Real Postgres/Neon apply still required before merge.
- [x] 3.6 RED+GREEN: `schemas/cuenta_cobro.py` — include new fields in `CuentaCobroOut`
- [x] 3.7 RED: test create first cuota → `posicion=primera`, `numero_cuota=1`
- [x] 3.8 GREEN: derive/persist `numero_cuota`/`posicion` at creation in `cuenta_cobro_service.py`
- [x] 3.9 RED: test duplicate `informe_final=True` and duplicate `posicion=primera` for same contract both raise `CUOTA_POSITION_CONFLICT` — **DEVIATION**: `posicion=primera` duplication is tested directly against the private `_verificar_conflicto_posicion` guard (not through `crear_cuenta_cobro`), since normal creation always DERIVES `posicion` from a live count and can never naturally collide with an existing PRIMERA cuota — the guard is a defensive backstop, not a user-triggerable path via the public service call. `informe_final` duplication IS reachable through the public service/API and is tested end-to-end (unit + API level).
- [x] 3.10 GREEN: add `CUOTA_POSITION_CONFLICT` to `core/exceptions.py`; enforce write-time invariant in `cuenta_cobro_service.py` (Reconciliation Note 1)
- [x] 3.11 RED: test one-time obligation required when `posicion=primera`, blank for `recurrente`/`final`
- [x] 3.12 GREEN: replace `_is_first_cuenta()` (L266-278) with `posicion == PRIMERA` in `checklist_service.py` — simplified to a pure sync helper (no DB round-trip needed anymore, consistent with the project's round-trip-reduction convention)
- [x] 3.12b RED+GREEN: rewire coherence R1 — replace `_derive_numero_cuota()` interim chronological derivation in `coherence_validator_service.py` with the stored `numero_cuota`/`posicion` fields, keeping the rule's external shape (slice #1 verify-report WARNING 1). `_derive_numero_cuota` is KEPT as a documented defensive fallback for a cuota somehow missing the persisted field, not deleted.
- [x] 3.12c RED+GREEN: surface SOFT coherence findings in the radicar response — add an `advertencias_coherencia` field to the radicar response schema so callers see SOFT findings without a second `validar_coherencia_cuenta` call (slice #1 verify-report WARNING 2)
- [x] 3.13 Integration test: `crear_cuenta_cobro`/`obtener_cuenta_cobro` outputs include new fields (service-level + API-level)
- [x] 3.14 Update `tests/test_radicar.py::_CODIGOS_OBLIGATORIOS` mirror with `CUOTA_POSITION_CONFLICT` — **DEVIATION** (same as slices #1/#2 tasks 1.22/2.18): confirmed `_CODIGOS_OBLIGATORIOS` is a checklist requisito-code list, not an error-code mirror; added a permanent NOTE comment there pointing to the real mirror, and added `test_crear_cuenta_cobro_duplicate_final_returns_cuota_position_conflict_code` to `tests/test_error_codes.py` instead.
- [x] 3.15 Local verification gate: `make format && make lint && uv run python -m pytest` green — `ruff check`/`ruff format --check` clean on all new/changed files (baseline-only pre-existing issues elsewhere, confirmed via `git stash` diff); mypy: 0 new errors (266 before and after, confirmed via `git stash` diff on `mypy app/`); full suite: 1258 passed / 14 deselected (baseline 1239 passed + 19 new tests, 0 regressions).
- [x] 3.16 **No push to remote without explicit user OK** — not pushed; commits are local only.

**Work-unit commits**: (a) 3.2-3.6 model+migration → `feat(cuota-position): add numero_cuota/posicion/informe_final fields`; (b) 3.7-3.10 write-time invariant → `feat(cuota-position): enforce CUOTA_POSITION_CONFLICT at creation`; (c) 3.11-3.12c → `fix(checklist): replace _is_first_cuenta heuristic with stored posicion` + `feat(coherence): rewire R1 to stored position and surface SOFT findings`.

---

## Slice 4 — Contract Adición events (P1b) · PR 4 · migration `026` · ~350 lines · depends on #3

- [x] 4.1 Checkpoint: confirm migration number `026` still free (rebase if needed) — confirmed via `alembic heads` (`025_cuota_position` sole head before this slice); used `026`.
- [x] 4.2 RED: model test — `adiciones_contrato` table shape (tipo enum, rpc_nuevo, cdp_nuevo, valor_adicion, nueva_fecha_fin, descripcion, fecha_evento); `Obligacion.una_vez: bool = False` — `tests/test_adiciones_contrato.py`.
- [x] 4.3 GREEN: create `app/models/adicion_contrato.py`; add `una_vez` to `app/models/obligacion.py` — also added `Contrato.adiciones` relationship (non-`selectin`, explicit-query pattern) and registered `AdicionContrato` in `app/models/__init__.py`.
- [x] 4.4 RED+GREEN: `alembic/versions/026_*` — `op.create_table` + `op.add_column`
- [x] 4.5 Neon verification: **BLOCKED** — no reachable Neon connection in this session (same constraint as slice #3, task 3.5; `.env`/`DATABASE_URL` not accessible to the executing agent). Verified instead via (a) `alembic heads`/`alembic history` parse cleanly with `026` as sole head, and (b) a dry-inspection script binding `alembic.operations.Operations` to a throwaway sqlite DB, calling `upgrade()` directly, confirming the exact expected column set on both `adiciones_contrato` (id/contrato_id/tipo/numero/rpc_nuevo/cdp_nuevo/valor_adicion/nueva_fecha_fin/descripcion/fecha_evento/created_at/updated_at) and `obligaciones` (+`una_vez`), inserting a row successfully, then calling `downgrade()` and confirming clean removal of both the table and the column. Real Postgres/Neon apply still required before merge — **flagged as a pre-merge gate**.
- [x] 4.6 RED+GREEN: `schemas/adicion.py` — `AdicionCreate`, `AdicionOut`
- [x] 4.7 RED: test recording an Adición with new RPC/CDP persists and is queryable; two events preserved in order (second doesn't overwrite first) — `tests/test_adiciones_contrato.py`.
- [x] 4.8 GREEN: implement `registrar_adicion`/`listar_adiciones` in new `app/services/adicion_contrato_service.py`. CROSS-CHANGE NOTE (2026-07-11): `secop_service.obtener_adiciones_contrato()` (landed in secop-full-acquisition slice 2) is the SECOP-side source — its `valor_adicion` is best-effort regex-parsed from free-text `descripcion` and MAY BE None (cb9c-h8sn has no structured value field); rows with `tipo="ADICION EN EL VALOR"` are the real additions. Handle None gracefully when materializing events. — **Handling implemented**: `AdicionContrato.valor_adicion` is nullable end-to-end (model/schema/service all accept `None` and never coerce to `0`); `registrar_adicion` accepts `valor_adicion: Decimal | None = None` as a first-class param, proven by `test_registrar_adicion_handles_none_valor_adicion` and `test_adicion_contrato_accepts_null_valor_adicion`. This slice does NOT build an automatic SECOP→`adiciones_contrato` sync job (no task in this slice asks for one) — `registrar_adicion` is the single write path an agent/orchestrator (or a future sync job) calls, whether the caller sourced `rpc_nuevo`/`valor_adicion` from SECOP or from a human.
- [x] 4.9 GREEN: wire `ValidationContext.adiciones` in `coherence_validator_service.py` to query real `adiciones_contrato` (completes R6, deferred from slice #1 per Reconciliation Note 4) — new `_load_adiciones_contexto()` chains each event's `rpc_nuevo`/`cdp_nuevo` against the closest EARLIER event that set the same field (the model only stores what an event introduced, not what it replaced) to reconstruct R6's expected `rpc_anterior`/`rpc_nuevo`/`cdp_anterior`/`cdp_nuevo` dict shape unchanged from the slice #1 stub's contract. Also returns `tiene_prorroga: bool` for R7 (task 4.11), now a field on `ValidationContext`.
- [x] 4.10 RED: test R6 flags stale clause using real Adición event data (replaces slice #1's stub) — `tests/services/test_coherence_validator_service.py::test_r6_hard_finding_with_real_adicion_events` (+ `test_r6_no_finding_on_first_recorded_event_no_prior_chain` and `test_r6_no_finding_when_new_identifier_already_present` for the no-false-positive edges). The slice #1 stub-level unit test (`test_r6_hard_finding_when_stale_rpc_present_in_stubbed_context`) is KEPT as a rule-level unit test independent of the loader — it still constructs `ValidationContext` manually.
- [x] 4.11 RED: test new SOFT rule — prórroga present + `informe_final=True` on an earlier cuota emits a warning, does not auto-unflag (Reconciliation Note 3) — `test_r7_soft_finding_when_prorroga_and_prior_cuota_marked_final` (+ `test_r7_no_finding_without_prorroga`, `test_r7_no_finding_when_prior_cuota_not_marked_final`). **DEVIATION (documented, scoping decision)**: `ValidationContext` only carries `cuenta`/`prior_cuenta` (not the full cuota history), so R7 checks the immediately-preceding cuota (`ctx.prior_cuenta.informe_final`) combined with `ctx.tiene_prorroga` (any recorded prórroga for the contrato) — not an exhaustive scan of every historical cuota. This matches the rule's SOFT/advisory nature and the existing `ValidationContext` shape; a contract with a "final" cuota further back than the immediate predecessor is not covered by this rule in this slice.
- [x] 4.12 GREEN: register `stale_final_after_prorroga` (R7, SOFT) in `RULES` — `RULES` registry now has 7 entries (`test_rules_registry_has_seven_rules`, replacing slice #1's `test_rules_registry_has_six_rules`).
- [x] 4.13 RED+GREEN: `app/tools/catalog/adiciones.py` — `registrar_adicion_contrato` (write), `listar_adiciones_contrato` (read) — registered in `app/tools/catalog/__init__.py`; integration tests via `invoke_tool` in `tests/test_adiciones_contrato.py`.
- [x] 4.14 Local verification gate: `make format && make lint && uv run python -m pytest` green — see slice verification note below.
- [x] 4.15 **No push to remote without explicit user OK** — not pushed; commits are local only.

**Work-unit commits**: (a) 4.2-4.6 model+migration → `feat(adiciones): add adiciones_contrato table and una_vez flag`; (b) 4.7-4.8, 4.13 service+tool → `feat(adiciones): record and list contract addition events`; (c) 4.9-4.12 → `feat(coherence): wire real adicion events into R6 and add R7 prorroga warning`.

---

## Slice 5 — Template ingestion (P2a) · PR 5 · migration `027` · ~380 lines (risk Medium-High) · depends on #4

- [x] 5.1 **Checkpoint (mandatory): confirm `backend-local-first-sync` merge status / rebase migration numbers** before starting — this slice touches document-reading paths — confirmed via `alembic heads` (026 sole head before this slice; no `backend-local-first-sync` branch present locally); used 027.
- [x] 5.2 RED: model test — `PlantillaOrganismo` shape (`usuario_id`, `entidad`, `tipo_documento`, `formato`, `estructura_json` JSONB, `fuente_documento_id` FK, timestamps) — `tests/test_plantilla_organismo.py`.
- [x] 5.3 GREEN: create `app/models/plantilla_organismo.py` — **DEVIATION**: `tipo_documento`/`formato` are plain `String` columns (not `Enum`), per design D5's literal field shape — deliberately avoids adding a new Postgres enum type (and the `values_callable` label-mismatch gotcha class) for this slice; validated as a `Literal` at the schema layer instead. Added a `UniqueConstraint(usuario_id, entidad_normalizada, tipo_documento)` (not in design's literal field list, but required so re-ingestion updates instead of duplicating — matches the ingestion service's upsert behavior).
- [x] 5.4 RED+GREEN: `alembic/versions/027_*` — `op.create_table` with JSONB column.
- [x] 5.5 Neon verification: **BLOCKED** — no reachable Neon connection in this session (same constraint as slices #3/#4). Verified instead via (a) `alembic heads`/`alembic history` parse cleanly with `027` as sole head, and (b) compiling the migration's DDL against the `postgresql` dialect via a mock engine (JSONB cannot execute against the sqlite test DB — unlike slices #3/#4's plain column adds, this is a genuine Postgres-only type, so the sqlite dry-run technique used previously does not apply here). Confirmed well-formed `CREATE TABLE`/`CREATE INDEX`/`DROP` DDL for both upgrade and downgrade. Real Postgres/Neon apply still required before merge — **flagged as a pre-merge gate**.
- [x] 5.6 RED+GREEN: `schemas/plantilla_organismo.py` — **DEVIATION**: `EstructuraPlantillaLLM.anexo_refs` is `list[str]` (literal anexo-reference strings), not design D5's shorthand `anexo_refs: bool` — required to satisfy the spec's explicit "Anexo reference preserved verbatim" acceptance criterion, which a bare boolean cannot. `estructura_json` is an unconstrained JSON blob (no DB schema enforces its shape), so this is an additive superset, not a contradiction — `bool(anexo_refs)` recovers the flag design describes.
- [x] 5.7 RED: test successful DOCX extraction persists columns/sections/anexo refs for the organism; anexo ref string preserved verbatim — `tests/services/test_plantilla_organismo_ingestion.py` (DAGMA 2-col + COEMPRESAR 3-col+anexo fixtures).
- [x] 5.8 GREEN: extend `requisito_inference_service.py` with structure extraction (`inferir_estructura_plantilla`, `ingerir_plantilla_organismo`, `obtener_plantilla_organismo`); normalize organism key via `text_match.normalize` over `Contrato.entidad` — **DEVIATION (scoping)**: design's File Changes table names only `requisito_inference_service.py` as touched for this behavior (no separate `plantilla_organismo_service.py` is listed anywhere in design/tasks); persistence/CRUD functions were added to that same file alongside extraction rather than inventing an undeclared service module, since the file already does DB-aware work (`inferir_requisitos` calls `checklist_service`).
- [x] 5.9 RED: test unreadable/scanned template degrades safely — no structure persisted, no hard error, ingestion of contract/organism record proceeds — `test_unreadable_template_degrades_safely_no_structure_persisted`.
- [x] 5.10 GREEN: implement graceful-degradation path — `ingerir_plantilla_organismo` returns `(None, avisos)` without persisting or raising when `inferir_estructura_plantilla` returns `None`.
- [x] 5.11 RED: test vision fallback chain (reused from CONTRATO extraction, `document_service` L407-426) is retried before declaring failure — `test_vision_fallback_retried_before_declaring_failure`.
- [x] 5.12 GREEN: wire resilient reader reuse — renamed `document_service._vision_model_chain` → public `vision_model_chain()` (one internal call-site updated) so `requisito_inference_service._extraer_estructura_via_vision` reuses the exact same resilient chain instead of a divergent reimplementation.
- [x] 5.13 RED+GREEN: `app/tools/catalog/plantillas_organismo.py` — `ingerir_plantilla_organismo` (write), `obtener_plantilla_organismo` (read) — `tests/test_plantilla_organismo_tool.py`.
- [x] 5.14 RED: test packager (slice #2) applies organism-specific folder structure when `PlantillaOrganismo` exists, falls back otherwise — `tests/test_informe_service.py::test_zip_evidencias_uses_anexo_style_folders_when_organismo_template_exists` / `test_zip_evidencias_uses_default_numbered_folders_when_no_organismo_template`. Superseded the slice #2 stub-only test (`test_resolver_estructura_organismo_es_stub_hasta_slice_5` → split into `..._returns_none_when_not_ingested` / `..._returns_real_lookup_when_ingested`).
- [x] 5.15 GREEN: wire real `PlantillaOrganismo` lookup into `informe_service` folder-structure builder (completes deferred task 2.9) — **DEVIATION (scoping, deliberate)**: tasks/design leave "applies organism-specific folder structure" underspecified (full column/section-driven layout is slice #6's `adaptive-informe-generation` responsibility, applied to the DOCX informes, not this ZIP folder builder). Minimal, testable, zero-regression wiring implemented: when the resolved structure's `anexo_refs` is non-empty (COEMPRESAR-style), evidence folders are numbered `"A{n}_..."` to mirror the institutional "Carpeta ... A1" convention instead of the default `"{idx:02d}_..."`; absent/empty `anexo_refs` (including DAGMA-style, no ingested template, or extraction degradation) keeps the exact pre-slice-5 default numbering — verified no-regression via the "no organismo template" test.
- [x] 5.16 Local verification gate: `make format && make lint && uv run python -m pytest` green — see slice verification note below.
- [x] 5.17 **No push to remote without explicit user OK** — not pushed; commits are local only.

**Work-unit commits**: (a) 5.2-5.6 model+migration → `feat(plantilla-organismo): add per-organism template structure model`; (b) 5.7-5.12 → `feat(plantilla-organismo): extract and persist template structure with graceful degradation`; (c) 5.13-5.15 → `feat(paquete): apply organism-specific folder structure when available`.

---

## Slice 6 — Adaptive generation (P2b) · PR 6 · no migration · ~380 lines · depends on #3, #5

- [x] 6.1 RED: test DAGMA 2-column layout selected when organism has an ingested 2-col template — `test_informe_actividades_dagma_2_columnas`
- [x] 6.2 RED: test COEMPRESAR 3-column layout + literal anexo refs included — `test_informe_actividades_coempresar_3_columnas_con_anexo_refs`
- [x] 6.3 RED: test default 4-column layout used when no organism template exists — `test_informe_actividades_default_4_columnas_sin_plantilla`
- [x] 6.4 GREEN: per-organism layout selection from `PlantillaOrganismo` wired into `informe_service` generators
- [x] 6.5 RED: test cuota 3 narrative built from cuotas 1-2 summaries; cuota 1 has no prior context — `test_contexto_progresivo_construido_desde_cuotas_previas` / `test_contexto_progresivo_ausente_en_primera_cuota`
- [x] 6.6 GREEN: progressive narrative bounded by `_MAX_TEXT_CHARS` (14000), degrading to K=3 — `test_contexto_progresivo_degrada_a_k3_cuando_excede_presupuesto`; fails-open (`test_contexto_progresivo_llm_error_falla_abierto`)
- [x] 6.7 RED: test every generated informe carries the draft header — `test_informe_actividades_siempre_carga_encabezado_borrador` / `test_informe_supervision_siempre_carga_encabezado_borrador`
- [x] 6.8 GREEN: draft label/header as a constant (not LLM-decided)
- [x] 6.9 RED: test one-time obligation blank in recurrente after primera — `test_obligacion_una_vez_visible_en_cuota_primera_y_ausente_en_recurrente`
- [x] 6.10 GREEN: honor `una_vez` (slice #4) + `posicion` (slice #3) in generator output
- [x] 6.10b Carry-over from slice #4 verify (WARNING 1): R7 `stale_final_after_prorroga` temporal ordering FIXED — now compares prórroga `fecha_evento` vs the final cuota's period. Tests: `test_r7_no_finding_when_prorroga_is_before_final_cuota` / `test_r7_soft_finding_when_prorroga_is_after_final_cuota_same_year_different_month`.
- [x] 6.11 Integration test: extended `generar_informe_cuota` tool — `test_generar_informe_actividades_tool_aplica_layout_organismo_y_borrador`
- [x] 6.12 Local verification gate: 59 targeted passed; ruff check + format --check clean on all touched files. (Full-suite run pending orchestrator confirmation — apply agent stalled on a stream watchdog during the lint step, not a test failure; orchestrator finished the gate.)
- [x] 6.13 **No push to remote without explicit user OK** — not pushed; local commits only (verified `git log origin/master..HEAD` non-empty). Post-verify cleanup (orchestrator): fixed the 7 new mypy `Document`-annotation errors via a `DocxDocument` type alias (informe_service.py now BELOW its pre-slice-6 mypy baseline), ruff+tests still green.

**Work-unit commits**: (a) 6.1-6.4 → `feat(informe): select per-organism layout for generated informes`; (b) 6.5-6.8 → `feat(informe): add progressive narrative and always-draft label`; (c) 6.9-6.10 → `feat(informe): blank one-time obligations after cuota 1`.

---

## Slice 7 — Requisito comprehension + e2e prep (P2c) · PR 7 · no migration · ~300 lines · depends on #1, #2, #5

- [x] 7.1 **Checkpoint (mandatory): confirm `backend-local-first-sync` merge status / rebase migration numbers** before starting — this slice touches document-reading paths — confirmed `_process_uploaded_document` absent from `document_service.py` (branch not merged); this slice adds NO migration, so no rebase/renumbering is needed.
- [x] 7.2 RED: test structured extraction returns name/category/`solo_primera_cuenta`/autogen-support fields from a requirement document
- [x] 7.3 GREEN: add `inferir_requisitos_estructurados` to `requisito_inference_service.py` (extends `inferir_requisitos` L88-168) — new `RequisitoEstructuradoLLM(RequisitoInferidoLLM)` schema (adds `categoria`/`permite_autogen`) + `REQUISITOS_ESTRUCTURADOS_SYSTEM` prompt; reuses `_slug`/`_map_a_estandar`/`_normalizar_keywords` unchanged.
- [x] 7.4 RED: test checklist preview reflects structured requisitos without persisting until confirmed
- [x] 7.5 GREEN: wire structured requisitos into `checklist_service.asegurar_checklist` (L300-369) preview path — new PURE `previsualizar_checklist()` (no DB writes) reusing `_codigos_estandar_a_crear` (generalized to a `_CustomLike` Protocol so it accepts both persisted `RequisitoCuenta` and non-persisted preview items) and `_is_first_cuenta`'s exact selection logic.
- [x] 7.5b Carry-over from slice #5 verify (WARNING + SUGGESTION b): validate `doc.tipo` in `ingerir_plantilla_organismo` — reject/skip ingestion of a `DocumentoFuente` whose `tipo` is not a template type (informe_actividades/informe_supervision) so a CEDULA/RUT can't be stored as a plantilla outside the documented Literal domain; restrict the `documento_fuente_id` lookup accordingly. — Raises `ValidationError` (matches the existing "sin entidad" pattern) via new `_TIPOS_PLANTILLA_VALIDOS` set.
- [x] 7.5c Carry-over from slice #5 verify (SUGGESTION a): drop the redundant `_get_contrato_con_ownership` round-trip in `_resolver_estructura_organismo` — the Contrato is already loaded+ownership-validated by `_load_context` in `generar_zip_evidencias` (perf, matches the round-trip-reduction convention). — New public `obtener_plantilla_organismo_por_contrato(db, usuario_id, contrato, tipo_documento)` (mirrors the `document_service.vision_model_chain()` public-promotion precedent, slice #5 task 5.12); `obtener_plantilla_organismo` now delegates to it.
- [x] 7.5d Carry-over from slice #6 verify (WARNING 1): implement design D6's `es_borrador: bool = True` as a machine-readable field on the informe generation output/tool response (currently only the DOCX text header exists — no API/tool caller can detect draft status without parsing the DOCX). Surface it through `preparar_radicacion`'s result so the orchestration reports draft status programmatically. — New public `informe_service.ES_BORRADOR = True` constant; surfaced as an `X-Es-Borrador` response header on the two direct DOCX download endpoints AND as `es_borrador` on `PreparaRadicacionResponse`.
- [x] 7.6 RED: test full orchestration runs checklist → coherence → packager in order, returns package location + LISTO/PENDIENTE status
- [x] 7.7 GREEN: create `app/services/radicacion_prep_service.py` — `preparar_radicacion()` calling `validar_coherencia_cuenta`, then `generar_zip_evidencias(modo="final")`/`obtener_estado_listo_pendiente`
- [x] 7.8 RED: test HARD coherence finding halts orchestration before packaging, surfaces `COHERENCE_CHECK_FAILED`
- [x] 7.9 GREEN: implement halt-before-packaging branch
- [x] 7.10 RED: test secret detection halts orchestration, surfaces `SECRET_DETECTED_IN_PACKAGE`
- [x] 7.11 GREEN: propagate packager exception through orchestration — both `SECRET_DETECTED_IN_PACKAGE` and `PACKAGE_PENDIENTE` propagate unchanged (no try/except wrapping the packager call).
- [x] 7.12 RED+GREEN: `app/tools/catalog/` — `inferir_requisitos_estructurados` (read, new `requisitos.py`), `preparar_radicacion` (write, new `radicacion.py`); new `app/schemas/radicacion_prep.py` (`PreparaRadicacionResponse`).
- [x] 7.13 Update `tests/journey/test_full_radicacion_journey.py` JourneyLedger — added step 9b: `invoke_tool("preparar_radicacion", ...)` demonstrating the 3-tool-calls-into-1 collapse for an agent-driven flow, logged as 1 new auto item (auto_count 14→15). Manual count UNCHANGED at 6 (still at the ceiling) — this HTTP-driven journey still calls `POST /radicar` directly for the actual state transition; `preparar_radicacion` doesn't remove that call, it's the pre-flight readiness+packaging step an AGENT would run INSTEAD of 3 separate tool calls before handing off to `radicar_cuenta`. UX friction confirmed acceptable: no regression, net-new capability demonstrated inline.
- [x] 7.14 Local verification gate: `make format && make lint && uv run python -m pytest` green — see slice verification note below.
- [x] 7.15 **No push to remote without explicit user OK** — not pushed; commits are local only.

**Work-unit commits**: (a) 7.2-7.5 → `feat(requisitos): extract structured requisitos and drive checklist preview`; (b) 7.6-7.12 → `feat(radicacion): orchestrate checklist, coherence, and packaging in preparar_radicacion`; (c) 7.13 → `test(journey): update ledger for radicacion-prep orchestration step`.

---

## Cross-Slice Notes

- `test_radicar.py::_CODIGOS_OBLIGATORIOS` mirror updates: slices #1, #2, #3 (4 new codes total: `COHERENCE_CHECK_FAILED`, `SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE`, `CUOTA_POSITION_CONFLICT`).
- `tests/journey/test_full_radicacion_journey.py` JourneyLedger: updated once, in slice #7, when `preparar_radicacion` adds a new orchestration step.
- Every migration slice (#3, #4, #5) MUST be verified on Neon (create_all no-ops column adds — SQLite test DB masks this).
- No slice pushes to a remote branch without explicit user OK — this is a hard session rule, encoded as the last task in every slice.

## Review Workload Forecast (recap)

- Estimated changed lines: ~350 / 380 / 320 / 350 / 380 / 380 / 300 (7 slices, ~2,460 total)
- 400-line budget risk: Medium per slice (slices #2 and #5 flagged Medium-High — may need a further split during apply if the real diff overruns ~400)
- Chained PRs recommended: Yes
- Delivery strategy: auto-chain
- Chain strategy: stacked-to-main
- Decision needed before apply: No — proceed with next autonomous slice per stacked-to-main, starting with PR 1
- Suggested work-unit PR split: PR 1 (validator) → PR 2 (packager) → PR 3 (position, migration 025) → PR 4 (adiciones, migration 026) → PR 5 (template ingestion, migration 027) → PR 6 (adaptive generation) → PR 7 (requisito comprehension + e2e prep)
