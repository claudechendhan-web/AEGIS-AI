from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from data_pipeline.common import (
    PipelineReport,
    _replace_path,
    canonical_json,
    iter_jsonl,
    load_json_line,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


def create_mixture(
    input_paths: Iterable[str | Path],
    output_path: str | Path,
    *,
    weights: Mapping[str, float],
    report_path: str | Path,
    max_records: int = 0,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    normalized_weights = {
        _canonical_category(category): float(value)
        for category, value in weights.items()
    }
    if not normalized_weights or not any(
        value > 0 for value in normalized_weights.values()
    ):
        raise PipelineError("mixture weights must contain a positive value")
    paths = [Path(path) for path in input_paths]
    if not paths:
        raise PipelineError("at least one input path is required")
    counts: Counter[str] = Counter()
    for path in paths:
        for _, line in iter_jsonl(path):
            try:
                record = load_json_line(line)
            except PipelineError:
                continue
            if isinstance(record, Mapping):
                category = _canonical_category(
                    str(record.get("task_type") or "UNCLASSIFIED")
                )
                counts[category] += 1
    total = sum(normalized_weights.values())
    target = max_records or sum(counts.values())
    quotas = {
        category: min(counts.get(category, 0), int(target * weight / total))
        for category, weight in normalized_weights.items()
    }
    selected = sum(quotas.values())
    if selected < target:
        categories = sorted(
            set(counts).union(normalized_weights),
            key=lambda category: category,
        )
        remaining = sorted(
            (
                (counts.get(category, 0) - quotas.get(category, 0), category)
                for category in categories
            ),
            reverse=True,
        )
        for available, category in remaining:
            if selected >= target:
                break
            addition = min(available, target - selected)
            if addition > 0:
                quotas[category] = quotas.get(category, 0) + addition
                selected += addition
    report = PipelineReport(
        stage="mixture",
        input_path=",".join(str(path) for path in paths),
        output_path=str(output_path),
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    emitted: Counter[str] = Counter()
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for line in _round_robin_lines(paths):
            try:
                record = load_json_line(line)
            except PipelineError:
                report.increment("malformed")
                continue
            if not isinstance(record, Mapping):
                report.increment("malformed")
                continue
            category = _canonical_category(
                str(record.get("task_type") or "UNCLASSIFIED")
            )
            if emitted[category] >= quotas.get(category, 0):
                continue
            stream.write(canonical_json(record))
            stream.write("\n")
            emitted[category] += 1
            report.increment("accepted")
            if sum(emitted.values()) >= target:
                break
    if sum(emitted.values()) < target:
        report.errors.append(
            f"requested {target} records but only {sum(emitted.values())} were available"
        )
    _replace_path(temporary, output)
    report_payload = report.finish().to_dict()
    report_payload["quotas"] = quotas
    report_payload["requested_weights"] = normalized_weights
    report_payload["shortfall"] = max(0, target - sum(emitted.values()))
    report_payload["emitted_by_category"] = dict(emitted)
    report_payload["available_by_category"] = dict(counts)
    write_json(report_path, report_payload)
    return report


def _canonical_category(value: str) -> str:
    category = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "AGENT_TRAJECTORIES": "AGENT_TRAJECTORY",
        "WEB_TASKS": "WEB_TASK",
        "TOOL_USAGE": "TOOL_USE",
    }
    return aliases.get(category, category)


def _round_robin_lines(
    paths: list[Path],
) -> Iterator[str]:
    iterators: list[Iterator[tuple[int, str]]] = [iter_jsonl(path) for path in paths]
    while iterators:
        remaining: list[Iterator[tuple[int, str]]] = []
        for iterator in iterators:
            try:
                _, line = next(iterator)
            except StopIteration:
                continue
            yield line
            remaining.append(iterator)
        iterators = remaining


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a configurable category mixture"
    )
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-records", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = PipelineConfig.load(args.config)
        report = create_mixture(
            args.input,
            args.output,
            weights=config.mixture,
            report_path=args.report,
            max_records=args.max_records,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Mixture error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
