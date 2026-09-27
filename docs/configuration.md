# Configuration

AEGISAI reads configuration in this order:

1. Built-in defaults
2. Optional TOML file passed with `--config`
3. Environment variables

The last source wins for each field. Unknown TOML keys and invalid URLs or log levels fail early with a clear configuration error.

## TOML

The file format is flat and uses the public field names:

```toml
application_name = "AEGISAI"
environment = "development"
log_level = "INFO"
inference_provider = "ollama"
model_name = "qwen2.5-coder:7b"
ollama_base_url = "http://127.0.0.1:11434"
database_path = "data/aegisai.db"
```

Run with the file explicitly:

```powershell
aegisai --config .\configs\default.toml "Hello"
```

The bundled `configs/default.toml` is a readable reference; the application defaults do not depend on loading that file.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `AEGISAI_APPLICATION_NAME` | Application name used in metadata and logs |
| `AEGISAI_ENVIRONMENT` | Deployment environment label |
| `AEGISAI_LOG_LEVEL` | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`, or `NOTSET` |
| `AEGISAI_INFERENCE_PROVIDER` | Provider name; Phase 1 implements `ollama` |
| `AEGISAI_MODEL_NAME` | Ollama model tag |
| `AEGISAI_OLLAMA_BASE_URL` | Ollama server root URL |
| `AEGISAI_DATABASE_PATH` | SQLite database path |

`OLLAMA_BASE_URL` is accepted as an alias for `AEGISAI_OLLAMA_BASE_URL`. The explicit AEGISAI-prefixed variable takes precedence.

## Command-line overrides

The CLI can override environment, log level, provider, model, Ollama URL, and database path without changing the process environment:

```powershell
aegisai --environment test --log-level DEBUG --model llama3.2:3b "Hello"
aegisai chat --new --database-path .\data\chat.db
```

## Database path

SQLite storage creates the database and parent directories automatically. The same path can be reused by a later CLI process; initialization applies pending migrations without destroying existing conversations.
