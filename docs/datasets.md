# Dataset Inventory

This inventory was inspected from the public Hugging Face dataset pages and APIs on 2026-09-25. Dataset revisions and sizes can change; the pipeline records the observed revision and file metadata in manifests.

## Sources

| Source | License | Observed size | Rows/splits | Primary format | Fields and quality notes |
| --- | --- | ---: | --- | --- | --- |
| `OLMo-Coding/starcoder-python-instruct` | Apache-2.0 dataset declaration; source-code terms still require review | ~6.68GB | ~1,540,220 rows in the viewer | JSONL shards | `instruction`, `text`, `id`, `metadata`, `added`, `created`, `source`; synthetic instructions paired with Python code, including Python 2/3 provenance. No native tool-call trajectory. |
| `Toprak1yu/agent-tool-use-trajectories` | Apache-2.0 | ~6.07MB | 10,000 `train` rows | Parquet | One `messages` list; ChatML system/user/assistant/tool turns, `tool_calls`, tool results, self-correction and error scenarios across SWE, database, web, and system domains. |
| `nvidia/Nemotron-Agentic-v1` | CC BY 4.0; the card also mentions Apache-2.0 material from a source dataset | ~5.5GB (API storage ~5.79GB) | `interactive_agent`: 19,028; `tool_calling`: 316,094 | JSONL | `uuid`, `messages`, `license`, `used_in`, `tools`, `reasoning`; synthetic multi-role trajectories and general function-calling data. The dataset viewer currently reports a schema-cast error, so raw JSONL inspection is required. |
| `neulab/agent-data-collection` | Per-subset licenses; see each subset `LICENSE` | ~499GB total | 18 declared configurations, 1M–10M category | JSONL `raw`, `std`, and SFT files | Collection-level viewer has a mixed-schema error. Use one representation per subset, normally `full_std.jsonl`, and retain subset provenance. |

## StarCoder schema

Each JSONL record has one of the observed instruction/code shapes:

- `instruction` plus `text`: a synthetic natural-language coding request and corresponding Python code
- `messages`: user/assistant message pairs (the bounded sample used by this repository has this shape)
- `id`: source record identifier
- `metadata`: extension, repository name/path, stars, and provenance
- `added`, `created`: timestamps where present
- `source`: source dataset label

Recommended use: coding and instruction-following. Do not infer tool execution, planning, or successful software outcomes from a code pair.

## Toprak schema

Each Parquet row contains a `messages` list. Messages can include:

- `system` tool declarations
- `user` goals
- `assistant` reasoning/content
- assistant `tool_calls` with name and arguments
- `tool` observations/results

Recommended use: function calling, tool use, multi-step trajectories, and recovery examples. Tool names include database, web, filesystem, and shell-like examples; examples must remain training data only and must not be executed by the pipeline.

## Nemotron schema

The raw JSONL files are split into `interactive_agent` and `tool_calling`. Records contain `uuid`, `messages`, `license`, `used_in`, `tools`, and `reasoning` metadata. Messages represent user goals, assistant decisions, function calls, tool observations, and final responses.

The dataset-server viewer currently fails while casting nested function schema fields. This is a source-viewer issue, not a reason to download the full 5.5GB blindly. The bounded downloader can fetch only the interactive split or a user-selected file.

## NeuLab subsets

The collection exposes standardized `full_std.jsonl` files and framework-specific SFT files. Relevant observed subsets include:

| Subset | Focus | License | `full_std.jsonl` size | Default use |
| --- | --- | --- | ---: | --- |
| `agenttuning_mind2web` | Small web-agent subset | Apache-2.0 | 172,682 bytes | Recommended for a first bounded browser sample |
| `codeactinstruct` | Code-as-action and execution/refinement trajectories | Apache-2.0 | 29,780,924 bytes | Recommended for code/tool interaction |
| `openhands` | Recorded coding, tool, and web trajectories with feedback | MIT | 43,712,850 bytes | Recommended for computer/tool interaction |
| `nebius_SWE-agent-trajectories` | Real SWE-agent issue-resolution trajectories | CC BY 4.0 | 604,745,670 bytes | Explicitly inspect before bounded download |
| `swe-smith` | Synthesized bug-fix trajectories | MIT | 1,504,520,320 bytes | Large software subset; not default |
| `swe-gym_openhands_sampled_trajectories` | Real GitHub software tasks | MIT | 158,971,576 bytes | Useful recovery sample; not default |
| `code_feedback` | Execution feedback and iterative code refinement | Apache-2.0 | 377,458,992 bytes | Large coding subset; not default |
| `mind2web` | Real-world web demonstrations | CC BY 4.0 | 3,221,701,753 bytes | Large browser subset; not default |
| `go-browse-wa` | Structured web exploration | MIT | 571,448,281 bytes | Large browser subset; not default |
| `nnetnav-wa` | WebArena exploration | Apache-2.0 | 517,088,759 bytes | Large browser subset; not default |
| `nnetnav-live` | Live web exploration | Apache-2.0 | 1,029,443,160 bytes | Large browser subset; not default |

The complete observed NeuLab configuration list is: `agenttuning_alfworld`, `agenttuning_db`, `agenttuning_kg`, `agenttuning_mind2web`, `agenttuning_os`, `agenttuning_webshop`, `code_feedback`, `codeactinstruct`, `go-browse-wa`, `mind2web`, `nebius_SWE-agent-trajectories`, `nnetnav-live`, `nnetnav-wa`, `openhands`, `orca_agentinstruct`, `swe-gym_openhands_sampled_trajectories`, `swe-smith`, and `synatra`. Most expose `raw`, `std`, and framework-specific SFT representations; the inventory manifest records the observed file paths and sizes.

The configuration exposes `neulab_mind2web`, `neulab_openhands`, and `neulab_codeact` as independently bounded aliases for the first three recommended files. The aggregate `neulab` source is used for bounded downloading, while the aliases are used for independent preparation and reporting.

## Overlap and merge policy

Do not merge `raw`, `std`, and multiple SFT views of the same NeuLab subset; they are alternate representations and will inflate counts. Toprak, Nemotron, and NeuLab all contain tool-agent trajectories and may overlap in style or source material. StarCoder and NeuLab code subsets may overlap in code provenance. The pipeline records source alias, revision, file, row, license, raw-file hash, and config hash for every example, then performs per-source and global content/normalized-text deduplication before mixture creation.

The default mixture is a provisional starting point, not an equal four-source blend:

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

Change `configs/dataset.toml` after reviewing statistics. Do not use the NeuLab collection as an unqualified source of “all agent data.”

## Licensing and safety

Dataset-card licenses do not automatically clear every underlying code sample, repository, or embedded tool output. Preserve `LICENSE` files and source metadata, review redistribution terms, and do not execute downloaded code or shell-like examples. AEGISAI's data pipeline is local preparation only; it does not train, deploy, or autonomously execute any example.
