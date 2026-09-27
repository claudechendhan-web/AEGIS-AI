from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
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

_ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
_SENSITIVE_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\b(?:\+?\d[\d .()-]{7,}\d)\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
)


def quality_score(
    example: Mapping[str, Any], limits: Mapping[str, Any] | None = None
) -> float:
    limits = limits or {}
    max_chars = int(limits.get("max_chars", 200_000))
    messages = example.get("messages")
    if not isinstance(messages, list) or not messages:
        return 0.0
    score = 1.0
    if _trajectory_chars(example) > max_chars:
        score -= 0.25
    if limits.get("require_user", True) and not any(
        isinstance(message, Mapping) and message.get("role") == "user"
        for message in messages
    ):
        score -= 0.2
    if limits.get("require_assistant", False) and not any(
        isinstance(message, Mapping) and message.get("role") == "assistant"
        for message in messages
    ):
        score -= 0.1
    score -= min(0.4, 0.1 * len(_quality_flags(example, limits)))
    return round(max(0.0, min(1.0, score)), 4)


def filter_example(
    example: Any, limits: Mapping[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(example, Mapping):
        return None, "not_object"
    messages = example.get("messages")
    if not isinstance(messages, list) or not messages:
        return None, "empty_messages"
    if not all(isinstance(message, Mapping) for message in messages):
        return None, "malformed_messages"
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or role not in _ALLOWED_ROLES:
            return None, "invalid_role"
        if not isinstance(content, str):
            return None, "malformed_message_fields"
        if role == "user" and not content.strip():
            return None, "empty_user"
        if (
            role == "assistant"
            and not content.strip()
            and not message.get("tool_calls")
        ):
            return None, "empty_assistant"
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            return None, "invalid_tool_calls"
        for call in calls:
            if not isinstance(call, Mapping) or not isinstance(call.get("name"), str):
                return None, "invalid_tool_call"
            if not call.get("name", "").strip():
                return None, "invalid_tool_call"
            arguments = call.get("arguments", {})
            if not isinstance(arguments, Mapping):
                return None, "invalid_tool_arguments"
    limits = limits or {}
    if _trajectory_chars(example) > int(limits.get("max_chars", 200_000)):
        return None, "too_long"
    roles = {str(message.get("role")) for message in messages}
    if limits.get("require_user", True) and "user" not in roles:
        return None, "missing_user"
    if limits.get("require_assistant", False) and "assistant" not in roles:
        return None, "missing_assistant"
    normalized = dict(example)
    normalized["quality_flags"] = _quality_flags(normalized, limits)
    normalized["quality_score"] = quality_score(normalized, limits)
    minimum = float(limits.get("minimum_quality_score", 0.0))
    if normalized["quality_score"] < minimum:
        return None, "quality_below_threshold"
    return normalized, None


def _quality_flags(
    example: Mapping[str, Any], limits: Mapping[str, Any] | None = None
) -> list[str]:
    limits = limits or {}
    messages = example.get("messages")
    if not isinstance(messages, list):
        return ["malformed_messages"]
    flags: list[str] = []
    roles = [
        message.get("role") for message in messages if isinstance(message, Mapping)
    ]
    text = " ".join(
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, Mapping)
    )
    pending_calls = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        calls = message.get("tool_calls", [])
        if message.get("role") == "assistant" and isinstance(calls, list):
            pending_calls += len(calls)
        if message.get("role") == "tool":
            if pending_calls:
                pending_calls -= 1
            else:
                flags.append("orphan_tool_result")
    if pending_calls:
        flags.append("missing_tool_result")
    if any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS):
        flags.append("sensitive_data_review")
    if len(text.strip()) < 20 and not any(
        isinstance(message, Mapping) and message.get("tool_calls")
        for message in messages
    ):
        flags.append("low_information_review")
    if limits.get("require_assistant", False) and "assistant" not in roles:
        flags.append("missing_assistant")
    return flags


def _trajectory_chars(example: Mapping[str, Any]) -> int:
    total = 0
    messages = example.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            total += len(str(message.get("content", "")))
            calls = message.get("tool_calls", [])
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, Mapping):
                        total += len(canonical_json(call.get("arguments", {})))
    results = example.get("tool_results", [])
    if isinstance(results, list):
        total += len(canonical_json(results))
    return total


def filter_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    report_path: str | Path,
    rejects_path: str | Path,
    limits: Mapping[str, Any] | None = None,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    input_file = Path(input_path)
    output = Path(output_path)
    rejects = Path(rejects_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rejects.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint.load(Path(report_path).with_suffix(".checkpoint.json"))
    start_line = checkpoint.get_int("last_line") if resume else 0
    if not resume:
        checkpoint.state = {}
    record_limit = remaining_record_limit(max_records, start_line)
    report = PipelineReport(
        stage="filter",
        input_path=str(input_file),
        output_path=str(output),
    )
    with (
        output.open(
            "a" if resume else "w", encoding="utf-8", newline="\n"
        ) as output_stream,
        rejects.open(
            "a" if resume else "w", encoding="utf-8", newline="\n"
        ) as reject_stream,
    ):
        for line_number, line in iter_jsonl(input_file, start_line=start_line):
            if record_limit == 0:
                break
            report.increment("records_seen")
            try:
                record = load_json_line(line)
            except PipelineError:
                report.increment("malformed")
                reject_stream.write(
                    canonical_json(
                        {"row_number": line_number, "reason": "malformed JSON record"}
                    )
                )
                reject_stream.write("\n")
                reject_stream.flush()
                checkpoint.state = {"last_line": line_number}
                checkpoint.save()
                if (
                    record_limit is not None
                    and report.counters.get("records_seen", 0) >= record_limit
                ):
                    break
                continue
            filtered, reason = filter_example(record, limits)
            if filtered is None:
                report.increment("rejected")
                report.increment(reason or "invalid")
                reject_stream.write(
                    canonical_json(
                        {"row_number": line_number, "reason": reason, "raw": record}
                    )
                )
                reject_stream.write("\n")
            else:
                output_stream.write(canonical_json(filtered))
                output_stream.write("\n")
                report.increment("accepted")
                for flag in set(filtered.get("quality_flags", [])):
                    report.increment(f"flag_{flag}")
            output_stream.flush()
            reject_stream.flush()
            checkpoint.state = {"last_line": line_number}
            checkpoint.save()
            if (
                record_limit is not None
                and report.counters.get("records_seen", 0) >= record_limit
            ):
                break
    write_json(report_path, report.finish().to_dict())
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply deterministic quality filters")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rejects", type=Path, required=True)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = PipelineConfig.load(args.config)
        report = filter_jsonl(
            args.input,
            args.output,
            report_path=args.report,
            rejects_path=args.rejects,
            limits=config.quality,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Filter error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
