# Training Data Format

This document describes the data-only preparation target. It is not a training implementation and does not authorize automatic model changes.

## Normalized example

Every accepted record is JSONL with provenance:

```json
{
  "id": "source:original-id",
  "source": "dataset-or-configured-source",
  "task_type": "TOOL_USE",
  "messages": [
    {"role": "user", "content": "Goal"},
    {"role": "assistant", "content": "Next action", "tool_calls": []}
  ],
  "tool_calls": [],
  "tool_results": [],
  "metadata": {
    "source": "source",
    "source_file": "path",
    "row_number": 1,
    "revision": "revision",
    "license": "license",
    "source_alias": "source-alias",
    "input_sha256": "sha256",
    "raw_file_sha256": "sha256-or-unavailable",
    "config_sha256": "sha256",
    "agentic_flow": {
      "user_goal_present": true,
      "plan_evidence": false,
      "tool_selection_count": 0,
      "permission_check": "not_present_in_source",
      "tool_result_count": 0,
      "completion_evidence": true
    },
    "classification": {"label": "TOOL_USE", "evidence": ["tool_calls_field"]}
  },
  "license": "license",
  "quality_score": 0.95
}
```

`messages` preserves the source-supported conversation or action trajectory. `tool_calls` and `tool_results` are extracted only when present; the pipeline never invents a successful result or completion. Failed trajectories can be retained when their source provenance and failure structure are preserved.

## Task categories

The classifier can assign:

`CODING`, `DEBUGGING`, `INSTRUCTION_FOLLOWING`, `PLANNING`, `TOOL_USE`, `FUNCTION_CALLING`, `AGENT_TRAJECTORY`, `WEB_TASK`, `RESEARCH`, `FILE_OPERATIONS`, `DATA_PROCESSING`, `REASONING`, `BUSINESS_AUTOMATION`, `ERROR_RECOVERY`, `GENERAL_CONVERSATION`, and `SAFETY_AND_PERMISSIONS`.

A source that does not support a category remains unclassified until evidence is present. Source subset names and explicit tool/message fields are retained as evidence. This avoids turning every code or conversation example into an unsupported business or autonomy label.

## Quality policy

The default filter removes structural failures and flags review items:

- non-object, empty, or empty-user examples
- malformed roles, content, tool calls, or tool arguments
- empty assistant messages without tool calls
- configured character-limit violations across message and tool payloads
- configured quality-score thresholds
- non-blocking sensitive-data review flags

Unusual, difficult, failed, or long examples are flagged rather than aggressively discarded. Every reject file contains a reason and source row. The final mixture is made only after stage statistics are available.

## Mixtures

`configs/dataset.toml` contains a provisional category mixture. It is intentionally not a permanent equal-source blend:

```text
coding              0.20
tool_use            0.20
agent_trajectories  0.05
web_task            0.15
planning            0.10
debugging           0.10
error_recovery      0.10
reasoning           0.05
business_automation 0.05
```

Change the weights after reviewing category counts, source overlap, licenses, and failure rates. Unclassified examples are reported and are not silently assigned to a business category. The global deduplicated pool is mixed deterministically and then split into `datasets/processed/mixture-splits/`; the original source-specific splits remain available for comparison.

## Business capability boundary

The corpus is intended to support legitimate software development, research, data processing, report generation, business intelligence, document processing, customer-support automation, and internal workflow assistance. It must not be used to teach fraud, spam, credential theft, impersonation, unauthorized access, financial manipulation, or guaranteed-money claims.

## Hardware boundary

The available machine has a 4GB GeForce 840M and CPU-only PyTorch. This repository does not attempt 7B pretraining or fine-tuning. The immediate deliverable is a reproducible corpus with manifests, hashes, statistics, provenance, and explicit quality decisions. Any later model-training decision requires a separate resource review and user approval.
