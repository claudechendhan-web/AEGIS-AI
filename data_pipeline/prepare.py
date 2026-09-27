from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.errors import AegisError
from data_pipeline.classify import classify_jsonl
from data_pipeline.common import (
    PipelineError,
    SourceContext,
    _replace_path,
    iter_jsonl,
    relative_or_absolute,
    sha256_file,
    write_json,
)
from data_pipeline.config import PipelineConfig
from data_pipeline.deduplicate import deduplicate_jsonl
from data_pipeline.filter import filter_jsonl
from data_pipeline.mixture import create_mixture
from data_pipeline.normalize import normalize_jsonl
from data_pipeline.split import split_jsonl
from data_pipeline.statistics import calculate_statistics
from data_pipeline.validate import validate_jsonl


def prepare_sources(
    config: PipelineConfig,
    inputs: Mapping[str, str | Path],
    *,
    max_records: int | None = None,
    resume: bool = True,
    near_duplicates: bool = False,
) -> dict[str, Any]:
    config.ensure_directories()
    limit = _record_limit(config, max_records)
    config_sha256 = sha256_file(config.config_path)
    near_threshold = float(config.limits.get("near_duplicate_threshold", 0.92))
    near_enabled = near_duplicates or bool(config.quality.get("near_duplicates", False))
    source_reports: dict[str, Any] = {}
    mixture_inputs: list[Path] = []
    for name, input_value in inputs.items():
        source = config.source(name)
        if not source.enabled:
            raise PipelineError(f"source is disabled by configuration: {name}")
        input_path = Path(input_value)
        if not input_path.exists():
            raise PipelineError(f"input does not exist: {input_path}")
        subset = _infer_subset(input_path)
        source_root = config.processed / name
        staging_root = config.staging / name
        manifest_root = config.manifests / name
        source_root.mkdir(parents=True, exist_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        manifest_root.mkdir(parents=True, exist_ok=True)
        revision = _observed_revision(config, source.source_id)
        input_sha256 = sha256_file(input_path)
        raw_file_sha256 = (
            input_sha256 if _is_within(input_path, config.raw) else "unavailable"
        )
        source_context = SourceContext(
            source=source.source_id,
            license_name=source.license_name,
            revision=revision,
            file_name=str(input_path),
            format=source.dataset_format,
            extra={
                "subset": subset,
                "source_alias": name,
                "input_sha256": input_sha256,
                "raw_file_sha256": raw_file_sha256,
                "config_sha256": config_sha256,
            },
        )
        validation_report = validate_jsonl(
            input_path,
            source=source.source_id,
            source_format=source.dataset_format,
            report_path=manifest_root / "validation.json",
            rejects_path=staging_root / "validation-rejects.jsonl",
            max_records=limit,
            resume=resume,
        )
        normalized_path = staging_root / "normalized.jsonl"
        normalization_report = normalize_jsonl(
            input_path,
            normalized_path,
            context=source_context,
            report_path=manifest_root / "normalization.json",
            rejects_path=staging_root / "normalization-rejects.jsonl",
            max_records=limit,
            resume=resume,
        )
        classified_path = staging_root / "classified.jsonl"
        classification_report = classify_jsonl(
            normalized_path,
            classified_path,
            source=name,
            subset=subset,
            report_path=manifest_root / "classification.json",
            max_records=limit,
            resume=resume,
        )
        filtered_path = staging_root / "filtered.jsonl"
        filter_report = filter_jsonl(
            classified_path,
            filtered_path,
            report_path=manifest_root / "filter.json",
            rejects_path=staging_root / "filter-rejects.jsonl",
            limits=config.quality,
            max_records=limit,
            resume=resume,
        )
        deduplicated_path = source_root / "deduplicated.jsonl"
        duplicate_report = deduplicate_jsonl(
            filtered_path,
            deduplicated_path,
            report_path=manifest_root / "deduplicate.json",
            duplicates_path=staging_root / "duplicates.jsonl",
            near_duplicates=near_enabled,
            near_threshold=near_threshold,
            max_records=limit,
            resume=resume,
        )
        split_report = split_jsonl(
            deduplicated_path,
            source_root / "splits",
            report_path=manifest_root / "split.json",
            train_ratio=float(config.limits.get("train_ratio", 0.8)),
            validation_ratio=float(config.limits.get("validation_ratio", 0.1)),
            max_records=limit,
            resume=resume,
        )
        statistics = calculate_statistics(
            deduplicated_path,
            report_path=manifest_root / "statistics.json",
            duplicate_path=staging_root / "duplicates.jsonl",
            max_records=limit,
        )
        mixture_inputs.append(deduplicated_path)
        source_reports[name] = {
            "source_id": source.source_id,
            "input": str(input_path),
            "subset": subset,
            "revision": revision,
            "license": source.license_name,
            "input_sha256": input_sha256,
            "raw_file_sha256": raw_file_sha256,
            "validation": validation_report.to_dict(),
            "normalization": normalization_report.to_dict(),
            "classification": classification_report.to_dict(),
            "filter": filter_report.to_dict(),
            "deduplicate": duplicate_report.to_dict(),
            "split": split_report.to_dict(),
            "statistics": statistics,
        }
    global_input_path = config.staging / "global-input.jsonl"
    global_input_count = _write_round_robin_input(mixture_inputs, global_input_path)
    global_deduplicated_path = config.processed / "global-deduplicated.jsonl"
    global_duplicates_path = config.staging / "global-duplicates.jsonl"
    global_duplicate_report = deduplicate_jsonl(
        global_input_path,
        global_deduplicated_path,
        report_path=config.manifests / "global-deduplicate.json",
        duplicates_path=global_duplicates_path,
        near_duplicates=near_enabled,
        near_threshold=near_threshold,
        max_records=0,
        resume=resume,
    )
    global_statistics = calculate_statistics(
        global_deduplicated_path,
        report_path=config.manifests / "global-statistics.json",
        duplicate_path=global_duplicates_path,
    )
    mixture_path = config.processed / "mixture.jsonl"
    mixture_report = create_mixture(
        [global_deduplicated_path],
        mixture_path,
        weights=config.mixture,
        report_path=config.manifests / "mixture.json",
        max_records=limit,
    )
    mixture_split_report = split_jsonl(
        mixture_path,
        config.processed / "mixture-splits",
        report_path=config.manifests / "mixture-split.json",
        train_ratio=float(config.limits.get("train_ratio", 0.8)),
        validation_ratio=float(config.limits.get("validation_ratio", 0.1)),
        max_records=limit,
        resume=resume,
    )
    final_statistics = calculate_statistics(
        mixture_path,
        report_path=config.manifests / "final-statistics.json",
        duplicate_path=global_duplicates_path,
    )
    final_statistics["duplicates_removed_before_mixture"] = global_statistics[
        "duplicates"
    ]
    run_manifest_path = config.manifests / "prepare-run.json"
    run_manifest = {
        "config_path": str(config.config_path),
        "config_sha256": config_sha256,
        "sources": source_reports,
        "global_deduplicate": global_duplicate_report.to_dict(),
        "global_input_records": global_input_count,
        "mixture": mixture_report.to_dict(),
        "mixture_split": mixture_split_report.to_dict(),
        "outputs": {
            "global_input": {
                "path": str(global_input_path),
                "records": global_input_count,
                "sha256": sha256_file(global_input_path),
            },
            "global_deduplicated": {
                "path": str(global_deduplicated_path),
                "sha256": sha256_file(global_deduplicated_path),
            },
            "mixture": {
                "path": str(mixture_path),
                "sha256": sha256_file(mixture_path),
            },
        },
    }
    write_json(run_manifest_path, run_manifest)
    return {
        "sources": source_reports,
        "global_deduplicate": global_duplicate_report.to_dict(),
        "global_statistics": global_statistics,
        "mixture": mixture_report.to_dict(),
        "mixture_split": mixture_split_report.to_dict(),
        "final_statistics": final_statistics,
        "run_manifest": relative_or_absolute(run_manifest_path),
        "output": relative_or_absolute(mixture_path),
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _write_round_robin_input(paths: list[Path], output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    iterators = [iter_jsonl(path) for path in paths]
    count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        while iterators:
            remaining = []
            for iterator in iterators:
                try:
                    _, line = next(iterator)
                except StopIteration:
                    continue
                stream.write(line if line.endswith("\n") else f"{line}\n")
                count += 1
                remaining.append(iterator)
            iterators = remaining
    _replace_path(temporary, output)
    return count


def _observed_revision(config: PipelineConfig, source_id: str) -> str:
    inventory_path = config.manifests / "inventory.json"
    if not inventory_path.exists():
        return "unknown"
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"
    if not isinstance(payload, Mapping):
        return "unknown"
    datasets = payload.get("datasets", [])
    if not isinstance(datasets, list):
        return "unknown"
    for item in datasets:
        if not isinstance(item, Mapping) or item.get("dataset_id") != source_id:
            continue
        revision = item.get("revision")
        if isinstance(revision, str) and revision.strip():
            return revision
    return "unknown"


def _record_limit(config: PipelineConfig, max_records: int | None) -> int:
    if max_records is not None:
        if max_records < 0:
            raise PipelineError("max_records must be zero or greater")
        return max_records
    value = config.limits.get("max_records", 10_000)
    if not isinstance(value, int) or value < 0:
        raise PipelineError("limits.max_records must be a non-negative integer")
    return value


def _infer_subset(path: Path) -> str:
    parts = path.parts
    for part in reversed(parts[:-1]):
        if part not in {"raw", "staging", "processed", "datasets"}:
            return part
    return path.stem


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bounded data preparation pipeline"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--near-duplicates", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if len(args.source) != len(args.input):
        print("Each --source requires one --input", file=sys.stderr)
        return 2
    try:
        config = PipelineConfig.load(args.config)
        payload = prepare_sources(
            config,
            dict(zip(args.source, args.input)),
            max_records=args.max_records,
            resume=not args.no_resume,
            near_duplicates=args.near_duplicates,
        )
        print(json.dumps(payload, indent=2))
        return 0
    except (AegisError, PipelineError, OSError, ValueError) as exc:
        print(f"Preparation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
