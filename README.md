# AEGISAI

AEGISAI is a modular, local-first AI-agent platform built on a persistent
synchronous conversation core (Phase 1), an autonomous tool-using loop
(AEGIS-X), and a set of self-funding subsystems (`treasury`, `harvest`,
`revenue`, `escalation`).

There are two entrypoints, and they are different things:

| Entrypoint | What it is |
|---|---|
| `python -m cli.main` / `aegisai` | The Phase 1 conversation CLI. Persistent chat over local Ollama, plus a tool command. |
| `python agent.py` | The AEGIS-X composition root. Wires treasury, retrieval, live web, revenue evaluation, and the escalation queue into one process. |

## Architecture

```text
AEGISAI/
├── agent.py            AEGIS-X composition root and operator CLI
├── core/               Configuration, data structures, and shared errors
├── runtime/            Persistent synchronous orchestration; aegisx.py assembles the agent
├── inference/          Provider contract, Ollama implementation, chat, options
├── memory/             BM25 retrieval: chunking, tokenizing, ingest, store, retriever
├── tools/              Tool contracts, registry, permissions, calculator, filesystem, web, search, python_exec
├── agents/             Minimal non-autonomous agent and the tool-using loop
├── observability/      Structured JSON logging
├── security/           Revocable, durable capability grants
├── storage/            SQLite schema, migrations, and repositories
├── treasury/           Decimal money, append-only double-entry ledger, 20/80 reserve policy
├── harvest/            Bounded, resumable, treasury-metered documentation harvesting
├── revenue/            Evidence-gated opportunity evaluation, track record, self-tuning
├── escalation/         Queue of blockers the agent cannot resolve alone
├── training/           Bounded local SFT dataset preparation and feedback
├── data_pipeline/      Streaming inspection, normalization, filtering, and manifests
├── datasets/           Raw, staged, processed, and manifest data
├── api/                Reserved for a future API layer
├── cli/                Conversation, health, and tool commands
├── configs/            Default TOML configuration
├── docs/               Architecture, configuration, and development guidance
└── tests/              Test suite
```

Note that `memory/`, `security/`, and `tools/` are no longer reserved
placeholders; each contains working, tested code.

The Phase 1 conversation request path is:

```text
User input
    ↓
Conversation lookup/create
    ↓
Load previous messages
    ↓
Agent
    ↓
InferenceProvider
    ↓
Ollama
    ↓
Assistant response
    ↓
Persist assistant response
```

The runtime uses the `InferenceProvider` abstraction. `OllamaProvider` formats
prior role-aware messages into the prompt sent to the local `/api/generate`
endpoint. No cloud provider is required.

## Requirements

- Python 3.12 or newer
- Ollama installed and running locally
- The default model available locally

The runtime has no mandatory third-party dependencies. SQLite uses Python's standard-library `sqlite3`; HTTP uses the standard library.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

For runtime-only installation, use `python -m pip install -e .`.

## Ollama setup

```powershell
ollama serve
ollama list
ollama pull qwen2.5-coder:7b
```

## Local dataset preparation

AEGISAI includes a standard-library, streaming preparation tool for the public Apache-2.0 dataset `OLMo-Coding/starcoder-python-instruct`. The dataset contains roughly 1.54M Python instruction/code records and is about 6.68GB, so the default command prepares a bounded 10,000-record Python 3 sample:

```powershell
python -m training.prepare_dataset
```

The output is written to `data/training/starcoder_python_instruct.jsonl` with a manifest at `data/training/manifest.json`. This legacy single-source preparer remains separate from the four-source `data_pipeline`; use the latter for cross-source provenance, global deduplication, category mixtures, and final splits. Each normalized record contains an `id`, user/assistant `messages`, and source metadata.

For an explicitly bounded larger preparation:

```powershell
python -m training.prepare_dataset --max-files 4 --max-records 50000
python -m training.prepare_dataset --max-files 20 --max-records 0
```

The preparation tool does not train a model. The available machine has a 4GB GeForce 840M and CPU-only PyTorch, so full or practical 7B fine-tuning is not attempted. AEGISAI continues to use the existing local Ollama model until a separate, hardware-appropriate adaptation workflow is explicitly added. The dataset card declares Apache-2.0, but individual source-code licenses may still apply; review provenance before redistribution.

## Agent-data pipeline

The independent `data_pipeline` package inventories and prepares candidate agent datasets without training or executing downloaded examples. Install the optional Parquet dependency with `python -m pip install -e ".[data]"`, then use bounded commands such as:

```powershell
python -m data_pipeline.cli inspect --config configs/dataset.toml --output datasets/manifests/inventory.json
python -m data_pipeline.cli download --config configs/dataset.toml --source toprak --source neulab --max-bytes 80000000
python -m data_pipeline.cli prepare --config configs/dataset.toml --source toprak --input datasets/raw/toprak/data/train-00000-of-00001.parquet --max-records 500 --no-resume
```

Raw files remain under `datasets/raw`; validated, normalized, classified, filtered, deduplicated, split, and mixed outputs are written under `datasets/staging`, `datasets/processed`, and `datasets/manifests`. The default configuration never enables the full NeuLab collection; large subsets require explicit review. See [docs/data_pipeline.md](docs/data_pipeline.md) and [docs/datasets.md](docs/datasets.md).

## Configuration

The default values are defined in `core/config.py` and mirrored in `configs/default.toml`. Environment variables override file values and defaults:

| Setting | Environment variable | Default |
| --- | --- | --- |
| Application name | `AEGISAI_APPLICATION_NAME` | `AEGISAI` |
| Environment | `AEGISAI_ENVIRONMENT` | `development` |
| Log level | `AEGISAI_LOG_LEVEL` | `INFO` |
| Inference provider | `AEGISAI_INFERENCE_PROVIDER` | `ollama` |
| Model name | `AEGISAI_MODEL_NAME` | `qwen2.5-coder:7b` |
| Ollama base URL | `AEGISAI_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` |
| Database path | `AEGISAI_DATABASE_PATH` | `data/aegisai.db` |

`OLLAMA_BASE_URL` is also accepted as a convenience alias. TOML configuration uses the same field names and can be passed with `--config`.

## CLI

`python -m cli.main` and the installed `aegisai` script are the same program.
If the `aegisai` console script is not on your `PATH` (it is not, by default,
in this checkout), use `python -m cli.main` everywhere below.

Create a conversation and print its ID:

```powershell
python -m cli.main chat --new
```

Continue it with a message:

```powershell
python -m cli.main chat --id <conversation_id> "My favorite color is blue."
```

Create and send in one command:

```powershell
python -m cli.main chat --new "Hello"
```

List and inspect conversations:

```powershell
python -m cli.main conversations
python -m cli.main conversations --json
python -m cli.main conversation <conversation_id>
```

Check Ollama:

```powershell
python -m cli.main health
```

The Phase 0 one-shot form remains supported:

```powershell
python -m cli.main "Hello"
```

Run the harmless calculator tool:

```powershell
python -m cli.main tool calculator --expression "2 + 3 * 4"
```

Structured JSON input is also accepted by the tool interface and CLI when the shell preserves JSON quoting.

The calculator uses a restricted AST evaluator and never calls `eval()`. It requires only `READ_ONLY` capability.

## Execution model and risk

**The agent runs unrestricted by default. It is not sandboxed.**

With the default settings (`allow_network=True`, `allow_execution=True`,
`confine_workspace=False`), a goal you hand to `agent.py run` gives the model:

- **Every file you can read or write**, anywhere on the machine. The filesystem
  tools are not confined to `workspace/` unless you pass
  `--confine-workspace`.
- **The ability to run any program** as you, via `run_shell`.
- **Outbound network access.**

This is a deliberate choice, not an oversight. It is the right configuration
for a personal agent on a machine you control, and a capability check cannot
make an in-process tool safe anyway - see the `NETWORK` note below.

The one boundary that *is* enforced is the environment. Child processes do not
inherit your credentials: `tools/python_exec.py` scrubs every variable matching
`SCRUBBED_ENV` (`AWS_*`, `AZURE_*`, `GCP_*`, `GOOGLE_*`, `OPENAI_*`,
`ANTHROPIC_*`, `HF_*`, `HUGGINGFACE_*`, `NPM_*`, `DOCKER_*`, `KUBECONFIG`,
`SSH_*`, `STRIPE_*`, `BRAVE_*`, `TAVILY_*`, `SERPER_*`) from both execution
tools. This matters because the model chooses the command string and could
otherwise read a key and echo it to a network call in the same step. Note the
consequence: the agent cannot use your search-provider API keys for you.

Two honest limitations:

- **`NETWORK` is not an enforcement boundary.** Denying `NETWORK` blocks
  `web_fetch` and `web_search`, but `run_shell` can still `curl`. Separating
  the two needs an OS-level sandbox, not a capability check. Treat `NETWORK`
  as a statement about the web tools and nothing more.
- **Execution is not isolated.** `run_python` runs with `-I` and a scrubbed
  environment; `run_shell` runs a caller-supplied string through the shell.
  Neither is a kernel-level jail, and neither stops a determined escape.

To reduce exposure without giving up the agent:

```powershell
python agent.py --no-execution run "<goal>"   # no shell, no python
python agent.py --confine-workspace run "<goal>"  # filesystem limited to workspace/
python agent.py --no-network run "<goal>"     # blocks the web tools (not curl)
```

Run `agent.py doctor` first; it reports the model, the databases, and which
search keys are set.

## The agent CLI

`agent.py` is the AEGIS-X entrypoint. It uses four local SQLite databases
(`data/treasury.db`, `data/memory.db`, `data/revenue.db`, `data/harvest.db`)
that are created on first use.

Start here — this checks every dependency and tells you what is missing:

```powershell
python agent.py doctor
```

Read-only commands, safe to run any time:

```powershell
python agent.py status              # money, knowledge index, category health
python agent.py inbox               # blockers waiting on you
python agent.py permissions         # durable grants; nothing is granted by default
```

The main verbs:

```powershell
python agent.py run "<goal>"        # one goal through the full stack
python agent.py harvest             # fill the knowledge base from the web
python agent.py market              # probe a marketplace for demand evidence
python agent.py evaluate            # price opportunities against retrieved evidence
python agent.py cycle               # model-free learning loop: research, price, record
python agent.py autonomous          # run unattended for a bounded time
python agent.py review              # rate past runs so the agent learns
python agent.py tune                # re-derive thresholds from measured outcomes
```

`autonomous` and `harvest` perform real network requests and spend from the
treasury. Start with `doctor`, `status`, and `run` before using them.

Global flags include `--model`, `--base-url`, `--no-network`,
`--no-execution`, `--workspace`, and `--cost-per-request`. See
`python agent.py --help`.

## SQLite persistence

The default database is `data/aegisai.db`; parent directories are created automatically. SQLite foreign keys are enabled. Schema initialization runs automatically and is idempotent.

```text
conversations
- id TEXT PRIMARY KEY
- created_at TEXT NOT NULL
- updated_at TEXT NOT NULL
- metadata TEXT NOT NULL

messages
- id TEXT PRIMARY KEY
- conversation_id TEXT NOT NULL
- role TEXT NOT NULL
- content TEXT NOT NULL
- created_at TEXT NOT NULL
- metadata TEXT NOT NULL
```

Messages are role-checked against `system`, `user`, `assistant`, and `tool`, and are read in append order. Values are bound with parameterized SQL. Conversation deletion cascades to its messages.

## Tools and permissions

`Tool` implementations declare a name, description, input schema, output schema, and required capability. `ToolRegistry` rejects duplicate and unknown names, validates requested tools, validates input/output, and checks a `PermissionPolicy` before execution.

Supported capability names are:

- `READ_ONLY`
- `FILESYSTEM_READ`
- `FILESYSTEM_WRITE`
- `NETWORK`
- `SANDBOXED_EXECUTION`
- `PROCESS_EXECUTION`

`PROCESS_EXECUTION` is permanently disabled: `PermissionPolicy` raises
`PermissionDeniedError` if a policy is constructed with it. The grantable
replacement is `SANDBOXED_EXECUTION`, which `tools/python_exec.py` implements
as a Python runner (`-I`, fixed argv, no shell) and a shell runner that passes
`shell=True` on a caller-supplied string.

Despite the name, this is **not** a sandbox. It applies a wall-clock timeout, a
capped output buffer, `stdin=DEVNULL`, and a credential-scrubbed environment.
It does not confine the filesystem and does not isolate the process. See
[Execution model and risk](#execution-model-and-risk) above.

`agent.py` builds the default policy as `READ_ONLY`, `NETWORK`,
`FILESYSTEM_READ`, `FILESYSTEM_WRITE`, and `SANDBOXED_EXECUTION`. `NETWORK` is
included by default but is not an enforcement boundary, because the execution
tools bypass it.

Two separate permission systems exist and they are not interchangeable:

| | Scope | Lifetime |
|---|---|---|
| `tools/permissions.py` | What one run may do | One process, no memory |
| `security/grants.py` | What the agent may ever do | Durable on disk, revocable, audited |

Agent tool calls in the Phase 1 agent are explicit through `ToolCall` and
`Agent.execute_tool`; that agent does not autonomously decide to execute tools.
The AEGIS-X loop in `agents/loop.py` *does* plan and execute tool calls, and is
the component gated by the policy above.

## Tests and development checks

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy .
python -m compileall -q .
python -m pip check
```

See [docs/development.md](docs/development.md) for the development workflow and manual verification steps.

## Current limitations

- Persistence is local SQLite only; there is no distributed or remote database.
- Conversation history is sent to Ollama as a formatted prompt. There is no
  token-budget or context-compaction system on the Phase 1 chat path. (The
  AEGIS-X path has a separate BM25 index, which is retrieval, not compaction.)
- SQLite metadata must be JSON-serializable.
- Tools are synchronous. There is no parallel tool execution.
- The `aegisai` CLI registers only the calculator. The filesystem, web, search,
  and execution tools are reachable through `agent.py` and `runtime/aegisx.py`,
  not through the `aegisai tool` subcommand.
- Execution is not a jail. The agent is unrestricted by default and a
  capability check cannot make an in-process tool safe. See
  [Execution model and risk](#execution-model-and-risk).
- Web search defaults to the keyless Wikipedia provider. Brave, Tavily, and
  Serper are supported via environment variables.
- Fine-tuning is not possible on the stated hardware (4GB GPU, CPU-only
  PyTorch). `harvest` and `data_pipeline` build a knowledge base; they do not
  change model weights.
- Dataset preparation is separate from inference and does not change the
  Ollama model.
- No cloud providers, vector memory, multi-agent orchestration, or
  self-modification is included.

## Project status

The 667-test suite, `ruff check`, `ruff format --check`, `mypy`,
`compileall`, and `pip check` all pass.

This is **not** a finished product. `AEGIS-X_REFERENCE_ARCHITECTURE_AUDIT.md`
is a 19-phase plan, and roughly one phase is done. Phase 1 (foundation) is
complete: `core/ids.py`, `core/budget.py`, `core/transitions.py`, and
`core/tasks.py` exist and are tested. Most of what the plan specifies for
phases 2-19 does not: no `inference/{streaming,registry}.py`, no
`agents/{planner,replanner,verifier}.py`, no
`tools/{factory,executor,terminal,git,sandbox}.py`, no `observation/`,
`recovery/`, `context/`, `tasks/`, `scheduler/`, `workers/`, `multi_agent/`,
`evaluation/`, or `events/`. Treat that document as a design proposal, not a
description of the code.

Not yet wired into the loop: `core/budget.py` and `core/transitions.py` are
standalone and tested, but `agents/loop.py` still uses its own local
`Continue`/`Terminal` classes and its own step counter. Migrating it is the
first job of phase 3.

Two caveats about the development checks above:

- There is no `[tool.ruff]` or `[tool.mypy]` configuration in
  `pyproject.toml`, so both run on their defaults. The enabled rule set
  therefore depends on the installed tool version, and the `dev` extra pins
  only `ruff>=0.6`. A green `ruff check` is not reproducible across versions
  until a rule set is declared.
- `treasury`, `harvest`, `revenue`, and `escalation` appear in no phase of the
  audit plan. They are a separate, undocumented workstream whose scope and
  intended relationship to AEGIS-X have not been decided.
