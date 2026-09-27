# Data Pipeline

The pipeline is independent from the AEGISAI runtime. It prepares data for a future model-training decision without changing Ollama, the agent runtime, or the tool permission model.

## Directory contract

```text
datasets/
├── raw/         Original downloaded files; never modified
├── staging/     Validated, normalized, classified, and rejected records
├── processed/   Filtered, deduplicated, split, and mixed outputs
└── manifests/   Inventory, checkpoints, hashes, reports, and statistics
```

`data_pipeline` uses JSONL streaming by default. Toprak's small Parquet file can be read when the optional `data` extra is installed; its Arrow `messages` values may be JSON-encoded strings and are decoded during validation and normalization. StarCoder message-style records and instruction/text records are both supported.

```powershell
python -m pip install -e ".[data]"
```

## Stages

```text
inspect → download → validate → normalize → classify → filter
        → per-source deduplicate → global deduplicate → split
        → mixture → final split → statistics
```

- `inspect` queries metadata, licenses, splits, files, sizes, and bounded samples.
- `download` requires explicit source selection, uses the inspected revision when available, performs resumable HTTP downloads with byte limits, and records SHA-256 hashes.
- `validate` continues past malformed individual records and writes rejects.
- `normalize` preserves provenance and converts supported source formats to the common schema.
- `classify` assigns only evidence-backed task categories and records evidence.
- `filter` applies structural and configurable quality checks.
- `deduplicate` uses disk-backed SQLite exact/content hashes, normalized-text hashes, and optional bounded near-duplicate checks.
- `split` assigns deterministic hash-based train/validation/test partitions.
- `mixture` applies configurable category weights after statistics are available.
- `statistics` reports counts, categories, tool names, quality flags, token estimates, disk usage, and speed.

Normalized records retain source alias, local file, row number, observed revision, raw-file SHA-256, config SHA-256, license, and an `agentic_flow` evidence summary. The flow summary records what the source supports without inventing permission decisions or successful outcomes.

## Reproduce inspection

```powershell
python -m data_pipeline.cli inspect --config configs/dataset.toml --output datasets/manifests/inventory.json
```

## Download bounded files

```powershell
python -m data_pipeline.cli download --config configs/dataset.toml --source toprak --source neulab --max-bytes 80000000
```

Raw files are stored under `datasets/raw/<configured-source>/`. The `neulab_mind2web`, `neulab_openhands`, and `neulab_codeact` aliases make the three selected files independently preparable; large SWE, browser, and full Mind2Web subsets remain disabled and require explicit configuration changes and review.

## Run a source

```powershell
python -m data_pipeline.cli prepare `
  --config configs/dataset.toml `
  --source toprak `
  --input datasets/raw/toprak/data/train-00000-of-00001.parquet `
  --max-records 10000
```

The command runs validation, normalization, classification, filtering, per-source deduplication, global cross-source deduplication, splitting, mixture preparation, and statistics. Transform stages write checkpoints; the final mixture is deterministically rerunnable. `--max-records` is a total cap across resumed invocations for a stage; re-running with a larger cap or no cap resumes rather than duplicating records. Use `--no-resume` for a clean stage.

For the bounded cross-source sample used in this repository, pass one `--source`/`--input` pair per file:

```powershell
python -m data_pipeline.cli prepare `
  --config configs/dataset.toml `
  --source toprak --input datasets/raw/toprak/data/train-00000-of-00001.parquet `
  --source neulab_mind2web --input datasets/raw/neulab/agenttuning_mind2web/full_std.jsonl `
  --source neulab_openhands --input datasets/raw/neulab/openhands/full_std.jsonl `
  --source neulab_codeact --input datasets/raw/neulab/codeactinstruct/full_std.jsonl `
  --source starcoder --input data/training/starcoder_python_instruct.jsonl `
  --max-records 500 --no-resume
```

## Run stages independently

```powershell
python -m data_pipeline.cli validate --input file.jsonl --source source-id --report report.json --rejects rejects.jsonl
python -m data_pipeline.cli normalize --input file.jsonl --output normalized.jsonl --source source-id --license apache-2.0 --report report.json --rejects rejects.jsonl
python -m data_pipeline.cli classify --input normalized.jsonl --output classified.jsonl --source source-id --subset subset-name --report report.json
python -m data_pipeline.cli deduplicate --input classified.jsonl --output unique.jsonl --report report.json --duplicates duplicates.jsonl
python -m data_pipeline.cli filter --input unique.jsonl --output filtered.jsonl --report report.json --rejects rejects.jsonl
python -m data_pipeline.cli split --input filtered.jsonl --output-dir splits --report report.json
python -m data_pipeline.cli statistics --input filtered.jsonl --report statistics.json
```

## Resumability and resource behavior

Transformation stages append records, update line-number checkpoints, flush output before checkpointing, and report malformed records instead of aborting the entire file. Deduplication state is disk-backed in SQLite, so large inputs are not loaded into one in-memory collection. Global deduplication receives a round-robin stream of source outputs so source order does not bias the final mixture. The pipeline reports bytes, processing time, records per second, quality flags, and estimated tokens. Raw files are never rewritten.

Near-duplicate detection is optional and bounded by candidate count and a similarity threshold. Exact duplicate detection is always available. The pipeline commits deduplication state before advancing checkpoints and interleaves source files deterministically during mixture creation. It does not execute code, shell commands, URLs, or tool calls found in the data.

## Observed bounded run

The current manifests were produced from four downloaded files totaling 79,732,939 bytes. The per-source caps are samples, not estimates of the full source datasets:

| Source | Seen | Accepted | Rejected | Duplicates | Categories | Estimated tokens | Tool calls |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| Toprak | 500 | 500 | 0 | 0 | `TOOL_USE`: 500 | 44,720 | 965 |
| NeuLab Mind2Web | 118 | 118 | 0 | 0 | `WEB_TASK`: 118 | 34,624 | 0 |
| NeuLab OpenHands | 127 | 101 | 26 (`too_long`: 24; `empty_user`: 2) | 0 | `CODING`: 36; `ERROR_RECOVERY`: 62; `UNCLASSIFIED`: 3 | 1,284,090 | 1,332 |
| NeuLab CodeAct | 500 | 481 | 19 (`empty_assistant`) | 0 | `CODING`: 385; `ERROR_RECOVERY`: 96 | 391,639 | 1,264 |
| StarCoder sample | 500 | 500 | 0 | 0 | `CODING`: 500 | 425,299 | 0 |

The global deduplicated pool contains 1,700 accepted records and removes 0 cross-source duplicates in this sample. The resulting `datasets/processed/mixture.jsonl` contains 500 accepted records: 275 coding, 100 tool-use, 75 web-task, and 50 error-recovery; estimated size is 1,076,441 tokens with 1,458 tool calls. Its source counts are 130 StarCoder, 100 Toprak, and 270 NeuLab records. The final mixture split is 405 train, 45 validation, and 50 test; 82 records carry a non-blocking `sensitive_data_review` flag. Exact hashes, source metadata, timings, disk usage, and stage counters are in `datasets/manifests/`.

The run manifest is `datasets/manifests/prepare-run.json`; it records the config hash, source revisions, raw hashes, stage counters, global duplicate result, and processed-output hashes. The final outputs are `datasets/processed/global-deduplicated.jsonl`, `datasets/processed/mixture.jsonl`, and `datasets/processed/mixture-splits/`.
