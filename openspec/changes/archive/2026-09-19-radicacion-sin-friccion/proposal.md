# Proposal: Radicación Sin Fricción

Change: `radicacion-sin-friccion` · Project: `cashing` · Phase: sdd-apply complete — all 55 numbered slices in `tasks.md` (Phases 0-4 plus Phase 5, the post-checkpoint work and remediation) are `[x]`; awaiting a fresh `sdd-verify` (status as of 2026-09-19, backend `81aab19`, frontend `11bf4c4`; the "11/32 slices, 2026-09-13" line that stood here is obsolete) · Approach: **incremental hardening across agent, backend, frontend, and test infra — no rewrite**

**Resume point for a new session**: read `tasks.md` in this same directory for the full slice-by-slice status and history (its "Checkpoint 2026-09-19" block has the current masters and suite counts). No slice is pending; the next step is `sdd-verify`, then `sdd-archive`. Engram topic `sdd/radicacion-sin-friccion/state` (project `cashing-backend`) mirrors this file's status for cross-session recovery without re-reading the whole repo.

## Summary

Close the gap between "the domain logic exists" and "a user can actually get a cuenta de cobro from contract to radicada" — through the guided wizard, through the agent chat, fast, and verified with edge-case + Playwright coverage. Four parallel code audits (agent system, backend performance/robustness, frontend UX, test infrastructure) against `cashing-backend-master@7d33352` and `cashing-frontend@49fcf69` found the pieces are mostly built but never actually wired end to end. This change closes that wiring, in five phases, without a rewrite.

Full plan (rationale, file:line evidence, sequencing): published artifact
https://claude.ai/code/artifact/e9c67d19-e335-4cab-9cc9-d7f3e5a7b513
Engram: `project: cashing-backend`, topic `plan/radicacion-sin-friccion` (#637), session summary topic `session/2026-09-12-plan-radicacion` (#638).

## Relationship to `radicacion-stepper`

`openspec/changes/radicacion-stepper/` (F1–F10, frontend `feat/stepper-f10-free-nav`, not pushed/PR'd, `sdd-verify` recommended but never run) built the 7-step wizard shell this change assumes already exists. **This change does not redo that work.** It closes what the stepper's own `apply-progress.md` already flagged as open (CI/pipeline decision, `CuentaCobroResponse` optionality drift) plus what the stepper never attempted: the agent path, DB round-trip cost, and the wizard's final step actually calling `POST /radicar`. `radicacion-stepper` should be run through `sdd-verify` and archived independently; this change's Phase 3 builds on its shell rather than replacing it.

## Problem (confirmed by audit, not assumed)

1. **The guided wizard never radicates.** Step 7's "Finalizar" only sets local UI state (`stepper-shell.tsx:494`); the only `POST /radicar` call in the frontend lives in `checklist-full-view.tsx:367`, inside step 3's group. A user who follows the wizard to the end leaves the cuenta in `borrador`.
2. **The chat agent cannot complete the flow either.** The documented happy path costs ~18 of the 20 allowed tool iterations, re-sends ~12,500 prompt tokens per iteration (32 static tool schemas + system prompt), and `persistir_evidencias` requires the model to re-emit a payload that was already truncated to 12,000 chars under a 1,024-token output cap — a structurally impossible round-trip for any real contract.
3. **Every read is more expensive than it needs to be.** A `selectin` eager-load cascade (flagged PENDING since the July 2026 perf audit) still causes 8 queries per `Contrato`/`CuentaCobro` load; `GET checklist` costs 36–40 queries, `POST radicar` costs 60–80. Document generation (WeasyPrint, python-docx, openpyxl, zip) runs synchronously on the single Uvicorn worker (`--workers 1`), freezing the app for every user during PDF/DOCX/ZIP builds.
4. **There is no safety net.** Zero CI in either repo. 3 of 8 Playwright specs depend on real, uncommitted client documents under workspace-root `context/SYJ`/`context/DAGMA`. No LLM stub for the running app. `radicar` has no lock, so a double-submit is a real race.

## Decisions (settled by the user 2026-09-12 — do not reopen without asking)

| # | Decision | Consequence |
|---|----------|-------------|
| 1 | **Execution mode: automatic with mandatory reporting**, not interactive-blocking. This is a background/looped execution; the orchestrator states its operating parameters and proceeds rather than pausing for confirmation on every reversible step. | Sub-agents run per work unit; the user is informed after each, not asked before each. |
| 2 | **Artifact store: hybrid** — this `openspec/` change (committable, team-shareable) + Engram (`project: cashing-backend`, topic keys under `sdd/radicacion-sin-friccion/*`) + `codebase-memory-mcp` graph re-indexed on `cashing-backend-master`. | Every phase/slice completion is written to all three. |
| 3 | **Delivery strategy: ask-on-risk**, chain strategy **stacked-to-main** by default when a slice needs splitting (solo-maintainer repo, fast iteration preferred over a tracker-branch model). | Only escalated to the user if a slice's own review-workload forecast flags high risk. |
| 4 | **Local-first, always.** No `railway up`, no push that triggers a Vercel/Railway build, without an explicit local + `make test-pg` green pass first (existing standing rule, [[feedback-local-first]]). | Every phase ends with a local smoke check before any PR is opened. |
| 5 | **Strict TDD + explicit edge-case categories on every slice** (standing user rule): boundary values, empty/null/malformed input, concurrency/races, network failures (timeouts, 4xx/5xx, malformed responses), invalid state-machine transitions. A happy-path-only suite is not "done." | Named per-slice in tasks.md; a slice does not close without its edge cases green. |
| 6 | **Two-layer review before any merge**: the implementing sub-agent's own verification, plus a fresh-context adversarial reviewer (opus) per axis before a PR is opened — mirrors the 2026-09-08/09 process that caught real bugs both times it was used. | No PR opens on a single-agent's self-report alone. |
| 7 | **Work on `cashing-backend-master` only.** `cashing-backend` (`integracion-stepper-local`, 215 commits behind) is stale; its uncommitted diff is a strict subset of master and is discarded, not merged. | Every backend slice branches from `cashing-backend-master`'s `master`. |

## Scope (5 phases — see `tasks.md` for the full slice breakdown with file:line targets)

- **Phase 0 — Foundations & measurement** (this phase, in progress): repoint local dev scripts to the canonical worktree, a query-count regression fixture with per-endpoint budgets, agent-turn instrumentation (timing, iteration count, fallback depth), a minimal CI pipeline (backend pytest + frontend vitest/build + nightly `make test-pg`), and a deterministic fake-LLM seam for the running app and for Playwright.
- **Phase 1 — Close the loop**: step 7 actually calls `radicar`; `radicar` becomes idempotent/lock-safe; `requisitos_modo` NULL means one thing everywhere; no raw backend error codes reach the UI; the agent gets escape-hatch tools (`marcar_requisito`, `obtener_estado_radicacion`, PDF generation, edit activity), a fixed `persistir_evidencias` round-trip (handle-based, not re-emission), a trimmed per-iteration tool/token budget, and a turn-level timeout.
- **Phase 2 — Speed without breaking**: kill the `selectin` cascade behind `raise_on_sql` + explicit `selectinload`, remove double-loads (`_reload_*`), batch bulk-activity writes, offload document generation to threads, move package/report generation to a background job with polling, batch the evidence-upload loop, add DB row locking for `radicar`.
- **Phase 3 — Three-screen experience**: split the two largest frontend files before touching behavior, retire the duplicate creation paths (`RadicarModoModal`, the contract-detail shortcut, the legacy tabs page), collapse the wizard's cuota form into a pre-filled summary, default the checklist to standard with opt-in inference, add background progress feedback for the three currently-silent long waits, one confirmation idiom, a mobile pass, a minimal design-token consolidation, and dock the agent into the wizard sharing `stepper-state`.
- **Phase 4 — Edge cases & Playwright (transversal)**: concurrency/double-submit tests, network-failure-injection tests for Google/LLM adapters, `factory-boy`-based test builders, synthetic fixtures replacing the `context/SYJ`/`context/DAGMA` dependency, a shared `e2e/helpers/seed.ts`, systematic `data-testid` coverage, and a CI-run Playwright suite (journey + edge-cases + smoke) against the fake-LLM backend.

## Non-goals

- No rewrite of the agent engine (`CompiledGraph` stays; the chat-tools loop is fixed in place, not replaced).
- No new design system framework — token consolidation only, not a new component library.
- No mobile-app (Flutter) work — tracked separately, out of scope here (see `[[mobile-prod-readiness]]`).
- No production deploy as part of this change's own scope; each phase stops at a verified local/Neon-dev state per decision #4.
