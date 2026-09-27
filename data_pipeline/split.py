from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from data_pipeline.common import (
    Checkpoint,
    PipelineReport,
    canonical_json,
    iter_jsonl,
    load_json_line,
    remaining_record_limit,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


def split_jsonl(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    report_path: str | Path,
    train_ratio: float = 0.8,
    validation_ratio: float = 0.1,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if not 0.0 < train_ratio < 1.0:
        raise PipelineError("train_ratio must be between zero and one")
    if not 0.0 <= validation_ratio < 1.0:
        raise PipelineError("validation_ratio must be between zero and one")
    if train_ratio + validation_ratio >= 1.0:
        raise PipelineError("train and validation ratios must leave a test partition")
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    input_file = Path(input_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "train": destination / "train.jsonl",
        "validation": destination / "validation.jsonl",
        "test": destination / "test.jsonl",
    }
    checkpoint = Checkpoint.load(Path(report_path).with_suffix(".checkpoint.json"))
    start_line = checkpoint.get_int("last_line") if resume else 0
    if not resume:
        checkpoint.state = {}
    record_limit = remaining_record_limit(max_records, start_line)
    report = PipelineReport(
        stage="split",
        input_path=str(input_file),
        output_path=str(destination),
    )
    streams = {
        name: path.open("a" if resume else "w", encoding="utf-8", newline="\n")
        for name, path in paths.items()
    }
    try:
        for line_number, line in iter_jsonl(input_file, start_line=start_line):
            if record_limit == 0:
                break
            report.increment("records_seen")
            try:
                record = load_json_line(line)
            except PipelineError:
                report.increment("malformed")
                report.errors.append(f"line {line_number}: malformed JSON record")
                checkpoint.state = {"last_line": line_number}
                checkpoint.save()
                if (
                    record_limit is not None
                    and report.counters.get("records_seen", 0) >= record_limit
                ):
                    break
                continue
            if not isinstance(record, dict):
                report.increment("rejected")
                report.increment("not_object")
            else:
                partition = _partition(record, train_ratio, validation_ratio)
                streams[partition].write(canonical_json(record))
                streams[partition].write("\n")
                report.increment(partition)
            for stream in streams.values():
                stream.flush()
            checkpoint.state = {"last_line": line_number}
            checkpoint.save()
            if (
                record_limit is not None
                and report.counters.get("records_seen", 0) >= record_limit
            ):
                break
    finally:
        for stream in streams.values():
            stream.close()
    report_payload = report.finish().to_dict()
    report_payload["partitions"] = {name: str(path) for name, path in paths.items()}
    write_json(report_path, report_payload)
    return report


def _partition(
    record: dict[str, Any], train_ratio: float, validation_ratio: float
) -> str:
    identifier = str(record.get("id", canonical_json(record)))
    bucket = (
        int(hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
    )
    if bucket < train_ratio:
        return "train"
    if bucket < train_ratio + validation_ratio:
        return "validation"
    return "test"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministically split normalized JSONL"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--train-ratio", type=float, default=0.8)
    parser.add_argument("--validation-ratio", type=float, default=0.1)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        report = split_jsonl(
            args.input,
            args.output_dir,
            report_path=args.report,
            train_ratio=args.train_ratio,
            validation_ratio=args.validation_ratio,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Split error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
