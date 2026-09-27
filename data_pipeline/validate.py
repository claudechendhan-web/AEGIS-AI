from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from data_pipeline.common import (
    Checkpoint,
    PipelineReport,
    canonical_json,
    iter_input_records,
    remaining_record_limit,
    write_json,
)
from data_pipeline.config import PipelineConfig, PipelineError


def validate_record(record: Any, source: str) -> str | None:
    if not isinstance(record, Mapping):
        return "record_not_object"
    source_id = source.lower()
    if "starcoder" in source_id:
        if "messages" in record:
            return _validate_messages(record.get("messages"))
        return _validate_instruction_code(record)
    if "messages" in record:
        return _validate_messages(record.get("messages"))
    if "content" in record and isinstance(record.get("content"), list):
        return _validate_action_content(record.get("content"))
    if "conversations" in record:
        return _validate_conversations(record.get("conversations"))
    if "instruction" in record or "text" in record:
        return _validate_instruction_code(record)
    return "missing_supported_structure"


def validate_jsonl(
    input_path: str | Path,
    *,
    source: str,
    source_format: str,
    report_path: str | Path,
    rejects_path: str | Path,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    input_file = Path(input_path)
    rejects = Path(rejects_path)
    rejects.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint.load(Path(report_path).with_suffix(".checkpoint.json"))
    start_line = checkpoint.get_int("last_line") if resume else 0
    if not resume:
        checkpoint.state = {}
    record_limit = remaining_record_limit(max_records, start_line)
    report = PipelineReport(
        stage="validate",
        source=source,
        input_path=str(input_file),
        output_path=str(rejects),
    )
    with rejects.open(
        "a" if resume else "w", encoding="utf-8", newline="\n"
    ) as reject_stream:
        for line_number, record, parse_error in iter_input_records(
            input_file, source_format, start_line=start_line
        ):
            if record_limit == 0:
                break
            report.increment("records_seen")
            if parse_error is not None:
                report.increment("malformed")
                report.errors.append(f"line {line_number}: {parse_error}")
                _write_reject(reject_stream, line_number, source, parse_error, record)
                reject_stream.flush()
                checkpoint.state = {"last_line": line_number}
                checkpoint.save()
                if (
                    record_limit is not None
                    and report.counters.get("records_seen", 0) >= record_limit
                ):
                    break
                continue
            reason = validate_record(record, source)
            if reason is not None:
                report.increment("rejected")
                report.increment(reason)
                _write_reject(reject_stream, line_number, source, reason, record)
            else:
                report.increment("accepted")
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


def _validate_instruction_code(record: Mapping[str, Any]) -> str | None:
    for field_name in ("instruction", "text"):
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            return f"invalid_{field_name}"
    identifier = record.get("id")
    if identifier is not None and (
        not isinstance(identifier, str) or not identifier.strip()
    ):
        return "invalid_id"
    return None


def _validate_messages(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return "messages_not_nonempty_list"
    for index, message in enumerate(value):
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except json.JSONDecodeError:
                return f"message_{index}_invalid_json"
        if not isinstance(message, Mapping):
            return f"message_{index}_not_object"
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool", "human", "gpt"}:
            return f"message_{index}_invalid_role"
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            return f"message_{index}_invalid_content"
        calls = message.get("tool_calls")
        if calls is not None and not isinstance(calls, list):
            return f"message_{index}_invalid_tool_calls"
        if isinstance(calls, list):
            for call_index, call in enumerate(calls):
                if not isinstance(call, Mapping):
                    return f"message_{index}_invalid_tool_call_{call_index}"
                function = call.get("function")
                name = call.get("name")
                if isinstance(function, Mapping):
                    name = function.get("name")
                if not isinstance(name, str) or not name.strip():
                    return f"message_{index}_invalid_tool_call_{call_index}"
    return None


def _validate_conversations(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return "conversations_not_nonempty_list"
    for index, turn in enumerate(value):
        if not isinstance(turn, Mapping):
            return f"conversation_{index}_not_object"
        if not isinstance(turn.get("from"), str) or not isinstance(
            turn.get("value"), str
        ):
            return f"conversation_{index}_invalid_fields"
    return None


def _validate_action_content(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return "content_not_nonempty_list"
    for index, action in enumerate(value):
        if not isinstance(action, Mapping):
            return f"content_{index}_not_object"
        action_class = action.get("class_")
        if not isinstance(action_class, str) or not action_class:
            return f"content_{index}_missing_class"
        if action_class in {"api_action", "code_action"} and not isinstance(
            action.get("function", action.get("content")), str
        ):
            return f"content_{index}_invalid_action"
    return None


def _write_reject(
    stream: Any, line_number: int, source: str, reason: str, raw: Any
) -> None:
    stream.write(
        canonical_json(
            {
                "source": source,
                "row_number": line_number,
                "reason": reason,
                "raw": raw,
            }
        )
    )
    stream.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate raw JSONL records")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--format", default="jsonl")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--rejects", type=Path, required=True)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        report = validate_jsonl(
            args.input,
            source=args.source,
            source_format=args.format,
            report_path=args.report,
            rejects_path=args.rejects,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Validation error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
