# Agent Specification

Retroactive spec. Task ids refer to `tasks.md`.

## Purpose

The chat agent can complete the full radicacion playbook reliably, within budget, observably, and with user approval before writes.

## Requirements

### Requirement: Turn observability (0.3)

Each agent turn MUST record per-tool `duration_ms`, iteration count, turn duration, model and fallback depth, with `session_id` bound to logs per turn. Instrumentation MUST fail open (a tracer outage MUST NOT abort the turn), and concurrent turns MUST NOT leak log context.

#### Scenario: Tool error still timed
- WHEN a tool raises an error
- THEN its event still carries `duration_ms`

#### Scenario: Concurrent sessions
- WHEN two turns run concurrently
- THEN each log line carries only its own `session_id`

### Requirement: Deterministic fake LLM (0.4, 0.6)

With `LLM_PROVIDER=fake` (matching case-insensitively, invalid values folding to the real provider with a logged warning), the app MUST run a scripted, network-free agent. The fake MUST derive its next step only from `messages`, thread real IDs from prior tool results, produce schema-valid calls for every catalog tool, honor `response_format`, and support `FAKE_LLM_SCRIPT=happy|malformed|stall`.

#### Scenario: Happy path with real IDs
- GIVEN a local DB and the fake provider
- WHEN the playbook runs
- THEN each tool call uses IDs returned by earlier calls and completes without domain "not found" errors

#### Scenario: Degenerate tool results
- WHEN a prior tool result is malformed, lacks an `id`, or two tools return the same key
- THEN the fake retries once, then ends with text, without crashing

### Requirement: Escape-hatch tools (1.6)

The agent MUST have tools to mark a requisito (no_aplica / cumplido_manual / desvincular / observaciones), read radicacion state, generate the cuenta PDF, and edit an actividad. They MUST reuse existing service logic. Marking on a non-editable (`enviada`) cuenta MUST be rejected; cross-user access MUST be rejected (403, the pre-existing convention).

#### Scenario: Mark on enviada
- WHEN `marcar_requisito` targets an `enviada` cuenta
- THEN it is rejected and nothing changes

### Requirement: Evidence persistence by handle (1.7)

`descubrir_evidencias` MUST return a `handle_id` additively; `persistir_evidencias` MUST accept `cuenta_id` + handle instead of a re-emitted payload. Handles MUST be bound to the owning user and expire (20 min).

#### Scenario: Large discovery
- GIVEN a discovery of 10 obligaciones and 40 links
- WHEN persisted by handle
- THEN all are persisted

#### Scenario: Expired, foreign, or repeated handle
- WHEN the handle is expired THEN an actionable "rediscover" error is returned
- AND a handle from another user is rejected
- AND redeeming twice creates no duplicate rows

### Requirement: Bounded prompt and time budget (1.8)

The per-iteration tool set MUST be phase-gated (cuenta-scoped tools unlock on evidence of a real cuenta record in any tool output, sticky across recap), staying under 6,000 prompt tokens on a fresh conversation. A turn MUST have an overall timeout, and a model outage MUST surface a message in under 2 minutes. Production MUST fail startup if `LLM_PRODUCTION_FALLBACK_MODEL` is empty.

#### Scenario: Existing cuenta discovered via listing
- GIVEN the user has a cuenta found via `listar_cuentas_cobro`
- THEN cuenta-scoped tools become available and remain so after recap

### Requirement: Full playbook completes (1.9)

The documented 10-step sequence MUST complete via `chat_with_tools` within `MAX_TOOL_ITERATIONS` (20), ending in a successful `radicar_cuenta`. `MAX_CHAT_FILES` MUST be 6.

#### Scenario: Cap hit mid-playbook
- WHEN the iteration cap is reached
- THEN committed writes survive and the recap allows continuation

#### Scenario: Failure on a mid-batch file
- WHEN upload fails on file 4 of a batch
- THEN no orphan stub actividades remain

### Requirement: Agent docked in the wizard with approval gate (3.10)

`POST /api/v1/agent/chat/stream` MUST stream tool events (with duration) over SSE. Write tools (identified by `ToolSpec.tags`) MUST wait for user approval, with cancel and retry per tool. The background turn MUST NOT rely on the request's DB session and MUST NOT hold a pooled connection during approval waits. The panel MUST NOT cover the "Radicar cuenta" button at desktop viewports, and an unknown event type MUST NOT crash the wizard.

#### Scenario: Write tool awaits approval
- WHEN the agent proposes a write tool
- THEN nothing is written until approved; cancel aborts it

## Out of Scope (tracked follow-ups)

- Google-based evidence discovery/classification automation (manual layer only).
- Nested `BaseModel` `response_format` in the fake; Langfuse enablement (undeclared dependency).
- Failure-path `duration_ms`, `retry_count`, and multi-worker handle cache.
- Merging the two informe tools (deliberately not done).
