# SECOP Dataset Ingestion Specification

## Purpose

Wires three additional Socrata datasets (Adiciones, Modificaciones a Procesos,
Ubicaciones ejecución) into SECOP II acquisition and resolves the suspected
2024 archive gap, without weakening the schema/cache/dedup guarantees
`secop_service` already provides for `jbjy-vk9h`/`p6dx-8zbt`/doc archives.

## Requirements

### Requirement: Schema verification before wiring any new dataset

The system MUST verify the live Socrata schema of `cb9c-h8sn` (Adiciones),
`e2u2-swiw` (Modificaciones a Procesos), and `gra4-pcp2` (Ubicaciones
ejecución) exposes the expected join key before treating that dataset as
wired. A dataset whose live schema lacks its expected join key MUST NOT be
silently skipped.

#### Scenario: Schema matches expectations

- GIVEN `cb9c-h8sn` live schema contains `id_contrato`
- WHEN the dataset is queried during acquisition
- THEN it is treated as wired and its rows are ingested normally

#### Scenario: Schema missing expected join key

- GIVEN a wired dataset's live schema no longer exposes its expected join key
- WHEN acquisition runs
- THEN the dataset is marked unavailable, appended to `datasets_con_error`,
  and logged — never silently dropped

### Requirement: Dataset-specific join semantics preserved

The system MUST join `cb9c-h8sn` via `id_contrato`, `e2u2-swiw` via
`proceso_de_compra` (falling back to `id_del_portafolio`), and `gra4-pcp2` via
`referencia_del_contrato`, consistent with the existing multi-key linking
pattern in `buscar_documentos_contrato`.

#### Scenario: Row links to the correct contract

- GIVEN a contract with a known `proceso_de_compra`
- WHEN `e2u2-swiw` is queried
- THEN only rows matching that `proceso_de_compra` (or its `id_del_portafolio`
  fallback) are attributed to the contract

#### Scenario: No matching key for a contract

- GIVEN a contract lacking any of a dataset's join keys
- WHEN that dataset is queried
- THEN the contract receives zero rows from it — no error, no crash

### Requirement: Non-destructive upsert consistent with existing TTLs

New dataset rows MUST be cached via non-destructive upsert (existing rows
updated, never deleted on empty/failed refresh) and MUST respect the same
freshness TTL family already used for contract/proceso/document caches.

#### Scenario: Cached rows survive a failed refresh

- GIVEN cached rows already exist for a contract
- WHEN a refresh attempt returns zero rows or errors
- THEN previously cached rows remain unchanged and are still returned

### Requirement: Adición rows consumable by contract-addition-events

`cb9c-h8sn` rows MUST be exposed in a shape (contract link, `valor_adicion`,
effective date) that the `contract-addition-events` capability can consume as
its Adición source, without requiring that capability to query Socrata or the
raw ingestion tables directly.

#### Scenario: Adición event created from ingested row

- GIVEN `cb9c-h8sn` returns a new Adición row for an already-imported contract
- WHEN `contract-addition-events` requests pending Adición data for that
  contract
- THEN it receives the contract link, `valor_adicion`, and effective date
  without touching Socrata

### Requirement: 2024 archive gap resolved by live-probe, not assumption

The system MUST live-probe whether `3skv-9na7` actually covers 2023–2024
before changing the year-range mapping, and MUST act only on the probe's
result.

#### Scenario: Probe confirms 2023–2024 coverage

- GIVEN a live probe shows `3skv-9na7` returns 2024-dated documents
- WHEN the year-range mapping is corrected accordingly
- THEN 2024-contract documents appear in acquisition results and the
  `secop_docs_gap_2024` warning log is removed

#### Scenario: Probe confirms a real gap

- GIVEN a live probe shows no dataset covers 2024 documents
- WHEN acquisition runs for a 2024 contract
- THEN the `secop_docs_gap_2024` warning log is preserved and the gap is
  surfaced as a known limitation, not treated as a bug

#### Scenario: Probe finds partial/anomalous coverage

- GIVEN a live probe shows a dataset returns SOME 2024-dated rows, but they
  are confined to a single bulk-load batch (not full-year coverage) rather
  than either a clean "covers 2024" or "returns zero 2024 rows" result
- WHEN the year-range mapping decision is made
- THEN the partial result is treated as a real gap for the uncovered portion
  of the year (the `secop_docs_gap_2024` warning is preserved), any factually
  inaccurate comment implying "no 2024 data exists at all" is corrected to
  describe the actual nuance, and the deviation from a clean binary probe
  result is documented rather than silently rounded to either scenario above

## Tool Surface (`TOOL_REGISTRY`)

| Tool | Semantics | Notes |
|------|-----------|-------|
| `sincronizar_documentos_secop` | write | Existing service, newly registered as a tool; now fans out to the 3 new datasets |
| `obtener_estado_datasets_secop` | read | Surfaces `datasets_con_error` and per-dataset schema-verification status |

## Error Codes

No new HTTP-facing error code; dataset unavailability is surfaced via
`datasets_con_error` (existing field), not an exception.

## Deferred to sdd-design

- Whether schema verification runs at request-time, on a schedule, or both.
- Exact migration for any new columns needed to persist Ubicaciones/
  Modificaciones a Procesos rows.
