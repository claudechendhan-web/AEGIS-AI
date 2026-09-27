# Architecture

## Design goals

AEGISAI remains synchronous, local-first, and replaceable. The runtime depends on the `InferenceProvider` interface rather than on Ollama-specific request logic. Storage and tools have their own abstractions so SQLite and individual capabilities do not leak into the agent or CLI layers.

## Module boundaries

### `core`

`core.config.Settings` is the central configuration object. `core.models` contains role-aware messages, conversations, requests, responses, and events. Conversations expose both the existing `conversation_id` field and an `id` convenience property. `core.errors` contains explicit configuration, inference, database, conversation, role, tool, and permission errors.

### `storage`

`storage.base.ConversationRepository` defines conversation and ordered-message operations without importing `sqlite3`. `storage.sqlite.SQLiteStorage` owns the connection and applies migrations automatically. `storage.repositories.SQLiteConversationRepository` contains parameterized SQLite operations and maps rows back to core models.

The initial migration creates `conversations` and `messages`, enables foreign-key cascade behavior through the connection, and records `PRAGMA user_version`. Initialization is safe to call repeatedly and does not drop or recreate existing data.

### `inference`

`inference.base.InferenceProvider` defines synchronous `generate` and `health_check` operations. `inference.ollama.OllamaProvider` continues to use the local `/api/generate` and `/api/tags` endpoints. When a request has prior messages, the provider formats the role-aware transcript into the prompt while preserving the current one-shot behavior.

### `agents` and `runtime`

`agents.agent.Agent` is a non-autonomous provider adapter. It also exposes an explicit `execute_tool(ToolCall)` boundary for future orchestration without allowing the model to trigger tools implicitly.

`runtime.runtime.Runtime` owns the persistent request lifecycle. It creates or loads a conversation, appends and persists the user message, passes the loaded history to the agent/provider, appends the assistant message, and persists the response. Conversation creation, lookup, listing, and deletion are exposed through the same repository contract.

### `tools`

`tools.base` defines `Tool`, `ToolResult`, `ToolCall`, and a small JSON-schema subset validator. `tools.registry.ToolRegistry` performs lookup, duplicate detection, requested-tool validation, permission checks, and output validation. `tools.calculator.CalculatorTool` uses an AST whitelist for integer and floating-point arithmetic.

### `permissions`

`tools.permissions.PermissionPolicy` uses explicit capabilities. The default policy allows `READ_ONLY` only. `PROCESS_EXECUTION` is rejected even if a caller attempts to grant it.

### `training`

`training.prepare_dataset` is an offline data-preparation boundary. It reads a bounded public Hugging Face JSONL source using the standard library, validates dataset metadata and records, and writes SFT-style messages plus a manifest. It does not import model-training frameworks or alter the inference provider.


`observability.logging` emits JSON records. The CLI keeps machine-readable responses on stdout and diagnostics on stderr.

## Conversation lifecycle

1. The CLI loads settings and opens the configured SQLite storage.
2. `chat --new` creates a UUID-backed conversation and prints its ID.
3. `chat --id` retrieves the conversation and all messages in append order.
4. Runtime appends the new user message and commits it before inference.
5. The provider receives the prior transcript plus the current user message.
6. Runtime adds the assistant message, with provider/model metadata, and commits it.
7. Listing and inspection commands read the persisted state.
8. Deletion removes the conversation and cascades to its messages.

If inference fails after the user message is committed, the user message remains available for retry and inspection.

## Tool execution lifecycle

```text
ToolCall
    ↓
ToolRegistry.find
    ↓
PermissionPolicy.require
    ↓
Input schema validation
    ↓
Tool.execute
    ↓
Output schema validation
    ↓
ToolResult
```

This sequence is explicit. The runtime does not ask the model to choose tools, and no tool can execute an arbitrary shell command.

## Failure handling

Configuration and database errors are raised before or during persistence. Missing conversations raise `ConversationNotFoundError`; invalid roles raise `InvalidMessageRoleError`; duplicate, unknown, denied, and invalid tool operations have dedicated exceptions. Provider connection and response errors remain `ProviderError` subclasses and are also `InferenceFailure` subclasses. The CLI converts expected errors into concise messages and non-zero exit codes without normal-user stack traces.

## Extension path

A future storage backend implements `ConversationRepository`. A future provider implements `InferenceProvider`. A future safe tool implements `Tool`, declares its schemas and capability, and is explicitly registered. None of those additions requires autonomous behavior or unrestricted execution.
