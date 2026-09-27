from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping
from enum import StrEnum
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


class TaskCategory(StrEnum):
    CODING = "CODING"
    DEBUGGING = "DEBUGGING"
    INSTRUCTION_FOLLOWING = "INSTRUCTION_FOLLOWING"
    PLANNING = "PLANNING"
    TOOL_USE = "TOOL_USE"
    FUNCTION_CALLING = "FUNCTION_CALLING"
    AGENT_TRAJECTORY = "AGENT_TRAJECTORY"
    WEB_TASK = "WEB_TASK"
    RESEARCH = "RESEARCH"
    FILE_OPERATIONS = "FILE_OPERATIONS"
    DATA_PROCESSING = "DATA_PROCESSING"
    REASONING = "REASONING"
    BUSINESS_AUTOMATION = "BUSINESS_AUTOMATION"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    GENERAL_CONVERSATION = "GENERAL_CONVERSATION"
    SAFETY_AND_PERMISSIONS = "SAFETY_AND_PERMISSIONS"


TASK_CATEGORIES = tuple(item.value for item in TaskCategory)


def classify_example(
    example: Mapping[str, Any], *, source: str, subset: str = ""
) -> tuple[str | None, list[str]]:
    existing = example.get("task_type")
    if isinstance(existing, str) and existing in TASK_CATEGORIES:
        return existing, ["source-provided"]
    text = _example_text(example)
    source_key = f"{source} {subset}".lower()
    evidence: list[str] = []
    label: str | None = None
    if "starcoder" in source_key:
        return TaskCategory.CODING.value, ["python_instruction_source"]
    if "toprak" in source_key:
        if example.get("tool_calls"):
            label = TaskCategory.TOOL_USE.value
            evidence.append("tool_calls_field")
        elif len(example.get("messages", [])) > 2:
            label = TaskCategory.AGENT_TRAJECTORY.value
            evidence.append("multi_turn_messages")
        else:
            label = TaskCategory.GENERAL_CONVERSATION.value
            evidence.append("conversation_source")
        return label, evidence
    if "nemotron" in source_key:
        if example.get("tool_calls"):
            label = TaskCategory.FUNCTION_CALLING.value
            evidence.append("tool_calls_field")
        elif len(example.get("messages", [])) > 2:
            label = TaskCategory.AGENT_TRAJECTORY.value
            evidence.append("multi_turn_messages")
        else:
            label = TaskCategory.GENERAL_CONVERSATION.value
            evidence.append("conversation_source")
        return label, evidence
    subset_key = subset.lower()
    if any(name in subset_key for name in ("swe", "code", "openhands", "coder")):
        if _matches(text, ("error", "exception", "traceback", "failed", "fix bug")):
            return TaskCategory.ERROR_RECOVERY.value, ["software_error_terms"]
        if example.get("tool_calls") or _matches(
            text,
            (
                "code",
                "function",
                "test",
                "software",
                "python",
                "bash",
                "shell",
                "repository",
                "file",
            ),
        ):
            return TaskCategory.CODING.value, ["software_source_evidence"]
        return None, ["software_source_without_content_evidence"]
    if any(name in subset_key for name in ("mind2web", "browse", "nnetnav")):
        if (
            _matches(text, ("web", "browser", "page", "click", "form", "website"))
            or len(example.get("messages", [])) > 2
        ):
            return TaskCategory.WEB_TASK.value, ["web_source_evidence"]
        return None, ["web_source_without_content_evidence"]
    if "agenttuning" in subset_key:
        if example.get("tool_calls") or len(example.get("messages", [])) > 2:
            return TaskCategory.AGENT_TRAJECTORY.value, ["agenttuning_source_evidence"]
        return None, ["agenttuning_source_without_content_evidence"]
    if _matches(text, ("permission", "safety", "allowed", "denied", "authorization")):
        return TaskCategory.SAFETY_AND_PERMISSIONS.value, ["safety_terms"]
    if _matches(text, ("plan", "step 1", "decompose", "milestone")):
        return TaskCategory.PLANNING.value, ["planning_terms"]
    if _matches(text, ("debug", "traceback", "exception", "stack trace")):
        return TaskCategory.DEBUGGING.value, ["debug_terms"]
    if _matches(text, ("sql", "dataframe", "spreadsheet", "csv", "database")):
        return TaskCategory.DATA_PROCESSING.value, ["data_terms"]
    if _matches(text, ("file", "directory", "path", "read_file", "write_file")):
        return TaskCategory.FILE_OPERATIONS.value, ["file_terms"]
    if _matches(text, ("search", "research", "documentation", "source")):
        return TaskCategory.RESEARCH.value, ["research_terms"]
    if _matches(
        text, ("invoice", "customer", "sales", "report", "workflow", "business")
    ):
        return TaskCategory.BUSINESS_AUTOMATION.value, ["business_terms"]
    if example.get("tool_calls"):
        return TaskCategory.TOOL_USE.value, ["tool_calls_field"]
    if len(example.get("messages", [])) > 2:
        return TaskCategory.AGENT_TRAJECTORY.value, ["multi_turn_messages"]
    if _matches(text, ("why", "reason", "calculate", "solve")):
        return TaskCategory.REASONING.value, ["reasoning_terms"]
    return None, evidence


def classify_jsonl(
    input_path: str | Path,
    output_path: str | Path,
    *,
    source: str,
    subset: str,
    report_path: str | Path,
    max_records: int = 0,
    resume: bool = True,
) -> PipelineReport:
    if max_records < 0:
        raise PipelineError("max_records must be zero or greater")
    input_file = Path(input_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = Checkpoint.load(Path(report_path).with_suffix(".checkpoint.json"))
    start_line = checkpoint.get_int("last_line") if resume else 0
    if not resume:
        checkpoint.state = {}
    record_limit = remaining_record_limit(max_records, start_line)
    report = PipelineReport(
        stage="classify",
        source=source,
        input_path=str(input_file),
        output_path=str(output),
    )
    with output.open("a" if resume else "w", encoding="utf-8", newline="\n") as stream:
        for line_number, line in iter_jsonl(input_file, start_line=start_line):
            if record_limit == 0:
                break
            report.increment("records_seen")
            try:
                record = load_json_line(line)
            except PipelineError:
                report.increment("malformed")
                report.errors.append(f"line {line_number}: malformed JSON record")
                stream.flush()
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
                label, evidence = classify_example(record, source=source, subset=subset)
                record["task_type"] = label
                metadata = record.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    record["metadata"] = metadata
                metadata["classification"] = {
                    "label": label,
                    "evidence": evidence,
                }
                stream.write(canonical_json(record))
                stream.write("\n")
                report.increment("accepted")
                if label is not None:
                    report.increment(f"category_{label}")
            stream.flush()
            checkpoint.state = {"last_line": line_number}
            checkpoint.save()
            if (
                record_limit is not None
                and report.counters.get("records_seen", 0) >= record_limit
            ):
                break
    write_json(report_path, report.finish().to_dict())
    return report


def _example_text(example: Mapping[str, Any]) -> str:
    values: list[str] = []
    messages = example.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                values.append(message["content"])
    metadata = example.get("metadata")
    if isinstance(metadata, Mapping):
        values.append(str(metadata.get("source_file", "")))
    return " ".join(values).lower()


def _matches(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(re.escape(term), text) for term in terms)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify normalized examples")
    parser.add_argument("--config", type=Path, default=Path("configs/dataset.toml"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--subset", default="")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-records", type=int, default=0)
    parser.add_argument("--no-resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        PipelineConfig.load(args.config)
        report = classify_jsonl(
            args.input,
            args.output,
            source=args.source,
            subset=args.subset,
            report_path=args.report,
            max_records=args.max_records,
            resume=not args.no_resume,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"Classification error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
