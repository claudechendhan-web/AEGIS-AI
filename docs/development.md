# Development

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

The runtime has no mandatory third-party packages. The development extra installs the test and static-analysis tools.

## Test suite

Run all Phase 0 and Phase 1 tests:

```powershell
python -m pytest -q
```

The provider tests use mocked HTTP responses, so the unit suite does not require Ollama. SQLite tests use temporary databases. Dataset preparation tests use mocked Hugging Face responses and do not download data.

Run the required quality checks:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy .
python -m compileall -q .
python -m pip check
```

## Database development

The default database path is `data/aegisai.db`. Use an isolated path while developing:

```powershell
aegisai --database-path .\data\dev.db conversations
```

`SQLiteStorage` creates parent directories and runs migrations during initialization. To add a schema change, append a new immutable `Migration` entry with a higher version; do not edit or drop an existing migration.

Useful direct checks:

```powershell
python -c "from storage import SQLiteStorage; s=SQLiteStorage('data/dev.db'); print(s.schema_version); s.close()"
```

All runtime values are bound as SQLite parameters. Do not interpolate user values into SQL.

## Dataset preparation

The public `OLMo-Coding/starcoder-python-instruct` dataset is prepared with the standard library; no paid API is used:

```powershell
python -m training.prepare_dataset
```

The default is one Python 3 JSONL source and at most 10,000 accepted records. The command validates the Hugging Face metadata, license, JSONL schema, duplicate IDs, and character limits. It writes SFT-style messages to `data/training/starcoder_python_instruct.jsonl` and a reproducibility manifest to `data/training/manifest.json`.

Use explicit limits for a larger local sample:

```powershell
python -m training.prepare_dataset --max-files 4 --max-records 50000
```

Setting `--max-records 0` removes the record cap. The source dataset is approximately 6.68GB, so review disk space before using that mode. This workflow prepares data only; it does not train or modify the Ollama model.

The current development machine exposes a 4GB GeForce 840M and CPU-only PyTorch. Do not attempt full 7B fine-tuning under a 3GB budget. A future training adapter must use a separately approved small model and parameter-efficient, resource-bounded workflow.

## Manual Ollama verification

Start Ollama and run:

```powershell
aegisai health
aegisai chat --new
aegisai chat --id <conversation_id> "Remember that my favorite color is blue."
aegisai chat --id <conversation_id> "What is my favorite color?"
aegisai conversation <conversation_id>
```

Run the calculator independently:

```powershell
aegisai tool calculator --expression "2 + 3 * 4"
```

The expected result is `14`. A tool requiring `FILESYSTEM_WRITE` is denied by the default policy. Constructing a policy that includes `PROCESS_EXECUTION` is rejected.

## Adding a safe tool

1. Implement `Tool` with explicit input and output schemas.
2. Choose the smallest capability that describes the operation.
3. Validate and constrain all input inside `execute`.
4. Register the tool explicitly in a `ToolRegistry`.
5. Add tests for success, invalid input, duplicate registration, and permission denial.
6. Do not add a generic shell, command, or unrestricted filesystem tool.

The agent exposes `execute_tool(ToolCall)` but does not autonomously call tools. Future orchestration can use that explicit boundary after adding a separate policy and planning phase.

## Security limitations

This phase provides capability declarations and checks, not a complete sandbox. SQLite files and conversation contents are local plaintext. Tool authors are responsible for resource limits, path restrictions, and safe handling of untrusted data. No `PROCESS_EXECUTION` capability can be enabled in Phase 1.
