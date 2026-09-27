from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.errors import AegisError
from data_pipeline.classify import classify_jsonl
from data_pipeline.common import SourceContext
from data_pipeline.config import PipelineConfig, PipelineError
from data_pipeline.deduplicate import deduplicate_jsonl
from data_pipeline.download import download_sources
from data_pipeline.filter import filter_jsonl
from data_pipeline.inspect import inspect_all
from data_pipeline.mixture import create_mixture
from data_pipeline.normalize import normalize_jsonl
from data_pipeline.prepare import prepare_sources
from data_pipeline.split import split_jsonl
from data_pipeline.statistics import calculate_statistics
from data_pipeline.validate import validate_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m data_pipeline.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect")
    _config_arg(inspect)
    inspect.add_argument("--output", type=Path, required=True)
    inspect.add_argument("--sample-limit", type=int, default=3)

    download = subparsers.add_parser("download")
    _config_arg(download)
    download.add_argument("--output", type=Path, default=Path("datasets/raw"))
    download.add_argument("--source", action="append", required=True)
    download.add_argument("--max-bytes", type=int)
    download.add_argument("--no-resume", action="store_true")

    validate = subparsers.add_parser("validate")
    _config_arg(validate)
    _input_arg(validate)
    validate.add_argument("--source", required=True)
    validate.add_argument("--format", default="jsonl")
    validate.add_argument("--report", type=Path, required=True)
    validate.add_argument("--rejects", type=Path, required=True)
    validate.add_argument("--max-records", type=int, default=0)
    validate.add_argument("--no-resume", action="store_true")

    normalize = subparsers.add_parser("normalize")
    _config_arg(normalize)
    _input_arg(normalize)
    normalize.add_argument("--output", type=Path, required=True)
    normalize.add_argument("--source", required=True)
    normalize.add_argument("--format", default="jsonl")
    normalize.add_argument("--license", default="unknown")
    normalize.add_argument("--revision", default="unknown")
    normalize.add_argument("--report", type=Path, required=True)
    normalize.add_argument("--rejects", type=Path, required=True)
    normalize.add_argument("--max-records", type=int, default=0)
    normalize.add_argument("--no-resume", action="store_true")

    deduplicate = subparsers.add_parser("deduplicate")
    _config_arg(deduplicate)
    _input_arg(deduplicate)
    deduplicate.add_argument("--output", type=Path, required=True)
    deduplicate.add_argument("--report", type=Path, required=True)
    deduplicate.add_argument("--duplicates", type=Path, required=True)
    deduplicate.add_argument("--near-duplicates", action="store_true")
    deduplicate.add_argument("--near-threshold", type=float, default=0.92)
    deduplicate.add_argument("--max-candidates", type=int, default=1000)
    deduplicate.add_argument("--max-records", type=int, default=0)
    deduplicate.add_argument("--no-resume", action="store_true")

    filter_parser = subparsers.add_parser("filter")
    _config_arg(filter_parser)
    _input_arg(filter_parser)
    filter_parser.add_argument("--output", type=Path, required=True)
    filter_parser.add_argument("--report", type=Path, required=True)
    filter_parser.add_argument("--rejects", type=Path, required=True)
    filter_parser.add_argument("--max-records", type=int, default=0)
    filter_parser.add_argument("--no-resume", action="store_true")

    classify = subparsers.add_parser("classify")
    _config_arg(classify)
    _input_arg(classify)
    classify.add_argument("--output", type=Path, required=True)
    classify.add_argument("--source", required=True)
    classify.add_argument("--subset", default="")
    classify.add_argument("--report", type=Path, required=True)
    classify.add_argument("--max-records", type=int, default=0)
    classify.add_argument("--no-resume", action="store_true")

    split = subparsers.add_parser("split")
    _config_arg(split)
    _input_arg(split)
    split.add_argument("--output-dir", type=Path, required=True)
    split.add_argument("--report", type=Path, required=True)
    split.add_argument("--train-ratio", type=float, default=0.8)
    split.add_argument("--validation-ratio", type=float, default=0.1)
    split.add_argument("--max-records", type=int, default=0)
    split.add_argument("--no-resume", action="store_true")

    statistics = subparsers.add_parser("statistics")
    _config_arg(statistics)
    _input_arg(statistics)
    statistics.add_argument("--report", type=Path, required=True)
    statistics.add_argument("--duplicates", type=Path)
    statistics.add_argument("--max-records", type=int, default=0)

    mixture = subparsers.add_parser("mixture")
    _config_arg(mixture)
    mixture.add_argument("--input", action="append", type=Path, required=True)
    mixture.add_argument("--output", type=Path, required=True)
    mixture.add_argument("--report", type=Path, required=True)
    mixture.add_argument("--max-records", type=int, default=0)

    prepare = subparsers.add_parser("prepare")
    _config_arg(prepare)
    prepare.add_argument("--source", action="append", required=True)
    prepare.add_argument("--input", action="append", required=True)
    prepare.add_argument("--max-records", type=int)
    prepare.add_argument("--near-duplicates", action="store_true")
    prepare.add_argument("--no-resume", action="store_true")
    return parser


def _config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))


def _input_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = PipelineConfig.load(args.config)
        config.ensure_directories()
        if args.command == "inspect":
            result: Any = inspect_all(
                config, args.output, sample_limit=args.sample_limit
            )
            result = result["summary"]
        elif args.command == "download":
            result = download_sources(
                config,
                args.output,
                source_names=args.source,
                max_bytes=args.max_bytes,
                resume=not args.no_resume,
            )
        elif args.command == "validate":
            result = validate_jsonl(
                args.input,
                source=args.source,
                source_format=args.format,
                report_path=args.report,
                rejects_path=args.rejects,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "normalize":
            context = SourceContext(
                source=args.source,
                license_name=args.license,
                revision=args.revision,
                file_name=str(args.input),
                format=args.format,
            )
            result = normalize_jsonl(
                args.input,
                args.output,
                context=context,
                report_path=args.report,
                rejects_path=args.rejects,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "deduplicate":
            result = deduplicate_jsonl(
                args.input,
                args.output,
                report_path=args.report,
                duplicates_path=args.duplicates,
                near_duplicates=args.near_duplicates,
                near_threshold=args.near_threshold,
                max_candidates=args.max_candidates,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "filter":
            result = filter_jsonl(
                args.input,
                args.output,
                report_path=args.report,
                rejects_path=args.rejects,
                limits=config.quality,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "classify":
            result = classify_jsonl(
                args.input,
                args.output,
                source=args.source,
                subset=args.subset,
                report_path=args.report,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "split":
            result = split_jsonl(
                args.input,
                args.output_dir,
                report_path=args.report,
                train_ratio=args.train_ratio,
                validation_ratio=args.validation_ratio,
                max_records=args.max_records,
                resume=not args.no_resume,
            ).to_dict()
        elif args.command == "statistics":
            result = calculate_statistics(
                args.input,
                report_path=args.report,
                duplicate_path=args.duplicates,
                max_records=args.max_records,
            )
        elif args.command == "mixture":
            result = create_mixture(
                args.input,
                args.output,
                weights=config.mixture,
                report_path=args.report,
                max_records=args.max_records,
            ).to_dict()
        else:
            if len(args.source) != len(args.input):
                parser.error("each --source requires one --input")
            result = prepare_sources(
                config,
                dict(zip(args.source, args.input)),
                max_records=args.max_records,
                resume=not args.no_resume,
                near_duplicates=args.near_duplicates,
            )
        print(json.dumps(result, indent=2))
        return 0
    except (AegisError, PipelineError, OSError, ValueError) as exc:
        print(f"Pipeline error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
