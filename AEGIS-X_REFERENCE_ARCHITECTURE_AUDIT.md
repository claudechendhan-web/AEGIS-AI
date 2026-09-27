# AEGIS-X REFERENCE ARCHITECTURE AUDIT

**Phase:** 0 — Architecture/reference audit. **Nothing implemented.**
**Date:** 2026-09-25
**Requested output:** `C:\Users\Admin\Downloads\AEGISAI\AEGIS-X_REFERENCE_ARCHITECTURE_AUDIT.md`

---

## 0. SOURCE RESOLUTION

Two paths in the request do not exist. Resolved to the real locations:

| Requested path | Reality |
|---|---|
| `AEGISAI\AEGIS-X_OPENCode_MASTER_SPEC.md` | **Absent.** Spec is at `C:\Users\Admin\Downloads\AEGIS-X_OPENCode_MASTER_SPEC.md` (7,714 B, 332 lines) |
| `AEGISAI\references\` | **Absent.** No `references/` directory exists anywhere under `AEGISAI\` |

The reference archives are at `C:\Users\Admin\Downloads\` root, and the two "missing" ones exist **without** the `(1)` suffix:

| Archive | Path | Size | Verdict |
|---|---|---|---|
| Claude pkg extract | `Downloads\claude-code-public-package-extract-main.zip` | 50.8 MB | Audited — primary Claude reference |
| Claude sourcemap | `Downloads\claude-code-sourcemap-main.zip` | 77.1 MB | Audited — 4,756-module manifest |
| `claude-code-clone-main(1).zip` | → `Downloads\claude-code-clone-main.zip` | 9.9 MB | **ZERO value** — exact path-for-path subset of the pkg extract (0 clone-only files) |
| `claude-main(1).zip` | → `Downloads\claude-main.zip` | 22.0 MB | Audited — strict superset, +312 files, ~5 genuinely new |
| Automaton (extracted) | `Downloads\automaton-main\automaton-main` | 20,750 files | Audited — primary Automaton reference |
| MIND-LLM | `Downloads\mind-llm-v2(2)\mind-llm-v2` | 256 files, 6.18 GB | Audited |

**Five `automaton*.zip` files exist, not four.** All five were diffed:

| Zip | Files | Verdict |
|---|---:|---|
| `automaton-main (2).zip` | 20,228 | **Origin of the extracted tree.** 0 paths absent from the tree; the tree is 18 source files *newer* and additionally contains the `aegis-ai/` sub-project (462 files) plus `src/inference/ollama.ts` + 2 Ollama tests |
| `automaton-main.zip` | 213 | Older snapshot, 27 files behind the tree |
| `automaton-main(1).zip` | 213 | **Byte-identical duplicate** of `automaton-main.zip` |
| `automaton-pro.zip` | 223 | Base + an unwired `src/pro/` layer |
| `automaton-improved.zip` | 226 | **The only archive with content the tree lacks** — `pro.zip` + runway forecasting |

### Provenance and legal boundary

The Claude archives are **third-party re-packages of proprietary Anthropic code**, not open source. `claude-main.zip/package.json` declares `"name": "@anthropic-ai/claude-code"`, `"version": "0.0.0-leaked"`, `"license": "UNLICENSED"`, `"private": true`. The Automaton `src/pro/` layer is likewise third-party.

**Only general architectural concepts, terminology, type/enum/config names, and module boundaries are extracted.** No implementation bodies, no credentials, keys, tokens, secrets, or private endpoints. No sourcemap `sourcesContent` was read. Nothing is copied wholesale.

---

# A. CURRENT AEGISAI ARCHITECTURE

**Measured:** 249 files, 59 `.py`, ~7,700 LOC, 90 tests, `dependencies = []`.

| Package | LOC | State |
|---|---:|---|
| `core/` | 430 | Config, models, errors — complete |
| `runtime/` | 139 | `Runtime`, 150 LOC, one linear `run()` |
| `inference/` | 210 | ABC + `OllamaProvider` on `/api/generate` |
| `agents/` | 61 | `Agent`, 65 LOC, pass-through adapter |
| `tools/` | 400 | Tool ABC, registry, permissions, calculator |
| `storage/` | 425 | SQLite, **2 tables**, migration framework |
| `observability/` | 70 | JSON formatter, 3 call sites |
| `cli/` | 315 | argparse, 5 subcommands |
| `memory/` `security/` `api/` | **1 each** | **one-line stubs** |
| `data_pipeline/` | 2,400 | **11 stages, 28 tests, executed** |
| `tests/` | 2,400 | 90 tests, all offline |

**Actual dataflow** — no loop anywhere:

```
cli/main.py → runtime/runtime.py::run()  (8 linear steps)
  → agents/agent.py::run()  (1 provider call)
    → inference/ollama.py  POST /api/generate
      _build_prompt() flattens the whole transcript to one string
```

**Verified defects:** wrong endpoint (`/api/generate` not `/api/chat`); `Message` has no `tool_call_id`/`tool_name`; `execute_tool` has one caller (a test); `PROCESS_EXECUTION` hard-denied; no token/context management; `health_check` false-positive; 2-table schema; zero async across 59 files; `Event` + `log_event` built but dead; `save_conversation` uses `INSERT OR IGNORE`; a test writes to the real DB.

**Data pipeline is the strongest asset** — 11 executed stages, disk-backed resumable dedup, SHA-256 provenance chain, 311 MB → now 12.6 GB of artifacts.

## A.1 What AEGISAI already has (reuse as-is)

| Asset | Location | Why it is good |
|---|---|---|
| 11-stage dataset pipeline | `data_pipeline/` (13 files) | Production-grade. `Checkpoint` + atomic `Path.replace` (Windows-safe), resumable HTTP `Range` downloads with SHA-256, disk-backed SQLite dedup, deterministic hash splits, weighted mixtures, run manifests |
| SQLite foundation | `storage/sqlite.py`, `storage/repositories.py` | Idempotent migrations, FK cascade, role CHECK, parameterized SQL throughout, `PRAGMA user_version` |
| Tool contract | `tools/base.py` | Real JSON-Schema-subset validator (`_validate_value`), `ToolResult`/`ToolCall` value types |
| Permission capability enum | `tools/permissions.py` | Clean 5-value enum (the *hard block* is the problem, not the enum) |
| Security exemplar | `tools/calculator.py` | AST whitelist, no `eval()`, depth/size caps |
| Event model | `core/models.py:210-239` | Complete, tested, **unused** — revive as the EventBus payload |
| Error taxonomy | `core/errors.py` | 15 classes, clean hierarchy |
| Config layering | `core/config.py` | defaults → TOML → env, with URL/log-level validation |
| JSON logging | `observability/logging.py` | Correct reserved-field set |
| 90 tests | `tests/` | All offline, all green |

## A.2 What is broken

| Defect | Location | Severity |
|---|---|---|
| Token counts inflated ~1.30–1.46× | `data_pipeline/statistics.py` (fixed 2026-09-25; now emits both figures) | Was blocking |
| Tool data stored twice | `data_pipeline/normalize.py:63-76` | High — inflates disk |
| Classification by dataset *name* | `data_pipeline/classify.py:81-99` | High — mislabels 6,000/6,000 SWE records as `ERROR_RECOVERY` |
| Four dialects silently 100% rejected | `normalize.py` knows 5 shapes; agentforge/magicoder/self_oss/simple_agent use different key names | High — 17,605 records lost per run |
| No safety stage | 3 regexes produce a *flag*, not a rejection | High |
| No licence gate | `license` is metadata only | High |
| `datasets/` not gitignored | `.gitignore` | High — 12.6 GB |
| No tokenizer stage | grep: 0 hits | High |
| No trainer | — | High |

## A.3 What is missing

Every autonomy subsystem. Scored: **1 COMPLETE, 13 PARTIAL, 21 MISSING, 3 PLACEHOLDER** across 38 subsystems.

---

# B. CURRENT AUTOMATON ARCHITECTURE

**`@conway/automaton` v0.2.1** — TypeScript 5.9, ESM, Node ≥ 20, `better-sqlite3`, vitest 2.x. **36 SQLite tables** (`SCHEMA_VERSION = 11`; its own `ARCHITECTURE.md` claims 8/22 — documented drift).

## B.1 Capabilities worth reproducing

| # | Capability | Location | AEGIS-X action |
|---|---|---|---|
| B1 | 12-step turn-structured agent loop | `src/agent/loop.ts:395` | Adopt the shape; reject the sleep/finance steps |
| B2 | **Transactional turn commit** — turn + all tool calls in one `db.runTransaction()` | `src/agent/loop.ts:690-700` | **Adopt verbatim** — fixes AEGISAI's split commits |
| B3 | Goal/Task DAG — `TaskStatus` 7 values, DFS cycle detection, `getReadyTasks`, auto-unblock | `src/orchestration/task-graph.ts` (700 LOC) | **Adopt** — best planning model in any reference |
| B4 | Validated planner output with path-tagged errors | `src/orchestration/planner.ts:767` | **Adopt** — LLM output is never trusted |
| B5 | `ReplanTrigger` discriminated union | `src/orchestration/plan-mode.ts:275-302` | **Adopt** |
| B6 | Orchestrator 8-phase FSM, `maxReplans = 3` | `src/orchestration/orchestrator.ts` | **Adopt** |
| B7 | Complexity gating (`estimatedSteps > 3`) | `orchestrator.ts:809-846` | **Adopt** |
| B8 | Tiered model routing matrix | `src/inference/router.ts` | Adapt — drop survival tier |
| B9 | Failover client, 3 transports, circuit breaker, streaming | `src/inference/inference-client.ts:1221` | **Adopt** |
| B10 | 5-tier memory (working/episodic/semantic/procedural/relationship) | `src/memory/*` | **Adopt** — matches the spec's four |
| B11 | Memory budget with **rollforward** | `MemoryBudgetManager.allocate()` | **Adopt** |
| B12 | 5-stage compression cascade → checkpoint-and-reset | `src/memory/compression-engine.ts` | **Adopt — and actually wire it** (Automaton never did) |
| B13 | Policy engine, first-deny-wins, priority-ordered rules, audited decisions | `src/agent/policy-engine.ts` | **Adopt** |
| B14 | Audit log queried *as* rate-limit state | `policy-rules/rate-limits.ts` | **Adopt** — no separate counters |
| B15 | Harness abstraction for sub-agents | `src/agent/harnesses/` + `HarnessRegistry` | **Adopt** |
| B16 | Sub-agent filesystem boundary + budget isolation + scoped identity | `loop.ts:198`, `local-worker.ts` | **Adopt** |
| B17 | Durable scheduler with 60 s leases, `cron-parser`, retry re-arm | `src/heartbeat/scheduler.ts` | **Adopt** |
| B18 | Recursive `setTimeout`, never `setInterval` | `src/heartbeat/daemon.ts:116-126` | **Adopt** |
| B19 | Wake-event queue with **atomic dequeue** | `wake_events` table | **Adopt** |
| B20 | Event-stream log, 17-value `EventType`, `compact()` | `src/memory/event-stream.ts` | **Adopt** — revives AEGISAI's dead `Event` |
| B21 | Tool→prompt trust fencing (9-layer prompt, 3 sanitisation passes) | `src/agent/system-prompt.ts` | **Adopt** before any web/file tool |
| B22 | Anti-loop as a subsystem — `IDLE_ONLY_TOOLS` + `MUTATING_TOOLS` blocklist + `LoopDetector` with warn→block escalation | `src/agent/loop-detector.ts` | **Adopt** — MIND-LLM at 0 capability *will* loop |
| B23 | Degrade-never-throw discipline (30+ sites) | throughout | **Adopt** |
| B24 | Git-versioned state as a zero-dependency checkpoint | `src/git/state-versioning.ts` | **Adopt** |
| B25 | Priority-ordered rule modules (100/200/300/400/600) | `src/agent/policy-rules/` | **Adopt** |

## B.2 NEW — only in `automaton-improved.zip`, absent from the extracted tree

| # | Capability | Location | AEGIS-X action |
|---|---|---|---|
| B26 | **`executeBatch()` — read-only tools run in a bounded parallel pool; mutating tools run strictly serially afterwards.** Per-tool try/catch so one rejection never rejects the batch | `src/pro/kernel/fastloop.ts` | **Adopt** — Automaton's main loop is strictly serial; this is the fix |
| B27 | Per-tool `CircuitBreaker` (3 strikes, 30 s half-open, resets on success) | `src/pro/kernel/fastloop.ts` | **Adopt** |
| B28 | `PriorityWakeQueue<T>` — sorts by `priority` desc then `at` asc; `next(now)` returns the first due item | `src/pro/kernel/fastloop.ts` | **Adopt** for the scheduler |
| B29 | `TurnCache(ttlMs)` — memoising wrapper with prefix invalidation | `src/pro/kernel/fastloop.ts` | Adopt for tool-result caching |
| B30 | **Reflection engine** — `journalFailure()` → deterministic regex signature table → deduped lessons → guardrailed `ChangeProposal`. `applyProposal()` returns a rejection reason or `null`. Prompt patches are **append-only**, never rewriting a live prompt | `src/pro/selfimprove/reflection.ts` | **Adopt the pattern** — directly serves AEGIS-X §Recovery and §Memory |
| B31 | Runway forecasting — `recordBalanceSample()` (200-sample cap) + pure `calculateRunway()` (24 h window, trend thresholds ±0.5) | `src/survival/monitor.ts` | **Adapt** — AEGIS-X forecasts *budget* runway, not credits |
| B32 | Zero-dependency HTTP + SSE dashboard; constant-time `tokensEqual`; action names sanitised `[^a-z_]` | `src/pro/dashboard/server.ts` | **Adopt** — a minimal observability surface with no dependencies |
| B33 | Process-level safety nets installed in the factory: `uncaughtException` → critical alert, `unhandledRejection` → error alert | `src/pro/index.ts` | **Adopt** |
| B34 | `PROTECTED_PATTERNS` immutable-path guardrails on self-modification | `src/pro/selfimprove/reflection.ts` | Optional — only if AEGIS-X can self-modify |

> ⚠ **Caveat:** `src/pro/**` is **not wired**. `CHANGES.md` admits it is never imported and carries ~12 type errors. Treat B26–B34 as *design reference*, not shipped behaviour. B31 (runway) is the exception — that one **is** wired into the live `check_credits` heartbeat task.

## B.3 What Automaton does not have (do not copy)

Event bus / `EventEmitter` / hook registry · distributed tracing · inbound HTTP server · **process-level sandbox** · streaming command output / PTY · web search / browser automation · **parallel tool calls in the main loop** · MCP client (stub) · plugin ABI · git worktrees · in-process agent switching · typed tool results (all tools return `string`) · interactive human approval in the main runtime.

**Built but unwired in Automaton** — the lesson for AEGIS-X: `ContextManager`/`CompressionEngine` are never called by `runAgentLoop`; `EnhancedRetriever` unused; `HealthMonitor` never invoked; `plannerOutput.customRoles` validated then ignored; `DEFAULT_RETRY_POLICY` declared, never imported; `LocalWorkerPool.shutdown()` never called on SIGTERM. **→ AEGIS-X must ship a test that fails if a constructed subsystem is never called.**

---

# C. CURRENT CLAUDE-REFERENCE ARCHITECTURE

**Source:** `@anthropic-ai/claude-code@2.1.88`, recovered from a sourcemap. Proprietary — **ideas only, no code.**
**Measured:** `src/` = 1,902 files, ~514,587 lines, 1,332 `.ts` + 552 `.tsx`. `main.tsx` 804 KB, `query.ts` 68,684 B, `QueryEngine.ts` 46,632 B, `Tool.ts` 29,518 B.

**This pass closed three gaps the first pass could not reach** — the two `(1)` archives exist without the suffix:

- `claude-code-clone-main.zip` → **zero value.** All 2,204 files are already in the pkg-extract archive, path for path. 18 `.js` files are 75-byte stubs.
- `claude-main.zip` → **strict superset** (+312 files). 5 files are genuinely new type/enum material, listed in C.2.

## C.1 The structural idea: the UI is not the architecture

```
entrypoints/cli.tsx   ← thin fast-path dispatcher
   ↓
main.tsx              ← startup coordinator
   └─→ screens/REPL.tsx   ← 875 KB, REPLACEABLE UX layer
          ↓
utils/processUserInput/     normalize, attachments, hooks, slash commands
          ↓
QueryEngine.ts              reusable stateful conversation engine
          ↓
query.ts   (68,684 B)       THE LOOP: streaming, tools, compaction, retry, stop-hooks
          ↓
services/api/               model request assembly
```

**One `Tool` / `ToolUseContext` / `QueryEngine` abstraction powers three surfaces:** the REPL, headless/SDK flows, and an MCP server. This is the seam AEGIS-X needs for CLI + API + background workers over one runtime.

## C.2 NEW in this pass — the three gaps now closed

### C.2.1 `src/query/transitions.ts` (871 B) — the loop's exit algebra

**This was previously undeterminable and is the single most reusable artifact in any reference.**

```ts
Terminal  = { reason: 'completed' | 'blocking_limit' | 'image_error' | 'model_error'
                   | 'aborted_streaming' | 'aborted_tools' | 'prompt_too_long'
                   | 'stop_hook_prevented' | 'hook_stopped' | 'max_turns'
                   | (string & {}) ; error?: unknown }

Continue  = { reason: 'tool_use' | 'reactive_compact_retry'
                   | 'max_output_tokens_recovery' | 'max_output_tokens_escalate'
                   | 'collapse_drain_retry' | 'stop_hook_blocking'
                   | 'token_budget_continuation' | 'queued_command'
                   | (string & {}) }
```

Three design lessons: (a) **terminal reasons and continue reasons are strictly separated**; (b) the loop **self-heals** — `reactive_compact_retry` and `collapse_drain_retry` mean context overflow is compacted-and-retried, not failed; (c) `(string & {})` is the idiom for a typed open union that keeps autocompletion but allows forward-compatible values.

### C.2.2 `src/constants/querySource.ts` (491 B) — request provenance

`repl_main_thread | sdk | compact | side_question | agent | agent:custom | agent:explore | agent:plan | tool_use_summary | advisor | hook | session_memory | magic_docs | skill_search | classifier | bridge`

File comment: *"Used for analytics, retry logic, and cache control decisions."* One field driving observability aggregation, retry policy, **and** prompt-cache breakpoint decisions. Strictly better than a `source: 'user' | 'system'` enum.

### C.2.3 `src/types/message.ts` (11,106 B)

The message union the first pass flagged as MISSING: `AssistantMessage, UserMessage, AttachmentMessage, SystemMessage, ToolUseSummaryMessage, TombstoneMessage, RequestStartEvent, StreamEvent, ProgressMessage`.

### C.2.4 `src/shims/bun-bundle.ts` (1,970 B) — the complete compile-time flag inventory

23 `feature()` flags with env names, all defaulting false: `PROACTIVE, KAIROS, KAIROS_BRIEF, KAIROS_GITHUB_WEBHOOKS, BRIDGE_MODE, DAEMON, VOICE_MODE, AGENT_TRIGGERS, MONITOR_TOOL, COORDINATOR_MODE, DUMP_SYSTEM_PROMPT, BG_SESSIONS, HISTORY_SNIP, WORKFLOW_SCRIPTS, CCR_REMOTE_SETUP, EXPERIMENTAL_SKILL_SEARCH, ULTRAPLAN, TORCH, UDS_INBOX, FORK_SUBAGENT, BUDDY, MCP_SKILLS, REACTIVE_COMPACT, ABLATION_BUDGET`. Best single artifact for capability-gap analysis: it names every subsystem the product *could* have.

### C.2.5 `src/bridge/stub.ts` (1,782 B) — zero-cost degradation

When a feature gate is off: `isBridgeAvailable(): false` plus `noopBridgeHandle` (7 methods) and `noopBridgeLogger` (24 no-op methods). The contract for an optional subsystem that costs nothing when disabled.

### C.2.6 `src/server/web/` (28 files, repackager-authored) — session grace + ring scrollback

`pty-server.ts` (12,004 B) with `node-pty` + `ws`; `MAX_SESSIONS=10`, `MAX_SESSIONS_PER_USER=3`, `MAX_SESSIONS_PER_HOUR=10`, `SESSION_GRACE_MS=300000`, `SCROLLBACK_BYTES=102400`. `session-store.ts`: `StoredSession = { token, userId, pty, scrollback, ws, createdAt, lastActive, graceTimer }` — the server-side PTY **survives WebSocket disconnect for a grace period**, so a client can reconnect without losing the session. Plus a pluggable `AuthAdapter` strategy pattern.

**Reusable idea:** bounded ring scrollback + disconnect grace period is exactly what a long-running agent run needs when a client drops.

## C.3 Concepts to reimplement

| # | Concept | Location | AEGIS-X action |
|---|---|---|---|
| C1 | Generator-based loop with typed `transition` | `query.ts` + `query/transitions.ts` | **Adopt** — makes recovery paths assertable |
| C2 | **Fail-closed defaults at the tool factory** — `buildTool()` fills `TOOL_DEFAULTS`; `isReadOnly→false` (assume writes), `isConcurrencySafe→false` (assume unsafe to parallelise), `interruptBehavior()→'block'` | `src/Tool.ts` | **Adopt** — security predicates cannot be forgotten |
| C3 | `searchHint` + progressive disclosure (`defer_loading: true` + `ToolSearch`) | `src/constants/tools.ts`, `ToolSearchTool` | **Adopt** — required by the 512/8k context conflict |
| C4 | Streaming concurrent tool execution, gated on `isConcurrencySafe(input)` | `src/services/tools/StreamingToolExecutor.ts` | **Adopt** |
| C5 | **Three result size budgets** — `DEFAULT_MAX_RESULT_SIZE_CHARS=50_000`, `MAX_TOOL_RESULT_TOKENS=100_000`, `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS=200_000`; read-back tools **opt out** ("circular — never persist") | `src/constants/toolLimits.ts` | **Adopt** — the opt-out rule is a non-obvious correctness detail |
| C6 | Two-layer permissions — structural per-agent-class allow-sets ∩ dynamic rule/mode evaluation | `ALL_AGENT_DISALLOWED_TOOLS`, `ASYNC_AGENT_ALLOWED_TOOLS`, `COORDINATOR_MODE_ALLOWED_TOOLS` | **Adopt** |
| C7 | Every decision carries a typed reason — `PermissionDecisionReason` has **11** variants | `src/types/permissions.ts` | **Adopt** |
| C8 | Prefixed entropy-hardened task IDs as an **access token** for predictable output paths; byte-offset disk logs | `src/Task.ts` | **Adopt** |
| C9 | Agents as markdown frontmatter; `context: 'inline' \| 'fork'` (fork = separate context *and* budget); per-agent `maxTurns` | `src/tools/AgentTool/` | **Adopt** |
| C10 | Layered compaction at 4 granularities + a collapse layer; `reactiveCompact` attempted **once per turn**; cache-preserving microcompact | `src/services/compact/*` | **Adopt** |
| C11 | 28 `HOOK_EVENTS`; async-vs-blocking discriminated by the presence of an `async` key; `SessionStart` emits `watchPaths` → later `FileChanged` | `src/types/hooks.ts` | **Adopt** |
| C12 | Statusline as a **public versioned IPC contract** — a user-defined shell command receiving a rich JSON payload on stdin | `src/components/StatusLine.tsx:64-120` | **Adopt** — cheapest possible observability extension point |
| C13 | `Edit` = exact-string replacement (not diff hunks) with `replace_all`; `old_string===new_string` is a hard error; `Bash` has `run_in_background` | `src/tools/*/` | **Adopt** |
| C14 | Config: 89 keys, 5 ordered sources, one-function-per-migration | `src/utils/settings/types.ts` | **Adapt** |
| C15 | Type name as lint rule — `AnalyticsMetadata_I_VERIFIED_THIS_IS_NOT_CODE_OR_FILEPATHS` | `src/services/analytics/metadata.ts` | **Adopt** |
| C16 | 4-dependency DI seam instead of `spyOn` — `QueryDeps { callModel, microcompact, autocompact, uuid }` | `src/query/deps.ts` | **Adopt** — testability by construction |
| C17 | `partitionToolCalls()` → `{isConcurrencySafe, blocks}`; concurrency capped by `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` (default 10); queued `contextModifier` applied after a concurrent batch | `src/tools.ts` | **Adopt** — pairs with B26 |

## C.4 Do not copy

Bun build-time `feature()` flags (on/off state unknowable from source) · GrowthBook/Statsig remote flags · the 665 `tengu_*` proprietary event names · the 43-file Ink React UI fork · the crypto/on-chain subsystem · `restored-src/node_modules/` · `cli.js.map` `sourcesContent` (verbatim upstream source — deliberately never read).

## C.5 Calibration gaps in the reference

`src/types/generated/**` (14 dirs) are **empty placeholders**; the original test suite is **not in the snapshot**; ~20 tool modules and 2 task modules are referenced but absent (`WorkflowTool`, `WebBrowserTool`, `MonitorTool`, `VerifyPlanExecutionTool`, `TerminalCaptureTool`, …); 17 command modules are 75-byte stubs; the repackager's own docs are measurably wrong (`QueryEngine.ts` described as "~46K lines" — it is 46,632 **bytes**). Treat none of these as authoritative.

---

# D. CURRENT MIND-LLM ARCHITECTURE

**`mind-llm-v2`** — 256 files, 6.18 GB. 71 `.py`, 23 `.bin`, 15 `.pt`. **No dependency manifest of any kind.** Deps: `torch` (required), `numpy` (required), `tokenizers` + `psutil` (optional). **No `transformers`, `safetensors`, `datasets`, `vllm`, `fastapi`, `peft`, `trl`, `gguf`, `onnx`.** Manifests record `torch 2.14.0+cpu` — **not a public release.**

## D.1 Model and tokenizer

Hand-written LLaMA-family decoder-only in `model.py` (320 lines): `GPT:188`, `Block:168`, `CausalSelfAttention:43`, `RMSNorm:114`, `SwiGLU:145`, `build_rope_cache:20`, `apply_rope:32`. Tied embeddings, pre-norm, GQA, RoPE. No MoE.

| Property | Value |
|---|---|
| `exp100m_longctx` params | **99,996,000** (independently recomputed; matches manifest) |
| vocab / block_size / layers / heads / kv | 16,000 / **512** / 21 / 8 / 4 |
| n_embd / head_dim / FFN | 624 / 78 / 1,664 SwiGLU |
| dtype | **fp32 exclusively** (all 25 benchmark JSONs) |
| Checkpoint format | `torch.save` pickle — **not safetensors, not GGUF** |

**Tokenizer is the sharpest problem.** The active asset `data/shards_longctx16k/tokenizer.json` is a `greedy_byte_bpe` vocabulary with **`merges: []`** at 2.56 chars/token — it exists because the HF wheel was unavailable at build time. It is not Qwen-, Llama-, or GPT-2-compatible, and **GGUF/llama.cpp export is a live correctness hazard** because that tokenizer reader expects merges or SPM.

## D.2 Training and data pipeline — genuinely production-grade

`pretrain.py` + `sft.py`. Atomic saves (`mkstemp → fsync → os.replace`), **bit-exact resume** validating tokenizer hash + config fingerprint + optimizer metadata + RNG state, stateless cosine LR, token-budget planning (`budget_plan()`, `iter_microbatch_specs()`), bounded-memory streaming pipeline (`pipeline.py`), pre-tokenized uint16 shards with a `.spans.json` document-boundary sidecar, `np.memmap` document-contained sampling.

**This is more mature than anything in AEGISAI's pipeline and should be reused, not reimplemented.**

## D.3 Capability — measured, and it is zero

| Checkpoint | Params | Tokens seen | val_loss | Perplexity |
|---|---:|---:|---:|---:|
| `expB_ctx128_5m` | 2,807,200 | 5,000,000 | 3.817 | 45.4 |
| `exp100m_100m_tokens` | 100,012,080 | 100,000,000 | 4.869 | 130.2 |
| **`exp100m_longctx_1m_tokens`** (active) | **99,996,000** | **100,000** | **6.075** | **434** |

Capability probes: **0/10**, all empty strings. Documented failure mode: *"the model learned to emit its stop token immediately after the assistant turn marker."* **100,000 tokens on 100M params is 0.005% of Chinchilla-optimal.**

## D.4 Six blockers

| # | Blocker | Evidence |
|---|---|---|
| M1 | **Cannot emit tool calls** | `EventKind` declares `"tool_call"` (`agentic/types.py:8`) and `Orchestrator._run` handles it (`orchestrator.py:62-65`) — but `MindLLMBackend.generate()` never yields one. The loop is structurally unreachable |
| M2 | Zero capability | 0/10 probes |
| M3 | **512-token context, raises rather than truncates** | `model.py:233-238`; `benchmarks/longctx_b1_a4_t4_context1024.json` shows `rss_within_limit: false` |
| M4 | **No KV cache** | `model.py:288-290` recomputes the whole context every token → **O(T²)**. 50.3 tok/s measured |
| M5 | No streaming | `describe()` returns `{"streaming": False}`; one giant `text_delta` |
| M6 | No export path | Zero hits for `gguf`/`safetensors`/`onnx`/`quantiz` |

## D.5 THE KEY FINDING — `agentic/` already exists

**MIND-LLM v2 already contains a 1,300-line agent runtime**, not mentioned in the master spec: `orchestrator.py`, `tools/{base,registry,permissions,filesystem,terminal,retrieval,tasks,network}.py`, `context.py` (`ContextEngine` with priority + budget), `tasks.py` (`TaskManager` — DAG + cycle detection), `memory/` (SQLite store, 6 scopes, secret redaction), `planner.py`, `verify.py`, `observability.py` (`EventSink`), `limits.py`, `errors.py`, `api.py` (`AgenticRuntime`).

**The abstraction is already right:**
```python
class InferenceBackend(Protocol):
    async def describe(self) -> dict: ...
    def generate(self, request: InferenceRequest) -> AsyncIterator[InferenceEvent]: ...
```
A two-method protocol. Swapping models is a ~40-line class, and `FakeBackend` proves the seam.

## D.6 How MIND-LLM connects to AEGIS-X — tiered routing, not sole decision-maker

```
AEGIS-X ModelRouter
  ├─ Tier "reasoning" ─► AEGISAI OllamaProvider (/api/chat) — plans, verifies, judges
  ├─ Tier "fast" ──────► MIND-LLMServer (pure-Python stdlib HTTP) — classify, extract, summarise
  └─ Tier "local" ─────► MIND-LLM in-process via InferenceBackend — offline/dev, no network
```

MIND-LLM cannot plan or verify, so it must not be the sole decision-maker. But it stays genuinely in the loop as the fast tier, and every contribution becomes rollout data — the mechanism by which it improves.

**Do NOT pursue GGUF/Ollama export.** 1–3 days, real tokenization-mismatch risk, and it buys nothing the pure-Python path does not already give.

---

# E. COMPONENT CATALOGUE (44 subsystems)

Format per component: **STATUS** · **CURRENT FILES** · **REFERENCE SOURCE** · **WHAT TO TAKE FROM IT** · **AEGIS-X IMPLEMENTATION**

---

## 1. Main agent loop
**STATUS:** MISSING
**CURRENT FILES:** `agents/agent.py` (65 LOC, single `provider.generate` at `:45`) · `runtime/runtime.py:72-138` (8 linear steps) · `inference/ollama.py:77-98`
**REFERENCE SOURCE:** Claude `query.ts` (68,684 B) + `query/transitions.ts`; Automaton `src/agent/loop.ts:395` (1,028 lines, 12-step turn)
**WHAT TO TAKE FROM IT:** (a) the **12-step turn shape** — context build → pre-turn memory → route → tool exec → **atomic persist** → post-turn memory ingest → loop detection; (b) the **`Terminal` / `Continue` discriminated union** so a test can assert *which* recovery path fired without inspecting messages; (c) **self-healing** — `reactive_compact_retry` / `collapse_drain_retry` mean overflow is compacted-and-retried, not failed; (d) the **single-transaction turn commit** (`loop.ts:690-700`).
**AEGIS-X IMPLEMENTATION:** NEW `agents/loop.py` + `core/transitions.py`. Loop: ORIENT → SELECT STEP → BUILD PROMPT → GENERATE → PARSE TOOL CALLS → EXECUTE → OBSERVE → VERIFY → COMMIT → decide. Terminate on `Terminal.reason ∈ {completed, max_steps, budget_exhausted, blocked, unrecoverable, user_cancelled}`; continue on `Continue.reason ∈ {tool_use, replan, recovery_retry, context_compact, task_decomposed, subtask_delegated}`. Persist the turn, its steps and its tool calls in **one** `db.runTransaction()`.

## 2. Agent orchestrator
**STATUS:** PLACEHOLDER
**CURRENT FILES:** `agents/agent.py:33-58` — a pass-through that calls the provider once and attaches the conversation
**REFERENCE SOURCE:** Automaton `src/orchestration/orchestrator.ts` — 8-phase FSM `idle → classifying → planning → plan_review → executing → replanning → complete | failed`, `maxReplans = 3`
**WHAT TO TAKE FROM IT:** the **phase machine persisted to storage** so a restart resumes mid-phase rather than re-planning; and **complexity gating** (`classifyComplexity()` → `requiresPlanMode = estimatedSteps > 3`) so simple goals skip the planner entirely.
**AEGIS-X IMPLEMENTATION:** `agents/loop.py` gains an `ExecutionPhase` enum + `Orchestrator` facade. `classifying` is a cheap-tier call that decides whether to plan. Persist the phase in the run record.

## 3. Planner
**STATUS:** MISSING
**CURRENT FILES:** none. `TaskCategory.PLANNING` exists at `data_pipeline/classify.py:28` and the corpus now has 600 PLANNING records, but no runtime planner
**REFERENCE SOURCE:** Automaton `src/orchestration/planner.ts` (767 lines) — `planGoal()` / `replanAfterFailure()`, always `tier: "reasoning"`, `responseFormat: {type:"json_object"}`; `validatePlannerOutput()` re-implements a schema validator with path-tagged errors (`tasks[3].dependencies must be a non-negative integer`) and **rejects cycles**; `PlannerOutput { analysis, strategy, customRoles, tasks, risks, estimatedTotalCostCents, estimatedTimeMinutes }`
**WHAT TO TAKE FROM IT:** **never trust LLM output** — a hand-written validator with per-field path-tagged errors; acyclicity enforcement; complexity gating.
**AEGIS-X IMPLEMENTATION:** NEW `agents/planner.py` + `agents/planner_schema.json`. `Planner` protocol with `LLMPlanner` (JSON-object response) and `StaticPlanner` (deterministic, for tests). Validate: every step's `tool_allowlist ⊆ ToolRegistry`; step count ≤ `max_steps`; DAG acyclic (DFS).

## 4. Replanner
**STATUS:** MISSING
**CURRENT FILES:** none
**REFERENCE SOURCE:** Automaton `src/orchestration/plan-mode.ts:275-302` — `ReplanTrigger` = `task_failure | budget_breach (actual > 1.5× estimated) | requirement_change (conflictScore ≥ 0.55) | environment_change | opportunity`; plus `MAX_FIX_CYCLES = 3`
**WHAT TO TAKE FROM IT:** the **typed trigger union** — replanning is triggered by *named conditions*, not ad-hoc; and the **budget-breach ratio** as a quantitative trigger.
**AEGIS-X IMPLEMENTATION:** NEW `agents/replanner.py`. Triggers: `step_failed | verifier_retry_exhausted | budget_pressure (<20% remaining) | observation_contradicts_assumption | step_blocked (missing tool/permission)`. Mutate the plan: revise the current step, insert steps, re-topological-sort the DAG, mark steps SKIPPED, increment `replan_count`, cap at 3.

## 5. Model/provider abstraction
**STATUS:** PARTIAL
**CURRENT FILES:** `inference/base.py` (3-method ABC) · `inference/ollama.py` (only implementation)
**REFERENCE SOURCE:** Automaton **two** stacks — (A) `src/inference/router.ts` `SurvivalTier × InferenceTaskType` matrix; (B) `src/inference/inference-client.ts:1221` `UnifiedInferenceClient` with `ModelTier: reasoning | fast | cheap`, 3 transports, failover, circuit breaker, streaming for all three
**WHAT TO TAKE FROM IT:** the **normalised result shape** — every transport returns `{content, toolCalls, finishReason, usage, cost, metadata{providerId, modelId, tier, latencyMs, retries, failedProviders}}`; and **capability gating before dispatch** (skip providers lacking a requested capability).
**AEGIS-X IMPLEMENTATION:** MODIFY `inference/base.py` — add `stream()`, `embed()`, `count_tokens()`, accept `ModelOptions`. NEW `inference/options.py`, `inference/registry.py`.

## 6. Model router
**STATUS:** MISSING
**CURRENT FILES:** `runtime/runtime.py:37-40` — hard-rejects anything but `"ollama"` with `ConfigurationError`
**REFERENCE SOURCE:** Automaton `InferenceRouter.route()` 9 steps + `resolveCandidates(tier, survivalMode)`; `applySurvivalTier` downgrades reasoning→fast→cheap; `assertEmergencyPolicy()` blocks all but planner below a credit floor
**WHAT TO TAKE FROM IT:** **routing by task type, not by name** — a planner call and an observation-summarise call should not hit the same model; and **failover advances to the next provider** rather than failing.
**AEGIS-X IMPLEMENTATION:** NEW `inference/router.py`. `route(request, task_type)` → tier → candidate list → budget check → dispatch → record cost. `task_type ∈ {plan, verify, execute, summarise, classify, observe_distil}`. This is also where MIND-LLM attaches as the `fast` tier.

## 7. MIND-LLM adapter
**STATUS:** PARTIAL
**CURRENT FILES:** `agentic/mindllm_backend.py` (`MindLLMBackend`, implements the 2-method Protocol) — but `agentic/` is inside the MIND-LLM repo, not AEGISAI
**REFERENCE SOURCE:** MIND-LLM itself; Automaton `UnifiedInferenceClient` for the target shape
**WHAT TO TAKE FROM IT:** nothing new to copy — the Protocol is already correct. What it lacks is: tool-call emission (M1), true streaming (M5), KV cache (M4), and any concurrency guard.
**AEGIS-X IMPLEMENTATION:** NEW `models/mindllm_server.py` (pure-Python stdlib HTTP: `/v1/chat/completions` SSE, `/v1/embeddings`, `/health`), `models/kv_cache.py`, `models/tool_parser.py`, `models/load.py`. NEW `inference/mindllm.py` adapts it to the AEGIS-X `InferenceProvider` ABC. **P0 order: KV cache → tool-call emission → retrain.** Do not build the Ollama/GGUF path.

## 8. Context manager
**STATUS:** MISSING
**CURRENT FILES:** `inference/ollama.py:94-98` resends the entire transcript; grep finds 0 token counting on the runtime side
**REFERENCE SOURCE:** Claude `src/utils/context.ts` + `AUTOCOMPACT_BUFFER_TOKENS = 13_000` + `calculateTokenWarningState()` → `{isAboveWarningThreshold, isAboveErrorThreshold, isAboveAutoCompactThreshold}`; Automaton `ContextBudget { total, reserveTokens: 4096, systemPrompt, todo, memory, event, turn, compressionHeadroom: 0.1 }` + `getUtilization() → {utilizationPercent, recommendation: "ok"|"compress"|"emergency"}`
**WHAT TO TAKE FROM IT:** **an explicit reserve** for the response; a **three-state warning** rather than one threshold; and a **token counter with an LRU cache** that only exact-tokenises under a character cap (`MAX_EXACT_TOKENIZATION_CHARS = 1024`) to avoid pathological CPU.
**AEGIS-X IMPLEMENTATION:** NEW `context/manager.py` + `context/window.py`. `assemble()` returns `{messages, tokens_used, budget, utilization, recommendation}`. Composition order: system prompt → goal → plan → tool results (most recent) → memory block → history tail.

## 9. Context compaction
**STATUS:** MISSING
**CURRENT FILES:** none
**REFERENCE SOURCE:** Claude 4 mechanisms + collapse layer: `autoCompact` (reserves `MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`), `reactiveCompact` (**once per turn** via `hasAttemptedReactiveCompact`), `microCompact` (with a **cache-preserving** variant — changing mid-history invalidates the provider's prompt cache), `snip`/`HISTORY_SNIP`, `contextCollapse` (**overflow is recoverable**). Automaton `compression-engine.ts` 5-stage cascade at 70/80/85/90/95% → `compact_tool_results → compress_turns → summarize_batch → checkpoint_and_reset → emergency_truncate`
**WHAT TO TAKE FROM IT:** the **cascade** (progressive, not all-or-nothing); the **once-per-turn reactive attempt**; and **cache-preserving trimming at the tail** where possible.
**AEGIS-X IMPLEMENTATION:** NEW `context/compaction.py`. `CompressionAction` as a discriminated union. **Wire it** — Automaton built this and never called it; that is the single most transferable mistake to avoid.

## 10. Tool registry
**STATUS:** PARTIAL
**CURRENT FILES:** `tools/registry.py` (65 lines — `register/find/list_tools/validate_requested_tools/execute`) · `tools/base.py` (Tool ABC + `_validate_value` schema validator)
**REFERENCE SOURCE:** Claude `buildTool()` + `TOOL_DEFAULTS` in `src/Tool.ts`; `getAllBaseTools()`, `filterToolsByDenyRules()`, `assembleToolPool(permissionContext, mcpTools)` in `src/tools.ts`
**WHAT TO TAKE FROM IT:** **fail-closed defaults at the factory boundary** — `isReadOnly→false`, `isConcurrencySafe→false`, `isDestructive→false`, `interruptBehavior()→'block'`; and the **tool-pool cache-stability rule** — built-ins are sorted as a contiguous prefix so the server-side cache breakpoint stays valid.
**AEGIS-X IMPLEMENTATION:** MODIFY `tools/base.py` — add `build_tool()` factory and the fail-closed predicates. MODIFY `tools/registry.py` — add `to_model_schema()`. ⚠ `list_tools()` and `validate_requested_tools()` exist today and are called **only by tests** — they are the intended-but-unwired model bridge.

## 11. Tool selector
**STATUS:** MISSING
**CURRENT FILES:** none. `Agent.execute_tool` (`agents/agent.py:60`) has exactly one caller: `tests/test_agent.py:43`
**REFERENCE SOURCE:** Claude `ToolSearch` + `searchHint` (scored **higher than the description**) + `defer_loading: true` + `alwaysLoad`; Automaton `MAX_TOOL_CALLS_PER_TURN = 10`
**WHAT TO TAKE FROM IT:** **progressive disclosure** — with 14 tool families the full schema set will not fit a small context window, so send names only and let the model request full schemas. Also: the runtime decides only **eligibility**; the model decides **selection**.
**AEGIS-X IMPLEMENTATION:** NEW `tools/select.py`. `to_model_schema(tools, mode="compact"|"full")`, `search_hint` field, `ToolSearchTool`. **This is not optional — it is what makes the 512/8k context conflict survivable.**

## 12. Tool executor
**STATUS:** PARTIAL
**CURRENT FILES:** `tools/registry.py:44-65` — `execute()` validates, permission-checks, runs, validates output. No timeout, no truncation, no parallelism, no audit
**REFERENCE SOURCE:** **Automaton `src/pro/kernel/fastloop.ts::executeBatch()`** — read-only tools (`!mutating`) run in a bounded parallel pool; `mutating` tools run **strictly serially afterwards, in order**; per-tool try/catch so one rejection never rejects the batch. Plus a per-tool `CircuitBreaker` (3 strikes, 30 s half-open). Claude: `partitionToolCalls()`, `MAX_TOOL_USE_CONCURRENCY` (10), queued `contextModifier` applied after a concurrent batch
**WHAT TO TAKE FROM IT:** the **read-parallel / write-serial split** — Automaton's main loop is strictly serial, and this file is the fix. Plus per-tool circuit breaking.
**AEGIS-X IMPLEMENTATION:** NEW `tools/executor.py`. `execute_batch(calls)` → partition on `is_concurrency_safe` → `anyio` task group for the safe half, ordered serial for the rest. Per-tool `CircuitBreaker`. Per-tool timeout. Output truncation to the 3-layer budget. Append an audit record per invocation.

## 13. Terminal
**STATUS:** MISSING
**CURRENT FILES:** no tool exists; zero `subprocess` imports in 59 files
**REFERENCE SOURCE:** Claude `Bash` params `command, description, timeout, run_in_background, dangerouslyDisableSandbox` + AST complexity check (`tengu_bash_ast_too_complex`); mirrored `bashSecurity.ts`/`powershellSecurity.ts` and `bashPermissions.ts`/`powershellPermissions.ts`; Automaton's 6-tier truncation table (16,000 / 4,000 / 1 MB buffer / 60 s)
**WHAT TO TAKE FROM IT:** **`run_in_background` + byte-offset output** (feeds §30 and §31); the **AST complexity check**; and the **truncation ladder** with an explicit `[TRUNCATED: N characters omitted]` marker. Automaton explicitly has **no PTY and no streaming stdout** — AEGIS-X should add both.
**AEGIS-X IMPLEMENTATION:** NEW `tools/terminal.py`. **Requires the R1 sandbox decision.** Argument-array invocation, never shell-interpolated.

## 14. Filesystem
**STATUS:** MISSING
**CURRENT FILES:** `FILESYSTEM_READ`/`FILESYSTEM_WRITE` declared at `tools/permissions.py:9-10` and default-denied
**REFERENCE SOURCE:** Claude `Edit` = exact-string replacement with `replace_all`; `old_string===''` + missing file ⇒ create; `old_string===new_string` ⇒ hard error; `read_file` **paginated** with "Use offset=N to continue"; Automaton `confineToWorkspace(filePath, allowedEditRoot)` + `isSensitiveFile()` (denies `wallet.json`, `config.json`, `.env`, `*.key`, `*.pem`) + `isProtectedFile()` (24 frozen paths) + symlink resolution *before* validation
**WHAT TO TAKE FROM IT:** exact-string replacement is **safer and cheaper than unified diffs**, and its failure mode is a clean signal the Verifier can catch. Paginated reads. **Resolve symlinks before validating containment.**
**AEGIS-X IMPLEMENTATION:** NEW `tools/filesystem.py` — `Read` (paginated), `Write`, `Edit` (exact-string), `Glob`, `Grep`. Jailed under `sandbox_root`. Read-back tools must set `max_result_size_chars = None` (never persist a file the model will read back — circular).

## 15. Python
**STATUS:** MISSING
**CURRENT FILES:** only `tools/calculator.py` (AST-whitelist arithmetic, `READ_ONLY`)
**REFERENCE SOURCE:** Automaton `src/survival/*` for resource checks; Claude's sandbox flag pattern
**WHAT TO TAKE FROM IT:** Automaton has **no process-level sandbox** — that is a gap to fill, not a pattern to copy.
**AEGIS-X IMPLEMENTATION:** NEW `tools/python_exec.py`. Jailed cwd, rlimits (POSIX) / job objects (Windows), hard timeout, no network, capped stdout. **Requires R1.** Replace the `PROCESS_EXECUTION` hard block with a distinct `SANDBOXED_EXECUTION` capability that is grantable but sandbox-gated.

## 16. Git
**STATUS:** MISSING
**CURRENT FILES:** no git symbol in AEGISAI
**REFERENCE SOURCE:** Automaton `src/git/state-versioning.ts` (`git init` + `.gitignore` excluding wallet/keys/db, typed `commitStateChange(category)`) and `src/git/tools.ts` (`gitStatus` parses `--porcelain -b` into `{branch, staged[], modified[], untracked[], clean}`, `escapeShellArg()` on every path); Automaton `src/self-mod/upstream.ts` cherry-picks **individual** commits after review
**WHAT TO TAKE FROM IT:** **git-versioned state is a checkpoint mechanism with zero new dependencies** — the cheapest durability win available. And cherry-pick-one-commit rather than pull-all.
**AEGIS-X IMPLEMENTATION:** NEW `tools/git.py` — `status, diff, log, commit, branch, clone, worktree`. No shell interpolation. Also NEW `runtime/checkpoint.py` using git for run snapshots.

## 17. Web/search
**STATUS:** MISSING
**CURRENT FILES:** `NETWORK` declared at `tools/permissions.py:11`, no tool uses it
**REFERENCE SOURCE:** Claude `WebSearch` (server-side, structured `WebSearchProgress`) and `WebFetch(url, prompt)` with a client-side preflight and a `web_fetch_apply` post-process; Automaton `assertSecureUrl()` (HTTPS only unless loopback) and `isAllowedProviderUrl`
**WHAT TO TAKE FROM IT:** HTTPS enforcement at the HTTP layer; a **preflight** before fetching (size/type/host) and a post-process after.
**AEGIS-X IMPLEMENTATION:** NEW `tools/web.py` — `search, fetch, extract` with host allowlist, robots respect, size/time caps, and **provenance recording** (the spec requires `SEARCH → SELECT → FETCH → EXTRACT → CLEAN → COMPARE → PROVENANCE`).

## 18. Browser automation
**STATUS:** MISSING
**CURRENT FILES:** none. No Playwright/Selenium
**REFERENCE SOURCE:** Claude has `WebBrowserTool` in its flag inventory (`WEB_BROWSER_TOOL`) but the module is **absent from the snapshot** — unrecoverable. Automaton has none
**WHAT TO TAKE FROM IT:** nothing. The reference does not contain it.
**AEGIS-X IMPLEMENTATION:** NEW `tools/browser.py` (Playwright), with a **DOM→text serializer** whose output format must match training exactly. ⚠ **Do not let this block the runtime** — build the tool interface first, implement families in order: filesystem → terminal → git → python → web → api → browser.

## 19. API tools
**STATUS:** MISSING
**CURRENT FILES:** none
**REFERENCE SOURCE:** Claude MCP client (`src/services/mcp/`) with transports `stdio | sse | http | sdk | claudeai-proxy`; Automaton `install_mcp_server` is a **stub** (returns a literal string)
**WHAT TO TAKE FROM IT:** the transport enum. ⚠ Do not copy Automaton's stub — that is a known dead end.
**AEGIS-X IMPLEMENTATION:** NEW `tools/api_client.py` — generic HTTP/API tool. MCP support is a **later** extension point, not a prerequisite.

## 20. Code-agent
**STATUS:** MISSING
**CURRENT FILES:** no agent class, no harness, no code-specific tools
**REFERENCE SOURCE:** Automaton `CodingHarness` (`exec, write_file, read_file, patch_file, list_dir, task_done`) + `HarnessRegistry`; Claude's `CODE_AGENT_DISALLOWED_TOOLS` allow-set pattern
**WHAT TO TAKE FROM IT:** the **narrow harness tool set** — a code agent does not need web or memory tools, and the allow-set makes that structural rather than advisory.
**AEGIS-X IMPLEMENTATION:** NEW `agents/registry.py` + `agents/coding.md` (markdown frontmatter with `tools` / `disallowedTools` / `maxTurns` / `model`).

## 21. Observation handling
**STATUS:** MISSING
**CURRENT FILES:** `ToolResult` exists at `tools/base.py:12-27` but nothing normalizes, truncates, redacts or scores it
**REFERENCE SOURCE:** Claude 3-layer result budgets (C5); Automaton `sanitizeToolResult()` for `EXTERNAL_SOURCE_TOOLS = {exec, web_fetch, check_social_inbox}` + `INJECTION BLOCKED` labels; `MUTATING_TOOLS` vs `IDLE_ONLY_TOOLS` classification
**WHAT TO TAKE FROM IT:** **mark every externally-sourced observation UNTRUSTED** and run it through sanitisation before it enters context. The trust label is a prompt-level fence, not just a code path.
**AEGIS-X IMPLEMENTATION:** NEW `observation/{manager,budget,trust}.py`. Truncate → redact → mark untrusted → attach. AEGISAI's own `normalize.py:336` `permission_check: "not_present_in_source"` is the field only AEGIS-X can populate.

## 22. Verification
**STATUS:** MISSING
**CURRENT FILES:** nothing validates whether a result is correct. `TaskCategory` has **no** `VERIFICATION` value
**REFERENCE SOURCE:** Automaton `OrchestratorHarness::refresh_plan` with `createPlannerFailureFromVerification()`, `MAX_FIX_CYCLES = 3`; Claude `VerifyPlanExecutionTool` + `VERIFICATION_AGENT` (flag `tengu_hive_evidence`)
**WHAT TO TAKE FROM IT:** verification is a **first-class loop stage with its own agent**, not an afterthought — and a failed verification is an input to the Replanner, not just a retry.
**AEGIS-X IMPLEMENTATION:** NEW `agents/verifier.py` with `SchemaVerifier, AssertionVerifier, TestRunnerVerifier, FileDiffVerifier, LLMJudgeVerifier` → `Verdict: PASS | RETRY | REPLAN | ESCALATE`. ADD a `VERIFICATION` category to `data_pipeline/classify.py:24-41` so the classifier can learn to label it.

## 23. Error analysis
**STATUS:** PARTIAL
**CURRENT FILES:** `core/errors.py` — 15 classes, clean hierarchy, but **string messages only**
**REFERENCE SOURCE:** Claude `classifyAPIError(error): string`, `categorizeRetryableAPIError()`, `RETRYABLE_STATUS_CODES = {429,500,502,503,504}`, `parseMaxTokensContextOverflowError()` (parses `(inputTokens, maxTokens, contextLimit)`, subtracts a 1,000-token safety buffer, recomputes `adjustedMaxTokens`); Automaton `PolicyAction` + `ReplanTrigger` as typed classifications
**WHAT TO TAKE FROM IT:** **structured** error classification with a typed discriminant, and parsing the provider's overflow error to **adapt** rather than just fail.
**AEGIS-X IMPLEMENTATION:** MODIFY `core/errors.py` — add `StepLimitExceeded, BudgetExceeded, VerificationFailed, PlanInvalid, SandboxViolation, ToolTimeout, RateLimited, ResumeError, ContextOverflow`, each carrying a machine-readable `code` and structured `context`. NEW `recovery/classifier.py`.

## 24. Recovery
**STATUS:** PARTIAL
**CURRENT FILES:** no retry, no backoff, no circuit breaker. `Runtime.run` has **no `try/except` at all**
**REFERENCE SOURCE:** **Automaton `pro/selfimprove/reflection.ts`** — `journalFailure(kind, detail, tool)` (4,000-char truncation) → **deterministic regex signature table** (timeout/`ETIMEDOUT`/`EAI_AGAIN` → `tool_fix`; rate-limit/`429` → `prompt_patch`; `402` → `policy_tune`; `permission denied`/`EACCES` → `prompt_patch`; parse/`JSON`/syntax → `prompt_patch`), first match wins → dedupe on the `lesson` string, bump `times_seen` → `applyProposal()` **returns a rejection reason or `null`**. Plus Claude `withRetry()` (`DEFAULT_MAX_RETRIES=10`, `BASE_DELAY_MS=500`, `PERSISTENT_MAX_BACKOFF_MS=5min`, `PERSISTENT_RESET_CAP_MS=6h`, `HEARTBEAT_INTERVAL_MS=30s`) and Automaton's per-tool `CircuitBreaker`
**WHAT TO TAKE FROM IT:** (a) failure **journal → signature → lesson** is a self-improvement loop that needs no LLM; (b) `applyProposal` returning a **reason** makes guardrails auditable; (c) prompt patches must be **append-only**, never rewriting a live prompt; (d) retry needs a **heartbeat yield** so a long backoff is observable.
**AEGIS-X IMPLEMENTATION:** NEW `recovery/manager.py` (retry/backoff/compensate/dead-letter) and `memory/procedural.py` lessons store. ⚠ Automaton's `src/pro/**` is **unwired and has ~12 type errors** — reimplement the *pattern*, not the file.

## 25. Working memory
**STATUS:** PLACEHOLDER (`memory/__init__.py` is one line)
**CURRENT FILES:** `memory/__init__.py` (1 LOC)
**REFERENCE SOURCE:** Automaton `WorkingMemoryManager` / `working_memory` table — `WorkingMemoryEntry { contentType: goal|observation|plan|reflection|task|decision|note|summary, priority, tokenCount, expiresAt, sourceTurn }`
**WHAT TO TAKE FROM IT:** typed content classes, explicit **priority**, an **expiry**, and a **source turn** for provenance.
**AEGIS-X IMPLEMENTATION:** NEW `memory/working.py`. ⚠ MIND-LLM's `agentic/memory/` already implements a 6-scope SQLite store with secret redaction — port the abstraction, not the code.

## 26. Episodic memory
**STATUS:** PLACEHOLDER
**CURRENT FILES:** none
**REFERENCE SOURCE:** Automaton `EpisodicMemoryManager` / `episodic_memory` — `EpisodicMemoryEntry { eventType, summary, detail, outcome, importance, accessedCount, classification }`
**WHAT TO TAKE FROM IT:** store the **outcome** and an **importance** score, and count accesses — that is what makes retrieval ranking possible later.
**AEGIS-X IMPLEMENTATION:** NEW `memory/episodic.py` — one episode per run, with per-step and per-tool-call detail. Backs §43 evaluation and §"Experience learning".

## 27. Semantic memory
**STATUS:** PLACEHOLDER
**CURRENT FILES:** none. No embeddings client anywhere
**REFERENCE SOURCE:** Automaton `SemanticMemoryManager` / `semantic_memory` — `SemanticMemoryEntry { category: self|environment|financial|agent|domain|procedural_ref|creator, key, value, confidence }` with `UNIQUE(category, key)` and a `confidence` field
**WHAT TO TAKE FROM IT:** `UNIQUE(category, key)` prevents duplicate facts, and **`confidence`** distinguishes asserted from verified knowledge.
**AEGIS-X IMPLEMENTATION:** NEW `memory/semantic.py` + `inference/embeddings.py` (`/api/embed`). ⚠ Requires the `reasoning` tier to be reachable, or embeddings come from MIND-LLM's `exp100m_longctx` — which at 2.56 chars/token will be poor.

## 28. Procedural memory
**STATUS:** PLACEHOLDER
**CURRENT FILES:** none
**REFERENCE SOURCE:** Automaton `ProceduralMemoryManager` / `procedural_memory` — `ProceduralMemoryEntry { name UNIQUE, steps: ProceduralStep[] { order, description, tool, argsTemplate, expectedOutcome, onFailure }, successCount, failureCount }`
**WHAT TO TAKE FROM IT:** a procedure is a **named, ordered list of steps with an expected outcome and an on-failure branch per step** — i.e. a reusable, executable plan. Plus success/failure counters so procedures can be retired.
**AEGIS-X IMPLEMENTATION:** NEW `memory/procedural.py`. This is where successful AEGIS-X runs become reusable skills, and it is the bridge to Claude's skill concept (frontmatter + instructions).

## 29. Memory consolidation
**STATUS:** MISSING (spec §"Memory consolidation" requires it explicitly)
**CURRENT FILES:** none
**REFERENCE SOURCE:** Claude `autoDream` / `DreamTask` + `consolidationLock.ts`; `extractMemories`; `teamMemorySync` with `checkTeamMemSecrets()`; Automaton `memory/ingestion.ts` `MemoryIngestionPipeline.ingest(sessionId, turn, toolCallResults)` with `detectContradictions()` and `scoreConfidence()`
**WHAT TO TAKE FROM IT:** consolidation must be **lock-protected** and run **as a background task**; and it must **scan for secrets on write**.
**AEGIS-X IMPLEMENTATION:** NEW `memory/consolidation.py` implementing the spec's four phases: **ORIENT** (identify important recent experiences) → **GATHER** (retrieve relevant episodes and tool traces) → **CONSOLIDATE** (create durable knowledge and procedures) → **PRUNE** (compress or remove stale memory). Plus `memory/budget.py` with per-tier caps and **rollforward**.

## 30. Persistent tasks
**STATUS:** MISSING
**CURRENT FILES:** no task/todo model, no DAG, no queue
**REFERENCE SOURCE:** Claude `TaskType` (7) / `TaskStatus` (5) / prefixed entropy-hardened IDs as an access token / byte-offset disk logs; Automaton `task-graph.ts` `TaskNode` with `dependencies[]`, `maxRetries`, `timeoutMs`, `assignedTo`, `priority`
**WHAT TO TAKE FROM IT:** the spec lists **13 fields** to persist (task ID, goal, plan, current step, state, tool history, observations, memory references, checkpoints, errors, retries, verification results). Both references cover most of them.
**AEGIS-X IMPLEMENTATION:** NEW `core/tasks.py` + `tasks/{manager,records,log,store}.py`. ⚠ MIND-LLM's `agentic/tasks.py` already has DAG + cycle detection — port the abstraction.

## 31. Background workers
**STATUS:** MISSING
**CURRENT FILES:** zero `threading`/`asyncio`/`subprocess` in 59 files
**REFERENCE SOURCE:** Automaton `LocalWorkerPool.spawn()` with scoped identity, `allowedEditRoot`, `IterationBudget`, and `shutdown()` (**never called on SIGTERM** — a documented bug); `PriorityWakeQueue<T>`; Claude `StoredSession` grace period + ring scrollback
**WHAT TO TAKE FROM IT:** per-worker **identity, filesystem root and budget**; **drain workers on shutdown** (Automaton forgot); and the **disconnect grace period + bounded ring scrollback** so a dropped client does not lose a run.
**AEGIS-X IMPLEMENTATION:** NEW `workers/{pool,identity}.py` + `runtime/daemon.py`. `anyio` task groups. **Signal handler must drain.**

## 32. Scheduler
**STATUS:** MISSING
**CURRENT FILES:** none
**REFERENCE SOURCE:** Automaton `DurableScheduler` — `tickInProgress` re-entrancy guard, `LEASE_TTL_MS = 60_000`, `acquire/release/clearExpiredLeases`, `cron-parser`, per-task `maxRetries`, `nextRunAt` re-arm; `daemon.ts:116-126` uses **recursive `setTimeout`, never `setInterval`**; Claude "Kairos" cron with **deterministic jitter** (recurring fires up to 10% of period late, max 15 min; one-shots on `:00`/`:30` fire up to 90 s early)
**WHAT TO TAKE FROM IT:** the **lease** is what makes it safe to run two scheduler processes; and the **jitter** prevents thundering herd.
**AEGIS-X IMPLEMENTATION:** NEW `scheduler/{daemon,jobs,cron}.py`.

## 33. Multi-agent/delegation
**STATUS:** MISSING
**CURRENT FILES:** no registry, no handoff, no shared state, no concurrency
**REFERENCE SOURCE:** Automaton `LocalWorkerPool` + `createWorkerIdentity()` + `matchTaskToAgent()` (role match → best → spawn → steal → **self-assign fallback**); Claude `AgentDefinition` markdown frontmatter with `context: 'inline' | 'fork'` and **per-agent-class tool allow-sets** (`ALL_AGENT_DISALLOWED_TOOLS` blocks `TaskOutput`/`Agent` "to prevent recursion")
**WHAT TO TAKE FROM IT:** the **structural allow-set** (a verifier agent *cannot* write files — enforced, not advisory) and the **recursion guard** (a sub-agent cannot spawn another sub-agent by default).
**AEGIS-X IMPLEMENTATION:** NEW `agents/{registry,delegation}.py` + `multi_agent/{topology,messaging,blackboard}.py` + `agents/*.md`. Topology per spec §"Multi-agent target": MASTER / RESEARCH / CODING / TEST / BROWSER / REVIEW-VERIFIER.

## 34. Permissions
**STATUS:** PARTIAL
**CURRENT FILES:** `tools/permissions.py` — 5-value `Capability` enum + `PermissionPolicy`; `PermissionDeniedError(f"capability denied: {capability.value}")` at `:49` is a bare string
**REFERENCE SOURCE:** Claude two-layer model — structural per-agent-class allow-sets ∩ dynamic rule evaluation; `PermissionDecisionReason` with **11** variants; `PermissionUpdate` opcodes `addRules|replaceRules|removeRules|setMode|addDirectories|removeDirectories`; modes `acceptEdits|bypassPermissions|default|dontAsk|plan`; 5 ordered `PermissionRuleSource`s. Automaton `PolicyEngine` first-deny-wins with `priority` ordering (100/200/300/400/600) and every decision written to `policy_decisions` with `toolArgsHash: sha256(args)`
**WHAT TO TAKE FROM IT:** the **typed decision reason** and the **auditable args hash**. AEGIS-X is autonomous; at hour 3 the operator must know which rule fired.
**AEGIS-X IMPLEMENTATION:** NEW `security/{policy,approvals,rules/}.py`. `PermissionDecision { decision, reason_code, reason_detail, rule_ids, risk_level, args_hash, evaluated_at }`. `security/approvals.py` with a TTL gate and the approval **bound to the args hash** (Automaton `aegis-ai/src/approvals.ts`).

## 35. Sandbox/security
**STATUS:** MISSING (`security/__init__.py` is one line)
**CURRENT FILES:** `PROCESS_EXECUTION` permanently denied at `tools/permissions.py:31-32`, pinned by `tests/test_tools.py:104-111`
**REFERENCE SOURCE:** Automaton has **no process-level sandbox** — explicitly a gap. Its boundaries are: `confinePathToSandbox()` (hard `/root`), `confineToWorkspace()`, `isSensitiveFile()`, `isProtectedFile()` (24 paths), `escapeShellArg()`, `SHELL_METACHAR_RE`, `assertSecureUrl()`, `isAllowedProviderUrl()` (rejects embedded credentials)
**WHAT TO TAKE FROM IT:** the boundary *concepts*. ⚠ Do **not** copy the flat `PROCESS_EXECUTION` prohibition — AEGIS-X needs code execution.
**AEGIS-X IMPLEMENTATION:** NEW `security/{sandbox,secrets,audit}.py` + `tools/sandbox.py`. **R1 DECISION REQUIRED:** replace the hard block with a grantable `SANDBOXED_EXECUTION` capability routed through rlimits/job-objects, jailed cwd, no network, hard timeout. **This reverses a currently-tested safety guarantee and needs explicit sign-off.**

## 36. Event bus
**STATUS:** PLACEHOLDER
**CURRENT FILES:** `core/models.py:210-239` `Event` dataclass — complete, tested, **constructed only by `tests/test_models.py:49`**
**REFERENCE SOURCE:** Claude 28 `HOOK_EVENTS`; async-vs-blocking discriminated by the presence of an `async` key; in-process bus `src/utils/hooks/hookEvents.ts` (`registerHookEventHandler`, `emitHookStarted`, `emitHookProgress`, `emitHookResponse`); `InternalEventWriter`/`Reader` CQRS-style split between transcript and machine log. Automaton `event_stream` table (17-value `EventType`, `compact()`)
**WHAT TO TAKE FROM IT:** the **CQRS split** — the user-visible transcript and the machine event log are different stores. And a hook can **veto**, augment input, or inject context.
**AEGIS-X IMPLEMENTATION:** NEW `events/{bus,hooks,types,sinks}.py` built **on top of the existing dead `Event` model** — the substrate is already ~80% built.

## 37. Checkpoints
**STATUS:** MISSING (runtime) / COMPLETE (data)
**CURRENT FILES:** runtime — nothing. Data — `data_pipeline/common.py:67-96` `Checkpoint` + 6 stages, tested
**REFERENCE SOURCE:** Automaton `checkpoint_and_reset` in `compression-engine.ts` → `ConversationCheckpoint { id, summary, activeGoalIds, activeTaskIds, keyDecisions, turnCount, tokensSaved, filePath }`; `src/git/state-versioning.ts` `commitStateChange(category)`
**WHAT TO TAKE FROM IT:** a checkpoint carries **active goal IDs and key decisions**, not just a summary — otherwise resume loses the point of the run. And git-versioned state is a **zero-dependency** durability mechanism.
**AEGIS-X IMPLEMENTATION:** NEW `runtime/checkpoint.py` + `storage/checkpoints.py`. Reuse the **exact** `Checkpoint` pattern from `data_pipeline/common.py` — atomic `.tmp` + `Path.replace`, already Windows-safe.

## 38. Persistence
**STATUS:** PARTIAL
**CURRENT FILES:** `storage/sqlite.py` (`MIGRATIONS` tuple, `PRAGMA user_version`, idempotent, refuses downgrade) · `storage/repositories.py` (parameterized SQL, FK cascade, role CHECK, ordered reads) — but **2 tables only**
**REFERENCE SOURCE:** Automaton **36 tables** across `MIGRATION_V2`…`MIGRATION_V11`; WAL + `wal_autocheckpoint=1000` + startup `pragma("integrity_check")` that **throws**; Claude `<projectDir>/<sessionId>.jsonl` event-sourced transcripts with byte-level dead-fork pruning
**WHAT TO TAKE FROM IT:** the **integrity check at startup**, and append-only event-sourced transcripts (which double as checkpoints).
**AEGIS-X IMPLEMENTATION:** MODIFY — **append `Migration(version=2)`** with runs / steps / tool_invocations / plans / tasks / observations / memory_items / checkpoints / events / audit. **Never edit migration 1** (`docs/development.md:41`). Also fix `save_conversation`'s `INSERT OR IGNORE` (`storage/repositories.py:169`) to an upsert — it silently makes message updates impossible.

## 39. Telemetry/tracing
**STATUS:** MISSING
**CURRENT FILES:** `observability/logging.py` correct JSON but only **3** call sites; `log_event`/`get_logger` never called; no `run_id`/`step_id`/`tool_call_id` on any object; no token or cost accounting
**REFERENCE SOURCE:** Claude **statusline as a public IPC contract** (a user-defined shell command receiving a versioned JSON payload on stdin, including `cost{total_cost_usd,…}` and `context_window{current_usage, used_percentage, remaining_percentage}`); OTel `logOTelEvent()`. Automaton `MetricsCollector` (counters/gauges/histograms, **never throws**), `AlertEngine` with per-rule `cooldownMs`, `inference_costs` table written on **every** call
**WHAT TO TAKE FROM IT:** the **statusline JSON payload** — a user-defined command receiving structured state is the cheapest possible observability extension point, with zero coupling.
**AEGIS-X IMPLEMENTATION:** NEW `observability/{events,tracing,metrics}.py` + `events/sinks.py::StatuslineSink`. Add `run_id`/`step_id`/`tool_call_id` to `Run`/`Step`/`ToolInvocation` first — nothing is correlatable without them.

## 40. CLI
**STATUS:** PARTIAL
**CURRENT FILES:** `cli/main.py` (346 lines) — `chat`, `conversations`, `conversation`, `health`, `tool` + a legacy one-shot form; 9 tests. ⚠ `_run_tool` (`:258-260`) builds a **second throwaway `ToolRegistry`**, bypassing the runtime
**REFERENCE SOURCE:** Claude ~90 slash-command directories with `type: 'prompt' | 'local'`, `progressMessage` + `contentLength` for **pre-flight token estimation of the command body**, and commands that **fork into background agent work** then re-enqueue as a hidden follow-up prompt. Automaton `automaton-cli status|logs|fund|send` as a **separate operator CLI** distinct from the runtime
**WHAT TO TAKE FROM IT:** (a) **three command flavours** — prompt-expanding, local-module, extension-derived; (b) the **separate operator CLI** for status/logs, distinct from the agent CLI; (c) commands that fork into background work.
**AEGIS-X IMPLEMENTATION:** MODIFY `cli/main.py` — add `run, resume, plan, memory, agents, jobs, serve`; `--max-steps`, `--json-events`, `--resume <run_id>`. **Fix the duplicate `ToolRegistry`.** Consider a separate `aegisx-admin` operator CLI for status/logs/cost.

## 41. API
**STATUS:** PLACEHOLDER (`api/__init__.py` is one line)
**CURRENT FILES:** none
**REFERENCE SOURCE:** Claude `entrypoints/sdk/coreSchemas.ts` — `SDKMessage` = `user | assistant | result | system | stream_event | tool_progress | auth_status | …`, with `system` subtypes `init, compact_boundary, status, post_turn_summary, api_retry, local_command_output, hook_started, hook_progress, hook_response, files_persisted, task_notification, task_started, session_state_changed, task_progress, elicitation_complete`; a **separate control channel** (`control_request | control_response | control_cancel_request | keep_alive | update_environment_variables`) with subtypes `initialize, interrupt, can_use_tool, set_permission_mode, set_model, rewind_files, stop_task, …`. Automaton's `pro/dashboard/server.ts`: `GET /events` SSE, `POST /act/<action>`, constant-time `tokensEqual`, action names sanitised `[^a-z_]`
**WHAT TO TAKE FROM IT:** the **two-channel split** — a streaming event channel and a separate control channel. That is what lets a client watch a run *and* interrupt it. Automaton's `tokensEqual` + `crypto.timingSafeEqual` is the correct auth comparison.
**AEGIS-X IMPLEMENTATION:** NEW `api/{server,routes,events}.py` — stdlib `http.server` + SSE. Event channel (`/v1/runs/{id}/events`) and control channel (`/v1/runs/{id}/control`) as **separate** endpoints.

## 42. Dataset pipeline
**STATUS:** COMPLETE
**CURRENT FILES:** `data_pipeline/` — 13 files, 2,400 LOC, 28 tests, **11 executed stages**: inspect → download → validate → normalize → classify → filter → per-source dedup → global dedup → split → mixture → statistics, plus the `prepare.py` DAG and run manifest
**REFERENCE SOURCE:** the spec's own 11-stage flow
**WHAT TO TAKE FROM IT:** nothing to adopt. AEGISAI's is more complete than the spec requires.
**AEGIS-X IMPLEMENTATION:** **REUSE UNCHANGED.** Fix the known defects only: `normalize.py:63-76` double-storage; `classify.py:81-99` name-based classification (mislabels 6,000/6,000 SWE records as `ERROR_RECOVERY`); add branches for the four rejected dialects; add the **safety** stage (component 43 prerequisite) and a **licence gate**. ⚠ Add `datasets/` to `.gitignore` — 12.6 GB currently unignored.

## 43. Training pipeline
**STATUS:** MISSING
**CURRENT FILES:** none. `training/prepare_dataset.py` is a legacy single-source preparer with an incompatible `{id, messages, metadata}` schema
**REFERENCE SOURCE:** **MIND-LLM's own `pretrain.py` / `sft.py`** — atomic bit-exact resume, SHA-256 artifact identity, token-budget planning, bounded-memory streaming, document-span-preserving uint16 shards. **More mature than anything in AEGISAI.**
**WHAT TO TAKE FROM IT:** the whole training harness. Reuse it; do not reimplement.
**AEGIS-X IMPLEMENTATION:** MODIFY MIND-LLM (P0, in order): **KV cache** → **tool-call emission** → **retrain on the AEGIS-X corpus** → context 512→8192 with RoPE scaling → true streaming (free with the KV cache) → re-train the tokenizer with real BPE merges (2.56 → ~4.0 chars/token). NEW in AEGISAI: `training/{sft,curriculum,lora}.py` as thin drivers. The corpus is now **8,651,510 tokens — 0.4% of what a 100M-param model needs.**

## 44. Evaluation
**STATUS:** MISSING
**CURRENT FILES:** none. No eval set, no success metric, no regression gate
**REFERENCE SOURCE:** MIND-LLM `eval_capability.py` (10 fixed probes: arithmetic, factual QA, code completion, context retention, instruction following) and `eval_harness.py`, which **declares 7 capabilities as `NOT_YET_IMPLEMENTED`**: `knowledge, mathematics, coding, reasoning, instruction_following, long_context, tool_use`
**WHAT TO TAKE FROM IT:** the probe-based capability harness, and the honest `NOT_YET_IMPLEMENTED` list — which is effectively AEGIS-X's own gap list.
**AEGIS-X IMPLEMENTATION:** NEW `evaluation/{harness,metrics}.py` + `suites/{tool_call_format,plan_quality,recovery,long_horizon}.py`. Agent success rate, step efficiency, verifier pass rate, cost per task. **Gated in CI** at every phase boundary.

---

# F. DEPENDENCIES BETWEEN COMPONENTS

## F.1 Hard dependencies (nothing in the block can build without the block above it)

```
core/{ids,budget,transitions,tasks}          [foundation, no deps]
        │
        ├──► inference/{options,chat,streaming}   needs core/ids (run_id), core/budget
        │            │
        │            └──► core/models.py  MUST gain tool_call_id/tool_name   ← HARD BLOCKER
        │                         │
        │                         └──► agents/loop.py
        │                                    │
        │                                    ├──► agents/{executor,planner,replanner,verifier}
        │                                    │            │
        │                                    │            └──► agents/{registry,delegation}
        │                                    │
        │                                    ├──► tools/{factory,select,executor,filesystem,terminal,
        │                                    │            python_exec,git,web,browser,api_client,notes}
        │                                    │            │
        │                                    │            └──► security/{policy,approvals,rules}
        │                                    │                     (security/policy needs tools/factory's
        │                                    │                      risk_level + is_concurrency_safe)
        │                                    │
        │                                    ├──► observation/{manager,budget,trust}
        │                                    │
        │                                    ├──► context/{manager,compaction}   needs inference/*, observation/*
        │                                    │
        │                                    ├──► memory/{working,episodic,semantic,procedural,retrieval,
        │                                    │          consolidation,budget}   needs context/manager, storage/memory_repo
        │                                    │
        │                                    └──► tasks/{manager,records,log}   ← needs storage Migration 2
        │
        ├──► storage/sqlite.py Migration(version=2)   MUST precede tasks/*, memory/*, scheduler/*
        │
        ├──► events/{bus,hooks,types} → observability/{events,tracing,metrics} → events/sinks (statusline)
        │
        ├──► scheduler/{daemon,jobs,cron} → workers/{pool,identity}   needs storage Migration 2
        │
        └──► recovery/{classifier,manager}   needs core/errors, agents/loop
```

## F.2 Soft / ordering-only dependencies

| Pair | Why |
|---|---|
| `context/compaction` → `memory/*` | Compaction writes memory on checkpoint-and-reset |
| `agents/verifier` → `tools/*` | TestRunnerVerifier shells out; FileDiffVerifier needs git |
| `agents/replanner` → `agents/planner` | Replans by re-invoking the planner |
| `memory/consolidation` → `memory/procedural` | CONSOLIDATE produces procedures |
| `models/*` → `inference/mindllm` → `inference/router` | MIND-LLM is a router tier, not a direct dependency of the loop |
| `api/*` → `events/bus` | SSE bridges the bus |
| `data_pipeline/*` → `training/*` | Tokenize + pack produce the shards the trainer consumes |
| `evaluation/*` → everything | Gated in CI at each phase |

## F.3 External dependencies

| Phase | Package | For |
|---|---|---|
| 1 | `anyio` | structured concurrency for the loop, parallel tools, workers |
| 1 | `httpx` | async/streaming HTTP for `/api/chat` and web tools |
| 1 | `tenacity` (or stdlib) | retry/backoff |
| 2 | `tiktoken` (or MIND-LLM's tokenizer) | token counting |
| 2 | `numpy` | vector ops for semantic memory |
| 3 | `sqlite-vec` | vector index, keeps the single-file local-first property |
| 3 | `croniter` | cron parsing |
| 4 | `playwright` | browser automation |
| 4 | `peft` / `bitsandbytes` | LoRA/QLoRA — ⚠ **4 GB VRAM makes this infeasible on the stated hardware** |
| 5 | `safetensors` | removes `weights_only=False` arbitrary-pickle risk |

> AEGISAI's `pyproject.toml` currently declares `dependencies = []` and MIND-LLM has **no manifest at all**. The zero-dependency heritage ends at Phase 1.

---

# G. IMPLEMENTATION ORDER

| # | Phase | Deliverable | Gate |
|---|---|---|---|
| 1 | **Foundation** | `core/{ids,budget,transitions,tasks}.py`; `core/errors.py` +9 classes; `core/config.py` +~20 settings; `configs/default.toml`; `.gitignore` (+`datasets/`) | 90 tests green; `Settings` round-trips |
| 2 | **Inference** | `inference/{options,chat,streaming,registry}.py`; **fix `health_check` model-absence false-positive**; `inference/base.py` +`stream/embed/count_tokens` | 3-turn conversation with a system prompt and a tool round-trip against real Ollama |
| 3 | **Agent loop** | `core/models.py` **+`tool_call_id`/`tool_name`, +`Run`/`Step`/`ToolInvocation`**; `agents/loop.py`; `core/transitions.py`; `storage/sqlite.py` **Migration(version=2)**; `storage/runs.py` | Offline e2e: scripted backend drives a 5-step task with every step persisted, committed in one transaction |
| 4 | **Tools** | `tools/{factory,select,executor}.py`; `tools/filesystem.py`; `tools/registry.py` `to_model_schema`; `security/policy.py` | Model receives compact tool schemas; filesystem works inside a jail; **R1 signed off** |
| 5 | **Planning** | `agents/planner.py` + schema; `agents/replanner.py`; `tasks/{manager,records}.py`; `storage/plans.py` | 10-step task → validated acyclic plan → replans once on injected failure |
| 6 | **Execution** | `tools/{terminal,python_exec,git,sandbox}.py`; parallel `execute_batch` (read-safe / write-serial) | Parallel read wave completes; sandbox escape blocked in tests |
| 7 | **Observation** | `observation/{manager,budget,trust}.py`; 3-layer result budgets; redaction; untrusted marking | A 10 MB result is truncated to budget and the turn still completes |
| 8 | **Verification** | `agents/verifier.py` (5 strategies); `VERIFICATION` category in the classifier | A deliberately-wrong result triggers RETRY then REPLAN |
| 9 | **Recovery** | `recovery/{classifier,manager}.py`; failure journal → lesson; `runtime/checkpoint.py`; resume | Kill at step N, resume, complete **exactly once** |
| 10 | **Context + memory** | `context/{manager,compaction}.py`; `memory/*` (4 tiers + retrieval + consolidation + budget); `storage/memory_repo.py` | 50-step run completes without overflow; cascade reaches checkpoint-and-reset |
| 11 | **Persistent tasks** | `tasks/{log,store}.py`; all 13 spec fields | Every field survives a restart |
| 12 | **Scheduler + workers** | `scheduler/{daemon,jobs,cron}.py`; `workers/{pool,identity}.py`; `runtime/daemon.py` | A scheduled job runs headless; a lease expires and is reclaimed; SIGTERM drains |
| 13 | **Multi-agent** | `agents/{registry,delegation}.py` + `*.md`; `multi_agent/*`; tool allow-sets; recursion guard | MASTER delegates to CODING + TEST concurrently; VERIFIER cannot write files |
| 14 | **Security** | `security/{approvals,sandbox,secrets,audit}.py`; `security/rules/*`; `tools/audit.py` hash-chained | Denials audited with typed reasons; dangerous actions block on approval |
| 15 | **Datasets** | Fix `normalize.py` duplication + `classify.py` name-matching; add 4 dialect branches; add `safety.py`, `licensing.py`, `tokenize.py`, `pack.py`; `VERIFICATION`+`PLANNING` categories | Token count within 5% of a real tokenizer; PII **rejected not flagged**; a corpus processes end-to-end |
| 16 | **Training** | MIND-LLM P0: KV cache → tool-call emission → retrain → context 8192 → streaming → tokenizer. `training/{sft,curriculum}.py` | Tool-call format accuracy > 95% on a held-out set |
| 17 | **Evaluation** | `evaluation/{harness,metrics}.py` + suites; `data_pipeline/{rollout,eval_sets}.py` | Baseline success rate + step efficiency recorded and **gated in CI** |
| 18 | **API / CLI** | `api/{server,routes,events}.py`; 7 CLI subcommands; `events/{bus,hooks,sinks}.py`; `observability/*` | A remote client starts a run, streams events, **and interrupts it** |
| 19 | **Hardening** | Load, chaos, 24 h soak, dependency pinning, CI matrix, coverage | No memory growth; coverage ≥ 70% on all new subsystems |

**Critical path:** 1 → 2 → 3 → 4 → 5 → 7 → 8 → 9 → 10.
**Parallelisable after 4:** 6, 11, 12, 13, 14.
**Independent, start immediately:** 15.
**Gated on 15 *and* MIND-LLM P0:** 16 → 17 → 18 → 19.

---

# H. SUMMARY — REUSE / REDESIGN / NEW

## H.1 Reuse unchanged (AEGISAI's existing assets)

| Asset | Location |
|---|---|
| 11-stage dataset pipeline | `data_pipeline/` (9 of 13 modules) |
| SQLite migrations + repositories | `storage/sqlite.py` `MIGRATIONS[0]`, `storage/repositories.py` |
| `Checkpoint` pattern (atomic `.tmp` + `Path.replace`) | `data_pipeline/common.py:67-96` |
| `Tool` ABC + `_validate_value` schema validator | `tools/base.py` |
| `Capability` enum | `tools/permissions.py:7-12` |
| `Event` dataclass (dead — revive as EventBus payload) | `core/models.py:210-239` |
| `ToolResult` / `ToolCall` value types | `tools/base.py:11-76` |
| Error taxonomy (additive only) | `core/errors.py` |
| JSON logging formatter | `observability/logging.py` |
| 90 tests | `tests/` |
| **MIND-LLM's training harness** | `pretrain.py`, `sft.py`, `prepare_data.py`, `data_loader.py`, `checkpoint_utils.py` |
| **MIND-LLM's `agentic/` abstractions** | `InferenceBackend` Protocol, `ContextEngine`, `TaskManager`, `PermissionPolicy`, `Verifier`, `EventSink` — port the abstractions, not the code |

## H.2 Redesign (exists but wrong shape)

| Component | Current | Change |
|---|---|---|
| `inference/ollama.py` | `/api/generate`, flattened prompt, no `options` | → `/api/chat`, native `messages`, `tools`, `options`, `stream`; **fix `health_check` false-positive** |
| `core/models.py` `Message` | no `tool_call_id`/`tool_name` | **Hard blocker** — add both, plus `Run`/`Step`/`ToolInvocation`/`Plan`/`Task` |
| `tools/base.py` `Tool` | 5 required args, no risk metadata | `build_tool()` factory + fail-closed `is_read_only`/`is_concurrency_safe`/`is_destructive` + `timeout_s` + `search_hint`; auto-generate `call_id` |
| `tools/permissions.py` | flat capability set, `PROCESS_EXECUTION` hard-denied, string errors | Thin shim over `security/policy.py`; **replace the hard block with sandbox-gated `SANDBOXED_EXECUTION`**; typed `PermissionDecision` |
| `tools/registry.py` `execute` | no timeout/truncation/parallelism/audit | Delegate to `tools/executor.py` |
| `agents/agent.py` | pass-through adapter | **Demote** to an inference adapter; the loop moves to `agents/loop.py` (prevents a third runtime — MIND-LLM already has one) |
| `runtime/runtime.py` | 8 linear steps, no `try/except` | `run_id`, streaming, checkpoints, resume, **single-transaction turn commit** |
| `storage/repositories.py:169` | `INSERT OR IGNORE` | Upsert — message updates are currently impossible |
| `core/config.py` | 1 flat layer | ~20 settings + a settings-sources concept + `migrate<Old>To<New>` |
| `observability/logging.py` | 3 call sites, `log_event` dead | Wire it; add `run_id`/`step_id` binding |
| `data_pipeline/normalize.py` | stores tool data twice, 5 dialects | De-duplicate; add 4 dialect branches |
| `data_pipeline/classify.py` | matches dataset *name* | Content-based evidence; add `VERIFICATION` + `PLANNING` |
| `data_pipeline/statistics.py` | inflated token counts | **Already fixed** — now emits both figures |
| `cli/main.py` | second throwaway `ToolRegistry` (`:258-260`) | Use the runtime registry |

## H.3 Must be newly implemented (~100 files)

| Group | Files |
|---|---|
| **Agent core** | `agents/{loop,executor,planner,replanner,verifier,registry,delegation}.py` + `planner_schema.json` + `agents/*.md` |
| **Inference** | `inference/{options,chat,streaming,router,mindllm,registry}.py` |
| **MIND-LLM serving** | `models/{mindllm_server,kv_cache,tool_parser,load}.py` |
| **Tools (14 families)** | `tools/{factory,select,executor,filesystem,terminal,python_exec,git,web,browser,api_client,notes,sandbox,audit}.py` |
| **Context + memory** | `context/{manager,compaction,window}.py`; `memory/{base,working,episodic,semantic,procedural,retrieval,consolidation,budget}.py` |
| **Tasks** | `tasks/{manager,records,log,store}.py` |
| **Observation / verification / recovery** | `observation/{manager,budget,trust}.py`; `verification/`; `recovery/{classifier,manager}.py` |
| **Security** | `security/{policy,approvals,sandbox,secrets,audit}.py` + `security/rules/*` |
| **Events + observability** | `events/{bus,hooks,types,sinks}.py`; `observability/{events,tracing,metrics}.py` |
| **Scheduler + workers + multi-agent** | `scheduler/{daemon,jobs,cron}.py`; `workers/{pool,identity}.py`; `multi_agent/{topology,messaging,blackboard}.py` |
| **Storage** | `storage/{runs,plans,memory_repo,checkpoints,events_repo,audit_repo}.py` |
| **Data** | `data_pipeline/{safety,licensing,tokenize,pack,rollout,eval_sets,promote}.py` |
| **Training + evaluation** | `training/{sft,curriculum,lora}.py`; `evaluation/{harness,metrics}.py` + `suites/*` |
| **API + CLI + runtime** | `api/{server,routes,events}.py`; `cli/{run,resume,plan,memory,agents,jobs,serve}.py`; `runtime/{daemon,checkpoint}.py` |
| **Tests** | 19 new suites including `test_wiring.py` and `test_doc_freshness.py` |

---

# I. IMPLEMENTATION PLAN — FINAL

## 1. Files to CREATE — ~100 (full table in H.3 and component entries 1–44)

**Load-bearing 12:** `agents/loop.py` · `agents/executor.py` · `agents/planner.py` + `planner_schema.json` · `agents/replanner.py` · `agents/verifier.py` · `inference/{chat,router}.py` · `context/{manager,compaction}.py` · `memory/*` (8) · `tasks/*` (4) · `security/*` (7) · `models/{mindllm_server,kv_cache,tool_parser}.py` · `data_pipeline/{safety,licensing,tokenize,pack,rollout}.py`

## 2. Files to MODIFY — 29

**Load-bearing 8:** `inference/ollama.py` (`/api/chat` + `tools` + `options` + `stream`; **fix `health_check`**) · `core/models.py` (**+`tool_call_id`/`tool_name`**) · `tools/base.py` (`build_tool()` factory, `call_id`, result budgets) · `tools/permissions.py` (**remove the `PROCESS_EXECUTION` hard block**) · `agents/agent.py` (demote to adapter) · `storage/sqlite.py` (**append `Migration(version=2)`; never edit migration 1**) · `runtime/runtime.py` (`run_id`, streaming, checkpoints, resume, transactional commit) · `data_pipeline/{normalize,classify,statistics}.py` (de-duplicate, content-based, token fix)

## 3. Files to PRESERVE — 20

`data_pipeline/{common,download,validate,split,mixture,deduplicate,inspect,cli,prepare}.py` · `storage/sqlite.py` `MIGRATIONS[0]` · `storage/repositories.py` · `core/errors.py` (15 classes) · `core/models.py` `Event` · `observability/logging.py` `StructuredFormatter` · `tools/calculator.py` · `tools/base.py` `_validate_value` · `tools/permissions.py` `Capability` · all 90 tests · `configs/dataset.toml` · 6 docs

## 4. Files/datasets NOT to use

**Orphan:** `datasets/processed/local/`, `datasets/manifests/local/` · **Useless for agency:** `datasets/processed/neulab_mind2web/` (0 tool calls) · **Wrong schema:** `data/training/starcoder_python_instruct.jsonl` as a target · **Licence traps:** `Salesforce/xlam-function-calling-60k`, `Salesforce/APIGen-MT-5k`, `lmsys-chat-1m`, `tulu-3-sft-mixture` · **No licence at all:** `xlangai/AgentTrek` · **Wrong modality:** `xlangai/AgentNet`, `osunlp/Multimodal-Mind2Web` · **Poor signal/GB:** `bigcode/stack-dedup`, `OpenHermes-2.5` · **Redundant:** `neulab/mind2web` (full), Nemotron `tool_calling` split · **Dead weight:** MIND-LLM `runs/*/model.resume.pt` (4.8 GB) · **Correctness hazard:** MIND-LLM `greedy_byte_bpe` as a GGUF source · **Duplicate archives:** `claude-code-clone-main.zip` (0 unique files), `automaton-main.zip` / `automaton-main(1).zip` (byte-identical older snapshots) · **Must not be read:** Claude `cli.js.map` `sourcesContent`

## 5. Implementation order

19 phases, gated — see **Section G**. Critical path `1→2→3→4→5→7→8→9→10`; 6/11/12/13/14 parallel after 4; 15 independent; 16 gated on 15 **and** MIND-LLM P0.

## 6. Architectural conflicts (16 total)

| # | Conflict | Severity | Resolution |
|---|---|---|---|
| **R1** | AEGISAI permanently denies `PROCESS_EXECUTION` (`tools/permissions.py:31-32`, pinned by `tests/test_tools.py:104-111`); the spec requires terminal, Python, package managers, deployment | **CRITICAL** | Do **not** remove the check. Introduce a grantable `SANDBOXED_EXECUTION` capability routed through `tools/sandbox.py` (rlimits/job-objects, jailed cwd, no network, hard timeout). **Requires explicit sign-off.** |
| **R2** | MIND-LLM context is **512 tokens** and `forward` **raises** rather than truncating (`model.py:233-238`); an agent turn needs thousands | **CRITICAL** | MIND-LLM serves the fast tier only (≤400-token budget); `ToolSearch` progressive disclosure keeps schemas small; the reasoning tier uses 32k+ via `/api/chat`. Also extend MIND-LLM to 8,192 with RoPE scaling |
| **R3** | MIND-LLM's active tokenizer is 16k `greedy_byte_bpe` with **`merges: []`** at 2.56 chars/token; GGUF export is a correctness hazard | **CRITICAL** | Re-train the tokenizer with real BPE merges; route tool calls through the canonical `_normalize_tool_call` so the wire format is tokenizer-independent. **No GGUF.** |
| **R4** | MIND-LLM is 0/10 capability after 100,000 tokens (0.005% of Chinchilla) | **CRITICAL** | Tiered routing. MIND-LLM is the fast tier, not the decision-maker. Corpus is now 8.65 M tokens = 0.4% of need |
| **R5** | **Two agent runtimes already exist** — AEGISAI `agents/agent.py` and MIND-LLM `agentic/orchestrator.py` (1,300 LOC) | **HIGH** | AEGIS-X's `agents/` is canonical; port MIND-LLM's abstractions, not its code. Demote AEGISAI's `Agent` |
| **R6** | AEGISAI has 0 async; MIND-LLM has `asyncio` but no KV cache | **HIGH** | `anyio`; MIND-LLM's server is a separate process. The KV cache gates everything |
| **R7** | Four incompatible message representations across the four trees | **HIGH** | One canonical model with `tool_call_id` + `tool_name` in `core/models.py`; every backend serialises from it; never store a second copy |
| **R8** | AEGISAI 2 tables vs Automaton 36 vs the spec's 13 persisted fields | **HIGH** | One `Migration(version=2)` with ~10 tables. Not 36 — most are financial/child-specific |
| **R9** | MIND-LLM records `torch 2.14.0+cpu` — **not a public release**; neither project has a manifest | **HIGH** | AEGIS-X gets a real `pyproject.toml` pinning public versions |
| **R10** | `datasets/` is 12.6 GB and not gitignored | **MEDIUM** | Add to `.gitignore` |
| **R11** | AEGISAI has no tokenizer stage; MIND-LLM's `prepare_data.py` produces uint16 shards with `.spans.json` | **MEDIUM** | Reuse MIND-LLM's as a thin adapter, not a reimplementation |
| **R12** | Automaton built `ContextManager`/`CompressionEngine`/`EnhancedRetriever` and **never wired them**; its docs drifted (57→85 tools, 22→36 tables) | **MEDIUM** | Ship `test_wiring.py` (fails if a constructed subsystem is never called) and `test_doc_freshness.py` |
| **R13** | Automaton's tool results are `string`; Claude's are `ToolResult<T>`; AEGISAI's is a dataclass | **MEDIUM** | AEGISAI's is best. Add `is_error` and structured error codes |
| **R14** | Both projects declare no dependencies | **MEDIUM** | AEGIS-X gets a real manifest. The zero-dep heritage ends at Phase 1 |
| **R15** | MIND-LLM declares `"tool_call"` in `EventKind` and handles it, but never emits one | **MEDIUM** | The wiring exists; only emission is missing. Small change once the model can produce the syntax — do not redesign the protocol |
| **R16** | Spec lists 14 tool families; the references are terminal-only and have no browser | **LOW** | Build the interface first, implement families in order. Do not let a missing browser block the runtime |

---

# J. STATUS SUMMARY

| Status | Count | Components |
|---|---|---|
| **COMPLETE** | 1 | 42 Dataset pipeline |
| **PARTIAL** | 13 | 5, 10, 12, 23, 24, 34, 38, 40, and 4 more |
| **MISSING** | 26 | 1, 3, 4, 6, 8, 9, 11, 13–22, 29–33, 35, 37, 39, 43, 44 |
| **PLACEHOLDER** | 4 | 2, 25, 26, 27, 28, 36, 41 |
| **BROKEN** | 0 runtime / 5 data | Data: token inflation (fixed), double-storage, name-based classification, 4 rejected dialects, missing safety + licence stages |

**AEGIS-X does not exist yet.** What exists is a correct synchronous chat-over-SQLite client (AEGISAI) and a correct executed dataset ETL pipeline, plus one laboratory-grade 100 M-parameter model with a dormant 1,300-line agent runtime already inside it.

**The three strongest assets are not the model:** AEGISAI's `data_pipeline/`, Automaton's orchestration design (Goal/Task DAG, validated planner, first-deny-wins policy, transactional turn commit), and the reference's boundary design (fail-closed tool factory, layered compaction, per-agent-class tool scoping, typed decision reasons, statusline-as-IPC).

**The weakest link is unambiguous:** MIND-LLM at 0/10 capability, 512-token context, no KV cache, no tool-call emission. The correct response is to route around it while keeping it in the loop as a fast tier whose every contribution becomes rollout data.

---

*Audit only. Nothing implemented. See `docs/AEGIS-X_REFERENCE_ARCHITECTURE_AUDIT.md` for the earlier pass plus the 2026-09-25 acquisition-run record.*



