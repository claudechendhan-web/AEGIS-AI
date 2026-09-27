from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_pipeline.common import (
    canonical_json,
    directory_size,
    iter_jsonl,
    load_json_line,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


def calculate_statistics(
    input_path: str | Path,
    *,
    report_path: str | Path,
    duplicate_path: str | Path | None = None,
    max_records: int = 0,
) -> dict[str, Any]:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    started = time.perf_counter()
    counters: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    tool_names: Counter[str] = Counter()
    quality_total = 0.0
    quality_count = 0
    quality_flags: Counter[str] = Counter()
    for line_number, line in iter_jsonl(input_path):
        if max_records and counters["records_seen"] >= max_records:
            break
        counters["records_seen"] += 1
        try:
            record = load_json_line(line)
        except PipelineError:
            counters["malformed"] += 1
            continue
        if not isinstance(record, Mapping):
            counters["malformed"] += 1
            continue
        counters["accepted"] += 1
        sources[str(record.get("source", "unknown"))] += 1
        category = record.get("task_type")
        categories[str(category) if category else "UNCLASSIFIED"] += 1
        quality = record.get("quality_score")
        if isinstance(quality, (int, float)):
            quality_total += float(quality)
            quality_count += 1
        flags = record.get("quality_flags", [])
        if isinstance(flags, list):
            quality_flags.update(str(flag) for flag in flags)
        messages = record.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, Mapping):
                    roles[str(message.get("role", "unknown"))] += 1
                    content = message.get("content")
                    if isinstance(content, str):
                        counters["characters"] += len(content)
                    calls = message.get("tool_calls", [])
                    if isinstance(calls, list):
                        counters["tool_calls"] += len(calls)
                        for call in calls:
                            if isinstance(call, Mapping):
                                tool_names[str(call.get("name", "unknown"))] += 1
                                counters["tool_argument_characters"] += len(
                                    canonical_json(call.get("arguments", {}))
                                )
        results = record.get("tool_results", [])
        if isinstance(results, list):
            counters["tool_result_characters"] += len(canonical_json(results))
        if line_number <= 0:
            counters["invalid_line_numbers"] += 1
    duplicates = 0
    if duplicate_path is not None and Path(duplicate_path).exists():
        for _, _ in iter_jsonl(duplicate_path):
            duplicates += 1
    elapsed = time.perf_counter() - started
    average_quality = quality_total / quality_count if quality_count else 0.0
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "input_path": str(input_path),
        "records_seen": counters["records_seen"],
        "accepted": counters["accepted"],
        "malformed": counters["malformed"],
        "duplicates": duplicates,
        "characters": counters["characters"],
        "tool_argument_characters": counters["tool_argument_characters"],
        "tool_result_characters": counters["tool_result_characters"],
        "estimated_tokens": round(
            (counters["characters"] + counters["tool_argument_characters"]) / 4
        ),
        "estimated_tokens_basis": (
            "message content + tool-call arguments only. tool_results is an "
            "exact mirror of the tool-role messages already counted in "
            "characters, so including it double counts. Measured inflation "
            "before this fix: 1.46x on the Phase-1 mixture."
        ),
        "estimated_tokens_with_tool_result_doublecount": round(
            (
                counters["characters"]
                + counters["tool_argument_characters"]
                + counters["tool_result_characters"]
            )
            / 4
        ),
        "tool_calls": counters["tool_calls"],
        "average_quality_score": round(average_quality, 4),
        "quality_flags": dict(sorted(quality_flags.items())),
        "categories": dict(sorted(categories.items())),
        "sources": dict(sorted(sources.items())),
        "roles": dict(sorted(roles.items())),
        "tool_names": dict(tool_names.most_common()),
        "processing_seconds": round(elapsed, 3),
        "records_per_second": round(
            counters["records_seen"] / elapsed if elapsed else 0.0, 3
        ),
        "disk_bytes": {
            "input": directory_size(input_path),
            "duplicates": directory_size(duplicate_path) if duplicate_path else 0,
        },
    }
    write_json(report_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calculate streaming corpus statistics"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duplicates", type=Path)
    parser.add_argument("--max-records", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        payload = calculate_statistics(
            args.input,
            report_path=args.report,
            duplicate_path=args.duplicates,
            max_records=args.max_records,
        )
        print(json.dumps(payload, indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Statistics error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
