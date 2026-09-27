# AEGIS-X REFERENCE ARCHITECTURE AUDIT

**Phase:** 0 - Audit only. No implementation performed.
**Date:** 2026-09-25
**Sources inspected:** 4 (AEGISAI, Automaton, Claude-reference archives, MIND-LLM v2)

---

## 0. SCOPE, PROVENANCE, AND LEGAL BOUNDARY

### 0.1 What was inspected

| Source | Path | Size | Access |
|---|---|---|---|
| AEGISAI (main) | `C:\Users\Admin\Downloads\AEGISAI` | 249 files, 348 MB | Full read |
| AEGIS-X Master Spec | `C:\Users\Admin\Downloads\AEGIS-X_OPENCode_MASTER_SPEC.md` | 7.7 KB / 332 lines | Full read |
| Automaton | `C:\Users\Admin\Downloads\automaton-main\automaton-main` | 20,750 files | Deep analysis |
| MIND-LLM v2 | `C:\Users\Admin\Downloads\mind-llm-v2(2)\mind-llm-v2` | 256 files, 6.18 GB | Deep analysis |
| Claude ref #1 | `claude-code-public-package-extract-main.zip` | 50.8 MB, 2,239 entries | Deep analysis |
| Claude ref #2 | `claude-code-sourcemap-main.zip` | 77.1 MB, 5,724 entries | Deep analysis |
| Claude ref #3 | `claude-code-clone-main(1).zip` | - | **NOT PRESENT** |
| Claude ref #4 | `claude-main(1).zip` | - | **NOT PRESENT** |

### 0.2 Legal boundary (mandatory, per spec "Important rule")

The Claude archives are **third-party research re-packages** of the public npm package `@anthropic-ai/claude-code@2.1.88`, recovered from a source map. They are **not open source**. Archive 1's own README states: *"Do not treat this repository as open source. Do not reuse Anthropic's proprietary code in commercial products or closed redistributions."* `package/cli.js` carries `(c) Anthropic PBC. All rights reserved`.

**This audit extracts only general architectural ideas, terminology, and structural patterns.** No proprietary implementation is reproduced. No credentials, keys, tokens, secrets, or private endpoints were sought or surfaced. The `cli.js.map` `sourcesContent` field (which embeds verbatim upstream source) was **deliberately not read** - only its `sources` array was used for module boundaries.

Several identified patterns (fail-closed tool defaults, layered context compaction, per-agent-class tool scoping, typed decision reasons, one-function-per-migration config versioning) are common industry practice and are safe to reimplement from first principles.

### 0.3 What was NOT done

No source file was modified, created, deleted, moved, or renamed. No dependency installed. No dataset downloaded. No training started. No MIND-LLM checkpoint altered. No destructive command run. The only file created is this report.

---

# A. CURRENT AEGISAI ARCHITECTURE

## A.1 Measured inventory

| Package | Files | LOC | Status |
|---|---:|---:|---|
| `core/` | 4 | 430 | Config, models, errors - complete |
| `runtime/` | 2 | 139 | `Runtime`, 150 LOC, one linear `run()` |
| `inference/` | 3 | 210 | ABC + `OllamaProvider` on `/api/generate` |
| `agents/` | 2 | 61 | `Agent`, 65 LOC, pass-through adapter |
| `tools/` | 5 | 400 | Tool ABC, registry, permissions, calculator |
| `storage/` | 4 | 425 | SQLite, 2 tables, migration framework |
| `observability/` | 2 | 70 | JSON log formatter |
| `cli/` | 2 | 315 | argparse, 5 subcommands + legacy |
| `memory/` | 1 | 1 | **STUB** |
| `security/` | 1 | 1 | **STUB** |
| `api/` | 1 | 1 | **STUB** |
| `training/` | 2 | 419 | Legacy single-source preparer |
| `data_pipeline/` | 13 | 2,400 | **11 stages, 28 tests, actually executed** |
| `tests/` | 15 | 2,400 | 90 tests, all offline |
| **Total** | **59 .py** | **~7,700** | `dependencies = []` - zero third-party runtime deps |

## A.2 Actual runtime dataflow

```text
user input
  |
cli/main.py (argparse; 5 subcommands + legacy one-shot)
  |
runtime/runtime.py :: Runtime.run()   <- 8 linear steps, NO loop
  |- storage.create_conversation / get_conversation
  |- Conversation.add_message(USER) -> storage.append_message   [COMMITTED FIRST]
  |- agents/agent.py :: Agent.run()
  |    \- inference/ollama.py :: OllamaProvider.generate()
  |         \- POST /api/generate  {"model","prompt","stream":false}
  |              \_ _build_prompt() FLATTENS transcript
  |                 -> "Conversation history:\nrole: content\n..."
  |- Conversation.add_message(ASSISTANT)  [dedup guard at :104-121]
  \- storage.append_message(ASSISTANT)
  |
AgentResponse -> stdout
```

## A.3 Verified structural defects (blocking)

| # | Defect | Evidence | Consequence |
|---|---|---|---|
| D1 | Wrong Ollama endpoint | `inference/ollama.py:53` uses `/api/generate`; `/api/chat` never used | No chat template, no native roles, no tool protocol |
| D2 | `Message` cannot represent a tool result | `core/models.py:34-79` - no `tool_call_id`, no `tool_name` | Tool results are uncorrelatable. Hard blocker |
| D3 | Tool system disconnected from LLM | `Agent.execute_tool` (`agents/agent.py:60`) has **one caller: `tests/test_agent.py:43`** | `ToolRegistry.list_tools()` / `validate_requested_tools()` (`tools/registry.py:33-42`) exist for the model bridge and are used **only by tests** |
| D4 | `PROCESS_EXECUTION` permanently denied | `tools/permissions.py:31-32` raises unconditionally; pinned by `tests/test_tools.py:104-111` | Code-execution agent cannot be built without reversing a tested guarantee |
| D5 | No agent loop | No `while` in `agents/`, `runtime/`, `inference/` | `Agent.run` calls the provider exactly once |
| D6 | No context/token management | `inference/ollama.py:94-98` resends entire transcript; no `options{}` sent | Unbounded growth; no `num_ctx`, `temperature`, `stop` |
| D7 | `health_check` false-positive | `inference/ollama.py:115-116` appends to `detail`; `ProviderHealth.healthy` aliases `reachable` (`inference/base.py:17-18`) | A missing model reports healthy, exit code 0 |
| D8 | 2-table schema | `storage/sqlite.py:26-51` - only `conversations`, `messages` | Nowhere to persist run/step/tool state |
| D9 | Zero async | grep across 59 files: **0 hits** for `asyncio` / `async def` / `threading` / `subprocess` | No concurrency, no background work, no streaming |
| D10 | `Event` + `log_event` dead | `core/models.py:210-239` (constructed only by `tests/test_models.py:49`); `observability/logging.py:70-75` never called | Event/trace substrate ~80% built, wired to nothing |
| D11 | `save_conversation` swallows conflicts | `storage/repositories.py:169` uses `INSERT OR IGNORE` | Message updates silently impossible |
| D12 | Test writes to real DB | `tests/test_runtime.py:40,66,74` - `Settings()` default path, no storage override | Creates `data/aegisai.db` in the repo |

## A.4 Data pipeline - the strongest subsystem

`data_pipeline/` is 11 stages / ~2,400 LOC / 28 tests and **has actually been executed** (311 MB of artifacts).

```text
inspect -> download -> validate -> normalize -> classify -> filter
        -> per-source dedup -> global dedup -> split -> mixture -> statistics
        + prepare.py DAG + run manifest (config/input/output SHA-256 chain)
```

**Verified defects (data side):**

| # | Defect | Evidence | Measured impact |
|---|---|---|---|
| D13 | **Token accounting inflated 1.46x** | `data_pipeline/statistics.py:81-83` counts `tool_results` on top of content it already counted | Every `*/statistics.json` + `final-statistics.json` token figure is wrong. Measured on `mixture.jsonl`: reported 1,076,441 vs true 752,795 |
| D14 | Schema stores tool data twice | `data_pipeline/normalize.py:63-76` - `tool_calls` in messages **and** top-level; `tool_results` mirrors all 1,099 tool messages exactly | ~40% storage waste; feeds D13 |
| D15 | Classification by dataset name, not content | `data_pipeline/classify.py:81-99` - `"code"` in a subset name means `CODING` | 385/481 `neulab_codeact` labeled `CODING` though record 1 is a `wiki_table_questions` SQL task |
| D16 | No safety stage | Only 3 regexes producing a *flag*, not a rejection (`data_pipeline/filter.py:23-27,138`) | 82 PII-flagged records are **emitted** into the mixture |
| D17 | No license enforcement | `license` is a metadata string only; no allow/deny list | `neulab` is `per-subset` (`configs/dataset.toml:62`) with no per-subset resolution |
| D18 | No tokenizer | grep: 0 hits for `tokeniz` / `input_ids` / `attention_mask` / `.arrow` | Pipeline terminates at JSONL |
| D19 | Orphan outputs | `datasets/processed/local/`, `datasets/manifests/local/` reference a `local` source absent from `configs/dataset.toml` | 3 records; validation/test splits are 0 bytes |
| D20 | Configured-but-never-downloaded | `nvidia/Nemotron-Agentic-v1` (`configs/dataset.toml:50`) - 335,122 rows incl. a 316,094-row `tool_calling` split | Highest-value unexploited asset |
| D21 | `datasets/` not gitignored | `.gitignore` covers `data/`, `*.sqlite*` but not `datasets/` | 311 MB of third-party content would be committed |

## A.5 Measured corpus state

| Source | Seen | Accepted | Rejected | Categories | Tool calls | Tool msgs |
|---|---:|---:|---:|---|---:|---:|
| toprak | 500 | 500 | 0 | TOOL_USE 500 | 965 | 965 |
| neulab_codeact | 500 | 481 | 19 (`empty_assistant`) | CODING 385, ERROR_RECOVERY 96 | 1,264 | **3** |
| neulab_openhands | 127 | 101 | 26 (`too_long` 24, `empty_user` 2) | CODING 36, ERROR_RECOVERY 62, UNCLASS 3 | 1,332 | 1,245 |
| neulab_mind2web | 118 | 118 | 0 | WEB_TASK 118 | **0** | **0** |
| starcoder | 500 | 500 | 0 | CODING 500 | 0 | 0 |
| **mixture.jsonl** | 500 | 500 | - | CODING 275, TOOL_USE 100, WEB_TASK 75, ERROR_RECOVERY 50 | 1,458 | 1,099 |

Global pool 1,700 records; 0 cross-source duplicates; final split 405/45/50.
**`PLANNING`, `DEBUGGING`, `REASONING`, `AGENT_TRAJECTORY`, `VERIFICATION` = 0 records** (`datasets/manifests/mixture.json:12-21`).

---

# B. CURRENT AUTOMATON CAPABILITIES

**Project:** `@conway/automaton` v0.2.1 - TypeScript 5.9, ESM, Node >= 20, `better-sqlite3`, vitest 2.x.
**Layout:** `src/` (runtime), `src/__tests__/` (67 files, ~1,559 test blocks), `aegis-ai/` (second runtime with session/approval layers), `packages/cli/`.
**Measured schema:** `SCHEMA_VERSION = 11`, **36 tables**. Its own `ARCHITECTURE.md` claims 8/22 - documented drift.

## B.1 Capabilities AEGIS-X should reproduce

| # | Capability | Automaton implementation | AEGIS-X action |
|---|---|---|---|
| B1 | **Turn-structured agent loop** | `src/agent/loop.ts:395` - 12-step turn: sleep check, inbox claim, context build, pre-turn memory, orchestrator tick, todo injection, route, tool exec, **atomic persist**, post-turn memory ingest, loop detection | Adopt the 12-step shape; **reject** the sleep/finance/credit steps |
| B2 | **Transactional turn commit** | `loop.ts:690-700` - turn + all tool calls + inbox ack in ONE `db.runTransaction()` | **Adopt verbatim** - directly fixes AEGISAI's split user/assistant commits |
| B3 | **Goal/Task DAG** | `src/orchestration/task-graph.ts` (700 LOC) - `TaskStatus` 7 values, `GoalStatus` 4, `TaskNode`, DFS cycle detection, `getReadyTasks`, auto-unblock via `json_each`, block-dependents-on-failure | **Adopt** - the single best planning model in any reference |
| B4 | **Validated planner output** | `src/orchestration/planner.ts:767` - `validatePlannerOutput()` re-implements a schema validator with path-tagged errors (`tasks[3].dependencies must be a non-negative integer`) and rejects cycles | **Adopt** - LLM output must never be trusted |
| B5 | **Replanner trigger union** | `src/orchestration/plan-mode.ts:275-302` - `ReplanTrigger` = `task_failure` / `budget_breach` / `requirement_change` / `environment_change` / `opportunity`; budget breach at >1.5x estimate | **Adopt** - spec section 3 requires a Replanner |
| B6 | **Orchestrator FSM** | `src/orchestration/orchestrator.ts` - 8 phases `idle -> classifying -> planning -> plan_review -> executing -> replanning -> complete/failed`; `maxReplans = 3` | **Adopt** |
| B7 | **Complexity gating** | `classifyComplexity()` - cheap-tier call, `requiresPlanMode = estimatedSteps > 3` | **Adopt** - avoids over-planning simple tasks |
| B8 | **Tiered model routing** | `src/inference/router.ts` - `SurvivalTier` x `InferenceTaskType` routing matrix + `InferenceBudgetTracker` | **Adapt** - drop survival tier, keep the matrix concept |
| B9 | **Failover client** | `src/inference/inference-client.ts:1221` - resolve candidates, skip open-circuit, skip capability-mismatch, retry `[1000,2000,4000]`, **advance to next provider**; `UnifiedInferenceResult` normalizes all providers to one shape | **Adopt** - spec "Model architecture" demands the runtime not depend on one model |
| B10 | **5-tier memory** | `working` / `episodic` / `semantic` / `procedural` / `relationship`, each a SQLite manager with its own table | **Adopt** - spec "Memory" names exactly these four |
| B11 | **Memory budget with rollforward** | `MemoryBudgetManager.allocate()` - per-tier caps (working 1500, episodic 3000, semantic 3000, procedural 1500); **unused budget rolls forward** | **Adopt** |
| B12 | **5-stage compression cascade** | `src/memory/compression-engine.ts` - thresholds 70/80/85/90/95% -> compact_tool_results -> compress_turns -> summarize_batch -> **checkpoint_and_reset** -> emergency_truncate; `CompressionAction` is a discriminated union | **Adopt, and actually wire it** (Automaton built it and never connected it) |
| B13 | **Policy engine, first-deny-wins** | `src/agent/policy-engine.ts` - `PolicyAction = allow/deny/quarantine`; rules sorted by `priority` ascending; every decision written to `policy_decisions` with `toolArgsHash: sha256(args)` | **Adopt** - this is the model for AEGIS-X's PermissionManager |
| B14 | **Priority-ordered rule modules** | `validation(100)` / `path-protection(200)` / `command-safety(300)` / `authority(400)` / `rate-limits(600)` / `financial` | **Adopt** - priority ordering is the key idea |
| B15 | **Queryable audit log as rate-limit state** | `policy-rules/rate-limits.ts` queries `policy_decisions` to enforce `1/day`, `10/hr`, `3/day` | **Adopt** - elegant; no separate counters needed |
| B16 | **Harness abstraction for sub-agents** | `src/agent/harnesses/` - `CodingHarness` / `GeneralHarness` / `OrchestratorHarness` + `HarnessRegistry.register(role, ctor)` | **Adopt** - spec "Multi-agent target" |
| B17 | **Sub-agent filesystem boundary** | `loop.ts:198` `allowedEditRoot: process.cwd()`; `confineToWorkspace(filePath, allowedEditRoot)` in each harness | **Adopt** |
| B18 | **Sub-agent budget isolation** | `createBudgetFromTask()` -> `IterationBudget { maxTurns, maxCostCents, timeoutMs }`; parent capped at 25 turns / 3 children | **Adopt** |
| B19 | **Scoped worker identity** | `createWorkerIdentity()` clones parent, renames `worker-<role>-<suffix>`, `address: local://<id>`, per-worker sandbox | **Adopt** |
| B20 | **Durable scheduler with leases** | `src/heartbeat/scheduler.ts` - `tickInProgress` re-entrancy guard, `LEASE_TTL_MS = 60_000`, `acquire/release/clearExpiredLeases`, `cron-parser`, per-task `maxRetries`, `nextRunAt` re-arm | **Adopt** - spec "Scheduler" |
| B21 | **Recursive setTimeout, not setInterval** | `src/heartbeat/daemon.ts:116-126` - explicit comment: overlap protection | **Adopt** - small, correct, easy to get wrong |
| B22 | **Wake-event queue** | `insertWakeEvent()` / `consumeNextWakeEvent()` - **atomic dequeue**, `wake_events` table | **Adopt** |
| B23 | **Event-stream log** | `event_stream` table + `EventStream.append()`; 17-value `EventType` union; `compact(olderThan, "reference"/"summarize")` | **Adopt** - AEGISAI has a dead `Event` dataclass to build on |
| B24 | **Tool-to-prompt trust fencing** | 9-layer system prompt; untrusted layers individually fenced; skills get 3 independent sanitization passes | **Adopt** - required before any web/file tool |
| B25 | **Anti-loop as a subsystem** | `IDLE_ONLY_TOOLS` (20 names) + `MUTATING_TOOLS` blocklist (~45) + reusable `LoopDetector` (identical-args 3x, pattern 3x, idle-only 3x) with **escalation warn -> block**; feeds a synthetic `system` message with an escape hatch | **Adopt** - MIND-LLM at 0 capability will loop |
| B26 | **Degrade-never-throw discipline** | 30+ catch-and-continue sites: memory failure never blocks the loop; `creditsCents: -1` sentinel; planner failure falls back to a single-task plan; logger/metrics never throw | **Adopt** |
| B27 | **Git-versioned state** | `src/git/state-versioning.ts` - `git init ~/.automaton` + `.gitignore` excluding wallet/keys/db; typed `commitStateChange(category)` | **Adopt** - a checkpoint mechanism with zero new deps |
| B28 | **Migrations** | `src/state/schema.ts` - `MIGRATION_V2` ... `MIGRATION_V11`, each in a transaction, `ALTER` individually try/caught for idempotency | **Adopt** - pattern already in AEGISAI `storage/sqlite.py` |
| B29 | **Config layering** | defaults -> `automaton.json` (shallow spread, **deep-merge for 3 sub-objects**) -> `process.env` -> explicit overrides | **Adopt** - AEGISAI has 1 flat layer |
| B30 | **Secrets resolved in-memory, never written to `process.env`** | `loop.ts:136-138` explicit comment | **Adopt** |
| B31 | **Dedupe/idempotency as DB primitives** | `heartbeat_dedup` (TTL keys), `Idempotency-Key` header, atomic dequeue, `UNIQUE` idempotency column | **Adopt** |
| B32 | **Provider URL allowlist** | `isAllowedProviderUrl` (HTTPS only; HTTP loopback only; rejects embedded credentials), `isAllowedOverrideHost` | **Adopt** |
| B33 | **File-history snapshots + rewind** | `fileHistoryTrackEdit/MakeSnapshot/Rewind/CanRestore`, `/rewind` command, `checkOriginFileChanged()` | **Adapt** - a lightweight rewind without a full history store |
| B34 | **Self-description as protection** | `PROTECTED_FILES` - 24 entries listing the agent's own guardrail modules; 27 `FORBIDDEN_COMMAND_PATTERNS` blocking `sed`/`>` against them | **Optional** - only relevant if AEGIS-X can self-modify (spec does not require it) |

## B.2 What Automaton does NOT have (do not copy these gaps)

Verified absences: in-process event bus / `EventEmitter` / hook registry (only 3 callbacks + DB log tables); distributed tracing / spans / OTel; inbound HTTP server / REST / WebSocket; **process-level sandbox** (container/ns/seccomp/rlimits); streaming command output / PTY; web search tool / browser automation; **parallel tool calls in the main loop (strictly sequential, `loop.ts:647-686`)**; MCP client (stub returns a literal string); plugin ABI; git worktrees / branch-per-task; agent switching in-process; typed tool results (all tools return `string`); interactive human approval in the main runtime (only in `aegis-ai/`).

**Built-but-unwired in Automaton** (a direct warning for AEGIS-X): `ContextManager`/`CompressionEngine` are not called by `runAgentLoop`; `EnhancedRetriever` unused (loop uses plain `MemoryRetriever`); `HealthMonitor` never invoked from the loop; `plannerOutput.customRoles` validated and persisted but never applied; `DEFAULT_RETRY_POLICY` declared, never imported; `gray-matter` declared, hand-rolled instead. **Lesson: AEGIS-X must wire every subsystem it builds, and must add a test that fails if a constructed subsystem is never called.**

---

# C. CLAUDE-REFERENCE ARCHITECTURE

**Source:** `@anthropic-ai/claude-code@2.1.88`, recovered from `cli.js.map` (4,756 modules). Proprietary - **ideas only, no code.**
**Measured:** `src/` = 1,902 files, ~514,587 lines, 1,332 `.ts` + 552 `.tsx`. Largest: `screens/REPL.tsx` 875 KB, `main.tsx` 785 KB.

## C.1 The key structural idea: the UI is not the architecture

```text
entrypoints/cli.tsx   <- thin fast-path dispatcher
   |
main.tsx              <- startup coordinator (config, auth, policy, telemetry, MCP, plugins)
   \-> screens/REPL.tsx   <- 875 KB, REPLACEABLE UX layer
          |
utils/processUserInput/     normalize, attachments, hooks, slash commands
          |
QueryEngine.ts              reusable stateful conversation engine
          |
query.ts                    THE LOOP: streaming, tools, compaction, retry, stop-hooks
          |
services/api/claude.ts      model request assembly
```

**One `Tool` / `ToolUseContext` / `QueryEngine` abstraction powers three surfaces:** the REPL, headless/SDK flows, and an MCP server. **This is the seam AEGIS-X needs** if it wants CLI + API + background workers over one runtime.

## C.2 Concept catalogue

Format per concept: what it does -> why AEGIS-X needs it -> AEGISAI equivalent -> AEGISAI files -> verdict.

### C.1 - Agent loop as an async-generator state machine

- **Concept:** `query()` wraps `queryLoop()`. Each iteration streams one assistant turn, executes tool uses, decides how to continue. A typed field `transition: Continue | undefined` records **why** the loop continued, so tests assert *which recovery path fired* without inspecting message content. Constants: `MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3`, `turnCount`.
- **Why AEGIS-X:** MIND-LLM will produce malformed/empty/truncated turns. A typed continuation reason is the difference between "the loop is broken" and "the model hit a token cap and we recovered".
- **AEGISAI equivalent:** None. No loop exists.
- **Files:** `agents/agent.py`, `runtime/runtime.py`.
- **Verdict:** **NEWLY IMPLEMENT** - `agents/loop.py` + `core/transitions.py`.

### C.2 - Fail-closed defaults at the tool factory boundary

- **Concept:** every tool is built via `buildTool()` which fills `TOOL_DEFAULTS` so no tool can omit a security predicate. Documented defaults: `isReadOnly -> false` (**assume writes**), `isConcurrencySafe -> false` (**assume unsafe to parallelize**), `isDestructive -> false`, `checkPermissions -> {behavior:'allow'}`, `interruptBehavior() -> 'block'`.
- **Why AEGIS-X:** AEGISAI's `Tool.__init__` (`tools/base.py:78-98`) takes 5 required args, but a new `Tool` subclass can still omit a risk declaration entirely.
- **AEGISAI equivalent:** Partial - `required_capability` is mandatory (`tools/base.py:85`).
- **Files:** `tools/base.py`, `tools/registry.py`, `tools/permissions.py`.
- **Verdict:** **REDESIGN** `tools/base.py` - add a `build_tool()` factory plus `risk_level`, `is_read_only`, `is_concurrency_safe`, `is_destructive`, `timeout_s`, `interrupt_behavior` with the same fail-closed defaults.

### C.3 - `searchHint` and progressive tool disclosure

- **Concept:** each tool carries a one-line `searchHint` ("Prefer terms not already in the tool name"). Tools can be sent with `defer_loading: true`; the model must call `ToolSearch` to load a full schema first. `ToolSearch` scoring: required vs optional terms, prefix match, **`searchHint` scored higher than description**. `alwaysLoad` for a small always-included set. MCP tools named `mcp__<server>__<tool>`.
- **Why AEGIS-X:** AEGIS-X targets 14 tool families. Sending all schemas every turn will not fit a small context window (see conflict R2).
- **AEGISAI equivalent:** None.
- **Files:** `tools/base.py`, `tools/registry.py`, `inference/*`.
- **Verdict:** **NEWLY IMPLEMENT** - `tools/select.py` + `to_model_schema()`. **Critical for R2.**

### C.4 - Streaming concurrent tool execution

- **Concept:** `StreamingToolExecutor` starts eligible tool calls **as blocks arrive from the stream**, not after the message completes. `ToolStatus = queued / executing / completed / yielded`. Per-tool gating via `isConcurrencySafe(input)`, evaluated on the *parsed* input. A `contextModifier` field exists specifically because parallel tools cannot mutate shared context - honored only for non-concurrency-safe tools.
- **Why AEGIS-X:** AEGIS-X runs read-only tools that are safely parallel (read_file, grep, glob, web fetch). Serial execution (as in Automaton) wastes wall-clock.
- **AEGISAI equivalent:** None.
- **Verdict:** **NEWLY IMPLEMENT** - `ToolExecutor` with an `anyio` task group, gated on `is_concurrency_safe`.

### C.5 - Three independent tool-result size budgets

- **Concept:** `DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000`; `MAX_TOOL_RESULT_TOKENS = 100_000`; `BYTES_PER_TOKEN = 4`; `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000` (catches the N-parallel-tools blowup). Oversized results spill to a file and are replaced with a preview. Read-back-capable tools (e.g. `Read`) **opt out** - the comment reads: *"Persisting to a file the model reads back with Read is circular - never persist."* `FileReadTool` sets `maxResultSizeChars: Infinity` for exactly this reason.
- **Why AEGIS-X:** A single `read_file` on a 10 MB log would blow the context window instantly.
- **AEGISAI equivalent:** None. `ToolResult.output` (`tools/base.py:12-27`) is unbounded.
- **Files:** `tools/base.py`, `core/context.py` (new).
- **Verdict:** **NEWLY IMPLEMENT** - and the "read-back tools opt out" rule is a non-obvious correctness detail worth adopting directly.

### C.6 - Two-layer tool permission model

- **Concept:** (a) **structural** allow-set per agent class - `ALL_AGENT_DISALLOWED_TOOLS`, `ASYNC_AGENT_ALLOWED_TOOLS`, `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS`, `COORDINATOR_MODE_ALLOWED_TOOLS`; intersected with (b) **dynamic** rule/mode evaluation producing a `PermissionResult`. Modes: `acceptEdits | bypassPermissions | default | dontAsk | plan`. Behaviors: `allow | deny | ask | passthrough`. Rule sources in precedence order: `userSettings -> projectSettings -> localSettings -> flagSettings -> policySettings`.
- **Why AEGIS-X:** spec "Multi-agent" names 5 agent types. A verifier agent must **not** be able to write files; a browser agent must not run shell.
- **AEGISAI equivalent:** Layer (b) only - `PermissionPolicy` (`tools/permissions.py`) is a flat capability set.
- **Files:** `tools/permissions.py`, `security/__init__.py`.
- **Verdict:** **REDESIGN** - add `AgentClass` + static allow-sets intersected with `PermissionPolicy`.

### C.7 - Every decision carries a typed reason

- **Concept:** `PermissionDecisionReason` - **11 variants**: `rule | mode | subcommandResults | permissionPromptTool | hook | asyncAgent | sandboxOverride | classifier | workingDir | safetyCheck | other`. Plus `HookResult.outcome` (4), `TaskStatus` (5), `ToolStatus` (4). Unified "explain yourself to the UI and to telemetry".
- **Why AEGIS-X:** AEGIS-X is autonomous. When a tool is denied at hour 3, the operator must know which rule fired and why.
- **AEGISAI equivalent:** None. `PermissionDeniedError(f"capability denied: {capability.value}")` (`tools/permissions.py:49`) is a bare string.
- **Files:** `tools/permissions.py`, `core/errors.py`.
- **Verdict:** **REDESIGN** - `PermissionDecision` dataclass with `decision`, `reason_code`, `reason_detail`, `rule_ids`, `risk_level`, `evaluated_at`.

### C.8 - Prefixed task IDs as an access token

- **Concept:** task ID = 1 type prefix + 8 chars from a 36-symbol alphabet with confusables deliberately excluded. Prefixes `b`(local_bash) `a`(local_agent) `r`(remote_agent) `t`(teammate) `w`(workflow) `m`(monitor_mcp) `d`(dream). Stated rationale: task output files are **predictable paths, so the ID is the access-control token** - sufficient to resist brute-force symlink attacks. `TaskStatus = pending / running / completed / failed / killed`; terminal = the last three. Output is a **disk-backed log read at a byte offset** (`outputFile`, `outputOffset`), so a poller can resume mid-stream.
- **Why AEGIS-X:** spec "Persistent tasks" requires tool history + output retrieval. Byte-offset logs let a client tail a long-running tool without re-execution.
- **AEGISAI equivalent:** None.
- **Verdict:** **NEWLY IMPLEMENT** - adopt both the prefixed-ID scheme and the byte-offset log.

### C.9 - Agents defined as markdown + frontmatter, scoped by tool allow-set

- **Concept:** `AgentDefinition` fields: `agentType, description, tools?, disallowedTools?, model?` (`'inherit'` allowed), `permissionMode?, maxTurns?, memory: 'user'|'project'|'local', background?, isolation: 'worktree'|'remote', color, whenToUse, source`. Merged from `built-in | plugin | userSettings | projectSettings | policySettings | flagSettings`. `context: 'inline' | 'fork'` - fork runs as a sub-agent with a **separate context and token budget**. `maxTurns` per agent.
- **Why AEGIS-X:** spec "Multi-agent target" names MASTER / RESEARCH / CODING / TEST / BROWSER / REVIEW-VERIFIER. Declarative agent files make these editable without code changes.
- **AEGISAI equivalent:** None.
- **Verdict:** **NEWLY IMPLEMENT** - `agents/*.md` + `agents/registry.py`.

### C.10 - Layered context management at four granularities

- **Concept:** 4 compaction mechanisms plus a collapse layer. `autoCompact` (usage >= threshold, reserves `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000` before the cap); `reactiveCompact` (context-overflow from API, **attempted once per turn** via `hasAttemptedReactiveCompact`); `microCompact` (within-turn stale tool-result trimming, with a **cache-preserving** variant); `snip`/`HISTORY_SNIP` (pre-query trim); `contextCollapse` (mutable-region collapsing - **overflow can be recovered from**, not just failed); `sessionMemoryCompact`. Constants: `AUTOCOMPACT_BUFFER_TOKENS = 13_000`, `WARNING_THRESHOLD_BUFFER_TOKENS = 20_000`, `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3`. `calculateTokenWarningState()` returns a 3-flag object.
- **Why AEGIS-X:** AEGIS-X runs are long-horizon. This is the difference between a run that completes at step 40 and one that dies at step 12.
- **AEGISAI equivalent:** None. `inference/ollama.py:94-98` is the *problem* (unbounded transcript).
- **Verdict:** **NEWLY IMPLEMENT** - `context/manager.py` + `context/compaction.py`. The **cache-preserving** variant matters if the fast tier is served with prefix caching.

### C.11 - Hooks: 27 events, async-vs-blocking discriminated by a key

- **Concept:** `PreToolUse / PostToolUse / PostToolUseFailure / Notification / UserPromptSubmit / SessionStart / SessionEnd / Stop / StopFailure / SubagentStart / SubagentStop / PreCompact / PostCompact / PermissionRequest / PermissionDenied / Setup / TeammateIdle / TaskCreated / TaskCompleted / Elicitation / ElicitationResult / ConfigChange / WorktreeCreate / WorktreeRemove / InstructionsLoaded / CwdChanged / FileChanged`. Hook output protocol: `{continue, suppressOutput, stopReason, decision:'approve'|'block', reason, systemMessage, hookSpecificOutput}` **or** `{async:true, asyncTimeout?}` - discriminated by the presence of the `async` key. Hooks can **veto**, augment input, inject context, replace MCP tool output, and drive the permission dialog. `SessionStart` emits `watchPaths` so the runtime watches paths and later fires `FileChanged` (a self-scheduling watch mechanism).
- **Why AEGIS-X:** spec section 19 requires an `EventBus`. Hooks are the generalisation: they let the operator, a policy engine, and telemetry all observe the same lifecycle without coupling.
- **AEGISAI equivalent:** None. `core/models.py:210-239` `Event` dataclass is built and **unused** - a natural seed.
- **Files:** new `events/bus.py`, `events/hooks.py`.
- **Verdict:** **NEWLY IMPLEMENT** - on top of the existing dead `Event` model.

### C.12 - Statusline as a public IPC contract

- **Concept:** the statusline is a **user-defined shell command receiving a rich JSON payload on stdin** - a versioned public IPC contract. Payload: `session_id, transcript_path, cwd, permission_mode, agent_id, session_name, model{id,display_name}, workspace{current_dir,project_dir,added_dirs}, version, output_style{name}, cost{total_cost_usd,total_duration_ms,total_api_duration_ms,total_lines_added,total_lines_removed}, context_window{total_input_tokens,total_output_tokens,context_window_size,current_usage,used_percentage,remaining_percentage}, exceeds_200k_tokens, rate_limits{five_hour,seven_day}, agent{name}, worktree{name,path,branch}`.
- **Why AEGIS-X:** spec section 19 requires an `AgentTrace/Telemetry`. A **JSON payload on stdin to a user-defined command** is the cheapest possible observability extension point - zero coupling, fully composable.
- **AEGISAI equivalent:** `observability/logging.py` - JSON lines to stderr only.
- **Verdict:** **NEWLY IMPLEMENT** - `events/sinks.py` `StatuslineSink` emitting exactly this payload shape.

### C.13 - Tool semantics worth adopting exactly

- **Concept:** `Edit` uses **exact-string replacement** (not diff hunks) with a `replace_all` flag. `old_string === ''` on a nonexistent file means create. `old_string === new_string` is a hard error: *"No changes to make: old_string and new_string are exactly the same."* `NotebookEdit` has `edit_mode: 'replace'|'insert'|'delete'`, `cell_type: 'code'|'markdown'`; insert goes **after** `cell_id`. `Bash` params: `command, description, timeout, run_in_background, dangerouslyDisableSandbox`. AST-based command complexity check (`tengu_bash_ast_too_complex`).
- **Why AEGIS-X:** Exact-string replacement is **safer and cheaper to implement** than unified diffs, and its failure mode (no match) is a clean signal the verifier can catch. `run_in_background` maps directly to spec "Background/long-running tasks".
- **AEGISAI equivalent:** None.
- **Verdict:** **NEWLY IMPLEMENT** - `tools/filesystem.py` `EditTool` with these exact semantics.

### C.14 - Configuration: 89 keys, 5 ordered sources, one-function-per-migration

- **Concept:** `SETTING_SOURCES` precedence: `userSettings -> projectSettings -> localSettings -> flagSettings -> policySettings`; `policySettings` and `flagSettings` are read-only. Invalid entries are **dropped but left in the file** for the user to fix. `src/migrations/` has one function per settings evolution, named `migrate<Old>To<New>`. Schema validated with a cross-field `.check(ctx => ...)`.
- **AEGISAI equivalent:** 1 flat layer - `core/config.py` `from_mapping` (defaults -> TOML -> env).
- **Verdict:** **EXTEND** `core/config.py` with a sources concept; adopt the migration naming convention.

### C.15 - Metadata hygiene via type name as lint rule

- **Concept:** a distinct type literally named `AnalyticsMetadata_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS` - the name *is* the lint rule, so accidental path/PII leakage into telemetry is a type error.
- **Why AEGIS-X:** Directly relevant to AEGISAI defect D16 (unfiltered PII in the corpus) and to any telemetry AEGIS-X emits.
- **Verdict:** **ADOPT** - name the sanitization-result type this way in `observability/`.

## C.2 Claude concepts AEGIS-X should NOT copy

- **Bun build-time `feature()` flags** (`COORDINATOR_MODE`, `CONTEXT_COLLAPSE`, `CACHED_MICROCOMPACT`, `FORK_SUBAGENT`, ...) - AEGIS-X must not have flags whose on/off state is unknowable from source. Automaton and AEGISAI both suffer doc drift; this makes it worse.
- **GrowthBook/Statsig remote flag service** - external dependency, out of scope.
- **`tengu_*` telemetry vocabulary** - 665 event names, proprietary naming. Use AEGIS-X's own.
- **Ink/React terminal UI** - 43-file custom fork. Out of scope; AEGIS-X's CLI is argparse.
- **Crypto/on-chain subsystem** (`viem`, `siwe`, `tweetnacl`, ERC-8004, x402 USDC payments) - entirely irrelevant.
- **`restored-src/node_modules/`** - ~4,800 vendored dependency files. No value to AEGIS-X.
- **`cli.js.map` `sourcesContent`** - verbatim upstream source. Deliberately not read; must not be read.

## C.3 Documented gaps in the Claude reference (for calibration)

The extract is **missing `src/types/message.ts`** (imported by dozens of files - the `Message` union shape is unknown), **`src/constants/querySource.ts`**, **`src/types/statusLine.ts`**, and **`src/types/generated/**` (14 empty placeholder directories)**. There are **no test files**, no `package.json` in Archive 1, and CLI flag names could not be recovered. **The 665 `tengu_*` event names are call-site vocabulary, not a validated enum.** Do not treat any of these as authoritative.

---

# D. MIND-LLM INTEGRATION

## D.1 Measured state

**Layout:** 256 files, 6.177 GB. 71 `.py`, 69 `.json`, 23 `.bin`, 15 `.pt`, 19 `.md`. **No dependency manifest of any kind** (`requirements.txt`, `pyproject.toml`, `environment.yml`, `setup.py` all ABSENT). Real deps: `torch` (required), `numpy` (required), `tokenizers` (optional), `psutil` (optional). **No `transformers`, `safetensors`, `datasets`, `vllm`, `fastapi`, `peft`, `trl`, `gguf`, `onnx`.** Developed on Python 3.14.7; manifests record `torch 2.14.0+cpu` and `2.14.0+cu130` - **PyTorch 2.14 is not in the public release line.**

## D.2 Model architecture

Hand-written LLaMA-family decoder-only transformer in `model.py` (320 lines). Classes: `GPT` (`:188`), `Block` (`:168`), `CausalSelfAttention` (`:43`), `RMSNorm` (`:114`), `SwiGLU` (`:145`), `MLP` (`:129`), `build_rope_cache` (`:20`), `apply_rope` (`:32`). Tied embeddings (`self.head.weight = self.tok_emb.weight`, `:202`), pre-norm, GQA support via `n_kv_head`, SDPA with manual masked-softmax fallback. No MoE. No `transformers`.

**The AEGIS-X candidate - `exp100m_longctx`:**

| Property | Value | Source |
|---|---|---|
| Parameters | **99,996,000** | independently recomputed from `ModelConfig.parameter_breakdown()`; matches manifest |
| vocab_size | 16,000 | `runs/exp100m_longctx_1m_tokens/model.experiment_manifest.json` |
| block_size (context) | **512** | same |
| n_layer / n_head / n_kv_head | 21 / 8 / 4 | same |
| n_embd / head_dim / FFN | 624 / 78 / 1,664 (SwiGLU) | same |
| rope_theta | 10,000.0 | same |
| dtype | **fp32 exclusively** | all 25 benchmark JSONs report `"dtype": "fp32"` |
| Fingerprint | `b44474b24b76deaf` | `checkpoint_utils.py:28` |

## D.3 Tokenizer

`tokenizer.py` (296 lines). `BPETokenizer` with two interchangeable backends behind one on-disk contract. Special tokens (`tokenizer.py:16`): `<pad>=0, <unk>=1, <bos>=2, <eos>=3, <user>=4, <assistant>=5, <end_turn>=6`. Byte alphabet = GPT-2's reversible table (`:20`).

| Asset | Backend | Vocab | Merges | chars/token |
|---|---|---:|---:|---:|
| `data/shards_longctx16k/tokenizer.json` **(ACTIVE)** | `greedy_byte_bpe` | 16,000 | **0** | 2.56 |
| `data/shards/tokenizer.json` | HF `tokenizers` BPE | 6,000 | 5,804 | 2.41 |
| `data/tokenizer_candidates/byte_bpe_32000.json` | `greedy_byte_bpe` | 32,000 | 0 | 2.82 |

**The active tokenizer is a longest-match vocabulary with ZERO BPE merges.** It exists because the HF wheel was unavailable in the build environment. Consequences:

- **Not Qwen-compatible, not Llama-compatible, not GPT-2-compatible.** 2.56 chars/token is roughly 45% worse than a modern 16k BPE.
- **GGUF/llama.cpp export is a live correctness hazard** - `llama.cpp`'s tokenizer reader expects BPE merges or SPM, not a longest-match vocabulary.

## D.4 Training pipeline (measured, from the run manifests)

| Hyperparameter | Value | Source |
|---|---|---|
| batch_size / grad_accum | 1 / 4 | `runs/exp100m_longctx_1m_tokens/model.experiment_manifest.json:30-42` |
| Effective batch | 2,048 tokens/update | derived |
| lr / min_lr / warmup | 3e-4 / 3e-5 / 10 steps | same |
| Steps planned / run | 489 / **49** | same |
| **Tokens seen** | **100,000** | same |
| **best_val_loss** | **6.075** (perplexity approx 434) | same |
| Wall clock | 5,461 s (91 min) | same |
| Device / dtype | **cpu** / **fp32** | same |

**Checkpoint format:** `torch.save` pickle (`.pt`). **NOT safetensors, NOT GGUF.** Best-checkpoint keys: `model_state, config, config_fingerprint, train_config, step, val_loss, tokens_seen, scheduler_state, tokenizer_hash, repro_metadata`. Resume keys add `optim_state, scaler_state, rng_state`. Writers use `atomic_save()` - `mkstemp` then write then `fsync` then `os.replace`. **This is genuinely production-grade engineering.**

**Dataset pipeline:** `pipeline.py` (raw -> normalize -> language_filter -> quality_filter -> dedup -> deterministic split), then `prepare_data.py` -> **pre-tokenized uint16 shards** with a `.spans.json` sidecar preserving document boundaries, then `data_loader.py` `ShardedData` (`np.memmap`, document-contained sampling). Active dataset: **2,447,762 total tokens** (2,234,181 train / 213,581 val).

## D.5 THE BLOCKING FINDING: `agentic/` ALREADY EXISTS

**MIND-LLM v2 already contains a 1,300-line agent runtime.** This is the single most important discovery in this audit and it is not mentioned in the master spec.

```text
mind-llm-v2/agentic/
  orchestrator.py    Orchestrator - the agent/tool loop
  types.py           Message, ToolCall, ToolResult, GenerationParams,
                     InferenceEvent/Request/Result, Usage
  tools/             base (ToolSpec), registry (ToolRegistry), permissions
                     (PermissionPolicy - fail-closed, deny_all default),
                     filesystem, terminal, retrieval, tasks, network
  context.py         ContextEngine (priority + budget)
  tasks.py           TaskManager, TaskRecord, TaskStatus (DAG, cycle detection)
  memory/            models, store (SQLite), retrieve, extract, consolidate, locks
  planner.py         Plan, PlanStep
  verify.py          Verifier, Verification
  observability.py   EventSink, AuditEvent
  limits.py          ExecutionLimits
  errors.py          AgenticError + 5 subclasses
  inference.py       InferenceBackend Protocol (describe, generate)
  mindllm_backend.py MindLLMBackend - the model adapter
  fake_backend.py    FakeBackend - deterministic test double
  api.py             AgenticRuntime.create(backend, tools, policy, limits, event_sink)
  __main__.py        raise SystemExit("CLI intentionally deferred")
```

Plus `docs/ARCHITECTURE_GAP_ANALYSIS.md` (41 KB) - the blueprint that produced `agentic/`.

**The abstraction is already correct:**

```python
class InferenceBackend(Protocol):
    async def describe(self) -> dict: ...
    def generate(self, request: InferenceRequest) -> AsyncIterator[InferenceEvent]: ...
```

**A two-method protocol. Swapping in a different model is a ~40-line class.**

## D.6 MIND-LLM model capability - measured, and it is zero

| Checkpoint | Params | Tokens seen | val_loss | Perplexity |
|---|---:|---:|---:|---:|
| `scaled_experiment.pt` | 1,557,120 | 4,608,000 | 4.425 | 83.5 |
| `expB_ctx128_5m` | 2,807,200 | 5,000,000 | 3.817 | 45.4 |
| `exp100m_100m_tokens` | 100,012,080 | 100,000,000 | 4.869 | 130.2 |
| **`exp100m_longctx_1m_tokens` (active)** | **99,996,000** | **100,000** | **6.075** | **434** |

**Capability probes - all zeros, all empty strings** (`checkpoints/scaled_experiment.capability_base.json`, `sft_demo.capability_instruct.json`): arithmetic 0/4, factual QA 0/3, code completion 0/2, context retention 0/1, instruction following 0/2. The documented failure mode: *"the model learned to emit its stop token immediately after the assistant turn marker."* Base output for `"The capital of France is "`: `"oy.\n \"\"\"\n \"\"\"\n \"\"\"\n \"\"\"\n"`.

**100,000 tokens on 100M parameters is 0.005% of Chinchilla-optimal (~2B tokens).**

## D.7 Six blockers for AEGIS-X

| # | Blocker | Evidence | Impact |
|---|---|---|---|
| M1 | **Cannot emit tool calls** | `EventKind` includes `"tool_call"` (`agentic/types.py:8`) and `Orchestrator._run` handles it (`orchestrator.py:62-65`) - but `MindLLMBackend.generate()` never yields one. Only `text_delta`, `usage`, `completed` | The tool loop is **structurally unreachable** with the real model. Only `ScriptedBackend` in tests exercises it |
| M2 | **Zero capability** | 0/10 probes | Cannot plan, follow instructions, or produce coherent multi-turn text |
| M3 | **512-token context** | `GPT.forward` **raises** `ValueError` if `T > block_size` (`model.py:233-238`) - refuses to truncate | A real agent turn (system + tool schemas + memory + history + results) is thousands of tokens. `benchmarks/longctx_b1_a4_t4_context1024.json`: `peak_rss_mb: 5636`, `rss_within_limit: false` |
| M4 | **No KV cache** | `model.py:288-290` re-runs the entire truncated context every token. No `past_key_values` / `use_cache` / `StaticCache` anywhere | Generation is **O(T^2)**. Measured 50.3 tok/s at ctx 512, batch 1, fp32, 4 threads. AEGIS-X is unusable |
| M5 | **No streaming** | `describe()` returns `{"streaming": False}`; one giant `text_delta` after full completion | Any live UI appears frozen |
| M6 | **No export path** | Zero hits for `gguf` / `safetensors` / `onnx` / `llama.cpp` / `quantiz` / `export` | Cannot be served by Ollama; Ollama **only loads GGUF** |

## D.8 How MIND-LLM connects to AEGIS-X

**Decision: two-model tiered routing, not one model.**

```text
AEGIS-X ModelRouter
   |
   |-- Tier "reasoning" --> AEGISAI OllamaProvider  (/api/chat)
   |     Qwen2.5-7B or 1.5B-Instruct. Plans, verifies, judges.
   |     ALREADY BUILT: inference/ollama.py needs /api/chat + tools + options
   |
   |-- Tier "fast" ------> MIND-LLMServer (pure-Python, stdlib HTTP)
   |     exp100m_longctx, fp32/bf16, KV-cached, streaming.
   |     Classification, extraction, summarization, observation distillation.
   |     NEW: models/mindllm_server.py
   |
   \-- Tier "local-fallback" --> MIND-LLM in-process via InferenceBackend Protocol
         MIND-LLM's own agentic/ for offline / dev / testing.
         No network. Full parity with the AEGIS-X loop.
```

**Why this is correct, not a compromise:**

1. MIND-LLM at 0/10 capability **cannot** plan or verify. Wiring it as the sole decision-maker produces a system that cannot function.
2. Automaton already demonstrates the value of tiered routing (`SurvivalTier` x `InferenceTaskType` matrix). AEGIS-X inherits that pattern.
3. MIND-LLM's `InferenceBackend` Protocol means the swap is one class. `FakeBackend` proves the seam.
4. AEGIS-X **keeps MIND-LLM genuinely in the loop** as a fast tier, and every MIND-LLM contribution becomes rollout data (spec "Experience learning") - the mechanism by which it improves.

**Adapter needed: YES** - `models/mindllm_server.py` + `inference/mindllm.py`. Not an Ollama adapter; a direct HTTP adapter to a pure-Python MIND-LLM process.

**Do NOT pursue GGUF/Ollama export.** Cost 1-3 days, real risk of silent tokenization mismatch (`merges: []` vocabulary), and it buys nothing the pure-Python path does not already provide. Revisit only if MIND-LLM is trained past 100M tokens and needs a fast C++ runtime.

## D.9 MIND-LLM work required before it can be an AEGIS-X backend

| Priority | Work | File | Why |
|---|---|---|---|
| **P0** | **KV cache** | `model.py:288-290` | Removes the O(T^2) wall. Everything else is unmeasurable until this lands |
| **P0** | **Tool-call emission** - constrained decoding or a trained tool-call SFT head | `agentic/mindllm_backend.py` | M1. Without it the loop is unreachable |
| **P0** | **Retrain on AEGIS-X data** - target >= 20B tokens for a 100M model, or grow the model | `pretrain.py`, `sft.py` | M2. AEGISAI has 752,795 real tokens - **0.0037% of what is needed** |
| **P1** | **Context 512 -> 8,192** with RoPE extension | `config.py` `block_size`; RoPE scaling not implemented | M3 |
| **P1** | **True streaming** | `mindllm_backend.py:92` | M5. Lands free with the KV cache |
| **P1** | **Re-train the tokenizer** with real BPE merges | `prepare_data.py:143` | 2.56 -> ~4.0 chars/token, roughly 35% sequence-length reduction |
| **P1** | **Dependency manifest** (`pyproject.toml`) | new | Nothing is installable or reproducible today |
| **P2** | **bf16/fp16 inference** | `model.py` | 400 MB -> 200 MB weights; 2x matmul speed |
| **P2** | **safetensors export** | new | Removes `weights_only=False` arbitrary-pickle-execution risk |
| **P2** | **Batched generation** - fix `stop_token` `.all()` | `model.py:305` | Batch > 1 is currently broken |
| **P3** | **Gradient checkpointing** - `--grad_checkpoint` is a declared no-op | `pretrain.py:262` | Needed to train the larger config |
| **P3** | **Distributed training** | new | DDP/FSDP. Absent; explicitly deferred in `ROADMAP.md:74-82` |
| **P3** | **Delete 4.8 GB of dead resume checkpoints** | `runs/*/model.resume.pt` | Only `model.pt` is needed for inference |

**Immediate free win:** MIND-LLM's own `pretrain.py` already implements token-budget planning (`budget_plan()`, `iter_microbatch_specs()`), stateless LR scheduling, bit-exact resume with tokenizer-hash and RNG validation, and atomic saves. **These are directly reusable patterns for AEGIS-X's training pipeline and are more mature than anything in AEGISAI.**

---

# E. DATA / TRAINING ARCHITECTURE

## E.1 Pipeline stage coverage (spec "Hugging Face training pipeline")

```text
Hugging Face
    \-- Downloader .................. COMPLETE   data_pipeline/download.py
    |                                resumable HTTP Range, .part, max_bytes, SHA-256
Raw dataset ...................... COMPLETE   datasets/raw/ (311 MB, 4 files)
    \-- License/provenance check ... PARTIAL     license recorded, NEVER enforced (D17)
    \-- Format normalization ....... COMPLETE   normalize.py - 5 schemas -> 1
    \-- Quality filtering ......... COMPLETE   filter.py - structural + quality score
    \-- Deduplication .............. COMPLETE   deduplicate.py - disk-backed SQLite
    |                                exact + normalized-text + bounded near-dup
Safety filtering ................. MISSING     3 regexes -> a flag, not a rejection (D16)
    \-- Train/validation split ..... COMPLETE   split.py - deterministic SHA-256 bucket
    \-- Tokenizer .................. MISSING     nothing consumes a tokenizer (D18)
    \-- Binary/tokenized ........... MISSING     no .bin/.arrow/uint32 packing
    \-- MIND-LLM ................... MISSING     no trainer, no LoRA, no eval harness
```

**7 of 11 stages implemented.** Everything through splitting is production-grade and needs **zero rewrite**. The gap is strictly at the tail.

## E.2 What AEGISAI's data contributes to MIND-LLM

| Contribution | Measured |
|---|---|
| Provenance chain | Every record carries source, file, row, revision, license, input SHA-256, raw-file SHA-256, config SHA-256 (`normalize.py:24-33`, `:351-367`) |
| `agentic_flow` contract | 8 signals per example (`normalize.py:315-348`): `user_goal_present, plan_evidence, tool_selection_count, permission_check, tool_result_count, observation_count, next_action_evidence, completion_evidence` |
| 7 of 8 flow signals are populated by public data | Only `permission_check` is hardcoded `"not_present_in_source"` (`:336`) - **only AEGIS-X rollouts can fill it** |
| Disk-backed dedup | Handles 1.7M-record pools without loading into memory |
| Resumability | `Checkpoint` class + per-record `last_line`; tested at `tests/test_data_pipeline.py:232,492,217` |
| Mixture | Weighted category quotas + round-robin interleave (`mixture.py:56-79`) |

**Measured corpus contribution to MIND-LLM: 752,795 real tokens.** Against a 100M-parameter model needing ~2B tokens (Chinchilla), that is **0.037%**. AEGISAI's data pipeline is the *engine*; it is not yet the *fuel*.

## E.3 Dataset corrections required before any download

| # | Correction | File | Severity |
|---|---|---|---|
| E1 | Remove `tool_results` from the token count (1.46x inflation) | `data_pipeline/statistics.py:81-83` | **Blocking** |
| E2 | Resolve `tool_calls` / `tool_results` duplication | `data_pipeline/normalize.py:63-76` | **Blocking** |
| E3 | Add a `tools` slot to the normalized schema (ToolACE, UltraData, SWE-v3 all carry per-record tool defs; the current schema has no field for them) | `data_pipeline/normalize.py:79-89` | High |
| E4 | Add `VERIFICATION` + `PLANNING` categories | `data_pipeline/classify.py:24-41` | High |
| E5 | Content-based (not name-based) code classification | `data_pipeline/classify.py:81-99` | High |
| E6 | Add a safety stage (toxicity, jailbreak, PII scrub, de-identification) | new `data_pipeline/safety.py` | High |
| E7 | Add a license allow/deny list with a commercial gate | new `data_pipeline/licensing.py` | High |
| E8 | Add a tokenizer stage | new `data_pipeline/tokenize.py` | High |
| E9 | Add a shard/packing stage (reuse MIND-LLM's `prepare_data.py` + `data_loader.py` spans pattern) | new `data_pipeline/pack.py` | High |
| E10 | Add `datasets/` to `.gitignore` | `.gitignore` | High |
| E11 | Drop the `local` orphan | `datasets/processed/local/`, `datasets/manifests/local/` | Low |
| E12 | Demote `training/prepare_dataset.py` to a raw-source fetcher | `training/prepare_dataset.py` | Low |

## E.4 Measured defects in the reference corpora

| Finding | Evidence | Impact |
|---|---|---|
| `neulab_mind2web` has **0 tool calls** | 118 records, 0 `tool_calls`, 0 tool messages. Actions are `<finish> A. None of the above. </finish>` - multiple choice over inline HTML | Teaches format-matching, not agency. **Replace with `xlangai/AgentTrek`** |
| `neulab_codeact` is not code | Record 1 is a `wiki_table_questions` SQL task; 385/481 labeled `CODING` on a name match (D15) | Needs content-based filtering |
| `neulab_openhands` raw contains CJK | Record 1: `写一个坦克大战小游戏` | Low in the processed output (10 CJK chars out of 1.57M) but scales with a larger run |
| `nemotron` never downloaded | `configs/dataset.toml:50`; 335,122 rows including a 316,094-row `tool_calling` split | **Highest-value unexploited asset** |

## E.5 Recommended dataset additions (no downloads performed)

| Dataset | Size | Tokens | Stage | License |
|---|---:|---:|---|---|
| `Team-ACE/ToolACE` | 37 MB | 14 M | 4 | Apache-2.0 |
| `voxozi/agentforge-multiturn-toolcall` | 10 MB | 4 M | 4 | Apache-2.0 |
| `NousResearch/hermes-function-calling-v1` | 50 MB | 18 M | 4 | Apache-2.0 |
| `LiteMind/Simple-agent-traces` | <10 MB | 2 M | 4 | Apache-2.0 |
| `bigcode/self-oss-instruct-sc2-exec-filter-50k` | 90 MB | 35 M | 2 | Permissive |
| `HuggingFaceTB/smoltalk` (selective configs) | 800 MB | 500 M | 1 | Apache-2.0 |
| `nvidia/Nemotron-Agentic-v1` (`interactive_agent` only) | 1.5 GB | 120 M | 3+4 | CC-BY-4.0 |
| `ise-uiuc/Magicoder-OSS-Instruct-75K` | 203 MB | 40 M | 2 | MIT |
| `xlangai/AgentTrek` | 200 MB | 50 M | 5 | verify before use |
| `open-thoughts/OpenThoughts3-1.2M` (code slice only) | 6 GB | 1.5 B | 3 | Apache-2.0 |
| `nvidia/Nemotron-SFT-SWE-v3` | 11.7 GB | 2.5 B | 6 | CC-BY-4.0 |
| `SWE-Gym/OpenHands-Sampled-Trajectories` | 500 MB | 80 M | 6 | MIT |
| `openbmb/UltraData-SFT-Agent-2609` (3 of 4 configs) | 25 GB | 250 M | 6 | Apache-2.0 |
| **`data_pipeline/rollout.py` -> AEGIS-X** | grows | grows | 7 | own |

**Excluded:** `Salesforce/xlam-function-calling-60k` (CC-BY-4.0/NC conflict + gated + DeepSeek-derivative dispute); `Salesforce/APIGen-MT-5k` (explicit `cc-by-nc-4.0` + GPT-4 competitor restriction); `xlangai/AgentNet` (vision-language); `bigcode/stack-dedup` (signal/GB off by ~1000x); `lmsys-chat-1m` (non-commercial + PII); `tulu-3-sft-mixture` (ODC-BY, non-commercial subsets); `OpenHermes-2.5`; `neulab/mind2web` full.

**Curriculum totals:** minimum (Stages 1-4) **~1.24 B tokens / 447 MB**; recommended (Stages 1-6) **~4.29 B tokens / 46 GB**; with 12 months of AEGIS-X rollouts **~7.29 B tokens**.

---

# F. CAPABILITY MATRIX

Legend: **COMPLETE** - **PARTIAL** - **MISSING** - **BROKEN** - **PLACEHOLDER**

| # | Subsystem | Status | Evidence |
|---|---|---|---|
| 1 | **Agent orchestrator** | **PLACEHOLDER** | `agents/agent.py` = 65 LOC pass-through; no state machine, no budget, no termination |
| 2 | **Main agent loop** | **MISSING** | No `while` in `agents/`, `runtime/`, `inference/`. MIND-LLM's `agentic/orchestrator.py` has one but is unreachable (M1) |
| 3 | **Planner** | **MISSING** | No planner symbol in AEGISAI. MIND-LLM has `agentic/planner.py` (Plan/PlanStep types only, no generator) |
| 4 | **Replanner** | **MISSING** | Nothing in any of the four trees |
| 5 | **Model router** | **MISSING** | `Runtime.__init__:37-40` hard-rejects anything but `"ollama"` |
| 6 | **MIND-LLM adapter** | **PARTIAL** | `agentic/mindllm_backend.py` implements the 2-method Protocol; but M1 (no tool_call), M4 (no KV cache), M5 (no streaming) |
| 7 | **Context manager** | **MISSING** | `inference/ollama.py:94-98` resends unbounded transcript; no token counting anywhere |
| 8 | **Memory** | **PLACEHOLDER** | `memory/__init__.py` = 1 line. MIND-LLM has a real 6-scope SQLite store; AEGIS-X must choose one |
| 9 | **Tool registry** | **PARTIAL** | Contract + validation + dedup are good (`tools/base.py`, `tools/registry.py`); **no model-facing serialization**; `list_tools()` / `validate_requested_tools()` unused by production code |
| 10 | **Tool selector** | **MISSING** | No model-driven selection. `execute_tool` has one caller: a test |
| 11 | **Tool executor** | **PARTIAL** | `ToolRegistry.execute` (`tools/registry.py:44-65`) validates + permission-checks + runs. But: no timeout, no output truncation, no parallelism, no audit record |
| 12 | **Filesystem tools** | **MISSING** | No tool exists. `FILESYSTEM_READ/WRITE` declared and default-denied |
| 13 | **Terminal tools** | **MISSING** | No tool. Zero `subprocess` imports in 59 files |
| 14 | **Python execution** | **MISSING** | Only `tools/calculator.py` (AST-whitelist arithmetic, `READ_ONLY`) |
| 15 | **Git** | **MISSING** | No git symbol in AEGISAI. Automaton has `src/git/`; the Claude reference has `EnterWorktree` |
| 16 | **Web research** | **MISSING** | `NETWORK` declared, no tool uses it. No HTTP client in `tools/` |
| 17 | **Browser automation** | **MISSING** | No Playwright/Selenium. Spec "Tools" requires it |
| 18 | **API tools** | **MISSING** | No HTTP client tool. Spec "Internet and online research" requires `SEARCH -> FETCH -> EXTRACT` |
| 19 | **Code-agent** | **MISSING** | No agent class, no harness, no code-specific tools |
| 20 | **Observation manager** | **MISSING** | `ToolResult` exists (`tools/base.py:12`) but nothing normalizes, truncates, redacts, or scores it |
| 21 | **Verifier** | **MISSING** | Nothing validates whether a result is correct. `TaskCategory` has no `VERIFICATION` value |
| 22 | **Error recovery** | **PARTIAL** | `core/errors.py` taxonomy is complete (15 classes, clean hierarchy). But **no retry, no backoff, no circuit breaker, no compensation**. `Runtime.run` has no `try/except` |
| 23 | **Task manager** | **MISSING** | No task/todo model, no DAG, no queue. MIND-LLM has `agentic/tasks.py` (DAG + cycle detection) |
| 24 | **Background workers** | **MISSING** | Zero `threading` / `asyncio` / `subprocess` in 59 files |
| 25 | **Scheduler** | **MISSING** | No cron, no interval, no queue, no lease |
| 26 | **Multi-agent** | **MISSING** | No registry, no handoff, no shared state, no concurrency |
| 27 | **Permissions** | **PARTIAL** | `tools/permissions.py` has a real capability enum + `PermissionPolicy`. Missing: no agent-class scoping, no typed decision reason, no approval gate, no audit table, no rate limits |
| 28 | **Sandbox** | **MISSING** | `security/__init__.py` = 1 line. `PROCESS_EXECUTION` permanently denied |
| 29 | **Event bus** | **PLACEHOLDER** | `core/models.py:210-239` `Event` dataclass is complete and **constructed only by a test** |
| 30 | **Checkpoints** | **MISSING** (runtime) / **COMPLETE** (data) | Runtime: nothing. Data: `data_pipeline/common.py:67-96` + 6 stages, tested |
| 31 | **Persistence** | **PARTIAL** | SQLite quality is high (WAL-equivalent pragmas, FK cascade, role CHECK, parameterized SQL, idempotent migrations). **But only 2 tables** - no run/step/tool/plan/memory state |
| 32 | **Logging** | **PARTIAL** | `observability/logging.py` correct JSON. Only **3** call sites. `log_event` / `get_logger` dead |
| 33 | **Telemetry** | **MISSING** | No metrics, no traces, no spans, no `run_id`/`step_id`/`tool_call_id` on any object, no token/cost accounting |
| 34 | **CLI** | **PARTIAL** | 5 subcommands + legacy, well tested (9 tests). Missing: `run`, `resume`, `plan`, `memory`, `agents`, `jobs`, `serve`, `--json-events` |
| 35 | **API** | **PLACEHOLDER** | `api/__init__.py` = 1 line. No HTTP server, no REST, no SSE, no IPC |
| 36 | **Dataset pipeline** | **COMPLETE** | 11 stages, ~2,400 LOC, 28 tests, **actually executed** (311 MB artifacts, run manifest with SHA-256 chain) |
| 37 | **Training pipeline** | **MISSING** | No trainer, no LoRA, no optimizer, no loss, no eval harness. MIND-LLM's `pretrain.py` / `sft.py` exist and are usable |
| 38 | **Evaluation** | **MISSING** | No eval set, no success metric, no regression gate. MIND-LLM has `eval_capability.py` (10 probes) and 25 benchmark JSONs, with 7 declared capabilities marked `NOT_YET_IMPLEMENTED` |

**Score: 1 COMPLETE, 13 PARTIAL, 21 MISSING, 3 PLACEHOLDER.**

---

# G. EXACT FILE MAP

## G.1 Files to CREATE (56 new files)

### Core / foundation
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `core/ids.py` | `new_run_id/step_id/tool_call_id/trace_id`; context binding | stdlib | Nothing carries a correlation ID today (D10) |
| `core/budget.py` | `TokenBudget, StepBudget, CostBudget`; exhaustion raises | stdlib | Automaton B8; required for any bounded run |
| `core/transitions.py` | `Continue` discriminated union (typed continuation reason) | stdlib | Claude C.1; makes recovery assertable |
| `core/tasks.py` | `Task`, `TaskStatus`, DAG protocol | stdlib | spec section 20 |

### Agent
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `agents/loop.py` | `AgentLoop` - the ReAct state machine. 12-step turn per Automaton B1; transactional commit per B2 | `anyio`, `core/`, `inference/`, `tools/`, `tasks/`, `context/` | **The centerpiece.** Nothing exists |
| `agents/executor.py` | `Executor` - one Step to one ToolCall: permission, jail, schema, budget, execute, truncate, audit | `tools/`, `security/` | Automaton B16/17; Claude C.4 |
| `agents/planner.py` | `Planner` protocol + `LLMPlanner` (JSON-object response format) + `StaticPlanner` (tests) | `inference/`, `core/tasks.py` | Automaton B4; spec section 2 |
| `agents/planner_schema.json` | Planner output JSON Schema | - | Automaton B4 |
| `agents/replanner.py` | `ReplanTrigger` union; triggers on step FAIL / verifier RETRY x N / budget pressure / observation contradiction | `agents/planner.py`, `tasks/` | Automaton B5; spec section 3 |
| `agents/verifier.py` | `SchemaVerifier, AssertionVerifier, TestRunnerVerifier, FileDiffVerifier, LLMJudgeVerifier` -> `Verdict: PASS/RETRY/REPLAN/ESCALATE` | `tools/`, `core/` | spec section 9. Highest-value NEW subsystem |
| `agents/registry.py` | `AgentRegistry, AgentSpec`; load `agents/*.md` frontmatter | stdlib | Claude C.9; Automaton B16 |
| `agents/delegation.py` | `Delegation` protocol; supervisor/worker; per-child budget + filesystem root | `anyio`, `agents/registry.py` | Automaton B18/19; spec "Multi-agent" |
| `agents/planner.md`, `coding.md`, `verifier.md`, `browser.md` | Declarative agent definitions | - | Claude C.9 |

### Inference
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `inference/options.py` | `ModelOptions(temperature, top_p, top_k, num_ctx, num_predict, stop, seed, repeat_penalty)` | stdlib | D6 - no `options{}` is ever sent |
| `inference/chat.py` | `/api/chat` client: native `messages`, `tools`, `stream` | stdlib | **D1 - the critical fix** |
| `inference/streaming.py` | Incremental NDJSON/SSE parser -> `StreamEvent` | stdlib | D9 - no streaming exists |
| `inference/router.py` | `ModelRouter.route(request)` by `task_type` x tier; failover; circuit breaker | `inference/`, `core/budget.py` | Automaton B8/B9; spec section 15 |
| `inference/mindllm.py` | `MindLLMInferenceProvider` - HTTP adapter to `models/mindllm_server.py` | `httpx` | D.8 - the MIND-LLM connection |
| `inference/registry.py` | Provider factory | - | Automaton B9 |

### Models (MIND-LLM serving)
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `models/mindllm_server.py` | Pure-Python stdlib HTTP server for MIND-LLM; `/v1/chat/completions` (SSE), `/v1/embeddings`, `/health` | `torch`, `numpy` (vendored) | D.8 - D.6 says do NOT use GGUF/Ollama |
| `models/kv_cache.py` | KV-cache implementation for MIND-LLM `GPT` | `torch` | **M4 - highest-priority MIND-LLM work** |
| `models/tool_parser.py` | Grammar/JSON-constrained decode + `ToolCall` extraction from MIND-LLM output | - | **M1 - makes the tool loop reachable** |
| `models/load.py` | Safe checkpoint load (`safetensors` preferred; `weights_only=True`); tokenizer-compat check | `safetensors` | `weights_only=False` is arbitrary pickle execution |

### Tools
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `tools/factory.py` | `build_tool()` with fail-closed defaults | - | Claude C.2 |
| `tools/select.py` | `ToolSelector`; `searchHint` scoring; progressive disclosure | `tools/registry.py` | Claude C.3 - **required by conflict R2** |
| `tools/executor.py` | Execution engine: timeout, parallel wave, output truncation, audit | `anyio` | Automaton B13; Claude C.4/C.5 |
| `tools/filesystem.py` | `Read, Write, Edit (exact-string, replace_all), Glob, Grep`; path jail | `pathlib` | spec "Tools"; Claude C.13 |
| `tools/terminal.py` | `Bash`/`PowerShell`: `command, description, timeout, run_in_background`; AST complexity check; output truncation | `subprocess` | spec "Tools"; Claude C.13. **Requires R1 resolution** |
| `tools/python_exec.py` | Sandboxed Python: jailed cwd, rlimits/job-objects, hard timeout, no network | - | spec "Tools"; **requires R1 resolution** |
| `tools/git.py` | `status, diff, log, commit, branch, clone, worktree`; no shell interpolation | `subprocess` (arg arrays) | spec "Tools"; Automaton B27 |
| `tools/web.py` | `search, fetch, extract` with allowlist, robots, size/time limits | `httpx` | spec "Internet and online research" |
| `tools/browser.py` | Playwright driver; DOM to text serialization | `playwright` | spec "Tools" |
| `tools/api_client.py` | Generic HTTP/API tool | `httpx` | spec "Tools" |
| `tools/notes.py` | Durable scratchpad exposed to the agent | `sqlite3` | Automaton B13 (working memory as a tool) |
| `tools/sandbox.py` | Windows job objects / POSIX rlimits + seccomp | - | Fills an Automaton absence |
| `tools/audit.py` | Append-only hash-chained invocation log | `sqlite3` | Automaton B13; Claude C.7 |

### Context / memory / tasks
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `context/manager.py` | `ContextManager.assemble()`; `ContextBudget`; utilization % | `tiktoken` or MIND-LLM tokenizer | Claude C.10; Automaton B12 - **and it must be WIRED** |
| `context/compaction.py` | `auto_compact, reactive_compact (once/turn), micro_compact, snip, collapse`; checkpoint-and-reset | `context/manager.py`, `inference/` | Claude C.10; Automaton B12 |
| `context/window.py` | Token counting, truncation | - | D6 |
| `memory/base.py` | `MemoryStore` ABC | stdlib | AEGIS-X's 1-line stub |
| `memory/working.py` | Working tier | `sqlite3` | spec "Memory"; Automaton B10 |
| `memory/episodic.py` | Episodic tier | `sqlite3` | spec "Memory"; Automaton B10 |
| `memory/semantic.py` | Semantic tier | `sqlite3`, `numpy` | spec "Memory"; Automaton B10 |
| `memory/procedural.py` | Procedural tier | `sqlite3` | spec "Memory"; Automaton B10 |
| `memory/retrieval.py` | Hybrid keyword + vector; relevance scoring; feedback loop | `sqlite3`, `numpy` | Automaton B11 |
| `memory/consolidation.py` | **ORIENT -> GATHER -> CONSOLIDATE -> PRUNE** (spec "Memory consolidation") | `memory/*` | Spec requires it explicitly |
| `memory/budget.py` | Per-tier caps with **rollforward** | - | Automaton B11 |
| `tasks/manager.py` | `TaskManager`; DAG; cycle detection; `get_ready_tasks` | `sqlite3` | Automaton B3; spec "Persistent tasks" |
| `tasks/records.py` | All 13 spec "Persistent tasks" fields | - | spec "Persistent tasks" |
| `tasks/log.py` | Byte-offset-addressable disk log; prefixed entropy-hardened IDs | - | Claude C.8 |
| `tasks/store.py` | Task persistence | `sqlite3` | - |

### Observation / verification / recovery
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `observation/manager.py` | Normalize, score, redact, mark-untrusted | `tools/`, `memory/` | spec section 8 |
| `observation/budget.py` | 3-layer size budgets (chars / tokens / per-message) | - | Claude C.5 |
| `observation/trust.py` | Untrusted marking | - | Automaton B24 |
| `verification/__init__.py` | Thin facade over `agents/verifier.py` | - | spec section 9 |
| `recovery/classifier.py` | Error classification | - | Automaton B26; Claude C-section error taxonomy |
| `recovery/manager.py` | Retry / backoff / compensate / dead-letter | `tenacity` or stdlib | spec section 10 |

### Security / events / observability
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `security/policy.py` | `PermissionManager`; `PermissionDecision(decision, reason_code, reason_detail, rule_ids, risk_level)`; priority-ordered rules | - | Automaton B13; Claude C.6/C.7. Moves capability logic out of `tools/` |
| `security/approvals.py` | TTL-bound approval gate; approval bound to the **args hash** | - | Automaton `aegis-ai/src/approvals.ts`; spec "Security" |
| `security/sandbox.py` | Process isolation | - | Fills an Automaton absence |
| `security/secrets.py` | Redaction before prompt-injection; secret store | - | spec "Security" |
| `security/audit.py` | Append-only, hash-chained audit log | `sqlite3` | spec "Security" |
| `security/rules/` (`validation`, `path_protection`, `command_safety`, `authority`, `rate_limits`) | Priority-ordered rule modules | - | Automaton B14 |
| `events/bus.py` | `EventBus` over the existing `core.models.Event` | stdlib | spec section 17; revives D10 |
| `events/hooks.py` | `HOOK_EVENTS`; async-vs-blocking discriminated by an `async` key; can veto/augment | `events/bus.py` | Claude C.11 |
| `events/types.py` | Typed event payloads | - | Automaton B23 |
| `events/sinks.py` | stderr, JSONL file, SQLite, callback, `StatuslineSink` (JSON payload on stdin) | - | Claude C.12 |
| `observability/events.py` | Typed event schema + run/step/tool correlation | `core/ids.py` | Automaton B23 |
| `observability/tracing.py` | Span lifecycle, nested spans | - | Fills an Automaton absence |
| `observability/metrics.py` | Counters/gauges/histograms: tokens, latency, cost, tool success rate | - | spec section 19 |

### Scheduler / workers / multi-agent
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `scheduler/daemon.py` | Recursive `setTimeout` tick (never `setInterval`) | - | Automaton B21 |
| `scheduler/jobs.py` | `Job` table; leases; `maxRetries`; `nextRunAt` re-arm; dead-letter | `sqlite3` | Automaton B20 |
| `scheduler/cron.py` | Cron parsing + **deterministic jitter** | `croniter` | Claude reference cron subsystem |
| `workers/pool.py` | `WorkerPool`; scoped identity; per-worker `allowedEditRoot`; drain on SIGTERM | `anyio` | Automaton B16/B17 |
| `workers/identity.py` | Worker identity creation | - | Automaton B19 |
| `multi_agent/topology.py` | MASTER / RESEARCH / CODING / TEST / BROWSER / REVIEW-VERIFIER; delegation graph | `agents/registry.py` | spec "Multi-agent target" |
| `multi_agent/messaging.py` | `MessageTransport`; typed envelopes; retry | `sqlite3` | Automaton sub-agents |
| `multi_agent/blackboard.py` | Shared task state | `sqlite3` | spec section 8 multi-agent |

### Storage (append `Migration(version=2)`)
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `storage/runs.py` | `RunRepository` - runs, steps, tool_invocations | - | D8 |
| `storage/plans.py` | `PlanRepository` - plans, tasks, observations | - | Automaton B3 |
| `storage/memory_repo.py` | `MemoryItemRepository` | - | Automaton B10 |
| `storage/checkpoints.py` | `CheckpointRepository` - snapshots + resume | - | Automaton B12; spec "Persistent tasks" |
| `storage/events_repo.py` | `EventStreamRepository`; `compact()` | - | Automaton B23 |
| `storage/audit_repo.py` | `PolicyDecisionRepository`; rate-limits query it as state | - | Automaton B15 |

### Data / training / evaluation
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `data_pipeline/safety.py` | Toxicity, jailbreak, PII scrub, de-identification, secret detection | - | D16, E6 |
| `data_pipeline/licensing.py` | Allow/deny list; commercial-use gate; per-subset resolution | - | D17, E7 |
| `data_pipeline/tokenize.py` | Tokenizer stage; records vocab id + hash per shard | MIND-LLM `tokenizer.py` | D18, E8 |
| `data_pipeline/pack.py` | uint16 shards + `.spans.json` document-boundary sidecar | `numpy` | E9; reuses MIND-LLM's proven pattern |
| `data_pipeline/rollout.py` | Export AEGIS-X runs to SFT JSONL; fills `permission_check` | - | spec Stage 7 + "Experience learning" |
| `data_pipeline/eval_sets.py` | Held-out agentic eval sets | - | spec Phase 10 |
| `data_pipeline/promote.py` | Category slices for training | - | - |
| `training/sft.py` | SFT trainer for MIND-LLM (reuse `pretrain.py` conventions) | `torch` | spec Phase 9 |
| `training/curriculum.py` | 7-stage curriculum with per-stage mix ratios | - | spec "Training curriculum" |
| `training/lora.py` | LoRA/QLoRA (when hardware allows) | `peft` | optional |
| `evaluation/harness.py` | Agent success rate, step efficiency, verifier pass rate, cost per task | - | spec Phase 10 |
| `evaluation/metrics.py` | Metric definitions | - | - |
| `evaluation/suites/` | `tool_call_format, plan_quality, recovery, long_horizon` | - | - |

### API / CLI / runtime
| New file | Purpose | Deps | Why |
|---|---|---|---|
| `api/server.py` | stdlib `http.server` REST + SSE | stdlib | `api/` stub; spec Phase 11 |
| `api/routes.py` | `/v1/runs`, `/v1/runs/{id}/steps`, `/v1/runs/{id}/events`, `/v1/tools`, `/v1/health` | - | - |
| `api/events.py` | SSE bridge from `events/bus.py` | - | - |
| `cli/run.py` | `aegis-x run` | - | spec Phase 11 |
| `cli/resume.py` | `aegis-x resume <run_id>` | - | spec "Persistent tasks" |
| `cli/plan.py` | `aegis-x plan` | - | - |
| `cli/memory.py` | `aegis-x memory` | - | - |
| `cli/agents.py` | `aegis-x agents` | - | - |
| `cli/jobs.py` | `aegis-x jobs` | - | - |
| `cli/serve.py` | `aegis-x serve` | - | - |
| `runtime/daemon.py` | Long-lived worker process | - | Automaton supervisor pattern |
| `runtime/checkpoint.py` | Run snapshot + idempotent resume | - | Automaton B12; spec "Persistent tasks" |

### Tests (19 new files)
| New file | Covers |
|---|---|
| `tests/test_agent_loop.py` | Loop transitions, step limit, budget stop, verifier stop |
| `tests/test_planner.py` | Plan schema validation, cycle rejection, replan triggers |
| `tests/test_replanner.py` | Each `ReplanTrigger` |
| `tests/test_verifier.py` | Each strategy; PASS/RETRY/REPLAN/ESCALATE |
| `tests/test_executor.py` | Permission -> jail -> schema -> budget -> truncate ordering |
| `tests/test_context_window.py` | Truncation, compaction cascade, checkpoint-and-reset |
| `tests/test_budget.py` | Token/step/cost accounting, exhaustion |
| `tests/test_memory.py` | 4 tiers, retrieval, consolidation ORIENT->PRUNE |
| `tests/test_checkpoint_resume.py` | Kill at step N, resume, assert **idempotent** completion |
| `tests/test_sandbox.py` | Path escape, fork bomb, timeout, network attempt from sandbox |
| `tests/test_new_tools.py` | Each of the 14 tool families |
| `tests/test_permissions.py` | Typed decision reasons, rule priority ordering, rate limits |
| `tests/test_api.py` | REST contract, SSE ordering, idempotent submission |
| `tests/test_wiring.py` | **Fails if any constructed subsystem is never called** (the Automaton unwired-subsystem lesson) |
| `tests/test_doc_freshness.py` | Documented tool/table counts match code (the Automaton doc-drift lesson) |
| `tests/test_e2e_offline.py` | **Full loop, `ScriptedBackend`, no network** |
| `tests/test_mindllm_adapter.py` | KV cache, streaming, tool-call emission, context clamping |
| `tests/test_dataset_safety.py` | PII records rejected not flagged |
| `tests/test_dataset_licensing.py` | Deny-list enforcement, commercial gate |

## G.2 Files to MODIFY (29 files)

| File | Change | Severity | Why |
|---|---|---|---|
| `inference/ollama.py` | `/api/generate` to `/api/chat`; native `messages`; `tools`; `options`; `stream`; **fix `health_check` model-absence false-positive** (`:115-116`) | **Critical** | D1, D7 |
| `inference/base.py` | Add `stream()`, `embed()`, `count_tokens()`; accept `ModelOptions` | **Critical** | The ABC must express the real contract |
| `core/models.py` | Add `tool_call_id`, `tool_name`, `name` to `Message`; add `Run`, `Step`, `ToolInvocation`, `Plan`, `Task`, `Observation`, `MemoryItem`, `Budget` | **Critical** | D2 - hard blocker |
| `core/config.py` | Add ~20 settings; add a settings-sources concept; `migrate<Old>To<New>` naming | **Critical** | D6; Claude C.14 |
| `core/errors.py` | Add `StepLimitExceeded, BudgetExceeded, VerificationFailed, PlanInvalid, SandboxViolation, ToolTimeout, RateLimited, ResumeError, ContextOverflow` | High | New subsystems |
| `configs/default.toml` | Mirror the new settings | High | - |
| `agents/agent.py` | **Demote** to a thin `InferenceProvider` adapter; move the loop to `agents/loop.py` | **Critical** | D5; prevents a third runtime (R5) |
| `tools/base.py` | Add `build_tool()` factory; fail-closed `is_read_only` / `is_concurrency_safe` / `is_destructive`; `timeout_s`; `risk_level`; `search_hint`; auto-generate `ToolCall.call_id`; **result size budgets** | **Critical** | Claude C.2/C.3/C.5; `call_id` defaults to `""` today |
| `tools/permissions.py` | **Remove the `PROCESS_EXECUTION` hard block** (`:31-32`); thin shim over `security/policy.py` | **Critical** | D4, R1 |
| `tools/registry.py` | Add `to_model_schema()`; wire `list_tools()` / `validate_requested_tools()`; output truncation; audit record | High | D3 |
| `runtime/runtime.py` | Add `run_id` correlation, `stream()` passthrough, checkpoint save/load, `resume()`, per-step timeout; **single-transaction commit** per Automaton B2 | **Critical** | D8, Automaton B2 |
| `storage/sqlite.py` | Append `Migration(version=2)` with runs / steps / tool_invocations / plans / tasks / observations / memory_items / checkpoints / events / audit | **Critical** | D8. **Append only - never edit migration 1** |
| `storage/base.py` | Add `RunRepository`, `PlanRepository`, `MemoryRepository`, `CheckpointRepository` protocols | High | - |
| `storage/repositories.py` | `save_conversation` `INSERT OR IGNORE` to upsert (`:169`); implement new repositories | High | D11 |
| `observability/logging.py` | Wire `log_event`; add `run_id`/`step_id` context binding; `AnalyticsMetadata_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS` | High | D10; Claude C.15 |
| `memory/__init__.py` | Replace the 1-line docstring with real re-exports | High | PLACEHOLDER |
| `security/__init__.py` | Replace the 1-line docstring with real re-exports | High | PLACEHOLDER |
| `api/__init__.py` | Replace the 1-line docstring with real re-exports | High | PLACEHOLDER |
| `cli/main.py` | Add `run, resume, plan, memory, agents, jobs, serve`; `--max-steps`, `--json-events`, `--resume <run_id>`; **fix `_run_tool` to use the runtime registry** (`:258-260`) | High | Second disconnected tool registry |
| `pyproject.toml` | Add new packages; add real dependencies | **Critical** | - |
| `.gitignore` | **Add `datasets/`**, `*.part` | **Critical** | D21 - 311 MB exposure |
| `data_pipeline/statistics.py` | **Remove the `tool_results` double count** (`:81-83`) | **Critical** | D13 - every token figure is 1.46x wrong |
| `data_pipeline/normalize.py` | Resolve `tool_calls`/`tool_results` duplication (`:63-76`); **add a `tools` slot** (`:79-89`); populate `permission_check` from rollouts (`:336`) | **Critical** | D14, E3, Stage 7 |
| `data_pipeline/classify.py` | Add `VERIFICATION` + `PLANNING` (`:24-41`); **content-based** code classification (`:81-99`) | High | D15, E4, E5 |
| `data_pipeline/prepare.py` | Wire the new safety + licensing stages into the DAG (`:79-196`) | High | D16, D17 |
| `data_pipeline/config.py` | Add `license_allowed`, `license_denied`, `commercial_use_required` | Medium | E7 |
| `training/prepare_dataset.py` | Demote to a raw-source fetcher; remove the duplicate `{id, messages, metadata}` schema | Medium | E12 |
| `tests/test_runtime.py` | Use `tmp_path` / `:memory:` (`:40,66,74`) | Medium | D12 |
| `tests/test_tools.py` | Update `test_process_execution_is_permanently_disabled` (`:104-111`) for the sandbox-gated capability | High | R1 resolution |
| `README.md` | Rewrite architecture + limitations (231 lines, Phase-1-accurate today) | High | spec OpenCode rule 8 |
| `docs/architecture.md` | Add loop, planner, verifier, memory, tool-call sections | High | - |
| `docs/configuration.md` | Add every new setting and env var | High | - |
| `docs/development.md` | Document sandbox + tool-authoring rules; wire the 6 quality gates into CI | Medium | No CI exists |

## G.3 Files to PRESERVE (20 files/directories)

| Path | Why |
|---|---|
| `data_pipeline/common.py` | `Checkpoint`, `PipelineReport`, atomic `Path.replace` (Windows-safe), `sha256_file`, `iter_jsonl` - all production-grade |
| `data_pipeline/download.py` | Resumable HTTP `Range` + `.part` + `max_bytes` + SHA-256. Correct |
| `data_pipeline/validate.py` | Per-record tolerance; rejects with reason + row number |
| `data_pipeline/split.py` | Deterministic SHA-256 bucketing |
| `data_pipeline/mixture.py` | Weighted quotas + round-robin interleave |
| `data_pipeline/deduplicate.py` | Disk-backed SQLite exact/normalized/near-dup - **keep; add MinHash/LSH for Stage 6 scale** |
| `data_pipeline/inspect.py` | HF metadata / license / revision / size inspection |
| `data_pipeline/config.py` | Path resolution, source config, mixture weights |
| `data_pipeline/cli.py` | 11-stage CLI |
| `data_pipeline/prepare.py` | The DAG + run manifest - extend, do not replace |
| `storage/sqlite.py` `MIGRATIONS[0]` | **Never edit. Append version 2** |
| `storage/repositories.py` | Parameterized SQL, FK cascade, role CHECK, JSON metadata, ordered reads |
| `core/errors.py` (existing 15 classes) | Clean hierarchy; additive only |
| `core/models.py` `Event` | Complete, tested, unused - **revive it as the EventBus payload** |
| `observability/logging.py` `StructuredFormatter` | Correct JSON with a proper reserved-field set |
| `tools/base.py` `_validate_value` | Working JSON-Schema subset validator - keep until `pydantic` is adopted |
| `tools/calculator.py` | AST-whitelist arithmetic, no `eval()`. The security exemplar for all new tools |
| `tools/permissions.py` `Capability` enum | The enum is fine; the *hard block* is the problem |
| `tests/` (90 existing) | Must stay green. Add a regression test pinning `mixture.json` category counts |
| `configs/dataset.toml` | 9 sources with licenses + `max_bytes` + `enabled` - the model for new source configs |
| `docs/*.md` (6 files) | Accurate as Phase-1 documentation; extend, do not delete |

## G.4 Files/datasets that should NOT be used

| Item | Why |
|---|---|
| `datasets/processed/local/`, `datasets/manifests/local/` | Orphan - `local` is not in `configs/dataset.toml`; 3 records; 0-byte splits (D19) |
| `datasets/processed/neulab_mind2web/` (118 records) | **0 tool calls**; multiple choice over inline HTML. Teaches format-matching, not agency |
| `data/training/starcoder_python_instruct.jsonl` as a **training target** | Incompatible `{id, messages, metadata}` schema. Keep only as a raw source |
| `neulab/codeactinstruct` records with `wiki_table_questions` content | SQL-QA mislabeled `CODING` by name match (D15) |
| `Salesforce/xlam-function-calling-60k` | CC-BY-4.0/NC conflict + gated + DeepSeek-derivative dispute |
| `Salesforce/APIGen-MT-5k` | Explicit `cc-by-nc-4.0` + GPT-4 competitor restriction |
| `xlangai/AgentNet` (OpenCUA) | Vision-language. Zero usable text tokens |
| `osunlp/Multimodal-Mind2Web` | Vision-language |
| `bigcode/stack-dedup` / The Stack | Signal/GB off by roughly 1000x |
| `lmsys/lmsys-chat-1m` | Non-commercial + real user PII |
| `allenai/tulu-3-sft-mixture` | ODC-BY with unresolved non-commercial subsets |
| `teknium/OpenHermes-2.5` | Indiscriminate merge; heavy smoltalk overlap |
| `glaiveai/glaive-function-calling-v2` | Low quality; use the cleaned 5k inside Hermes |
| `neulab/mind2web` (full 3.2 GB) | AgentTrek is smaller and more agentic |
| `nvidia/Nemotron-Agentic-v1` `tool_calling` (316,094 rows) | ~20 GB of redundancy vs ToolACE + Hermes |
| MIND-LLM `runs/*/model.resume.pt` (4.8 GB) | Resume state; only `model.pt` is needed for inference |
| MIND-LLM `checkpoints/scaling_small.pt` | Referenced in `SCALING_RESULTS.md:42` but **the file is absent**; only its manifest remains |
| MIND-LLM `greedy_byte_bpe` 16k tokenizer **as a GGUF export source** | `merges: []` - live correctness hazard (D.3) |
| Automaton `aegis-ai/` blockchain / wallet / crypto subsystems | `viem`, `siwe`, `tweetnacl`, ERC-8004, x402 USDC payments - irrelevant to AEGIS-X |
| Automaton `soul/`, `replication/`, `social/`, `registry/` | Identity / self-replication subsystems; not in the AEGIS-X spec |
| Claude ref `restored-src/node_modules/` (~4,800 files) | Vendored dependencies; no value |
| Claude ref `cli.js.map` `sourcesContent` | **Verbatim upstream source. Deliberately not read; must not be read.** |

## G.5 Architectural conflicts

| # | Conflict | Severity | Resolution |
|---|---|---|---|
| **R1** | AEGISAI permanently forbids `PROCESS_EXECUTION` (`tools/permissions.py:31-32`, pinned by `tests/test_tools.py:104-111`); AEGIS-X "Tools" requires terminal + Python + package managers + deployment | **CRITICAL** | Do **not** simply remove the check. Introduce a distinct `SANDBOXED_EXECUTION` capability that is grantable but routes through `tools/sandbox.py` with rlimits / job-objects, a jailed cwd, no network, and a hard timeout. Update the test to assert *sandboxing*, not *prohibition*. **Requires explicit user decision.** |
| **R2** | MIND-LLM context is **512 tokens**; `GPT.forward` **raises** rather than truncating (`model.py:233-238`). An AEGIS-X turn (system + tool schemas + memory + history + results) is thousands of tokens | **CRITICAL** | Three-pronged: (a) MIND-LLM only ever serves the **fast tier** with a <= 400-token budget; (b) `ToolSearch` progressive disclosure (Claude C.3) keeps the schema footprint small; (c) the reasoning tier uses a 32k+ model via `/api/chat`. **Also extend MIND-LLM to 8,192 with RoPE scaling** (`ROADMAP.md:59` marks it NOT IMPLEMENTED) |
| **R3** | MIND-LLM tokenizer is 16k `greedy_byte_bpe` with `merges: []` at 2.56 chars/token; Ollama's native tool format assumes a modern BPE; GGUF export is a hazard | **CRITICAL** | Re-train the tokenizer with real BPE merges before any SFT (`prepare_data.py:143` already does this - it just was not used at 16k). Serialize tool calls through the **canonical** `_normalize_tool_call` shape (`normalize.py:268-291`) so the runtime format is tokenizer-independent. **Do not pursue GGUF.** |
| **R4** | MIND-LLM has 0/10 capability probes and 100,000 training tokens (0.005% of Chinchilla); AEGIS-X needs a planner and a verifier | **CRITICAL** | Model router with a reasoning tier for planning/verification; MIND-LLM as the fast tier only. AEGIS-X's data pipeline is the engine but holds 752,795 real tokens (0.037% of a 100M model's requirement) - the corpus must grow roughly 1,000x |
| **R5** | **Two agent runtimes already exist**: AEGISAI `agents/agent.py` and MIND-LLM `agentic/orchestrator.py` (1,300 LOC). Building a third wastes both | **HIGH** | AEGIS-X's `agents/` is the canonical runtime. Harvest MIND-LLM's `agentic/` as a **reference design** (its `InferenceBackend` Protocol, `ContextEngine`, `TaskManager` DAG, fail-closed `PermissionPolicy`, `Verifier`, `EventSink` are all well-designed) and **port the abstractions, not the code**. Demote AEGISAI's `Agent` to an inference adapter |
| **R6** | AEGISAI has 0 async; MIND-LLM has `asyncio` but no KV cache | **HIGH** | Introduce `anyio`. MIND-LLM's server runs in a separate process so the GIL is not shared. The KV cache (M4) is the gating item |
| **R7** | AEGISAI's data schema is `{messages, tool_calls, tool_results}`; MIND-LLM's prompt format is hardcoded ID concatenation (`mindllm_backend.py:46-59`); the Claude reference uses native content blocks; Automaton uses flattened strings | **HIGH** | Define **one** canonical internal message model with `tool_call_id` + `tool_name` (AEGISAI `core/models.py` is the right home). Each backend serializes from it. Never store a second representation. This also resolves D14 |
| **R8** | AEGISAI storage has 2 tables; Automaton has 36; the Claude reference is event-sourced. spec "Persistent tasks" lists 13 fields to persist | **HIGH** | One `Migration(version=2)` with roughly 10 tables. Do not adopt Automaton's 36 - most are financial / child-lifecycle specific |
| **R9** | MIND-LLM records `torch 2.14.0+cpu` - **not a public release**. No `requirements.txt` / `pyproject.toml` in either project | **HIGH** | Create `pyproject.toml` in AEGIS-X pinning a *public* torch. MIND-LLM must be a vendored dependency with a declared version |
| **R10** | AEGISAI `data/` is gitignored; `datasets/` is not (311 MB) | **MEDIUM** | Add `datasets/` to `.gitignore`. Keep a small `datasets/manifests/` allowlist if manifests must be versioned |
| **R11** | AEGISAI's tokenizer stage is missing; MIND-LLM's `prepare_data.py` produces uint16 shards with a `.spans.json` sidecar | **MEDIUM** | **Reuse MIND-LLM's `prepare_data.py` and `data_loader.py` in spirit.** They are more mature than anything in AEGISAI's pipeline. Add `data_pipeline/pack.py` as a thin adapter, not a reimplementation |
| **R12** | Automaton built `ContextManager` / `CompressionEngine` / `EnhancedRetriever` and **never wired them**; its `ARCHITECTURE.md` drifted (57 to 85 tools, 22 to 36 tables) | **MEDIUM** | Add `tests/test_wiring.py` that fails if any constructed subsystem is never called. Add `tests/test_doc_freshness.py` asserting documented tool/table counts match code |
| **R13** | Automaton's tool results are plain `string`; the Claude reference uses typed `ToolResult<T>`; AEGISAI uses a `ToolResult` dataclass | **MEDIUM** | AEGISAI's `ToolResult` is the best of the three. Keep it; add `is_error` and structured error codes (the reference's `tool_use_error` pattern) |
| **R14** | Both AEGISAI and MIND-LLM declare `dependencies = []` / no manifest | **MEDIUM** | AEGIS-X gets a real `pyproject.toml`. The zero-dependency heritage ends at Phase 1 |
| **R15** | MIND-LLM has no tool-call emission, but `agentic/types.py` declares `EventKind="tool_call"` and `orchestrator.py:62-65` handles it | **MEDIUM** | The wiring exists; only the emission is missing. This is a **small** change once the model can produce the syntax - do not redesign the event protocol |
| **R16** | spec "Tools" lists 14 tool families including browser automation and deployment; the Claude reference is a **terminal** product; Automaton has no browser | **LOW** | Build the tool interface first (`tools/factory.py`), then implement families in priority order: filesystem, terminal, git, python, web, api, browser. Do not let a missing browser block the runtime |

## G.6 Implementation order (summary)

See **Section I** for the full 19-phase sequence with gates.

---

# H. FINAL TARGET TREE

Extends AEGISAI's existing **flat top-level package layout** (already declared as `packages = [...]` in `pyproject.toml`) rather than introducing a new `aegis-x/` root. Existing packages are extended; new ones are added alongside.

```text
AEGISAI/                                  <- flat layout preserved
|-- core/                                 <- EXTENDED
|   |-- config.py           MODIFIED       settings + sources + migrations
|   |-- models.py           MODIFIED       + tool_call_id/tool_name, Run/Step/Plan/Task
|   |-- errors.py           MODIFIED       + 9 error classes
|   |-- ids.py              NEW            run/step/tool_call/trace ids
|   |-- budget.py           NEW            TokenBudget, StepBudget, CostBudget
|   |-- transitions.py      NEW            typed Continue union
|   `-- tasks.py            NEW            Task, TaskStatus, DAG protocol
|
|-- agents/                                <- REWRITTEN around a real loop
|   |-- agent.py            MODIFIED       demoted to an inference adapter
|   |-- loop.py             NEW            * AgentLoop - the ReAct state machine
|   |-- executor.py         NEW            * Executor - Step to ToolCall
|   |-- planner.py          NEW            * Planner protocol + LLMPlanner + StaticPlanner
|   |-- planner_schema.json NEW
|   |-- replanner.py        NEW            * Replanner + ReplanTrigger union
|   |-- verifier.py         NEW            * Verifier - 5 strategies
|   |-- registry.py         NEW            AgentRegistry, AgentSpec
|   |-- delegation.py       NEW            Delegation protocol, supervisor/worker
|   |-- planner.md          NEW            declarative agent definitions
|   |-- coding.md           NEW
|   |-- verifier.md         NEW
|   `-- browser.md          NEW
|
|-- inference/                             <- THE CRITICAL FIX
|   |-- base.py             MODIFIED       + stream/embed/count_tokens, ModelOptions
|   |-- ollama.py           MODIFIED       /api/generate to /api/chat + tools + options
|   |-- options.py          NEW            ModelOptions
|   |-- chat.py             NEW            /api/chat client
|   |-- streaming.py        NEW            NDJSON/SSE parser
|   |-- router.py           NEW            * ModelRouter - task_type x tier, failover
|   |-- mindllm.py          NEW            * MIND-LLM HTTP adapter
|   `-- registry.py         NEW            provider factory
|
|-- models/                                <- NEW (MIND-LLM serving)
|   |-- mindllm_server.py   NEW            * stdlib HTTP server for MIND-LLM
|   |-- kv_cache.py         NEW            * KV cache (removes O(T^2) generation)
|   |-- tool_parser.py      NEW            * tool-call extraction / constrained decode
|   `-- load.py             NEW            safe checkpoint load
|
|-- tools/                                 <- EXTENDED
|   |-- base.py             MODIFIED       build_tool(), fail-closed, budgets
|   |-- registry.py         MODIFIED       to_model_schema, truncation, audit
|   |-- permissions.py      MODIFIED       shim; PROCESS_EXECUTION block removed
|   |-- calculator.py       PRESERVED      security exemplar
|   |-- factory.py          NEW            build_tool() + fail-closed defaults
|   |-- select.py           NEW            ToolSelector, searchHint, progressive disclosure
|   |-- executor.py         NEW            timeout, parallel wave, truncation, audit
|   |-- filesystem.py       NEW            Read/Write/Edit/Glob/Grep
|   |-- terminal.py         NEW            Bash/PowerShell + background
|   |-- python_exec.py      NEW            sandboxed Python
|   |-- git.py              NEW            status/diff/log/commit/branch/worktree
|   |-- web.py              NEW            search/fetch/extract + provenance
|   |-- browser.py          NEW            Playwright
|   |-- api_client.py       NEW            generic HTTP/API
|   |-- notes.py            NEW            durable scratchpad
|   |-- sandbox.py          NEW            rlimits / job-objects / seccomp
|   `-- audit.py            NEW            hash-chained invocation log
|
|-- context/                               <- NEW
|   |-- manager.py          NEW            * ContextManager, ContextBudget, utilization
|   |-- compaction.py       NEW            * 5-stage cascade + checkpoint-and-reset
|   `-- window.py           NEW            token counting, truncation
|
|-- memory/                                <- WAS A 1-LINE STUB
|   |-- __init__.py         MODIFIED       real re-exports
|   |-- base.py             NEW            MemoryStore ABC
|   |-- working.py          NEW
|   |-- episodic.py         NEW
|   |-- semantic.py         NEW
|   |-- procedural.py       NEW
|   |-- retrieval.py        NEW            hybrid keyword + vector
|   |-- consolidation.py    NEW            * ORIENT -> GATHER -> CONSOLIDATE -> PRUNE
|   `-- budget.py           NEW            per-tier caps + rollforward
|
|-- tasks/                                 <- NEW
|   |-- manager.py          NEW            * TaskManager, DAG, cycle detection
|   |-- records.py          NEW            all 13 spec "Persistent tasks" fields
|   |-- log.py              NEW            byte-offset output log
|   `-- store.py            NEW
|
|-- observation/                           <- NEW
|   |-- manager.py          NEW            * ObservationManager - normalize, score, redact
|   |-- budget.py           NEW            3-layer size budgets
|   `-- trust.py            NEW            untrusted marking
|
|-- verification/                          <- NEW (thin facade over agents/verifier.py)
|   |-- __init__.py
|   `-- strategies/         NEW
|
|-- recovery/                              <- NEW
|   |-- __init__.py
|   |-- classifier.py       NEW            * error classification
|   |-- manager.py          NEW            retry / backoff / compensate
|   `-- replan_bridge.py    NEW
|
|-- security/                              <- WAS A 1-LINE STUB
|   |-- __init__.py         MODIFIED
|   |-- policy.py           NEW            * PermissionManager, PermissionDecision
|   |-- approvals.py        NEW            TTL + args-hash-bound approval gate
|   |-- sandbox.py          NEW
|   |-- secrets.py          NEW            redaction + secret store
|   |-- audit.py            NEW
|   `-- rules/              NEW            validation, path, command, authority, rate
|
|-- events/                                <- NEW
|   |-- bus.py              NEW            * EventBus over the existing Event model
|   |-- hooks.py            NEW            HOOK_EVENTS, async-vs-blocking
|   |-- types.py            NEW
|   `-- sinks.py            NEW            stderr/file/sqlite/callback/statusline
|
|-- storage/                               <- EXTENDED
|   |-- base.py             MODIFIED       new repository protocols
|   |-- sqlite.py           MODIFIED       + Migration(version=2)
|   |-- repositories.py     MODIFIED       upsert; new repositories
|   |-- runs.py             NEW
|   |-- plans.py            NEW
|   |-- memory_repo.py      NEW
|   |-- checkpoints.py      NEW
|   |-- events_repo.py      NEW
|   `-- audit_repo.py       NEW
|
|-- scheduler/                             <- NEW
|   |-- daemon.py           NEW            recursive setTimeout tick
|   |-- jobs.py             NEW            leases, retries, dead-letter
|   `-- cron.py             NEW            cron + deterministic jitter
|
|-- workers/                               <- NEW
|   |-- pool.py             NEW            WorkerPool, scoped identity, drain
|   `-- identity.py         NEW
|
|-- multi_agent/                           <- NEW
|   |-- topology.py         NEW            MASTER/RESEARCH/CODING/TEST/BROWSER/VERIFIER
|   |-- messaging.py        NEW
|   `-- blackboard.py       NEW
|
|-- observability/                         <- EXTENDED
|   |-- logging.py          MODIFIED       + correlation context
|   |-- events.py           NEW
|   |-- tracing.py          NEW            spans
|   `-- metrics.py          NEW
|
|-- runtime/                               <- EXTENDED
|   |-- runtime.py          MODIFIED       run_id, streaming, checkpoints, resume
|   |-- daemon.py           NEW            long-lived worker process
|   `-- checkpoint.py       NEW
|
|-- data_pipeline/                         <- EXTENDED (strongest subsystem)
|   |-- common.py           PRESERVED      Checkpoint, PipelineReport, atomic writes
|   |-- download.py         PRESERVED      resumable, bounded, hashed
|   |-- validate.py         PRESERVED      per-record tolerance
|   |-- normalize.py        MODIFIED       dedupe schema; add `tools` slot
|   |-- classify.py         MODIFIED       + VERIFICATION/PLANNING; content-based
|   |-- filter.py           PRESERVED
|   |-- deduplicate.py      PRESERVED      + MinHash/LSH for Stage 6 scale
|   |-- split.py            PRESERVED
|   |-- mixture.py          PRESERVED
|   |-- statistics.py       MODIFIED       fix the 1.46x double count
|   |-- inspect.py          PRESERVED
|   |-- config.py           MODIFIED       license policy fields
|   |-- prepare.py          MODIFIED       wire new stages into the DAG
|   |-- cli.py              MODIFIED       new stage commands
|   |-- safety.py           NEW            * toxicity / jailbreak / PII / secret filtering
|   |-- licensing.py        NEW            * allow-deny list + commercial gate
|   |-- tokenize.py         NEW            * tokenizer stage
|   |-- pack.py             NEW            uint16 shards + .spans.json
|   |-- rollout.py          NEW            * AEGIS-X to SFT export
|   |-- eval_sets.py        NEW            held-out agentic eval sets
|   `-- promote.py          NEW            category slices
|
|-- training/                              <- EXTENDED
|   |-- __init__.py         MODIFIED
|   |-- prepare_dataset.py  MODIFIED       demoted to a raw fetcher
|   |-- sft.py              NEW            * MIND-LLM SFT
|   |-- curriculum.py       NEW            * 7-stage curriculum
|   `-- lora.py             NEW            (when hardware allows)
|
|-- evaluation/                            <- NEW
|   |-- harness.py          NEW            agent success rate, step efficiency
|   |-- metrics.py          NEW
|   `-- suites/             NEW            tool_call_format, plan_quality, recovery
|
|-- api/                                   <- WAS A 1-LINE STUB
|   |-- __init__.py         MODIFIED
|   |-- server.py           NEW            stdlib http.server + SSE
|   |-- routes.py           NEW
|   `-- events.py           NEW
|
|-- cli/                                   <- EXTENDED
|   |-- main.py             MODIFIED       + 7 subcommands; use the runtime tool registry
|   |-- run.py              NEW
|   |-- resume.py           NEW
|   |-- plan.py             NEW
|   |-- memory.py           NEW
|   |-- agents.py           NEW
|   |-- jobs.py             NEW
|   `-- serve.py            NEW
|
|-- configs/                               <- EXTENDED
|   |-- default.toml        MODIFIED
|   |-- dataset.toml        MODIFIED       new sources + license policy
|   |-- tools.toml          NEW
|   `-- agents/             NEW            per-agent config
|
|-- docs/                                  <- EXTENDED
|   |-- architecture.md     MODIFIED
|   |-- configuration.md    MODIFIED
|   |-- development.md      MODIFIED
|   |-- datasets.md         MODIFIED
|   |-- data_pipeline.md    MODIFIED
|   |-- training_data.md    MODIFIED
|   |-- security.md         NEW
|   |-- tools.md            NEW            tool-authoring guide
|   `-- AEGIS-X_REFERENCE_ARCHITECTURE_AUDIT.md   <- promote this report here
|
|-- tests/                                 <- EXTENDED (90 -> ~200)
|   |-- ... (14 existing)   PRESERVED      must stay green
|   |-- test_wiring.py      NEW            * fails if a subsystem is built but unwired
|   |-- test_doc_freshness.py NEW          * documented counts must match code
|   `-- ... (17 new suites) NEW
|
|-- data/                                  <- PRESERVED (gitignored)
|-- datasets/                              <- PRESERVED; ADD to .gitignore
|-- pyproject.toml          MODIFIED
|-- README.md               MODIFIED
`-- .gitignore              MODIFIED
```

`*` = load-bearing subsystem. Total: roughly 25 new packages worth of files, ~100 new modules, extending AEGISAI's existing flat layout rather than nesting a new root.

---

# I. IMPLEMENTATION ORDER

| # | Phase | Deliverable | Gate (must be true to proceed) |
|---|---|---|---|
| 1 | **Foundation** | `core/ids.py`, `core/budget.py`, `core/transitions.py`, `core/tasks.py`, `core/errors.py` extension, `core/config.py` extension, `configs/default.toml`, `.gitignore` | 90 existing tests green; `Settings` round-trips |
| 2 | **Inference** | `inference/options.py`, `inference/chat.py` (`/api/chat` + `tools` + `options` + `stream`), `inference/streaming.py`, `health_check` fix, `inference/base.py` extension | A 3-turn conversation with a system prompt and a tool round-trip against real Ollama |
| 3 | **Agent loop** | `agents/loop.py`, `agents/registry.py`, `core/models.py` (`tool_call_id`/`tool_name` + `Run`/`Step`/`ToolInvocation`), `storage/sqlite.py` Migration 2, `storage/runs.py` | Offline e2e: a scripted backend drives a 5-step task to completion with every step persisted |
| 4 | **Tools** | `tools/factory.py`, `tools/select.py`, `tools/executor.py`, `tools/filesystem.py`, `tools/registry.py` (`to_model_schema`, truncation, audit), `security/policy.py` | Model receives tool schemas; filesystem tools work inside a jail; **R1 sandbox design approved** |
| 5 | **Planning** | `agents/planner.py` + schema, `agents/replanner.py`, `tasks/manager.py`, `tasks/records.py`, `storage/plans.py` | A 10-step task produces a validated acyclic plan, persists it, and replans once on injected failure |
| 6 | **Execution** | `agents/executor.py` (parallel via `anyio`, gated on `is_concurrency_safe`), `tools/terminal.py`, `tools/python_exec.py`, `tools/git.py`, `tools/sandbox.py` | A parallel read-only tool wave completes; sandbox escape blocked in tests |
| 7 | **Observation** | `observation/manager.py`, 3-layer result budgets, truncation, redaction, untrusted marking, `context/window.py` | A 10 MB tool result is truncated to budget and the model still completes the turn |
| 8 | **Verification** | `agents/verifier.py` (5 strategies), `verification/`, `VERIFICATION` category in the classifier | A deliberately-wrong result triggers RETRY then REPLAN |
| 9 | **Recovery** | Recovery paths in `agents/loop.py`, `recovery/classifier.py`, retry/backoff, circuit breaker, dead-letter, `runtime/checkpoint.py` | Kill at step N, resume, complete **exactly once** (idempotency test) |
| 10 | **Memory** | `memory/*` (4 tiers), `memory/retrieval.py`, `memory/budget.py` (rollforward), `memory/consolidation.py`, `context/manager.py`, `context/compaction.py`, `storage/memory_repo.py` | A 50-step run completes without context overflow; the cascade reaches checkpoint-and-reset |
| 11 | **Tasks** | `tasks/records.py` full field set, `tasks/log.py` (byte-offset), task tools | All 13 spec "Persistent tasks" fields survive a restart |
| 12 | **Scheduling** | `scheduler/daemon.py`, `scheduler/jobs.py`, `scheduler/cron.py`, `workers/pool.py`, `runtime/daemon.py` | A scheduled job runs headless; a job lease expires and is reclaimed |
| 13 | **Multi-agent** | `multi_agent/topology.py`, `multi_agent/messaging.py`, `multi_agent/blackboard.py`, `agents/delegation.py`, tool-scoping allow-sets | MASTER delegates to CODING + TEST concurrently; VERIFIER cannot write files |
| 14 | **Security** | `security/approvals.py`, `security/rules/*`, `security/secrets.py`, `security/audit.py` (hash-chained), `tools/audit.py` | Denials are audited with typed reasons; a dangerous action blocks on approval |
| 15 | **Datasets** | Fix D13 / D14 / E1-E5; add `data_pipeline/{safety,licensing,tokenize,pack}.py`; update `configs/dataset.toml`; then Tier 1 downloads | `mixture.json` token count within 5% of a real tokenizer; PII records **rejected not flagged**; a 447 MB minimum corpus processes end to end |
| 16 | **Training** | MIND-LLM: KV cache, tokenizer re-train, context 512 to 8192, SFT on Stages 1-4; `training/sft.py`, `training/curriculum.py` | Tool-call format accuracy > 95% on a held-out set |
| 17 | **Evaluation** | `evaluation/harness.py` + suites; `data_pipeline/rollout.py`; `data_pipeline/eval_sets.py` | A baseline agent success rate and step efficiency are recorded and **gated in CI** |
| 18 | **API / CLI** | `api/server.py` + `routes.py` + `events.py`; 7 CLI subcommands; `events/bus.py` + `hooks.py`; `observability/*` | A remote client starts a run and streams step events; the statusline renders live state |
| 19 | **Production hardening** | Load tests, chaos tests, long-run soak, memory-leak audit, dependency pinning, CI matrix, coverage thresholds | 24h soak with no memory growth; coverage >= 70% on all new subsystems |

**Critical path:** 1 -> 2 -> 3 -> 4 -> 5 -> 7 -> 8 -> 9 -> 10.
Phases 6, 11, 12, 13, 14 are parallelizable after 4.
Phase 15 is independent of the runtime and can start immediately.
Phase 16 is gated on 15 **and** the MIND-LLM P0 work (D.9).

**Test plan per phase:** unit (pure, no I/O) + integration (scripted backend) + adversarial (safety) for every subsystem; one offline e2e exercising the full chain; `tests/test_wiring.py` and `tests/test_doc_freshness.py` at every gate; the 90 existing tests green at every commit.

---

# J. DO NOT IMPLEMENT YET

This phase is **AUDIT ONLY**. Confirmed: nothing was implemented.

Not done in this phase:
- No source file modified, deleted, renamed, or moved
- No dependency installed
- No dataset downloaded
- No existing code overwritten
- No destructive command run
- No long training started
- No MIND-LLM model changed
- No checkpoint deleted (including the 4.8 GB of MIND-LLM resume files - removal is recommended in D.9 but was **not** performed)

**Awaiting approval before:** Phase 1 implementation, any `AEGISAI_*` dataset download, and the R1 sandbox-capability decision (which reverses a currently-tested safety guarantee and therefore requires explicit sign-off).

---

# I. FINAL SUMMARY - THE SIX REQUESTED ANSWERS

## 1. EXACT FILES THAT SHOULD BE CREATED

**~100 new files.** Full detail in **Section G.1**. The load-bearing 12:

| File | Purpose |
|---|---|
| `agents/loop.py` | * The agent loop. Does not exist in any of the four trees in runnable form |
| `agents/executor.py` | * Step to ToolCall to ToolResult |
| `agents/planner.py` + `planner_schema.json` | * Plan generation with validated output |
| `agents/replanner.py` | * `ReplanTrigger` union |
| `agents/verifier.py` | * 5 verification strategies |
| `inference/chat.py` + `options.py` + `router.py` | * `/api/chat` (fixes D1) + model routing |
| `context/manager.py` + `compaction.py` | * Context budgeting and the 5-stage cascade |
| `memory/*` (8 files) | 4 tiers + retrieval + consolidation + budget |
| `tasks/*` (4 files) | * DAG task manager carrying all 13 spec fields |
| `security/*` (7 files) | * Policy, approvals, sandbox, audit, rules |
| `models/mindllm_server.py` + `kv_cache.py` + `tool_parser.py` | * The MIND-LLM connection |
| `data_pipeline/{safety,licensing,tokenize,pack,rollout}.py` | * The data-side gaps |

Plus 19 test files (including `test_wiring.py` and `test_doc_freshness.py`), 2 docs, 7 CLI subcommands, 3 API files, 7 storage repositories, 5 scheduler/worker modules, 3 multi-agent modules, 4 observability modules.

## 2. EXACT FILES THAT SHOULD BE MODIFIED

**29 files.** Full detail in **Section G.2**. The load-bearing 8:

| File | Change |
|---|---|
| `inference/ollama.py` | `/api/generate` to `/api/chat`; add `tools`, `options`, `stream`; **fix the `health_check` false-positive** |
| `core/models.py` | Add `tool_call_id` + `tool_name` to `Message`; add `Run` / `Step` / `ToolInvocation` / `Plan` / `Task` |
| `tools/base.py` | Add the `build_tool()` fail-closed factory; generate `call_id`; add result size budgets |
| `tools/permissions.py` | **Remove the `PROCESS_EXECUTION` hard block** in favour of a sandbox-gated capability |
| `agents/agent.py` | Demote to an inference adapter (prevents a third runtime) |
| `storage/sqlite.py` | Append `Migration(version=2)` with roughly 10 new tables. **Never edit migration 1** |
| `runtime/runtime.py` | Add `run_id`, streaming, checkpoints, resume, single-transaction turn commit |
| `data_pipeline/statistics.py` | **Remove the `tool_results` double count at `:81-83`** - every token figure in the repo is 1.46x wrong |

## 3. EXACT FILES THAT SHOULD BE PRESERVED

**20 files/directories.** Full detail in **Section G.3**. Highlights:

- **`data_pipeline/` (9 of 13 modules)** - the strongest subsystem in the repository. 11 stages, ~2,400 LOC, 28 tests, **actually executed**. Preserve `common.py`, `download.py`, `validate.py`, `split.py`, `mixture.py`, `deduplicate.py`, `inspect.py`, `cli.py` verbatim. `statistics.py` and `normalize.py` get surgical fixes only.
- **`storage/sqlite.py` `MIGRATIONS[0]`** - never edit. Append version 2.
- **`storage/repositories.py`** - parameterized SQL, FK cascade, role CHECK, ordered reads.
- **`core/models.py` `Event`** - complete, tested, unused. **Revive it as the EventBus payload.**
- **`observability/logging.py` `StructuredFormatter`** - correct.
- **`tools/calculator.py`** - the security exemplar for every new tool.
- **`tools/base.py` `_validate_value`** - working JSON-Schema subset validator; keep until `pydantic` is adopted.
- **The 90 existing tests** - must stay green at every commit.
- **`configs/dataset.toml`** - 9 sources with licenses + `max_bytes` + `enabled`; the model for new source configs.
- **The 6 docs** - accurate as Phase-1 documentation; extend, do not delete.

**Preserve as design references (do not merge):** MIND-LLM's `agentic/` abstractions, `pretrain.py` token-budget planning and bit-exact resume, `prepare_data.py` / `data_loader.py` span-bounded sharding, `checkpoint_utils.py` atomic saves and SHA-256 artifact identity. Port the ideas; do not couple to the files.

## 4. EXACT FILES/DATASETS THAT SHOULD NOT BE USED

**20 items.** Full detail in **Section G.4**. The most important:

- **Orphan data:** `datasets/processed/local/`, `datasets/manifests/local/`
- **Useless for agency:** `datasets/processed/neulab_mind2web/` (0 tool calls)
- **Wrong schema:** `data/training/starcoder_python_instruct.jsonl` as a training target
- **Licensing traps:** `Salesforce/xlam-function-calling-60k`, `Salesforce/APIGen-MT-5k`
- **Wrong modality:** `xlangai/AgentNet`, `osunlp/Multimodal-Mind2Web`
- **Poor signal/GB:** `bigcode/stack-dedup`, `teknium/OpenHermes-2.5`, `glaiveai/glaive-function-calling-v2`
- **Licensing traps (general):** `lmsys/lmsys-chat-1m` (non-commercial + PII), `allenai/tulu-3-sft-mixture` (ODC-BY with non-commercial subsets)
- **Redundant at scale:** `neulab/mind2web` (full), `nvidia/Nemotron-Agentic-v1` `tool_calling` split
- **Dead weight:** MIND-LLM `runs/*/model.resume.pt` (4.8 GB); the absent `checkpoints/scaling_small.pt`
- **Correctness hazard:** MIND-LLM `greedy_byte_bpe` tokenizer as a GGUF export source
- **Out of scope:** Automaton's blockchain / wallet / replication / social subsystems
- **Must not be read:** the Claude archive's `cli.js.map` `sourcesContent` (verbatim upstream source)

## 5. THE IMPLEMENTATION ORDER

**19 phases, fully specified in Section I with gates for each.**

**Critical path:** 1 Foundation -> 2 Inference -> 3 Agent loop -> 4 Tools -> 5 Planning -> 7 Observation -> 8 Verification -> 9 Recovery -> 10 Memory

**Parallelizable after phase 4:** 6 Execution, 11 Tasks, 12 Scheduling, 13 Multi-agent, 14 Security

**Independent, can start immediately:** 15 Datasets

**Gated on both 15 and the MIND-LLM P0 work:** 16 Training, then 17 Evaluation, 18 API/CLI, 19 Production hardening

**The first four phases are the ones that matter most**, and only the first is a pure refactor:
1. `core/` foundation (ids, budget, transitions, config) - low risk
2. `inference/` to `/api/chat` - **the fix that unblocks everything**
3. `agents/loop.py` + Migration 2 - the centerpiece
4. `tools/` real system - only after R1 is decided

## 6. ARCHITECTURAL CONFLICTS BETWEEN AEGISAI, AUTOMATON, CLAUDE-REFERENCE CONCEPTS, AND MIND-LLM

**16 conflicts identified. Full table in Section G.5. The six that will break the build if ignored:**

| # | Conflict | Why it breaks |
|---|---|---|
| **R1** | AEGISAI hard-denies `PROCESS_EXECUTION`; the spec requires terminal, Python, package managers, deployment | The test at `tests/test_tools.py:104-111` pins the prohibition. Terminal and Python tools are unimplementable until this is reversed **in favour of sandboxing, not removal**. Requires explicit sign-off. |
| **R2** | MIND-LLM has 512 tokens and **raises** rather than truncating; an agent turn needs thousands | Any prompt that overflows is a crash, not a degradation. Compounded by the absence of progressive tool disclosure. |
| **R3** | MIND-LLM's tokenizer is 16k `greedy_byte_bpe` with **zero merges**; the runtime tool format must be tokenizer-independent | Any GGUF/Ollama path risks silent tokenization mismatch. The fix is to re-train the tokenizer and to route tool calls through AEGISAI's canonical `_normalize_tool_call`. |
| **R4** | MIND-LLM scores 0/10 on capability probes after 100,000 training tokens | It cannot plan, cannot verify, cannot follow instructions. Making it the sole decision-maker produces a non-functional system. The tiered router is the resolution. |
| **R5** | **Two agent runtimes already exist** - AEGISAI `agents/agent.py` and MIND-LLM `agentic/orchestrator.py` | Building a third wastes both and fragments the design. AEGIS-X's `agents/` must be canonical; MIND-LLM's `agentic/` is a reference to port abstractions from. |
| **R7** | Four incompatible message representations across the four trees | A tool result cannot be correlated if the internal model has no `tool_call_id`. One canonical model in AEGISAI `core/models.py`; every backend serializes from it; never store a second copy (which also resolves D14). |

**Lower-severity but structural:** R6 (async), R8 (schema scope), R9 (non-public torch, no manifests in either project), R10 (311 MB gitignore gap), R11 (reuse MIND-LLM's sharding), R12 (Automaton's unwired subsystems - hence `test_wiring.py`), R13 (tool result typing), R14 (zero-dependency heritage ends at Phase 1), R15 (MIND-LLM's `tool_call` event is declared and handled but never emitted - a small gap once the model can produce the syntax), R16 (14 tool families vs a terminal-only reference).

---

## CLOSING NOTE

The three strongest assets in this project are not the model:

1. **AEGISAI's `data_pipeline/`** - 11 production-grade stages, disk-backed resumable dedup, SHA-256 provenance chains, 28 passing tests, and 311 MB of actually-executed artifacts. Two surgical bugs (1.46x token inflation, schema duplication) stand between it and production readiness.
2. **Automaton's orchestration design** - the Goal/Task DAG, the validated planner, the first-deny-wins policy engine, the transactional turn commit, and the memory budget with rollforward are directly portable and solve real problems.
3. **The Claude reference's boundary design** - the fail-closed tool factory, layered context compaction, per-agent-class tool scoping, typed decision reasons, and the statusline-as-public-IPC-contract.

The weakest link is unambiguous: **MIND-LLM is a laboratory artifact.** 0/10 capability probes, 100,000 training tokens, a 512-token context, no KV cache, no streaming, no tool-call emission, and no export path. The correct engineering response is not to make AEGIS-X depend on it, but to **route around it while keeping it in the loop as a fast tier whose every contribution becomes rollout data.** That is how MIND-LLM becomes an agent rather than a dependency.

---

# K. ACQUISITION RUN - 2026-09-25 (executed after the audit)

Phase 15 (Datasets) was executed. Scope agreed: **agent-weighted composition, download + run pipeline only.**

## K.1 What was acquired

| | |
|---|---|
| Raw downloaded | **4,549.8 MB (4.44 GB)** across 30 files |
| Files / sources | 30 files, 18 prepared sources, 10 new repos |
| Download errors | **0** |
| Pipeline | `prepare` exit 0, 11 stages, 125.5 min, `config_sha256 36b5de14612fc433` |
| Records processed | **83,469** across 18 sources |
| Global pool | **78,052** after cross-source dedup (0 exact/normalized duplicates) |
| Final mixture | **6,000** records / 10 distinct sources / **8,651,510 est. tokens** / 11,168 tool calls |
| Split | 4,847 train / 591 validation / 562 test |
| Mixture shortfall | **0** - every configured quota filled |
| Disk after | `datasets/` 12.6 GB in 537 files; 10.1 GB free of 120 GB |
| Tests / lint after | 90/90 pass, `ruff` clean |

## K.2 The gap the audit predicted, now closed

`PLANNING`, `DEBUGGING`, `REASONING`, `AGENT_TRAJECTORY` and `BUSINESS_AUTOMATION` were all **0 records** in the Phase-1 mixture. They are now populated:

| Category | Before | After |
|---|---:|---:|
| CODING | 275 | 1,982 |
| TOOL_USE | 100 | 1,200 |
| PLANNING | **0** | **600** |
| DEBUGGING | **0** | **600** |
| REASONING | **0** | **300** |
| AGENT_TRAJECTORY | **0** | **300** |
| BUSINESS_AUTOMATION | **0** | **300** |
| ERROR_RECOVERY | 50 | 600 |
| WEB_TASK | 75 | 118 |

## K.3 Sources added (all paths and sizes verified against the HF API before downloading)

| Source | License | Why |
|---|---|---|
| `Team-ACE/ToolACE` | apache-2.0 | nested/parallel/dependent function calls |
| `voxozi/agentforge-multiturn-toolcall` | apache-2.0 | 30.5% tool-failure recovery branches |
| `NousResearch/hermes-function-calling-v1` | apache-2.0 | strict JSON emission |
| `LiteMind/Simple-agent-traces` | apache-2.0 | 8,192-token discipline (see K.5, currently 0 records) |
| `bigcode/self-oss-instruct-sc2-exec-filter-50k` | odc-by | execution-validated Python |
| `ise-uiuc/Magicoder-OSS-Instruct-75K` | mit | multi-language code |
| `nvidia/Nemotron-Agentic-v1` (`interactive_agent`) | cc-by-4.0 | judge-filtered agentic trajectories; was configured but never downloaded |
| `HuggingFaceTB/smoltalk` (Smol-* subsets) | apache-2.0 | instruction following, system chats |
| `neulab` SWE-agent + code_feedback + browser | cc-by-4.0 / apache-2.0 / mit | real SWE and execution-feedback trajectories |

**Rejected on licence grounds:** `xlangai/AgentTrek` declares no licence in `cardData` and none in its README. `neulab_browser` (MIT) was substituted.

## K.4 Five defects the run exposed (all previously invisible)

**D13 CONFIRMED AND FIXED.** `data_pipeline/statistics.py:81-83` double-counted `tool_results`, which mirrors the tool-role messages already counted in `characters`. Measured inflation on the new mixture: **11,289,682 reported vs 8,651,510 actual = 1.30x**. Corrected; both figures are now emitted (`estimated_tokens` and `estimated_tokens_with_tool_result_doublecount`).

**NEW - four sources were 100% silently rejected on schema-key mismatch.** The normalizer supports five dialects; these sources use different key names for the same concept. Every record was rejected while the run still reported "accepted" at the normalize stage:

| Source | Actual keys | Normalizer reads | Records lost |
|---|---|---|---|
| `agentforge` | `conversations[].role/.content` | `conversations[].from/.value` | 5,000 |
| `magicoder` | `problem` / `solution` | `instruction` / `text` | 6,000 |
| `self_oss` | `instruction` / `response` | `instruction` / `text` | 6,000 |
| `simple_agent` | `messages[].text` | `messages[].content` | 605 |

**This is the same defect class as D15** (classify.py matching on dataset name rather than content) - a shallow structural assumption in the data layer. Three were recovered offline by rewriting the raw files into a dialect the existing normalizer reads; **130,858 records recovered**. `simple_agent` was not recovered in this run (605 records, low value against a 2-hour pipeline re-run).

**NEW - classifier mislabels the entire `neulab_swe` source.** `classify.py:82-83` returns `ERROR_RECOVERY` for any `swe`-subset record whose text contains "error". Real SWE-agent trajectories always do, so **6,000/6,000 were labelled ERROR_RECOVERY and 0 were labelled CODING**. The mixture consequently took its 600 ERROR_RECOVERY records from this mislabelled source.

**NEW - `nemotron_ia` self-duplicates at 60%.** 3,585 of 6,000 records were removed as internal duplicates. Synthetic multi-turn generation with a shared simulator produces near-identical trajectories.

**OBSERVED - global dedup found 0 cross-source duplicates over 78,052 records.** Plausible given genuinely distinct sources, but `near_duplicates = false` in `[quality]`, so near-duplicates were not checked. The Phase-1 run also found 0.

## K.5 Outstanding

1. `simple_agent` needs a `text`->`content` rewrite of `messages[]`, or a normalizer branch.
2. `normalize.py` should gain branches for the `role/content`, `problem/solution`, `instruction/response` and `messages[].text` dialects so this class of loss cannot recur silently.
3. `classify.py` should not derive `ERROR_RECOVERY` from a substring match on the `swe` subset; it needs content evidence.
4. `normalize.py:63-76` still stores `tool_calls` and `tool_results` twice (D14). Unfixed; it inflates `datasets/` but is now excluded from the token figure.
5. No safety stage (D16) and no licence gate (D17). 906 records in the mixture carry a non-blocking `sensitive_data_review` flag and are emitted anyway.
6. `datasets/` is still not gitignored (D21) - now 12.6 GB.
7. MIND-LLM remains at 0/10 capability with 100,000 training tokens. **8.65 M tokens is 0.4% of what a 100 M-parameter model needs.** Phase 16 remains blocked on the P0 MIND-LLM work in D.9.

## K.6 Files changed during this run

| File | Change |
|---|---|
| `configs/dataset.toml` | 12 new sources, 2 enabled, `max_records` 10,000 -> 6,000, licence notes with sizes and verification dates |
| `data_pipeline/statistics.py` | **D13 fix** - corrected `estimated_tokens`, added `estimated_tokens_basis` and the legacy double-count figure for comparison |
| `tests/test_data_pipeline.py` | updated the two stale config assertions; added 12 enabled-source assertions |
| `docs/AEGIS-X_REFERENCE_ARCHITECTURE_AUDIT.md` | this section |
| `datasets/raw/**` | +4.44 GB across 30 files, plus 8 derived dialect/JSONL files |
| `datasets/{staging,processed,manifests}/**` | regenerated by the pipeline |

90/90 tests pass and `ruff` is clean after all of it.

---

*End of audit. Section K records work executed after the audit was written.*


